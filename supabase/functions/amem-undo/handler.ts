// amem-undo: the phone's "Undo" button for an agent edit (docs/edits.md, "Push and undo").
//
// POST {"edit_id": "<uuid>", "token": "<undo token>"}. The token's sha256 must equal
// edits.undo_token_hash. Then the SQL function amem_request_undo queues the edit's one
// undo_requests row (a request closed without a revert is reopened; a reverted one is left
// alone) and `amem edits undo-poll` reverts the edit within 15 minutes. The reply text says
// what will happen. The function never reverts anything itself and never says why a request
// was refused.
//
// Pure logic, no Deno globals, so it runs under `deno test` and `node --test` alike.
// index.ts wires it to Deno.serve with the service role key from the function env.

export interface Deps {
  supabaseUrl: string;
  serviceKey: string;
  fetch: (input: string, init?: RequestInit) => Promise<Response>;
}

export const OK_TEXT = "Undo queued — reverts within 15 minutes";
export const ALREADY_TEXT = "Already undone — nothing to do";
export const NOTHING_TEXT = "Nothing to undo — this edit is no longer active";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const TOKEN_MIN = 16;
const TOKEN_MAX = 200;
const BODY_MAX = 2048;

function text(status: number, body: string): Response {
  return new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
}

export async function sha256Hex(s: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
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
  const { edit_id: editId, token } = body as Record<string, unknown>;
  if (typeof editId !== "string" || !UUID_RE.test(editId)) return text(400, "Bad request");
  if (typeof token !== "string" || token.length < TOKEN_MIN || token.length > TOKEN_MAX) {
    return text(400, "Bad request");
  }
  if (!deps.supabaseUrl || !deps.serviceKey) return text(500, "Not configured");

  const base = deps.supabaseUrl.replace(/\/+$/, "");
  const headers = {
    apikey: deps.serviceKey,
    Authorization: `Bearer ${deps.serviceKey}`,
    "Content-Type": "application/json",
  };
  const id = editId.toLowerCase();
  try {
    const found = await deps.fetch(`${base}/rest/v1/edits?id=eq.${id}&select=id,undo_token_hash`, { headers });
    if (!found.ok) return text(502, "Try again later");
    const rows = await found.json();
    const want = Array.isArray(rows) && rows.length === 1 ? rows[0]?.undo_token_hash : null;
    const got = await sha256Hex(token);
    if (typeof want !== "string" || !safeEqual(want, got)) return text(403, "Not allowed");

    // Same SQL function as `amem edits undo`: inserts the request, or reopens one that was
    // closed without a revert. It says what will happen, and the reply says exactly that.
    const rpc = await deps.fetch(`${base}/rest/v1/rpc/amem_request_undo`, {
      method: "POST",
      headers,
      body: JSON.stringify({ p_edit_id: id, p_source: "phone" }),
    });
    if (!rpc.ok) return text(502, "Try again later");
    const state = await rpc.json();
    if (state === "reverted") return text(200, ALREADY_TEXT);
    if (state === "nothing") return text(200, NOTHING_TEXT);
    if (state === "missing") return text(403, "Not allowed");
    if (state !== "queued") return text(502, "Try again later");
    // Best effort: the poll sets it too.
    await deps.fetch(`${base}/rest/v1/edits?id=eq.${id}&undo_requested_at=is.null`, {
      method: "PATCH",
      headers: { ...headers, Prefer: "return=minimal" },
      body: JSON.stringify({ undo_requested_at: new Date().toISOString() }),
    }).catch(() => undefined);
  } catch {
    return text(502, "Try again later");
  }
  return text(200, OK_TEXT);
}
