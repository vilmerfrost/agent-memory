// RFC 6238 TOTP (HMAC-SHA1, 30-second steps, 6 digits) with WebCrypto, so it runs in Deno and Node alike.
// The same algorithm and test vector as src/agent_memory/rsi_totp.py.

export const STEP = 30;
export const DIGITS = 6;
// RFC 6238 appendix B: ASCII "12345678901234567890" (base32 below), T = 59 s -> 287082 (6 digits).
export const RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ";
export const RFC_VECTOR: [number, string] = [59, "287082"];

const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function base32Decode(s: string): Uint8Array<ArrayBuffer> {
  const clean = s.replace(/=+$/g, "").replace(/\s+/g, "").toUpperCase();
  let bits = 0, value = 0;
  const out: number[] = [];
  for (const ch of clean) {
    const v = B32.indexOf(ch);
    if (v < 0) throw new Error("bad base32");
    value = (value << 5) | v;
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }
  return new Uint8Array(out);
}

export async function codeAt(secretB32: string, step: number): Promise<string> {
  const key = await crypto.subtle.importKey("raw", base32Decode(secretB32), { name: "HMAC", hash: "SHA-1" }, false,
    ["sign"]);
  const msg = new Uint8Array(8);
  let n = step;
  for (let i = 7; i >= 0; i--) {
    msg[i] = n & 0xff;
    n = Math.floor(n / 256);
  }
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, msg));
  const off = mac[mac.length - 1] & 0x0f;
  const bin = ((mac[off] & 0x7f) << 24) | (mac[off + 1] << 16) | (mac[off + 2] << 8) | mac[off + 3];
  return String(bin % 10 ** DIGITS).padStart(DIGITS, "0");
}

function same(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

/** The matched step (now ± window), or null. */
export async function verify(secretB32: string, code: unknown, nowMs: number, window = 1): Promise<number | null> {
  if (typeof code !== "string" || !/^[0-9]{6}$/.test(code)) return null;
  const now = Math.floor(nowMs / 1000 / STEP);
  let hit: number | null = null;
  for (let s = now - window; s <= now + window; s++) {
    if (same(await codeAt(secretB32, s), code) && hit === null) hit = s;   // check every step: constant work
  }
  return hit;
}
