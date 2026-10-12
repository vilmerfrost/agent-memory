"""Run store over Supabase's REST API (PostgREST), stdlib `urllib` only.

Why: cloud sessions reach the internet only through an HTTP(S) proxy; raw
Postgres (5432) is blocked there. HTTPS to `$AMEM_SUPABASE_URL/rest/v1/` works.

Auth: the header `apikey: $AMEM_SUPABASE_KEY` is sent only when that env var is
set, and no other auth header. Supabase's `sb_secret_...` / `sb_publishable_...`
keys belong in `apikey` only: they are not JWTs, so they never go in
`Authorization: Bearer`. In cloud sessions a network secret adds `apikey` on the
way out, so the session needs no key at all. The key is never printed or logged:
error messages hold the HTTP status and PostgREST's message only.

Transport rules (checked before any I/O, see `config.supabase_url_error`):
the URL must be http(s) with a host, and a remote host needs `https://`
(plain http only reaches a local test stub). Redirects are never followed: a
30x is a RestError, so neither the `apikey` header nor the body ever reaches a
host other than AMEM_SUPABASE_URL's, and a redirected write is never counted.

Ingest counts (`N new / M already present`): a pre-check GET of the batch's
`source_key`s (`source_key=in.(...)`) splits rows into new and present. New rows
are POSTed in batches as an upsert with `on_conflict=source_key` and
`Prefer: resolution=ignore-duplicates,return=minimal`, so a row that a parallel
ingest inserted between the GET and the POST is skipped by the server (it is
then counted as new; the only inexact case). Present rows whose `meta.failures`
changed get one PATCH of `meta` (the stored meta with `failures` replaced), the
same rule as the Postgres backend.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agent_memory import config
from agent_memory.routines_log import ParsedRun

TABLE = "runs"
TIMEOUT_S = 20.0
KEY_BATCH = 50  # source_keys per pre-check GET (64 hex chars each: ~3.5 KB of URL)
INSERT_BATCH = 500  # rows per POST


class RestError(config.BackendError):
    """An HTTP or network failure talking to PostgREST. Never holds the API key."""


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class _RefuseRedirects(HTTPRedirectHandler):
    """Never follow a 30x. urllib's default handler re-sends every header,
    `apikey` included, to whatever host the Location names, and turns a POST
    into a GET whose 200 would count rows as written. A redirect is an error."""

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> None:
        return None  # urllib then raises HTTPError(code) via the default error handler


# Proxy support (HTTPS_PROXY / NO_PROXY) stays: build_opener keeps ProxyHandler.
_OPENER = build_opener(_RefuseRedirects)


def _request(
    method: str,
    query: list[tuple[str, str]] | None = None,
    body: Any = None,
    prefer: str | None = None,
    *,
    table: str = TABLE,
) -> Any:
    """One PostgREST call. Returns the parsed JSON body, or None for an empty body.

    Raises RestError before any network I/O when AMEM_SUPABASE_URL is unusable
    (empty, not an http(s) URL, or plain http to a remote host).
    """
    base = config.supabase_url()
    problem = config.supabase_url_error(base)
    if problem:
        raise RestError(problem)
    url = f"{base}/rest/v1/{table}"
    if query:
        url += "?" + urlencode(query, quote_via=quote, safe=",().*")
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, default=str, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if prefer:
        headers["Prefer"] = prefer
    key = config.supabase_key()
    if key:
        headers["apikey"] = key
    try:
        req = Request(url, data=data, headers=headers, method=method)
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            if not 200 <= resp.status < 300:  # e.g. a 304, which urllib returns as-is
                raise RestError(f"{method} /rest/v1/{table} -> unexpected HTTP {resp.status}")
            raw = resp.read()
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RestError(f"{method} /rest/v1/{table} -> HTTP {exc.code} redirect refused "
                            "(check AMEM_SUPABASE_URL)") from None
        raise RestError(f"{method} /rest/v1/{table} -> HTTP {exc.code}: {_error_text(exc)}") from None
    except (URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RestError(f"{method} /rest/v1/{table} failed: {reason}") from None
    except ValueError:
        raise RestError(f"{method} /rest/v1/{table} failed: AMEM_SUPABASE_URL is not a valid URL") from None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise RestError(f"{method} /rest/v1/{table}: response is not JSON") from None


def _error_text(exc: HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return exc.reason or "error"
    try:
        body = json.loads(raw)
    except ValueError:
        return raw[:300] or str(exc.reason)
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("code", "message", "details", "hint") if body.get(k)]
        return " | ".join(parts)[:300] or str(exc.reason)
    return raw[:300]


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def insert_run(
    routine: str,
    status: str,
    *,
    source: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    summary: str | None = None,
    failure_reason: str | None = None,
    report_path: str | None = None,
    meta: dict[str, Any] | None = None,
) -> uuid.UUID:
    """POST one run and return its id. started_at defaults to now (client clock, UTC)."""
    row = {
        "routine": routine,
        "source": source,
        "started_at": _ts(started_at or datetime.now(UTC)),
        "finished_at": _ts(finished_at),
        "status": status,
        "summary": summary,
        "failure_reason": failure_reason,
        "report_path": report_path,
        "meta": meta or {},
    }
    got = _request("POST", [("select", "id")], [row], prefer="return=representation")
    if not isinstance(got, list) or not got or "id" not in got[0]:
        raise RestError("POST /rest/v1/runs returned no id")
    return uuid.UUID(str(got[0]["id"]))


def _row(r: ParsedRun) -> dict[str, Any]:
    return {
        "routine": r.routine,
        "source": r.source,
        "started_at": _ts(r.started_at),
        "status": r.status,
        "summary": r.summary,
        "report_path": r.report_path,
        "meta": r.meta,
        "source_key": r.source_key,
    }


def existing_meta(keys: Sequence[str]) -> dict[str, dict[str, Any]]:
    """source_key -> stored meta, for the keys that already exist. Batched GETs."""
    found: dict[str, dict[str, Any]] = {}
    for batch in _chunks(list(keys), KEY_BATCH):
        got = _request("GET", [("select", "source_key,meta"), ("source_key", f"in.({','.join(batch)})")])
        for item in got or []:
            found[item["source_key"]] = item.get("meta") or {}
    return found


def ingest_runs(rows: Iterable[ParsedRun]) -> tuple[int, int]:
    """Upsert parsed rows keyed by source_key. Returns (new, already_present)."""
    unique: dict[str, ParsedRun] = {}
    for r in rows:
        unique.setdefault(r.source_key, r)
    present = existing_meta(list(unique))
    new_rows = [r for k, r in unique.items() if k not in present]
    for batch in _chunks(new_rows, INSERT_BATCH):
        _request(
            "POST",
            [("on_conflict", "source_key")],
            [_row(r) for r in batch],
            prefer="resolution=ignore-duplicates,return=minimal",
        )
    for key, stored in present.items():
        failures = unique[key].meta.get("failures")
        if stored.get("failures") != failures:
            _request(
                "PATCH",
                [("source_key", f"eq.{key}")],
                {"meta": {**stored, "failures": failures}},
                prefer="return=minimal",
            )
    return len(new_rows), len(present)


def recent_runs(routine: str, limit: int = 10) -> list[dict[str, Any]]:
    """Newest runs first for one routine (same order as the Postgres backend)."""
    got = _request(
        "GET",
        [
            ("select", "*"),
            ("routine", f"eq.{routine}"),
            ("order", "started_at.desc.nullslast,created_at.desc"),
            ("limit", str(limit)),
        ],
    )
    return list(got or [])


def reachable() -> bool:
    """Cheap health check: `GET /rest/v1/runs?select=id&limit=1`."""
    try:
        _request("GET", [("select", "id"), ("limit", "1")])
    except RestError:
        return False
    return True
