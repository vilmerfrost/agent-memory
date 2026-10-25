// A software WebAuthn authenticator for tests only (never deployed: index.ts does not import it).
// ES256 key from WebCrypto, "none" attestation, a sign counter, and switches to break one thing at a time
// (UV flag, origin, RP ID, challenge, signature). Generated keys only; no real credentials.

import { b64url, unb64url } from "./web.ts";

// -- minimal CBOR (major types 0, 1, 2, 3, 5) ------------------------------------------------------------
type Cbor = number | string | Uint8Array | Map<Cbor, Cbor>;

function head(major: number, n: number): number[] {
  if (n < 24) return [(major << 5) | n];
  if (n < 0x100) return [(major << 5) | 24, n];
  if (n < 0x10000) return [(major << 5) | 25, n >> 8, n & 0xff];
  return [(major << 5) | 26, (n >>> 24) & 0xff, (n >>> 16) & 0xff, (n >>> 8) & 0xff, n & 0xff];
}

export function cbor(v: Cbor): Uint8Array<ArrayBuffer> {
  const out: number[] = [];
  const enc = (x: Cbor): void => {
    if (typeof x === "number") {
      out.push(...(x >= 0 ? head(0, x) : head(1, -1 - x)));
    } else if (typeof x === "string") {
      const b = new TextEncoder().encode(x);
      out.push(...head(3, b.length), ...b);
    } else if (x instanceof Uint8Array) {
      out.push(...head(2, x.length), ...x);
    } else {
      out.push(...head(5, x.size));
      for (const [k, val] of x) {
        enc(k);
        enc(val);
      }
    }
  };
  enc(v);
  return new Uint8Array(out);
}

type Bytes = Uint8Array<ArrayBuffer>;

function concat(...parts: Uint8Array[]): Bytes {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let i = 0;
  for (const p of parts) {
    out.set(p, i);
    i += p.length;
  }
  return out;
}

async function sha256(b: Bytes): Promise<Bytes> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", b));
}

function u32(n: number): Uint8Array {
  return new Uint8Array([(n >>> 24) & 0xff, (n >>> 16) & 0xff, (n >>> 8) & 0xff, n & 0xff]);
}

/** WebCrypto gives r||s; WebAuthn wants an ASN.1 DER ECDSA signature. */
function derSig(raw: Bytes): Bytes {
  const int = (b: Bytes): Bytes => {
    let i = 0;
    while (i < b.length - 1 && b[i] === 0) i++;
    let v = b.slice(i);
    if (v[0] & 0x80) v = concat(new Uint8Array([0]), v);
    return concat(new Uint8Array([0x02, v.length]), v);
  };
  const body = concat(int(raw.slice(0, 32)), int(raw.slice(32)));
  return concat(new Uint8Array([0x30, body.length]), body);
}

const FLAG_UP = 0x01, FLAG_UV = 0x04, FLAG_AT = 0x40;

export interface Tweak {
  origin?: string;
  rpId?: string;
  uv?: boolean;
  challenge?: string;        // sign another challenge than the options say
  counter?: number;          // force a counter value
  badSignature?: boolean;
  type?: string;
}

export class SoftAuthenticator {
  credentialId = b64url(crypto.getRandomValues(new Uint8Array(32)));
  counter = 0;
  private keys!: CryptoKeyPair;

  constructor(public rpId: string, public origin: string, public countUp = true) {}

  async init(): Promise<this> {
    this.keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]) as CryptoKeyPair;
    return this;
  }

  async coseKey(): Promise<Bytes> {
    const raw = new Uint8Array(await crypto.subtle.exportKey("raw", this.keys.publicKey));   // 0x04 | x | y
    return cbor(new Map<Cbor, Cbor>([[1, 2], [3, -7], [-1, 1], [-2, raw.slice(1, 33)], [-3, raw.slice(33, 65)]]));
  }

  private clientData(type: string, challenge: string, t: Tweak): Bytes {
    return new TextEncoder().encode(JSON.stringify({ type: t.type ?? type, challenge: t.challenge ?? challenge,
                                                     origin: t.origin ?? this.origin, crossOrigin: false }));
  }

  /** navigator.credentials.create(): a RegistrationResponseJSON. */
  async create(options: { challenge: string }, t: Tweak = {}): Promise<Record<string, unknown>> {
    const flags = FLAG_UP | FLAG_AT | ((t.uv ?? true) ? FLAG_UV : 0);
    const id = unb64url(this.credentialId);
    const authData = concat(await sha256(new TextEncoder().encode(t.rpId ?? this.rpId)), new Uint8Array([flags]),
      u32(this.counter), new Uint8Array(16), new Uint8Array([id.length >> 8, id.length & 0xff]), id, await this.coseKey());
    const att = cbor(new Map<Cbor, Cbor>([["fmt", "none"], ["attStmt", new Map()], ["authData", authData]]));
    return {
      id: this.credentialId, rawId: this.credentialId, type: "public-key", authenticatorAttachment: "platform",
      clientExtensionResults: {},
      response: { clientDataJSON: b64url(this.clientData("webauthn.create", options.challenge, t)),
                  attestationObject: b64url(att), transports: ["internal", "hybrid"] },
    };
  }

  /** navigator.credentials.get(): an AuthenticationResponseJSON. */
  async get(options: { challenge: string }, t: Tweak = {}): Promise<Record<string, unknown>> {
    if (this.countUp) this.counter += 1;
    const count = t.counter ?? this.counter;
    const flags = FLAG_UP | ((t.uv ?? true) ? FLAG_UV : 0);
    const authData = concat(await sha256(new TextEncoder().encode(t.rpId ?? this.rpId)), new Uint8Array([flags]), u32(count));
    const cd = this.clientData("webauthn.get", options.challenge, t);
    const raw = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, this.keys.privateKey,
                                                       concat(authData, await sha256(cd))));
    if (t.badSignature) raw[10] ^= 0xff;
    return {
      id: this.credentialId, rawId: this.credentialId, type: "public-key", authenticatorAttachment: "platform",
      clientExtensionResults: {},
      response: { clientDataJSON: b64url(cd), authenticatorData: b64url(authData), signature: b64url(derSig(raw)),
                  userHandle: null },
    };
  }
}
