import dns from "node:dns/promises";
import crypto from "node:crypto";

const ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789";
const DNS_TIMEOUT = 4500;

function timeout(ms, label) {
  return new Promise((_, reject) => {
    const timer = setTimeout(() => reject(Object.assign(new Error(`${label} timed out`), { code: "ETIMEOUT" })), ms);
    timer.unref?.();
  });
}

export function cleanSubdomain(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/\.duckdns\.org.*$/, "")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/^-+|-+$/g, "");
}

export function cleanDomain(value) {
  const text = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/\/.*$/, "");
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(text) ? text : "";
}

export function fullDomain(subdomain) {
  const name = cleanSubdomain(subdomain);
  return name ? `${name}.duckdns.org` : "";
}

export function randomSubdomain(prefix = "rac") {
  const bytes = crypto.randomBytes(10);
  let out = "";
  for (let index = 0; index < 8; index += 1) {
    out += ALPHABET[bytes[index] % ALPHABET.length];
  }
  return `${prefix}-${out}`;
}

export function titleCase(value) {
  return String(value || "")
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase())
    .trim();
}

async function resolveWith(type, domain) {
  try {
    const result = await Promise.race([dns[type](domain), timeout(DNS_TIMEOUT, type)]);
    return { type, found: Array.isArray(result) ? result.length > 0 : Boolean(result) };
  } catch (error) {
    if (["ENOTFOUND", "ENODATA", "ENONAME"].includes(error.code)) {
      return { type, found: false };
    }
    if (["ETIMEOUT", "ESERVFAIL", "EREFUSED", "ECONNREFUSED"].includes(error.code)) {
      return { type, found: false, unknown: true };
    }
    return { type, found: false, unknown: true, error: error.code || error.message };
  }
}

export async function checkSubdomainAvailability(subdomain) {
  const name = cleanSubdomain(subdomain);
  if (!name) throw new Error("invalid DuckDNS subdomain");
  const domain = fullDomain(name);
  const results = await Promise.all([
    resolveWith("resolve4", domain),
    resolveWith("resolve6", domain),
    resolveWith("resolveTxt", domain),
  ]);
  const records = results.filter((entry) => entry.found).map((entry) => entry.type);
  const unknown = results.some((entry) => entry.unknown);
  return {
    subdomain: name,
    domain,
    available: records.length === 0,
    claimed: records.length > 0,
    unknown,
    records,
  };
}

export async function pickSubdomain(
  { requested = "", token = "", force = false, tries = 6 } = {}
) {
  const wanted = cleanSubdomain(requested);
  if (wanted) {
    // A saved token means this subdomain is already ours; no need to probe it.
    if (token || force) {
      return { subdomain: wanted, domain: fullDomain(wanted), check: null, generated: false };
    }
    const check = await checkSubdomainAvailability(wanted);
    if (check.available || check.unknown) {
      return { subdomain: wanted, domain: fullDomain(wanted), check, generated: false };
    }
    const replacement = await pickGenerated(tries);
    return {
      ...replacement,
      replaced: wanted,
      reason: `duckdns record already exists (${check.records.join(", ") || "claimed"})`,
    };
  }

  return pickGenerated(tries);
}

async function pickGenerated(tries) {
  for (let attempt = 0; attempt < Math.max(1, tries); attempt += 1) {
    const subdomain = randomSubdomain();
    const check = await checkSubdomainAvailability(subdomain);
    if (check.available || check.unknown) {
      return { subdomain, domain: fullDomain(subdomain), check, generated: true };
    }
  }
  throw new Error("could not find an unused DuckDNS subdomain — pass --subdomain with one you own");
}
