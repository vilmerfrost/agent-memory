// Unit tests for handler.ts with a fake PostgREST (no network).
// Run: `deno test supabase/functions/amem-undo/` or `node --test supabase/functions/amem-undo/handler_test.ts`.

import { type Deps, handle, OK_TEXT, safeEqual, sha256Hex } from "./handler.ts";

// deno-lint-ignore no-explicit-any
const g = globalThis as any;
const test: (name: string, fn: () => Promise<void>) => void = g.Deno?.test ?? (await import("node:test")).test;

function eq(actual: unknown, expected: unknown, msg = ""): void {
  const a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${msg} expected ${e}, got ${a}`);
}

const EDIT = "0b8f8f3e-4c1a-4f7e-9d2a-1f2e3d4c5b6a";
const TOKEN = "tok-abcdefghijklmnopqrstuvwxyz012345";
const KEY = "service-key-for-tests";

interface Call { url: string; method: string; body: string | null; auth: string | null; prefer: string | null }

async function fakeDb(hash: string | null) {
  const calls: Call[] = [];
  const requests = new Set<string>();
  const deps: Deps = {
    supabaseUrl: "https://project.example.test/",
    serviceKey: KEY,
    fetch: async (url: string, init?: RequestInit) => {
      const h = new Headers(init?.headers);
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: (init?.body as string) ?? null, auth: h.get("Authorization"), prefer: h.get("Prefer") });
      if (method === "GET" && url.includes("/rest/v1/edits?")) {
        const rows = hash === null || !url.includes(`id=eq.${EDIT}`) ? [] : [{ id: EDIT, undo_token_hash: hash }];
        return new Response(JSON.stringify(rows), { status: 200 });
      }
      if (method === "POST" && url.includes("/rest/v1/undo_requests")) {
        requests.add(JSON.parse(init!.body as string).edit_id);
        return new Response(null, { status: 201 });
      }
      if (method === "PATCH") return new Response(null, { status: 204 });
      return new Response("unexpected", { status: 500 });
    },
  };
  return { deps, calls, requests };
}

function post(body: unknown): Request {
  return new Request("https://fn.example.test/amem-undo", {
    method: "POST",
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

test("good token: 200, one undo request, idempotent", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  for (let i = 0; i < 2; i++) {
    const res = await handle(post({ edit_id: EDIT, token: TOKEN }), db.deps);
    eq(res.status, 200);
    eq(await res.text(), OK_TEXT);
    eq(res.headers.get("content-type"), "text/plain; charset=utf-8");
  }
  eq([...db.requests], [EDIT]);
  const ins = db.calls.filter((c) => c.method === "POST");
  eq(ins.length, 2);
  eq(ins[0].url, "https://project.example.test/rest/v1/undo_requests?on_conflict=edit_id");
  eq(ins[0].prefer, "resolution=ignore-duplicates,return=minimal");
  eq(JSON.parse(ins[0].body!), { edit_id: EDIT, source: "phone" });
  eq(ins[0].auth, `Bearer ${KEY}`);
});

test("wrong token or unknown edit: 403, nothing inserted, no detail", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  let res = await handle(post({ edit_id: EDIT, token: TOKEN + "x" }), db.deps);
  eq(res.status, 403);
  eq(await res.text(), "Not allowed");
  res = await handle(post({ edit_id: "11111111-2222-3333-4444-555555555555", token: TOKEN }), db.deps);
  eq(res.status, 403);
  const none = await fakeDb(null);
  res = await handle(post({ edit_id: EDIT, token: TOKEN }), none.deps);
  eq(res.status, 403);
  eq(db.requests.size + none.requests.size, 0);
});

test("bad input: 4xx before any database call", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  const bad = ["not json", "[]", { edit_id: "x", token: TOKEN }, { edit_id: EDIT }, { edit_id: EDIT, token: "short" },
    { edit_id: EDIT, token: "t".repeat(201) }, { edit_id: `${EDIT}&id=neq.x`, token: TOKEN }, "x".repeat(3000)];
  for (const b of bad) {
    const res = await handle(post(b), db.deps);
    eq(res.status, 400, `body ${JSON.stringify(b).slice(0, 40)}`);
  }
  const get = await handle(new Request("https://fn.example.test/amem-undo"), db.deps);
  eq(get.status, 405);
  eq(db.calls.length, 0);
});

test("not configured: 500 without leaking", async () => {
  const db = await fakeDb(await sha256Hex(TOKEN));
  const res = await handle(post({ edit_id: EDIT, token: TOKEN }), { ...db.deps, serviceKey: "" });
  eq(res.status, 500);
  eq(await res.text(), "Not configured");
});

test("sha256Hex and safeEqual", async () => {
  eq(await sha256Hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  eq(safeEqual("abc", "abc"), true);
  eq(safeEqual("abc", "abd"), false);
  eq(safeEqual("abc", "ab"), false);
});
