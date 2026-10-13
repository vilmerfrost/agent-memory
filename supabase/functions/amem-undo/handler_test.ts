// Unit tests for handler.ts with a fake PostgREST (no network).
// Run: `deno test supabase/functions/amem-undo/` or `node --test supabase/functions/amem-undo/handler_test.ts`.

import { ALREADY_TEXT, type Deps, handle, NOTHING_TEXT, OK_TEXT, safeEqual, sha256Hex } from "./handler.ts";

// deno-lint-ignore no-explicit-any
const g = globalThis as any;
const test: (name: string, fn: () => Promise<void>) => void = g.Deno?.test ?? (await import("node:test")).test;

function eq(actual: unknown, expected: unknown, msg = ""): void {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg} expected ${e}, got ${a}`);
}

const EDIT = "0b8f8f3e-4c1a-4f7e-9d2a-1f2e3d4c5b6a";
const TOKEN = "tok-abcdefghijklmnopqrstuvwxyz012345";
const KEY = "service-key-for-tests";

interface Call { url: string; method: string; body: string | null; auth: string | null; prefer: string | null }

// The fake's undo_requests row mirrors amem_request_undo: none -> insert, closed without a
// revert -> reopen, reverted -> left alone. `opts.row` is the row before the tap; `opts.status`
// the edit's status (not kept -> "nothing"). The real SQL is tested in tests/test_edits_undo.py.
type Row = { handled: boolean; result: string | null; attempts: number };

async function fakeDb(hash: string | null, opts: { row?: Row; status?: string } = {}) {
  const calls: Call[] = [];
  const requests = new Set<string>();
  const rows = new Map<string, Row>();
  if (opts.row) rows.set(EDIT, { ...opts.row });
  const deps: Deps = {
    supabaseUrl: "https://project.example.test/",
    serviceKey: KEY,
    fetch: async (url: string, init?: RequestInit) => {
      const h = new Headers(init?.headers);
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: (init?.body as string) ?? null, auth: h.get("Authorization"), prefer: h.get("Prefer") });
      if (method === "GET" && url.includes("/rest/v1/edits?")) {
        const rows = hash === null || !url.includes(`id=eq.${EDIT}`) ? [] : [{ id: EDIT, undo_token_hash: hash }];
        return new Response(JSON.stringify(rows), { status: 200 });
      }
      if (method === "POST" && url.endsWith("/rest/v1/rpc/amem_request_undo")) {
        const id = JSON.parse(init!.body as string).p_edit_id;
        const row = rows.get(id);
        if (row?.result === "reverted") return new Response(JSON.stringify("reverted"), { status: 200 });
        rows.set(id, { handled: false, result: null, attempts: 0 });
        requests.add(id);
        const state = (opts.status ?? "kept") === "kept" ? "queued" : "nothing";
        return new Response(JSON.stringify(state), { status: 200 });
      }
      if (method === "PATCH") return new Response(null, { status: 204 });
      return new Response("unexpected", { status: 500 });
    },
  };
  return { deps, calls, requests, rows };
}

function post(body: unknown): Request {
  return new Request("https://fn.example.test/amem-undo", {
    method: "POST",
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

test("first tap: 200 queued via amem_request_undo, one request per edit", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  for (let i = 0; i < 2; i++) {
    const res = await handle(post({ edit_id: EDIT, token: TOKEN }), db.deps);
    eq(res.status, 200);
    eq(await res.text(), OK_TEXT);
    eq(res.headers.get("content-type"), "text/plain; charset=utf-8");
  }
  eq([...db.requests], [EDIT]);
  eq(db.rows.size, 1);
  const rpc = db.calls.filter((c) => c.method === "POST");
  eq(rpc.length, 2);
  eq(rpc[0].url, "https://project.example.test/rest/v1/rpc/amem_request_undo");
  eq(JSON.parse(rpc[0].body!), { p_edit_id: EDIT, p_source: "phone" });
  eq(rpc[0].auth, `Bearer ${KEY}`);
  eq(db.calls.filter((c) => c.method === "PATCH").length, 2);
});

test("re-tap after a closed failed request: reopened and queued", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN),
    { row: { handled: true, result: "failed (attempt 3): lock held", attempts: 3 } });
  const res = await handle(post({ edit_id: EDIT, token: TOKEN }), db.deps);
  eq(res.status, 200);
  eq(await res.text(), OK_TEXT);
  eq(db.rows.get(EDIT), { handled: false, result: null, attempts: 0 });
});

test("re-tap after the edit was reverted: honest text, no new work", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN), { row: { handled: true, result: "reverted", attempts: 1 } });
  const res = await handle(post({ edit_id: EDIT, token: TOKEN }), db.deps);
  eq(res.status, 200);
  eq(await res.text(), ALREADY_TEXT);
  eq(db.rows.get(EDIT), { handled: true, result: "reverted", attempts: 1 });
  eq(db.requests.size, 0);
  eq(db.calls.filter((c) => c.method === "PATCH").length, 0);
});

test("tap on an edit that is no longer active: honest text, not 'queued'", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN), { status: "auto-reverted" });
  const res = await handle(post({ edit_id: EDIT, token: TOKEN }), db.deps);
  eq(res.status, 200);
  eq(await res.text(), NOTHING_TEXT);
});

test("rpc fails or answers something unknown: 502, never 'queued'", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  for (const answer of [new Response("boom", { status: 404 }), new Response(JSON.stringify("weird"), { status: 200 })]) {
    const deps: Deps = {
      ...db.deps,
      fetch: (url, init) => url.includes("/rpc/") ? Promise.resolve(answer) : db.deps.fetch(url, init),
    };
    const res = await handle(post({ edit_id: EDIT, token: TOKEN }), deps);
    eq(res.status, 502);
    eq(await res.text(), "Try again later");
  }
});

test("wrong token or unknown edit: 403, nothing inserted, no detail", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  let res = await handle(post({ edit_id: EDIT, token: TOKEN + "x" }), db.deps);
  eq(res.status, 403);
  eq(await res.text(), "Not allowed");
  res = await handle(post({ edit_id: "11111111-2222-3333-4444-555555555555", token: TOKEN }), db.deps);
  eq(res.status, 403);
  const none = await fakeDb(null);
  res = await handle(post({ edit_id: EDIT, token: TOKEN }), none.deps);
  eq(res.status, 403);
  eq(db.requests.size + none.requests.size, 0);
});

test("bad input: 4xx before any database call", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  const bad = ["not json", "[]", { edit_id: "x", token: TOKEN }, { edit_id: EDIT }, { edit_id: EDIT, token: "short" },
    { edit_id: EDIT, token: "t".repeat(201) }, { edit_id: `${EDIT}&id=neq.x`, token: TOKEN }, "x".repeat(3000)];
  for (const b of bad) {
    const res = await handle(post(b), db.deps);
    eq(res.status, 400, `body ${JSON.stringify(b).slice(0, 40)}`);
  }
  const get = await handle(new Request("https://fn.example.test/amem-undo"), db.deps);
  eq(get.status, 405);
  eq(db.calls.length, 0);
});

test("not configured: 500 without leaking", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  const res = await handle(post({ edit_id: EDIT, token: TOKEN }), { ...db.deps, serviceKey: "" });
  eq(res.status, 500);
  eq(await res.text(), "Not configured");
});

test("sha256Hex and safeEqual", async () => {
  eq(await sha256Hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  eq(safeEqual("abc", "abc"), true);
  eq(safeEqual("abc", "abd"), false);
  eq(safeEqual("abc", "ab"), false);
});
