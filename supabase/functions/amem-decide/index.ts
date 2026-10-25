// amem-decide edge function entry point. Logic and docs: handler.ts (phone), web.ts (Night Desk /web/*).
// Deploy only after approval (docs/rsi/activation.md), in a logged-in CLI window (least-privilege.md A4):
//   supabase functions deploy amem-decide --no-verify-jwt
// The phone sends no JWT: the per-proposal, per-action token plus (for Apply / Undo) the TOTP code is the auth.
// The /web/* routes verify the owner's Supabase JWT themselves (web.ts), plus a passkey for Apply / Undo.
// SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY are set by the platform. Function secrets:
//   RSI_TOTP_SECRET     phone Apply / Undo (`amem rsi totp-enroll`); without it they are refused
//   RSI_DECIDE_ORIGIN   the browser origin allowed by CORS and by WebAuthn (Night Desk)
//   RSI_OWNER_USER_ID   the Supabase Auth user id of the owner; without it every /web/* call is denied
//   RSI_WEBAUTHN_RP_ID  optional; default night-desk-vilmer-frosts-projects.vercel.app

import { handle } from "./handler.ts";
import { RP_ID, serveWeb } from "./web.ts";

Deno.serve((req: Request) =>
  handle(req, {
    supabaseUrl: Deno.env.get("SUPABASE_URL") ?? "",
    serviceKey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
    anonKey: Deno.env.get("SUPABASE_ANON_KEY") ?? "",
    totpSecret: Deno.env.get("RSI_TOTP_SECRET") ?? "",
    origin: Deno.env.get("RSI_DECIDE_ORIGIN") ?? "",
    ownerUserId: Deno.env.get("RSI_OWNER_USER_ID") ?? "",
    rpId: Deno.env.get("RSI_WEBAUTHN_RP_ID") || RP_ID,
    now: () => Date.now(),
    fetch: (input, init) => fetch(input, init),
    web: serveWeb,
  })
);
