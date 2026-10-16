// amem-decide edge function entry point. Logic and docs: handler.ts.
// NOT deployed in Mission 003. Deploy only after approval (docs/rsi/activation.md):
//   supabase functions deploy amem-decide --no-verify-jwt
// (the ntfy buttons send no JWT; the per-proposal, per-action token is the auth). SUPABASE_URL and
// SUPABASE_SERVICE_ROLE_KEY are set by the platform.

import { handle } from "./handler.ts";

Deno.serve((req: Request) =>
  handle(req, {
    supabaseUrl: Deno.env.get("SUPABASE_URL") ?? "",
    serviceKey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
    fetch: (input, init) => fetch(input, init),
  })
);
