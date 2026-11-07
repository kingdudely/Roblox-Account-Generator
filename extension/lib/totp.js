// language: JavaScript, file: lib/totp.js
// RFC 6238 TOTP from an authenticator secret or otpauth:// URI. Google
// Authenticator's defaults are SHA-1, 6 digits, 30 seconds.

const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function normaliseSecret(value) {
  return String(value || "")
    .toUpperCase()
    .replace(/[\s-]/g, "")
    .replace(/=+$/, "");
}

function base32Decode(secret) {
  const clean = normaliseSecret(secret);
  if (!clean) throw new Error("the authenticator secret is empty");

  let buffer = 0;
  let bits = 0;
  const bytes = [];

  for (const char of clean) {
    const index = BASE32.indexOf(char);
    if (index < 0) throw new Error(`invalid base32 character: ${char}`);
    buffer = (buffer << 5) | index;
    bits += 5;
    if (bits >= 8) {
      bits -= 8;
      bytes.push((buffer >>> bits) & 0xff);
      buffer &= (1 << bits) - 1;
    }
  }

  if (!bytes.length) throw new Error("the authenticator secret is too short");
  return Uint8Array.from(bytes);
}

function hashName(algorithm) {
  const name = String(algorithm || "SHA1").toUpperCase().replace("-", "");
  if (name === "SHA256") return "SHA-256";
  if (name === "SHA512") return "SHA-512";
  return "SHA-1";
}

export function parseTotp(input) {
  const raw = String(input || "").trim();
  if (!raw) throw new Error("paste the authenticator secret or otpauth:// URI");

  if (/^otpauth:\/\//i.test(raw)) {
    if (!/^otpauth:\/\/totp\//i.test(raw)) {
      throw new Error("only otpauth://totp URIs are supported");
    }
    const url = new URL(raw);
    const secret = url.searchParams.get("secret");
    if (!secret) throw new Error("the otpauth URI has no secret parameter");

    const label = decodeURIComponent(url.pathname.replace(/^\/+/, ""));
    const email = label.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
    return {
      secret: normaliseSecret(secret),
      algorithm: (url.searchParams.get("algorithm") || "SHA1").toUpperCase(),
      digits: Math.min(8, Math.max(6, Number(url.searchParams.get("digits")) || 6)),
      period: Math.max(10, Number(url.searchParams.get("period")) || 30),
      issuer: url.searchParams.get("issuer") || "",
      label,
      email: email ? email[0].toLowerCase() : "",
    };
  }

  return {
    secret: normaliseSecret(raw),
    algorithm: "SHA1",
    digits: 6,
    period: 30,
    issuer: "",
    label: "",
    email: "",
  };
}

export async function totpCode(secret, options = {}, at = Date.now()) {
  const period = Math.max(10, Number(options.period) || 30);
  const digits = Math.min(8, Math.max(6, Number(options.digits) || 6));
  const counter = Math.floor(at / 1000 / period);

  const message = new Uint8Array(8);
  let remaining = counter;
  for (let index = 7; index >= 0; index -= 1) {
    message[index] = remaining & 0xff;
    remaining = Math.floor(remaining / 256);
  }

  const key = await crypto.subtle.importKey(
    "raw",
    base32Decode(secret),
    { name: "HMAC", hash: hashName(options.algorithm) },
    false,
    ["sign"]
  );
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, message));

  const offset = mac[mac.length - 1] & 0x0f;
  const binary =
    ((mac[offset] & 0x7f) << 24) |
    ((mac[offset + 1] & 0xff) << 16) |
    ((mac[offset + 2] & 0xff) << 8) |
    (mac[offset + 3] & 0xff);

  return String(binary % 10 ** digits).padStart(digits, "0");
}

export function secondsRemaining(period = 30, at = Date.now()) {
  const width = Math.max(10, Number(period) || 30);
  return width - (Math.floor(at / 1000) % width);
}
