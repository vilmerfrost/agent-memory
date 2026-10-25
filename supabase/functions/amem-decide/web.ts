// amem-decide /web/* routes: Night Desk Apply / Reject / Undo (docs/rsi/approval.md "Web decisions").
//
// Every route: Origin must equal RSI_DECIDE_ORIGIN, and Authorization must carry a Supabase user JWT whose
// subject is RSI_OWNER_USER_ID (checked locally, then confirmed by Supabase Auth /auth/v1/user, so a signed-out
// session or a forged token fails). Apply and Undo also need a WebAuthn passkey assertion (Face ID) over a
// one-use challenge bound to (kind, id, action, artifact hash); Reject needs the login only.
// The SQL functions (migration 20261025210000_rsi_web_decide.sql) consume the challenge, check the sign count
// and record the decision row or the edit undo request. Nothing here promotes or changes a file.
//
// Every reply is JSON with `result`; the HTTP status is informational.

import {
  generateAuthenticationOptions,
  generateRegistrationOptions,
  verifyAuthenticationResponse,
  verifyRegistrationResponse,
} from "npm:@simplewebauthn/server@14.0.3";
import type { Deps } from "./handler.ts";

export const RP_ID = "night-desk-vilmer-frosts-projects.vercel.app";
export const ROUTES = ["challenge", "decide", "enroll/options", "enroll/verify"] as const;
export type Route = typeof ROUTES[number];
const ROUTE_RE = /\/web\/(challenge|decide|enroll\/options|enroll\/verify)\/?$/;
const WEB_RE = /\/web(\/|$)/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const BODY_MAX = 16384;
const ALGS = [-7, -257];                                  // ES256 (Apple, Android), RS256 (Windows Hello)
const CHALLENGE_MS = 120_000;

/** Decide results the SQL may return (the contract's list). */
export const DECIDE_RESULTS = new Set(["recorded", "already", "denied", "expired", "missing", "hash-mismatch",
  "not-staged", "not-promoted", "invalid", "passkey-required", "passkey-invalid", "not-enrolled"]);
const CHALLENGE_RESULTS = new Set(["invalid", "missing", "expired", "not-staged", "not-promoted", "not-enrolled"]);
const STATUS: Record<string, number> = {
  ok: 200, recorded: 200, already: 200, enrolled: 200, denied: 403, invalid: 400, missing: 404, error: 502,
  "passkey-required": 401, "passkey-invalid": 403, "not-enrolled": 403,
};

export function webRoute(pathname: string): Route | "unknown" | null {
  const m = ROUTE_RE.exec(pathname);
  if (m) return m[1] as Route;
  return WEB_RE.test(pathname) ? "unknown" : null;
}

export function webCors(deps: Deps, req: Request): Record<string, string> {
  const o = req.headers.get("origin");
  return deps.origin && o === deps.origin
    ? { "Access-Control-Allow-Origin": deps.origin, "Vary": "Origin", "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "authorization, content-type", "Access-Control-Max-Age": "600" }
    : { "Vary": "Origin" };
}

function reply(result: string, h: Record<string, string>, extra: Record<string, unknown> = {}): Response {
  return new Response(JSON.stringify({ result, ...extra }), {
    status: STATUS[result] ?? 409,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...h },
  });
}

// -- small helpers (WebCrypto only) -------------------------------------------------------------------
export function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function unb64url(s: string): Uint8Array<ArrayBuffer> {
  const t = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(t + "=".repeat((4 - (t.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

export async function sha256hex(s: string): Promise<string> {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
}

function same(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

/** The enroll code as `amem rsi passkey-enroll` prints it, normalized: base32 letters only, upper case. */
export function normalizeEnrollCode(code: string): string {
  return code.toUpperCase().replace(/[^A-Z2-7]/g, "");
}

async function rpc(deps: Deps, fn: string, args: Record<string, unknown>): Promise<unknown> {
  const base = deps.supabaseUrl.replace(/\/+$/, "");
  const r = await deps.fetch(`${base}/rest/v1/rpc/${fn}`, {
    method: "POST",
    headers: { apikey: deps.serviceKey, Authorization: `Bearer ${deps.serviceKey}`, "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  if (!r.ok) throw new Error(`rpc ${fn} ${r.status}`);
  return await r.json();
}

// -- the owner's login --------------------------------------------------------------------------------
function jwtClaims(token: string): Record<string, unknown> | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const c = JSON.parse(new TextDecoder().decode(unb64url(parts[1])));
    return c && typeof c === "object" && !Array.isArray(c) ? c : null;
  } catch {
    return null;
  }
}

/** "ok", "denied" or "error" (Auth unreachable). A cheap local check first, then Supabase Auth confirms the
 *  token's signature and session (/auth/v1/user), so a decoded-but-forged token never passes. */
export async function checkOwner(deps: Deps, req: Request): Promise<"ok" | "denied" | "error"> {
  const owner = (deps.ownerUserId ?? "").toLowerCase();
  if (!UUID_RE.test(owner)) return "denied";                // not configured: nobody is the owner
  const m = /^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$/.exec(req.headers.get("authorization") ?? "");
  if (!m) return "denied";
  const c = jwtClaims(m[1]);
  if (!c || String(c.sub ?? "").toLowerCase() !== owner || c.role !== "authenticated" ||
      typeof c.exp !== "number" || c.exp * 1000 <= deps.now()) {
    return "denied";
  }
  let r: Response;
  try {
    r = await deps.fetch(`${deps.supabaseUrl.replace(/\/+$/, "")}/auth/v1/user`, {
      method: "GET",
      headers: { apikey: deps.anonKey || deps.serviceKey, Authorization: `Bearer ${m[1]}` },
    });
  } catch {
    return "error";
  }
  if (r.status >= 500) return "error";
  if (!r.ok) return "denied";
  try {
    const u = await r.json();
    return u && typeof u.id === "string" && u.id.toLowerCase() === owner ? "ok" : "denied";
  } catch {
    return "denied";
  }
}

// -- routes -------------------------------------------------------------------------------------------
/** deps.web for handler.ts: route by path. */
export function serveWeb(req: Request, deps: Deps, path: string): Promise<Response> {
  return handleWeb(req, deps, webRoute(path) ?? "unknown");
}

export async function handleWeb(req: Request, deps: Deps, route: Route | "unknown"): Promise<Response> {
  const h = webCors(deps, req);
  if (req.method === "OPTIONS") {
    return h["Access-Control-Allow-Origin"] ? new Response(null, { status: 204, headers: h }) : reply("denied", h);
  }
  if (!deps.origin || req.headers.get("origin") !== deps.origin) return reply("denied", h);
  if (req.method !== "POST" || route === "unknown") return reply("invalid", h);
  if (!deps.supabaseUrl || !deps.serviceKey) return reply("error", h);
  const who = await checkOwner(deps, req);
  if (who !== "ok") return reply(who, h);
  let body: Record<string, unknown>;
  try {
    const raw = await req.text();
    if (raw.length > BODY_MAX) return reply("invalid", h);
    const b = JSON.parse(raw);
    if (!b || typeof b !== "object" || Array.isArray(b)) return reply("invalid", h);
    body = b;
  } catch {
    return reply("invalid", h);
  }
  try {
    if (route === "challenge") return await challenge(deps, body, h);
    if (route === "decide") return await decide(deps, body, h);
    if (route === "enroll/options") return await enrollOptions(deps, body, h);
    return await enrollVerify(deps, body, h);
  } catch {
    return reply("error", h);                                // upstream failure: nothing was recorded by us
  }
}

function rpId(deps: Deps): string {
  return deps.rpId || RP_ID;
}

async function challenge(deps: Deps, b: Record<string, unknown>, h: Record<string, string>): Promise<Response> {
  const { kind, id, action } = b;
  if ((kind !== "proposal" && kind !== "edit") || typeof id !== "string" || !UUID_RE.test(id) ||
      (action !== "apply" && action !== "reject" && action !== "undo") || (kind === "edit" && action !== "undo")) {
    return reply("invalid", h);
  }
  const nonce = crypto.getRandomValues(new Uint8Array(32));
  const challengeB64 = b64url(nonce);
  const out = await rpc(deps, "amem_rsi_web_challenge",
    { p_kind: kind, p_id: id.toLowerCase(), p_action: action, p_nonce_hash: await sha256hex(challengeB64) });
  const o = (out && typeof out === "object" ? out : {}) as Record<string, unknown>;
  if (o.result !== "ok") {
    return reply(CHALLENGE_RESULTS.has(String(o.result)) ? String(o.result) : "error", h);
  }
  let options: unknown = null;
  if (o.needs_passkey === true) {
    const creds = Array.isArray(o.credentials) ? o.credentials as { id: string; transports?: string[] }[] : [];
    const opts = await generateAuthenticationOptions({
      rpID: rpId(deps), challenge: nonce, userVerification: "required", timeout: CHALLENGE_MS,
      allowCredentials: creds.map((c) => ({ id: c.id, transports: c.transports?.length ? c.transports : undefined })),
    });
    if (opts.challenge !== challengeB64) return reply("error", h);
    options = opts;
  }
  return reply("ok", h, { challenge_id: o.challenge_id, expires_at: o.expires_at, needs_passkey: o.needs_passkey === true,
                          request_options: options, summary: o.summary });
}

async function decideRpc(deps: Deps, cid: string, nonceHash: string | null, cred: string | null,
                         count: number | null): Promise<string> {
  const r = await rpc(deps, "amem_rsi_web_decide",
    { p_challenge: cid, p_nonce_hash: nonceHash, p_credential_id: cred, p_sign_count: count });
  return DECIDE_RESULTS.has(String(r)) ? String(r) : "error";
}

async function decide(deps: Deps, b: Record<string, unknown>, h: Record<string, string>): Promise<Response> {
  const cid = b.challenge_id;
  if (typeof cid !== "string" || !UUID_RE.test(cid)) return reply("invalid", h);
  const a = b.assertion;
  const assertion = a && typeof a === "object" && !Array.isArray(a) ? a as Record<string, unknown> : null;
  const credId = assertion && typeof assertion.id === "string" ? assertion.id : null;
  const ctx = await rpc(deps, "amem_rsi_web_context", { p_challenge: cid, p_credential_id: credId }) as
    Record<string, unknown> | null;
  if (!ctx || ctx.found !== true || ctx.kind === "enroll") return reply("invalid", h);
  const needs = ctx.action === "apply" || ctx.action === "undo";
  if (!needs || ctx.used === true || ctx.expired === true) {
    // Reject (login only), or a challenge that is already used / expired: the SQL answers already / expired
    // without looking at any assertion, and never records twice.
    return reply(await decideRpc(deps, cid, null, null, null), h);
  }
  if (!assertion || !credId) return reply("passkey-required", h);
  const cred = ctx.credential as { id: string; public_key: string; sign_count: number } | null;
  if (!cred || cred.id !== credId) return reply("not-enrolled", h);
  const nonceHash = String(ctx.nonce_hash);
  let ok = false, count = 0;
  try {
    const v = await verifyAuthenticationResponse({
      // deno-lint-ignore no-explicit-any
      response: assertion as any,
      expectedChallenge: async (c: string) => same(await sha256hex(c), nonceHash),
      expectedOrigin: deps.origin,
      expectedRPID: rpId(deps),
      expectedType: "webauthn.get",
      credential: { id: cred.id, publicKey: unb64url(cred.public_key), counter: Number(cred.sign_count) },
      requireUserVerification: true,
    });
    ok = v.verified === true && v.authenticationInfo.userVerified === true && v.authenticationInfo.credentialID === credId;
    count = v.authenticationInfo.newCounter;
  } catch {
    ok = false;
  }
  if (!ok) {
    // Burn the challenge: a wrong signature, origin, RP ID, UV flag or challenge never gets a second try.
    await decideRpc(deps, cid, "0".repeat(64), credId, 0);
    return reply("passkey-invalid", h);
  }
  return reply(await decideRpc(deps, cid, nonceHash, credId, count), h);
}

async function enrollOptions(deps: Deps, b: Record<string, unknown>, h: Record<string, string>): Promise<Response> {
  const code = typeof b.enroll_code === "string" ? normalizeEnrollCode(b.enroll_code) : "";
  if (code.length < 20 || code.length > 64) return reply("invalid", h);
  const nonce = crypto.getRandomValues(new Uint8Array(32));
  const challengeB64 = b64url(nonce);
  const out = await rpc(deps, "amem_rsi_web_enroll_start",
    { p_code_hash: await sha256hex(code), p_nonce_hash: await sha256hex(challengeB64) }) as Record<string, unknown> | null;
  if (!out || out.result !== "ok") {
    return reply(out?.result === "expired" ? "expired" : "invalid", h);
  }
  const exclude = Array.isArray(out.exclude) ? out.exclude as string[] : [];
  const options = await generateRegistrationOptions({
    rpName: "Night Desk", rpID: rpId(deps), userName: "night-desk-owner", userDisplayName: "Night Desk owner",
    userID: new TextEncoder().encode(String(deps.ownerUserId)), challenge: nonce, attestationType: "none",
    timeout: 300_000, supportedAlgorithmIDs: ALGS, excludeCredentials: exclude.map((id) => ({ id })),
    authenticatorSelection: { residentKey: "preferred", userVerification: "required" },
  });
  if (options.challenge !== challengeB64) return reply("error", h);
  return reply("ok", h, { challenge_id: out.challenge_id, expires_at: out.expires_at, creation_options: options });
}

async function enrollVerify(deps: Deps, b: Record<string, unknown>, h: Record<string, string>): Promise<Response> {
  const cid = b.challenge_id;
  const att = b.attestation;
  if (typeof cid !== "string" || !UUID_RE.test(cid) || !att || typeof att !== "object" || Array.isArray(att)) {
    return reply("invalid", h);
  }
  const ctx = await rpc(deps, "amem_rsi_web_context", { p_challenge: cid, p_credential_id: null }) as
    Record<string, unknown> | null;
  if (!ctx || ctx.found !== true || ctx.kind !== "enroll" || ctx.used === true) return reply("invalid", h);
  if (ctx.expired === true) return reply("expired", h);
  const nonceHash = String(ctx.nonce_hash);
  let info;
  try {
    const v = await verifyRegistrationResponse({
      // deno-lint-ignore no-explicit-any
      response: att as any,
      expectedChallenge: async (c: string) => same(await sha256hex(c), nonceHash),
      expectedOrigin: deps.origin,
      expectedRPID: rpId(deps),
      expectedType: "webauthn.create",
      requireUserPresence: true,
      requireUserVerification: true,
      supportedAlgorithmIDs: ALGS,
    });
    if (!v.verified) return reply("invalid", h);
    info = v.registrationInfo;
  } catch {
    return reply("invalid", h);
  }
  const r = await rpc(deps, "amem_rsi_web_enroll_finish", {
    p_challenge: cid, p_nonce_hash: nonceHash, p_credential_id: info.credential.id,
    p_public_key: b64url(info.credential.publicKey), p_sign_count: info.credential.counter,
    p_transports: info.credential.transports ?? [], p_device_type: info.credentialDeviceType,
    p_backed_up: info.credentialBackedUp,
  });
  return reply(r === "enrolled" || r === "expired" ? String(r) : "invalid", h);
}
