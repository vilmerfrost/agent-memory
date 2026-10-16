// amem-decide: phone decisions for an RSI proposal (docs/rsi/approval.md, docs/rsi/least-privilege.md A3).
//
// POST {"proposal_id": "<uuid>", "artifact_hash": "<64 hex>", "action": "apply"|"reject"|"undo", "token": "<token>",
//       "totp": "<6 digits>"}            (totp is required for apply and undo; reject needs none)
//
// Apply / Undo: the 6-digit code is checked here against RSI_TOTP_SECRET (a function secret; never on the Mac
// or in the database). The matched 30-second step goes to amem_rsi_record_phone_decision, which uses each step
// once, then checks sha256(token:proposal:artifact_hash:action), expiry, single use and the exact artifact, and
// appends one decision row. A wrong code is counted (amem_rsi_phone_bad_code); five lock the proposal.
// That is all this function does: it never changes production config. The promotion worker on the Mac acts
// on the row, and `improve promote` re-checks it. No secret configured -> Apply / Undo are refused.
//
// Pure logic, no Deno globals, so it runs under `deno test` and `node --test` alike.

import { verify } from "./totp.ts";

export interface Deps {
  supabaseUrl: string;
  serviceKey: string;
  totpSecret: string;          // base32; empty = phone Apply / Undo disabled
  origin: string;              // the decide page origin allowed by CORS; empty = no browser access
  now: () => number;           // ms since the epoch
  fetch: (input: string, init?: RequestInit) => Promise<Response>;
}

export const TEXT: Record<string, [number, string]> = {
  "recorded:apply": [200, "Approved — the change is applied within minutes"],
  "recorded:reject": [200, "Rejected — nothing will change"],
  "recorded:undo": [200, "Undo queued — the previous version is restored within minutes"],
  already: [200, "Already recorded — nothing to do"],
  expired: [200, "Expired — this proposal must be staged again"],
  "not-staged": [200, "This proposal is no longer open"],
  "not-promoted": [200, "Nothing to undo — this change is not applied"],
  "totp-replayed": [403, "That code was already used — wait for the next one"],
  locked: [403, "Locked after 5 wrong codes — decide in the terminal"],
};
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH_RE = /^[0-9a-f]{64}$/;
const ACTIONS = new Set(["apply", "reject", "undo"]);
const NEEDS_CODE = new Set(["apply", "undo"]);
const BODY_MAX = 2048;

function cors(deps: Deps, req: Request): Record<string, string> {
  const o = req.headers.get("origin");
  return deps.origin && o === deps.origin
    ? { "Access-Control-Allow-Origin": deps.origin, "Vary": "Origin", "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "content-type", "Access-Control-Max-Age": "600" }
    : {};
}

function text(status: number, body: string, extra: Record<string, string> = {}): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8", ...extra } });
}

async function rpc(deps: Deps, fn: string, args: Record<string, unknown>): Promise<unknown> {
  const base = deps.supabaseUrl.replace(/\/+$/, "");
  const r = await deps.fetch(`${base}/rest/v1/rpc/${fn}`, {
    method: "POST",
    headers: { apikey: deps.serviceKey, Authorization: `Bearer ${deps.serviceKey}`, "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  if (!r.ok) throw new Error(`rpc ${r.status}`);
  return await r.json();
}

export async function handle(req: Request, deps: Deps): Promise<Response> {
  const h = cors(deps, req);
  if (req.method === "OPTIONS") {
    return h["Access-Control-Allow-Origin"] ? new Response(null, { status: 204, headers: h }) : text(403, "Not allowed");
  }
  if (req.method !== "POST") return text(405, "Method not allowed", h);
  let raw: string;
  try {
    raw = await req.text();
  } catch {
    return text(400, "Bad request", h);
  }
  if (raw.length > BODY_MAX) return text(400, "Bad request", h);
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return text(400, "Bad request", h);
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return text(400, "Bad request", h);
  const { proposal_id: pid, artifact_hash: hash, action, token, totp } = body as Record<string, unknown>;
  if (typeof pid !== "string" || !UUID_RE.test(pid)) return text(400, "Bad request", h);
  if (typeof hash !== "string" || !HASH_RE.test(hash)) return text(400, "Bad request", h);
  if (typeof action !== "string" || !ACTIONS.has(action)) return text(400, "Bad request", h);
  if (typeof token !== "string" || token.length < 16 || token.length > 200) return text(400, "Bad request", h);
  if (!deps.supabaseUrl || !deps.serviceKey) return text(500, "Not configured", h);
  const proposal = pid.toLowerCase();
  let step: number | null = null;
  if (NEEDS_CODE.has(action)) {
    if (!deps.totpSecret) return text(503, "Phone approval is not set up — decide in the terminal", h);
    if (typeof totp !== "string" || !/^[0-9]{6}$/.test(totp)) return text(403, "Enter the 6-digit code", h);
    step = await verify(deps.totpSecret, totp, deps.now());
    if (step === null) {
      let n: unknown = null;
      try {
        n = await rpc(deps, "amem_rsi_phone_bad_code", { p_proposal: proposal });
      } catch {
        return text(502, "Try again later", h);
      }
      return text(403, `Wrong code (${typeof n === "number" ? Math.min(n, 5) : "?"} of 5)`, h);
    }
  }
  let state: unknown;
  try {
    state = await rpc(deps, "amem_rsi_record_phone_decision",
      { p_proposal: proposal, p_action: action, p_artifact_hash: hash, p_token: token, p_totp_step: step });
  } catch {
    return text(502, "Try again later", h);
  }
  const hit = TEXT[state === "recorded" ? `recorded:${action}` : String(state)];
  if (hit) return text(hit[0], hit[1], h);
  if (state === "denied" || state === "missing" || state === "hash-mismatch" || state === "invalid" ||
      state === "totp-required") {
    return text(403, "Not allowed", h);
  }
  return text(502, "Try again later", h);
}
