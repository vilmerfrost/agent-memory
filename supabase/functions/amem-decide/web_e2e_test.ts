// End-to-end: the real web.ts + handler.ts against the real SQL functions on a throwaway Postgres, with a
// software passkey. Only Supabase Auth is faked (one owner token). Started by tests/test_web_decide.py, which
// stages the data, passes it in AMEM_E2E_* and then checks the rows; skipped when AMEM_E2E_PG_URL is unset.
// Each step prints one `E2E {...}` line for the evidence.

import postgres from "npm:postgres@3.4.9";
import { type Deps, handle } from "./handler.ts";
import { RP_ID, serveWeb } from "./web.ts";
import { SoftAuthenticator, type Tweak } from "./softauth.ts";

const env = (k: string): string => Deno.env.get(k) ?? "";
const PG = env("AMEM_E2E_PG_URL");
const ORIGIN = `https://${RP_ID}`;
const OWNER = "6f1c2b9e-1d2a-4c3b-8e4f-5a6b7c8d9e0f";
// A fake JWT built in code (header, claims, dummy signature), so no token-shaped literal sits in the repo.
const b64url = (o: unknown): string => btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const TOKEN = `${b64url({ alg: "ES256" })}.${b64url({ sub: OWNER, role: "authenticated", exp: 4102444800 })}.c2ln`;
const TYPES: Record<string, string> = { p_challenge: "uuid", p_id: "uuid", p_sign_count: "bigint", p_transports: "text[]",
                                        p_backed_up: "boolean" };

function eq(actual: unknown, expected: unknown, msg = ""): void {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg} expected ${e}, got ${a}`);
}

function pgArray(v: unknown[]): string {
  return "{" + v.map((x) => `"${String(x).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`).join(",") + "}";
}

Deno.test({
  name: "web Apply / Reject / Undo end to end on Postgres",
  ignore: !PG,
  sanitizeResources: false,
  sanitizeOps: false,
  fn: async () => {
    const sql = postgres(PG, { max: 1, onnotice: () => {} });
    const fetchShim = async (url: string, init?: RequestInit): Promise<Response> => {
      if (url.endsWith("/auth/v1/user")) {
        const ok = new Headers(init?.headers).get("authorization") === `Bearer ${TOKEN}`;
        return new Response(JSON.stringify(ok ? { id: OWNER } : { msg: "bad jwt" }), { status: ok ? 200 : 401 });
      }
      const fn = url.split("/").pop() ?? "";
      if (!/^amem_rsi_web_[a-z_]+$/.test(fn)) return new Response("null", { status: 404 });
      const args = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      const names = Object.keys(args);
      const values = names.map((k) => Array.isArray(args[k]) ? pgArray(args[k] as unknown[]) : args[k] ?? null);
      const call = names.map((k, i) => `${k} => $${i + 1}::${TYPES[k] ?? "text"}`).join(", ");
      // deno-lint-ignore no-explicit-any
      const rows = await sql.unsafe(`select public.${fn}(${call}) as r`, values as any[]);
      return new Response(JSON.stringify(rows[0].r), { status: 200 });
    };
    const deps: Deps = { supabaseUrl: "https://e2e.supabase.test", serviceKey: "e2e-service", totpSecret: "", origin: ORIGIN,
                         ownerUserId: OWNER, rpId: RP_ID, now: () => Date.now(), fetch: fetchShim, web: serveWeb };
    const post = async (route: string, body: unknown) => {
      const r = await handle(new Request(`https://e2e.supabase.test/functions/v1/amem-decide/web/${route}`, {
        method: "POST", body: JSON.stringify(body),
        headers: { origin: ORIGIN, authorization: `Bearer ${TOKEN}`, "content-type": "application/json" },
      }), deps);
      return await r.json();
    };
    const log = (step: string, result: unknown, extra: Record<string, unknown> = {}) =>
      console.log("E2E " + JSON.stringify({ step, result, ...extra }));
    const passkey = await new SoftAuthenticator(RP_ID, ORIGIN).init();
    const decideSigned = async (kind: string, id: string, action: string, t: Tweak = {}) => {
      const ch = await post("challenge", { kind, id, action });
      if (ch.result !== "ok") return { ch, res: ch, body: null };
      const assertion = await passkey.get(ch.request_options, t);
      const body = { challenge_id: ch.challenge_id, assertion };
      return { ch, res: await post("decide", body), body };
    };
    try {
      // 1. enrollment with the code from `amem rsi passkey-enroll`
      const wrong = await post("enroll/options", { enroll_code: "A".repeat(24) });
      log("enroll/options wrong code", wrong.result);
      eq(wrong.result, "invalid");
      const opts = await post("enroll/options", { enroll_code: env("AMEM_E2E_CODE") });
      eq(opts.result, "ok", "enroll/options");
      const att = await passkey.create(opts.creation_options);
      const ver = await post("enroll/verify", { challenge_id: opts.challenge_id, attestation: att });
      log("enroll/verify", ver.result, { credential: passkey.credentialId.slice(0, 12) });
      eq(ver.result, "enrolled");
      const again = await post("enroll/verify", { challenge_id: opts.challenge_id, attestation: att });
      const used = await post("enroll/options", { enroll_code: env("AMEM_E2E_CODE") });
      log("enroll/verify again; options with the used code", [again.result, used.result]);
      eq([again.result, used.result], ["invalid", "invalid"]);

      // 2. Apply with Face ID, then the same request again
      const apply = await decideSigned("proposal", env("AMEM_E2E_APPLY"), "apply");
      eq([apply.ch.result, apply.ch.needs_passkey, apply.ch.summary.state], ["ok", true, "staged"]);
      log("apply", apply.res.result, { target: apply.ch.summary.target, artifact: String(apply.ch.summary.artifact_hash).slice(0, 12) });
      eq(apply.res.result, "recorded");
      const repeat = await post("decide", apply.body);
      log("apply repeated (same challenge + assertion)", repeat.result);
      eq(repeat.result, "already");
      const fresh = await post("challenge", { kind: "proposal", id: env("AMEM_E2E_APPLY"), action: "apply" });
      log("apply again with a new challenge", fresh.result);
      eq(fresh.result, "not-staged");

      // 3. Replay: the used assertion against a fresh challenge for another proposal
      const cross = await post("challenge", { kind: "proposal", id: env("AMEM_E2E_CROSS"), action: "apply" });
      const replay = await post("decide", { challenge_id: cross.challenge_id, assertion: apply.body!.assertion });
      log("replayed assertion on another proposal", replay.result);
      eq(replay.result, "passkey-invalid");
      const burned = await post("decide", { challenge_id: cross.challenge_id,
                                            assertion: await passkey.get(cross.request_options) });
      log("correct assertion on the burned challenge", burned.result);
      eq(burned.result, "expired");

      // 4. Bad signature, no UV, expired challenge (all on the CROSS proposal, which must stay staged)
      for (const [name, t] of [["bad signature", { badSignature: true }], ["UV flag missing", { uv: false }]] as [string, Tweak][]) {
        const r = await decideSigned("proposal", env("AMEM_E2E_CROSS"), "apply", t);
        log(name, r.res.result);
        eq(r.res.result, "passkey-invalid", name);
      }
      const ex = await post("challenge", { kind: "proposal", id: env("AMEM_E2E_CROSS"), action: "apply" });
      await sql`update rsi_web_challenges set expires_at = now() - interval '1 second' where id = ${ex.challenge_id}`;
      const exr = await post("decide", { challenge_id: ex.challenge_id, assertion: await passkey.get(ex.request_options) });
      log("expired challenge", exr.result);
      eq(exr.result, "expired");

      // 5. Reject: the login only, no passkey
      const rch = await post("challenge", { kind: "proposal", id: env("AMEM_E2E_REJECT"), action: "reject" });
      eq([rch.result, rch.needs_passkey, rch.request_options], ["ok", false, null]);
      const rj = await post("decide", { challenge_id: rch.challenge_id, assertion: null });
      const rj2 = await post("decide", { challenge_id: rch.challenge_id, assertion: null });
      log("reject without passkey; repeated", [rj.result, rj2.result]);
      eq([rj.result, rj2.result], ["recorded", "already"]);

      // 6. Undo of an older edit-loop change, with Face ID; then again with a new challenge
      const eu = await decideSigned("edit", env("AMEM_E2E_EDIT"), "undo");
      log("edit undo", eu.res.result, { target: eu.ch.summary.target });
      eq(eu.res.result, "recorded");
      const eu2 = await decideSigned("edit", env("AMEM_E2E_EDIT"), "undo");
      log("edit undo again (new challenge)", eu2.res.result);
      eq(eu2.res.result, "already");

      // 7. Undo of a promoted night proposal, with Face ID
      const pu = await decideSigned("proposal", env("AMEM_E2E_PROMOTED"), "undo");
      log("proposal undo", pu.res.result);
      eq(pu.res.result, "recorded");
      const nop = await post("challenge", { kind: "proposal", id: env("AMEM_E2E_CROSS"), action: "undo" });
      log("undo of a proposal that is not promoted", nop.result);
      eq(nop.result, "not-promoted");
      console.log("E2E " + JSON.stringify({ step: "done", passkey_count: passkey.counter }));
    } finally {
      await sql.end();
    }
  },
});
