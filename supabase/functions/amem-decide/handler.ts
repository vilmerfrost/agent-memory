// amem-decide: the phone's Apply / Reject / Undo buttons for an RSI proposal (docs/rsi/approval.md).
//
// POST {"proposal_id": "<uuid>", "artifact_hash": "<64 hex>", "action": "apply"|"reject"|"undo", "token": "<token>"}.
// The SQL function amem_rsi_record_decision checks sha256(token:proposal:artifact_hash:action) against the stored
// token hash, its expiry and single use, and that the proposal still has that exact artifact, then appends one
// decision row. That is all this function does: it never changes production config. The promotion worker
// (`amem rsi decide-poll` on the Mac) acts on the row; `improve promote` re-checks it.
//
// Pure logic, no Deno globals, so it runs under `deno test` and `node --test` alike.

export interface Deps {
  supabaseUrl: string;
  serviceKey: string;
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
};
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const HASH_RE = /^[0-9a-f]{64}$/;
const ACTIONS = new Set(["apply", "reject", "undo"]);
const BODY_MAX = 2048;

function text(status: number, body: string): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}

export async function handle(req: Request, deps: Deps): Promise<Response> {
  if (req.method !== "POST") return text(405, "Method not allowed");
  let raw: string;
  try {
    raw = await req.text();
  } catch {
    return text(400, "Bad request");
  }
  if (raw.length > BODY_MAX) return text(400, "Bad request");
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return text(400, "Bad request");
  }
  if (!body || typeof body !== "object" || Array.isArray(body)) return text(400, "Bad request");
  const { proposal_id: pid, artifact_hash: hash, action, token } = body as Record<string, unknown>;
  if (typeof pid !== "string" || !UUID_RE.test(pid)) return text(400, "Bad request");
  if (typeof hash !== "string" || !HASH_RE.test(hash)) return text(400, "Bad request");
  if (typeof action !== "string" || !ACTIONS.has(action)) return text(400, "Bad request");
  if (typeof token !== "string" || token.length < 16 || token.length > 200) return text(400, "Bad request");
  if (!deps.supabaseUrl || !deps.serviceKey) return text(500, "Not configured");
  const base = deps.supabaseUrl.replace(/\/+$/, "");
  let state: unknown;
  try {
    const rpc = await deps.fetch(`${base}/rest/v1/rpc/amem_rsi_record_decision`, {
      method: "POST",
      headers: { apikey: deps.serviceKey, Authorization: `Bearer ${deps.serviceKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ p_proposal: pid.toLowerCase(), p_action: action, p_artifact_hash: hash, p_token: token }),
    });
    if (!rpc.ok) return text(502, "Try again later");
    state = await rpc.json();
  } catch {
    return text(502, "Try again later");
  }
  const hit = TEXT[state === "recorded" ? `recorded:${action}` : String(state)];
  if (hit) return text(hit[0], hit[1]);
  if (state === "denied" || state === "missing" || state === "hash-mismatch" || state === "invalid") {
    return text(403, "Not allowed");
  }
  return text(502, "Try again later");
}
