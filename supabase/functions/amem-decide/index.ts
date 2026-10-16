// amem-decide edge function entry point. Logic and docs: handler.ts.
// Deploy only after approval (docs/rsi/activation.md), in a logged-in CLI window (least-privilege.md A4):
//   supabase functions deploy amem-decide --no-verify-jwt
// The phone sends no JWT: the per-proposal, per-action token plus (for Apply / Undo) the TOTP code is the auth.
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set by the platform. RSI_TOTP_SECRET and RSI_DECIDE_ORIGIN
// are function secrets (`amem rsi totp-enroll`). Without RSI_TOTP_SECRET, Apply / Undo are refused.

import { handle } from "./handler.ts";

Deno.serve((req: Request) =>
  handle(req, {
    supabaseUrl: Deno.env.get("SUPABASE_URL") ?? "",
    serviceKey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
    totpSecret: Deno.env.get("RSI_TOTP_SECRET") ?? "",
    origin: Deno.env.get("RSI_DECIDE_ORIGIN") ?? "",
    now: () => Date.now(),
    fetch: (input, init) => fetch(input, init),
  })
);
