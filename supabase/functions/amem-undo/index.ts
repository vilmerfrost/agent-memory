// amem-undo edge function entry point. Logic and docs: handler.ts.
// Deploy (the parent does this, never a worker):
//   supabase functions deploy amem-undo --no-verify-jwt
// (supabase/config.toml also sets verify_jwt = false: the ntfy Undo button sends no JWT;
// the per-edit token is the auth.) SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set by
// the platform.

import { handle } from "./handler.ts";

Deno.serve((req: Request) =>
  handle(req, {
    supabaseUrl: Deno.env.get("SUPABASE_URL") ?? "",
    serviceKey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
    fetch: (input, init) => fetch(input, init),
  })
);
