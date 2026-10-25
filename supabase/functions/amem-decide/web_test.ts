// Deno tests for the /web/* routes (web.ts) with a software authenticator (softauth.ts) and the real
// @simplewebauthn/server. The database is a small fake that answers the five SQL functions; the real SQL is
// tested in tests/test_web_decide.py, and both together in web_e2e_test.ts.
// Run: parent-run -- python3 scripts/edge_test.py amem-decide

import { type Deps, handle } from "./handler.ts";
import { b64url, normalizeEnrollCode, RP_ID, serveWeb, sha256hex, unb64url } from "./web.ts";
import { SoftAuthenticator, type Tweak } from "./softauth.ts";

function eq(actual: unknown, expected: unknown, msg = ""): void {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg} expected ${e}, got ${a}`);
}

const ORIGIN = `https://${RP_ID}`;
const OWNER = "6f1c2b9e-1d2a-4c3b-8e4f-5a6b7c8d9e0f";
const OTHER = "11111111-2222-4333-8444-555555555555";
const PID = "0b8f8f3e-4c1a-4f7e-9d2a-1f2e3d4c5b6a";
const EID = "7a7a7a7a-1b1b-4c4c-8d8d-9e9e9e9e9e9e";
const HASH = "c".repeat(64);
const NOW = 1_800_000_000_000;
const BASE = "https://example.supabase.co";
const URL_ = `${BASE}/functions/v1/amem-decide/web`;

function b64json(o: unknown): string {
  return btoa(JSON.stringify(o)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function jwt(claims: Record<string, unknown>): string {
  return `${b64json({ alg: "ES256", typ: "JWT" })}.${b64json(claims)}.c2lnbmF0dXJl`;
}
const GOOD = jwt({ sub: OWNER, role: "authenticated", aud: "authenticated", exp: NOW / 1000 + 600 });

type Call = { fn: string; args: Record<string, unknown> };

/** A fake Supabase: Auth knows the tokens in `valid`; the RPCs keep challenges and passkeys in memory. */
class Fake {
  calls: Call[] = [];
  authCalls = 0;
  valid = new Map<string, string>([[GOOD, OWNER]]);
  authDown = false;
  challenges = new Map<string, Record<string, unknown>>();
  creds = new Map<string, { id: string; public_key: string; sign_count: number }>();
  decideResult = "recorded";
  challengeResult: Record<string, unknown> | null = null;
  enrollStart: Record<string, unknown> = { result: "ok" };
  private n = 0;

  deps(extra: Partial<Deps> = {}): Deps {
    return {
      supabaseUrl: BASE, serviceKey: "service-key-for-tests", anonKey: "anon-key-for-tests", totpSecret: "",
      origin: ORIGIN, ownerUserId: OWNER, rpId: RP_ID, now: () => NOW, web: serveWeb,
      fetch: (url: string, init?: RequestInit) => this.fetch(url, init), ...extra,
    };
  }

  private json(v: unknown, status = 200): Promise<Response> {
    return Promise.resolve(new Response(JSON.stringify(v), { status }));
  }

  fetch(url: string, init?: RequestInit): Promise<Response> {
    if (url.endsWith("/auth/v1/user")) {
      this.authCalls++;
      if (this.authDown) return this.json({ msg: "down" }, 503);
      const tok = (new Headers(init?.headers).get("authorization") ?? "").replace(/^Bearer /, "");
      const id = this.valid.get(tok);
      return id ? this.json({ id }) : this.json({ msg: "invalid JWT" }, 401);
    }
    const fn = url.split("/").pop() ?? "";
    const args = JSON.parse(String(init?.body ?? "{}"));
    this.calls.push({ fn, args });
    if (fn === "amem_rsi_web_challenge") {
      if (this.challengeResult) return this.json(this.challengeResult);
      const id = `00000000-0000-4000-8000-${String(++this.n).padStart(12, "0")}`;
      const needs = args.p_action !== "reject";
      this.challenges.set(id, { kind: args.p_kind, action: args.p_action, nonce_hash: args.p_nonce_hash, used: false,
                                expired: false });
      return this.json({ result: "ok", challenge_id: id, expires_at: "2027-01-15T08:02:00Z", needs_passkey: needs,
        credentials: needs ? [...this.creds.values()].map((c) => ({ id: c.id, transports: ["internal"] })) : [],
        summary: { kind: args.p_kind, id: args.p_id, action: args.p_action, target: "~/.claude/CLAUDE.md",
                   artifact_hash: args.p_kind === "proposal" ? HASH : null, state: "staged" } });
    }
    if (fn === "amem_rsi_web_enroll_start") {
      if (this.enrollStart.result !== "ok") return this.json(this.enrollStart);
      const id = `00000000-0000-4000-9000-${String(++this.n).padStart(12, "0")}`;
      this.challenges.set(id, { kind: "enroll", action: "enroll", nonce_hash: args.p_nonce_hash, used: false, expired: false });
      return this.json({ result: "ok", challenge_id: id, expires_at: "2027-01-15T08:05:00Z", exclude: [] });
    }
    if (fn === "amem_rsi_web_context") {
      const c = this.challenges.get(args.p_challenge);
      if (!c) return this.json({ found: false });
      return this.json({ found: true, ...c, credential: this.creds.get(args.p_credential_id) ?? null });
    }
    if (fn === "amem_rsi_web_decide") return this.json(this.decideResult);
    if (fn === "amem_rsi_web_enroll_finish") return this.json("enrolled");
    return this.json(null, 404);
  }

  rpcs(fn?: string): Call[] {
    return this.calls.filter((c) => !fn || c.fn === fn);
  }
}

function post(route: string, body: unknown, opts: { origin?: string | null; token?: string | null } = {}): Request {
  const headers: Record<string, string> = { "content-type": "application/json" };
  const origin = opts.origin === undefined ? ORIGIN : opts.origin;
  const token = opts.token === undefined ? GOOD : opts.token;
  if (origin) headers.origin = origin;
  if (token) headers.authorization = `Bearer ${token}`;
  return new Request(`${URL_}/${route}`, { method: "POST", body: JSON.stringify(body), headers });
}

async function call(fake: Fake, route: string, body: unknown, opts = {}, extra: Partial<Deps> = {}) {
  const r = await handle(post(route, body, opts), fake.deps(extra));
  return { status: r.status, body: await r.json(), headers: r.headers };
}

async function enrolled(fake: Fake, countUp = true): Promise<SoftAuthenticator> {
  const a = await new SoftAuthenticator(RP_ID, ORIGIN, countUp).init();
  fake.creds.set(a.credentialId, { id: a.credentialId, public_key: b64url(await a.coseKey()), sign_count: 0 });
  return a;
}

async function signedDecide(fake: Fake, a: SoftAuthenticator, action = "apply", tweak: Tweak = {}) {
  const ch = await call(fake, "challenge", { kind: action === "undo" ? "edit" : "proposal", id: action === "undo" ? EID : PID, action });
  eq(ch.body.result, "ok", "challenge");
  const assertion = await a.get(ch.body.request_options, tweak);
  return { ch, res: await call(fake, "decide", { challenge_id: ch.body.challenge_id, assertion }) };
}

// ------------------------------------------------------------------------------------------- login, origin

Deno.test("preflight: Night Desk origin gets POST/OPTIONS and authorization, content-type; others are refused", async () => {
  const fake = new Fake();
  const ok = await handle(new Request(`${URL_}/decide`, { method: "OPTIONS", headers: { origin: ORIGIN,
    "access-control-request-method": "POST", "access-control-request-headers": "authorization, content-type" } }), fake.deps());
  eq([ok.status, ok.headers.get("access-control-allow-origin"), ok.headers.get("access-control-allow-methods"),
      ok.headers.get("access-control-allow-headers")], [204, ORIGIN, "POST, OPTIONS", "authorization, content-type"]);
  const bad = await handle(new Request(`${URL_}/decide`, { method: "OPTIONS", headers: { origin: "https://evil.test" } }),
                           fake.deps());
  eq([bad.status, bad.headers.get("access-control-allow-origin"), (await bad.json()).result], [403, null, "denied"]);
});

Deno.test("wrong or missing Origin: denied before any login or database call", async () => {
  for (const origin of ["https://evil.test", null, `${ORIGIN}.evil.test`, "http://" + RP_ID]) {
    const fake = new Fake();
    const r = await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" }, { origin });
    eq([r.body.result, fake.calls.length, fake.authCalls], ["denied", 0, 0], String(origin));
    eq(r.headers.get("access-control-allow-origin"), null);
  }
});

Deno.test("missing, malformed, expired or other-user JWT: denied, no database call", async () => {
  const cases: [string, string | null][] = [
    ["missing", null], ["not a jwt", "abc.def"], ["garbage payload", "a.b.c"],
    ["other user", jwt({ sub: OTHER, role: "authenticated", exp: NOW / 1000 + 600 })],
    ["expired", jwt({ sub: OWNER, role: "authenticated", exp: NOW / 1000 - 1 })],
    ["anon role", jwt({ sub: OWNER, role: "anon", exp: NOW / 1000 + 600 })],
    ["no exp", jwt({ sub: OWNER, role: "authenticated" })],
  ];
  for (const [name, token] of cases) {
    const fake = new Fake();
    const r = await call(fake, "decide", { challenge_id: PID, assertion: null }, { token });
    eq([r.status, r.body.result, fake.calls.length], [403, "denied", 0], name);
  }
});

Deno.test("a forged owner JWT (claims right, Supabase Auth says no) is denied", async () => {
  const fake = new Fake();
  const forged = jwt({ sub: OWNER, role: "authenticated", exp: NOW / 1000 + 600, forged: true });
  const r = await call(fake, "challenge", { kind: "proposal", id: PID, action: "reject" }, { token: forged });
  eq([r.body.result, fake.authCalls, fake.calls.length], ["denied", 1, 0]);
  fake.valid.set(forged, OTHER);                                  // Auth knows the token, but as another user
  eq((await call(fake, "challenge", { kind: "proposal", id: PID, action: "reject" }, { token: forged })).body.result, "denied");
});

Deno.test("owner not configured: every web call is denied; Auth down: error, nothing recorded", async () => {
  const fake = new Fake();
  eq((await call(fake, "challenge", { kind: "proposal", id: PID, action: "reject" }, {}, { ownerUserId: "" })).body.result, "denied");
  fake.authDown = true;
  const r = await call(fake, "challenge", { kind: "proposal", id: PID, action: "reject" });
  eq([r.status, r.body.result, fake.calls.length], [502, "error", 0]);
});

Deno.test("bad bodies are invalid and never reach the database", async () => {
  const bad: [string, unknown][] = [
    ["challenge", { kind: "edit", id: EID, action: "apply" }], ["challenge", { kind: "proposal", id: "x", action: "apply" }],
    ["challenge", { kind: "proposal", id: PID, action: "promote" }], ["challenge", [1]],
    ["decide", { challenge_id: "nope" }], ["enroll/options", { enroll_code: "SHORT" }],
    ["enroll/verify", { challenge_id: PID, attestation: "x" }], ["nowhere", {}],
  ];
  for (const [route, body] of bad) {
    const fake = new Fake();
    const r = await call(fake, route, body);
    eq([r.body.result, fake.calls.length], ["invalid", 0], `${route} ${JSON.stringify(body)}`);
  }
  const fake = new Fake();
  const big = await handle(post("decide", { challenge_id: PID, pad: "x".repeat(20000) }), fake.deps());
  eq([(await big.json()).result, fake.calls.length], ["invalid", 0]);
});

// ------------------------------------------------------------------------------------------- challenge

Deno.test("apply challenge: WebAuthn request options bound to the stored nonce hash, UV required", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  const r = await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" });
  eq([r.body.result, r.body.needs_passkey, r.body.summary.artifact_hash, r.body.summary.state], ["ok", true, HASH, "staged"]);
  const o = r.body.request_options;
  eq([o.rpId, o.userVerification, o.allowCredentials.map((c: { id: string }) => c.id)], [RP_ID, "required", [a.credentialId]]);
  eq(await sha256hex(o.challenge), fake.rpcs("amem_rsi_web_challenge")[0].args.p_nonce_hash);
  eq(unb64url(o.challenge).length, 32);
});

Deno.test("reject challenge needs no passkey; SQL refusals come back as JSON results", async () => {
  const fake = new Fake();
  const r = await call(fake, "challenge", { kind: "proposal", id: PID, action: "reject" });
  eq([r.body.result, r.body.needs_passkey, r.body.request_options], ["ok", false, null]);
  for (const word of ["missing", "not-staged", "not-promoted", "expired", "not-enrolled", "invalid"]) {
    fake.challengeResult = { result: word };
    const x = await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" });
    eq([x.body.result, Object.keys(x.body)], [word, ["result"]], word);
  }
  fake.challengeResult = { result: "something-else" };
  eq((await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" })).body.result, "error");
});

// ------------------------------------------------------------------------------------------- decide

Deno.test("apply with a valid Face ID assertion: recorded, with the nonce hash, credential and new count", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  const { ch, res } = await signedDecide(fake, a);
  eq([res.status, res.body], [200, { result: "recorded" }]);
  const d = fake.rpcs("amem_rsi_web_decide");
  eq(d.length, 1);
  eq(d[0].args, { p_challenge: ch.body.challenge_id, p_nonce_hash: fake.rpcs("amem_rsi_web_challenge")[0].args.p_nonce_hash,
                  p_credential_id: a.credentialId, p_sign_count: 1 });
});

Deno.test("edit undo with Face ID goes through the same decide call", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  const { res } = await signedDecide(fake, a, "undo");
  eq(res.body.result, "recorded");
  eq(fake.rpcs("amem_rsi_web_challenge")[0].args.p_kind, "edit");
});

Deno.test("bad signature, missing UV flag, wrong origin, wrong RP ID, wrong type: passkey-invalid and the challenge is burned", async () => {
  const tweaks: [string, Tweak][] = [["bad signature", { badSignature: true }], ["UV flag missing", { uv: false }],
    ["other origin", { origin: "https://evil.test" }], ["other RP ID", { rpId: "evil.test" }],
    ["create type", { type: "webauthn.create" }]];
  for (const [name, t] of tweaks) {
    const fake = new Fake();
    const a = await enrolled(fake);
    const { res } = await signedDecide(fake, a, "apply", t);
    eq(res.body.result, "passkey-invalid", name);
    const d = fake.rpcs("amem_rsi_web_decide");
    eq([d.length, d[0].args.p_nonce_hash, d[0].args.p_sign_count], [1, "0".repeat(64), 0], name);   // burn only
  }
});

Deno.test("an assertion signed for another challenge (other proposal or action) is refused", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  const first = await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" });
  const second = await call(fake, "challenge", { kind: "edit", id: EID, action: "undo" });
  const forFirst = await a.get(first.body.request_options);
  const r = await call(fake, "decide", { challenge_id: second.body.challenge_id, assertion: forFirst });
  eq(r.body.result, "passkey-invalid");
  eq(fake.rpcs("amem_rsi_web_decide")[0].args.p_nonce_hash, "0".repeat(64));
});

Deno.test("apply without an assertion: passkey-required, nothing consumed; unknown credential: not-enrolled", async () => {
  const fake = new Fake();
  await enrolled(fake);
  const ch = await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" });
  const r = await call(fake, "decide", { challenge_id: ch.body.challenge_id, assertion: null });
  eq([r.status, r.body.result, fake.rpcs("amem_rsi_web_decide").length], [401, "passkey-required", 0]);
  const stranger = await new SoftAuthenticator(RP_ID, ORIGIN).init();
  const x = await call(fake, "decide", { challenge_id: ch.body.challenge_id,
                                         assertion: await stranger.get(ch.body.request_options) });
  eq([x.body.result, fake.rpcs("amem_rsi_web_decide").length], ["not-enrolled", 0]);
});

Deno.test("reject needs only the login: decide gets no assertion even if one is sent", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  const ch = await call(fake, "challenge", { kind: "proposal", id: PID, action: "reject" });
  const r = await call(fake, "decide", { challenge_id: ch.body.challenge_id,
                                         assertion: await a.get({ challenge: "AAAA" }) });
  eq(r.body.result, "recorded");
  eq(fake.rpcs("amem_rsi_web_decide")[0].args, { p_challenge: ch.body.challenge_id, p_nonce_hash: null,
                                                 p_credential_id: null, p_sign_count: null });
});

Deno.test("used or expired challenge: the SQL answers (already / expired) without verifying anything again", async () => {
  for (const [flag, answer] of [["used", "already"], ["expired", "expired"]]) {
    const fake = new Fake();
    const a = await enrolled(fake);
    const ch = await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" });
    const assertion = await a.get(ch.body.request_options);
    fake.challenges.get(ch.body.challenge_id)![flag] = true;
    fake.decideResult = answer;
    const r = await call(fake, "decide", { challenge_id: ch.body.challenge_id, assertion });
    eq(r.body.result, answer, flag);
    eq(fake.rpcs("amem_rsi_web_decide")[0].args.p_nonce_hash, null, flag);
  }
});

Deno.test("a sign count that does not move forward is refused by the library before the database", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  fake.creds.get(a.credentialId)!.sign_count = 5;
  const { res } = await signedDecide(fake, a, "apply", { counter: 5 });
  eq(res.body.result, "passkey-invalid");
});

Deno.test("unknown decide words from SQL become error; unknown challenge is invalid", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  fake.decideResult = "weird";
  eq((await signedDecide(fake, a)).res.body.result, "error");
  eq((await call(fake, "decide", { challenge_id: "99999999-9999-4999-8999-999999999999", assertion: null })).body.result, "invalid");
});

// ------------------------------------------------------------------------------------------- enrollment

Deno.test("enroll: code normalized and hashed, creation options with UV required, verify stores the key", async () => {
  const fake = new Fake();
  const code = "abcd-efgh-ijkl-mnop-qrst-uvwx";
  const o = await call(fake, "enroll/options", { enroll_code: code });
  eq(o.body.result, "ok");
  eq(fake.rpcs("amem_rsi_web_enroll_start")[0].args.p_code_hash, await sha256hex(normalizeEnrollCode(code)));
  eq(normalizeEnrollCode(code), "ABCDEFGHIJKLMNOPQRSTUVWX");
  const co = o.body.creation_options;
  eq([co.rp.id, co.authenticatorSelection.userVerification, co.attestation], [RP_ID, "required", "none"]);
  eq(await sha256hex(co.challenge), fake.rpcs("amem_rsi_web_enroll_start")[0].args.p_nonce_hash);
  const a = await new SoftAuthenticator(RP_ID, ORIGIN).init();
  const v = await call(fake, "enroll/verify", { challenge_id: o.body.challenge_id, attestation: await a.create(co) });
  eq(v.body.result, "enrolled");
  const fin = fake.rpcs("amem_rsi_web_enroll_finish")[0].args;
  eq([fin.p_credential_id, fin.p_sign_count, fin.p_transports], [a.credentialId, 0, ["internal", "hybrid"]]);
  eq(typeof fin.p_public_key === "string" && (fin.p_public_key as string).length > 40, true);
});

Deno.test("enroll: bad or used code, wrong origin, no UV, reused or expired challenge", async () => {
  for (const word of ["invalid", "expired"]) {
    const fake = new Fake();
    fake.enrollStart = { result: word };
    eq((await call(fake, "enroll/options", { enroll_code: "A".repeat(24) })).body.result, word);
  }
  for (const [name, t] of [["origin", { origin: "https://evil.test" }], ["uv", { uv: false }],
                           ["rp", { rpId: "evil.test" }]] as [string, Tweak][]) {
    const fake = new Fake();
    const o = await call(fake, "enroll/options", { enroll_code: "A".repeat(24) });
    const a = await new SoftAuthenticator(RP_ID, ORIGIN).init();
    const v = await call(fake, "enroll/verify", { challenge_id: o.body.challenge_id,
                                                  attestation: await a.create(o.body.creation_options, t) });
    eq([v.body.result, fake.rpcs("amem_rsi_web_enroll_finish").length], ["invalid", 0], name);
  }
  for (const [flag, word] of [["used", "invalid"], ["expired", "expired"]]) {
    const fake = new Fake();
    const o = await call(fake, "enroll/options", { enroll_code: "A".repeat(24) });
    fake.challenges.get(o.body.challenge_id)![flag] = true;
    const a = await new SoftAuthenticator(RP_ID, ORIGIN).init();
    const v = await call(fake, "enroll/verify", { challenge_id: o.body.challenge_id,
                                                  attestation: await a.create(o.body.creation_options) });
    eq(v.body.result, word, flag);
  }
});

Deno.test("a decide challenge cannot finish an enrollment, an enroll challenge cannot decide", async () => {
  const fake = new Fake();
  const a = await enrolled(fake);
  const ch = await call(fake, "challenge", { kind: "proposal", id: PID, action: "apply" });
  const v = await call(fake, "enroll/verify", { challenge_id: ch.body.challenge_id, attestation: await a.create(ch.body.request_options) });
  eq(v.body.result, "invalid");
  const o = await call(fake, "enroll/options", { enroll_code: "A".repeat(24) });
  const d = await call(fake, "decide", { challenge_id: o.body.challenge_id, assertion: await a.get(o.body.creation_options) });
  eq(d.body.result, "invalid");
});

Deno.test("the phone path is unchanged next to the web routes", async () => {
  const fake = new Fake();
  const r = await handle(new Request(`${BASE}/functions/v1/amem-decide`, { method: "POST", body: JSON.stringify(
    { proposal_id: PID, artifact_hash: HASH, action: "reject", token: "tok-abcdefghijklmnopqrstuvwxyz012345" }) }),
    { ...fake.deps(), fetch: () => Promise.resolve(new Response(JSON.stringify("recorded"))) });
  eq([r.status, await r.text()], [200, "Rejected — nothing will change"]);
  const none = await handle(post("challenge", { kind: "proposal", id: PID, action: "reject" }), { ...fake.deps(), web: undefined });
  eq(none.status, 501);
});
