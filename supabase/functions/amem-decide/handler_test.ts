// Unit tests for handler.ts with a fake PostgREST (no network). The real SQL function is tested in
// tests/test_rsi_approval.py. Run: `node --test supabase/functions/amem-decide/handler_test.ts` (or deno test).

import { type Deps, handle, TEXT } from "./handler.ts";

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

function deps(answer: unknown, calls: { url: string; body: string }[] = [], ok = true): Deps {
  return {
    supabaseUrl: "https://example.supabase.co/",
    serviceKey: "service-key-for-tests",
    fetch: (url: string, init?: RequestInit) => {
      calls.push({ url, body: String(init?.body ?? "") });
      return Promise.resolve(new Response(JSON.stringify(answer), { status: ok ? 200 : 500 }));
    },
  };
}

function post(body: unknown): Request {
  return new Request("https://x/functions/v1/amem-decide", { method: "POST", body: JSON.stringify(body) });
}

const good = { proposal_id: PID, artifact_hash: HASH, action: "apply", token: TOKEN };

test("records an apply through the SQL function only (one rpc call, no table writes)", async () => {
  const calls: { url: string; body: string }[] = [];
  const r = await handle(post(good), deps("recorded", calls));
  eq(r.status, 200);
  eq(await r.text(), TEXT["recorded:apply"][1]);
  eq(calls.length, 1);
  eq(calls[0].url, "https://example.supabase.co/rest/v1/rpc/amem_rsi_record_decision");
  eq(JSON.parse(calls[0].body), { p_proposal: PID, p_action: "apply", p_artifact_hash: HASH, p_token: TOKEN });
});

test("reject and undo have their own replies", async () => {
  eq(await (await handle(post({ ...good, action: "reject" }), deps("recorded"))).text(), TEXT["recorded:reject"][1]);
  eq(await (await handle(post({ ...good, action: "undo" }), deps("recorded"))).text(), TEXT["recorded:undo"][1]);
});

test("a repeated tap is idempotent", async () => {
  const r = await handle(post(good), deps("already"));
  eq([r.status, await r.text()], [200, TEXT.already[1]]);
});

test("wrong token, unknown proposal or another artifact hash: 403 and nothing else said", async () => {
  for (const s of ["denied", "missing", "hash-mismatch", "invalid"]) {
    const r = await handle(post(good), deps(s));
    eq([r.status, await r.text()], [403, "Not allowed"], s);
  }
});

test("expired and closed proposals say so", async () => {
  eq((await handle(post(good), deps("expired"))).status, 200);
  eq(await (await handle(post(good), deps("not-staged"))).text(), TEXT["not-staged"][1]);
});

test("bad input never reaches the database", async () => {
  const bad = [
    { ...good, proposal_id: "not-a-uuid" }, { ...good, artifact_hash: "abc" }, { ...good, action: "promote" },
    { ...good, token: "short" }, [good], "x",
  ];
  for (const b of bad) {
    const calls: { url: string; body: string }[] = [];
    const r = await handle(post(b), deps("recorded", calls));
    eq([r.status, calls.length], [400, 0], JSON.stringify(b));
  }
  const get = await handle(new Request("https://x", { method: "GET" }), deps("recorded"));
  eq(get.status, 405);
  const big = await handle(new Request("https://x", { method: "POST", body: "x".repeat(3000) }), deps("recorded"));
  eq(big.status, 400);
});

test("upstream errors are 502, not a decision", async () => {
  eq((await handle(post(good), deps("recorded", [], false))).status, 502);
  eq((await handle(post(good), deps("weird-state"))).status, 502);
});
