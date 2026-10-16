// Unit tests for handler.ts and totp.ts with a fake PostgREST (no network). The real SQL functions are tested
// in tests/test_least_privilege.py. Run: `node --test supabase/functions/amem-decide/handler_test.ts`.

import { type Deps, handle, TEXT } from "./handler.ts";
import { codeAt, RFC_SECRET_B32, RFC_VECTOR, verify } from "./totp.ts";

// deno-lint-ignore no-explicit-any
const g = globalThis as any;
const test: (name: string, fn: () => Promise<void>) => void = g.Deno?.test ?? (await import("node:test")).test;

function eq(actual: unknown, expected: unknown, msg = ""): void {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg} expected ${e}, got ${a}`);
}

const PID = "0b8f8f3e-4c1a-4f7e-9d2a-1f2e3d4c5b6a";
const HASH = "a".repeat(64);
const TOKEN = "tok-abcdefghijklmnopqrstuvwxyz012345";
const SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP";
const NOW = 1_800_000_000_000;                   // ms
const STEP_NOW = Math.floor(NOW / 1000 / 30);
const ORIGIN = "https://decide.example.test";

type Call = { url: string; body: Record<string, unknown> };

function deps(answers: Record<string, unknown>, calls: Call[] = [], opts: Partial<Deps> = {}, ok = true): Deps {
  return {
    supabaseUrl: "https://example.supabase.co/",
    serviceKey: "service-key-for-tests",
    totpSecret: SECRET,
    origin: ORIGIN,
    now: () => NOW,
    fetch: (url: string, init?: RequestInit) => {
      const fn = url.split("/").pop() ?? "";
      calls.push({ url, body: JSON.parse(String(init?.body ?? "{}")) });
      return Promise.resolve(new Response(JSON.stringify(answers[fn] ?? null), { status: ok ? 200 : 500 }));
    },
    ...opts,
  };
}

function post(body: unknown, origin?: string): Request {
  return new Request("https://x/functions/v1/amem-decide",
    { method: "POST", body: JSON.stringify(body), headers: origin ? { origin } : {} });
}

const base = { proposal_id: PID, artifact_hash: HASH, token: TOKEN };
const PHONE = "amem_rsi_record_phone_decision";

test("TOTP matches the RFC 6238 vector (same as rsi_totp.py)", async () => {
  eq(await codeAt(RFC_SECRET_B32, Math.floor(RFC_VECTOR[0] / 30)), RFC_VECTOR[1]);
  const code = await codeAt(SECRET, STEP_NOW);
  eq(await verify(SECRET, code, NOW), STEP_NOW);
  eq(await verify(SECRET, await codeAt(SECRET, STEP_NOW - 1), NOW), STEP_NOW - 1);   // one step of clock skew
  eq(await verify(SECRET, await codeAt(SECRET, STEP_NOW - 3), NOW), null);           // too old
  eq(await verify(SECRET, "12345", NOW), null);
});

test("apply with a valid code: one call to the TOTP-gated function, with the matched step", async () => {
  const calls: Call[] = [];
  const r = await handle(post({ ...base, action: "apply", totp: await codeAt(SECRET, STEP_NOW) }),
    deps({ [PHONE]: "recorded" }, calls));
  eq([r.status, await r.text()], [200, TEXT["recorded:apply"][1]]);
  eq(calls.length, 1);
  eq(calls[0].url, `https://example.supabase.co/rest/v1/rpc/${PHONE}`);
  eq(calls[0].body, { p_proposal: PID, p_action: "apply", p_artifact_hash: HASH, p_token: TOKEN, p_totp_step: STEP_NOW });
});

test("apply or undo without a code never reaches the database", async () => {
  for (const action of ["apply", "undo"]) {
    for (const totp of [undefined, "", "12345", "abcdef", 123456]) {
      const calls: Call[] = [];
      const r = await handle(post({ ...base, action, totp }), deps({ [PHONE]: "recorded" }, calls));
      eq([r.status, calls.length], [403, 0], `${action} ${String(totp)}`);
    }
  }
});

test("a wrong code is counted and nothing is recorded", async () => {
  const calls: Call[] = [];
  const good = await codeAt(SECRET, STEP_NOW);
  const wrong = good === "000000" ? "111111" : "000000";
  const r = await handle(post({ ...base, action: "apply", totp: wrong }), deps({ amem_rsi_phone_bad_code: 3 }, calls));
  eq([r.status, await r.text()], [403, "Wrong code (3 of 5)"]);
  eq(calls.map((c) => c.url.split("/").pop()), ["amem_rsi_phone_bad_code"]);
});

test("replayed code and locked proposal say so", async () => {
  const totp = await codeAt(SECRET, STEP_NOW);
  eq(await (await handle(post({ ...base, action: "apply", totp }), deps({ [PHONE]: "totp-replayed" }))).text(),
    TEXT["totp-replayed"][1]);
  eq((await handle(post({ ...base, action: "apply", totp }), deps({ [PHONE]: "locked" }))).status, 403);
});

test("no TOTP secret configured: apply and undo refused, reject still works", async () => {
  const totp = await codeAt(SECRET, STEP_NOW);
  const calls: Call[] = [];
  const r = await handle(post({ ...base, action: "apply", totp }), deps({ [PHONE]: "recorded" }, calls, { totpSecret: "" }));
  eq([r.status, calls.length], [503, 0]);
  const rj = await handle(post({ ...base, action: "reject" }), deps({ [PHONE]: "recorded" }, [], { totpSecret: "" }));
  eq([rj.status, await rj.text()], [200, TEXT["recorded:reject"][1]]);
});

test("reject needs no code and passes a null step", async () => {
  const calls: Call[] = [];
  const r = await handle(post({ ...base, action: "reject" }), deps({ [PHONE]: "recorded" }, calls));
  eq(r.status, 200);
  eq(calls[0].body.p_totp_step, null);
});

test("undo with a valid code has its own reply", async () => {
  const totp = await codeAt(SECRET, STEP_NOW);
  eq(await (await handle(post({ ...base, action: "undo", totp }), deps({ [PHONE]: "recorded" }))).text(),
    TEXT["recorded:undo"][1]);
});

test("wrong token, unknown proposal, other artifact hash: 403 and nothing else said", async () => {
  const totp = await codeAt(SECRET, STEP_NOW);
  for (const s of ["denied", "missing", "hash-mismatch", "invalid", "totp-required"]) {
    const r = await handle(post({ ...base, action: "apply", totp }), deps({ [PHONE]: s }));
    eq([r.status, await r.text()], [403, "Not allowed"], s);
  }
});

test("CORS: only the decide page origin; preflight for others is refused", async () => {
  const ok = await handle(new Request("https://x", { method: "OPTIONS", headers: { origin: ORIGIN } }), deps({}));
  eq([ok.status, ok.headers.get("access-control-allow-origin")], [204, ORIGIN]);
  const bad = await handle(new Request("https://x", { method: "OPTIONS", headers: { origin: "https://evil.test" } }), deps({}));
  eq([bad.status, bad.headers.get("access-control-allow-origin")], [403, null]);
  const r = await handle(post({ ...base, action: "reject" }, "https://evil.test"), deps({ [PHONE]: "recorded" }));
  eq(r.headers.get("access-control-allow-origin"), null);
});

test("bad input never reaches the database", async () => {
  const good = { ...base, action: "reject" };
  const bad = [
    { ...good, proposal_id: "not-a-uuid" }, { ...good, artifact_hash: "abc" }, { ...good, action: "promote" },
    { ...good, token: "short" }, [good], "x",
  ];
  for (const b of bad) {
    const calls: Call[] = [];
    const r = await handle(post(b), deps({ [PHONE]: "recorded" }, calls));
    eq([r.status, calls.length], [400, 0], JSON.stringify(b));
  }
  eq((await handle(new Request("https://x", { method: "GET" }), deps({}))).status, 405);
  eq((await handle(new Request("https://x", { method: "POST", body: "x".repeat(3000) }), deps({}))).status, 400);
});

test("upstream errors are 502, not a decision", async () => {
  eq((await handle(post({ ...base, action: "reject" }), deps({ [PHONE]: "recorded" }, [], {}, false))).status, 502);
  eq((await handle(post({ ...base, action: "reject" }), deps({ [PHONE]: "weird-state" }))).status, 502);
});
