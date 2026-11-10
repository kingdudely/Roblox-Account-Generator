import dns from "node:dns/promises";
import crypto from "node:crypto";

const LEFT_WORDS = [
  "bright", "clear", "cedar", "maple", "north", "summit", "river", "silver",
  "golden", "prime", "happy", "green", "blue", "stone", "oak", "pine",
  "swift", "solid", "fresh", "urban", "sunny", "quiet", "rapid", "happy",
];
const RIGHT_WORDS = [
  "harbor", "works", "labs", "field", "house", "bridge", "point", "park",
  "grove", "yard", "line", "path", "wave", "cloud", "forge", "mill",
  "ridge", "creek", "bay", "lane", "shop", "studio", "market", "garden",
];
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

export function randomSubdomain() {
  const left = LEFT_WORDS[crypto.randomInt(LEFT_WORDS.length)];
  const right = RIGHT_WORDS[crypto.randomInt(RIGHT_WORDS.length)];
  const number = crypto.randomInt(100, 1000);
  return `${left}-${right}-${number}`;
}

export function titleCase(value) {
  return String(value || "")
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase())
    .trim();
}

async function resolveWith(type, domain, label = type) {
  try {
    const result = await Promise.race([dns[type](domain), timeout(DNS_TIMEOUT, type)]);
    return {
      source: "local",
      type: label,
      found: Array.isArray(result) ? result.length > 0 : Boolean(result),
    };
  } catch (error) {
    if (["ENOTFOUND", "ENODATA", "ENONAME"].includes(error.code)) {
      return { source: "local", type: label, found: false, nxdomain: true };
    }
    if (["ETIMEOUT", "ESERVFAIL", "EREFUSED", "ECONNREFUSED"].includes(error.code)) {
      return { source: "local", type: label, found: false, unknown: true };
    }
    return {
      source: "local",
      type: label,
      found: false,
      unknown: true,
      error: error.code || error.message,
    };
  }
}

// dns.google is DNSSEC-aware and avoids whatever negative cache the local
// resolver is holding. No key or login needed.
async function resolveDoh(type, domain) {
  const url = `https://dns.google/resolve?name=${encodeURIComponent(domain)}&type=${type}`;
  try {
    const response = await fetch(url, {
      headers: { accept: "application/dns-json" },
      signal: AbortSignal.timeout(DNS_TIMEOUT),
    });
    if (!response.ok) {
      return { source: "dns.google", type, found: false, unknown: true, status: response.status };
    }
    const data = await response.json();
    if (data.Status === 3) {
      return { source: "dns.google", type, found: false, nxdomain: true };
    }
    const answers = Array.isArray(data.Answer) ? data.Answer : [];
    return {
      source: "dns.google",
      type,
      found: answers.length > 0,
      answers: answers.length,
    };
  } catch (error) {
    return {
      source: "dns.google",
      type,
      found: false,
      unknown: true,
      error: error.message || String(error),
    };
  }
}

export async function checkSubdomainAvailability(subdomain) {
  const name = cleanSubdomain(subdomain);
  if (!name) throw new Error("invalid DuckDNS subdomain");
  const domain = fullDomain(name);

  const [local, doh] = await Promise.all([
    Promise.all([
      resolveWith("resolve4", domain, "A"),
      resolveWith("resolve6", domain, "AAAA"),
      resolveWith("resolveTxt", domain, "TXT"),
    ]),
    Promise.all(["A", "AAAA", "TXT", "CNAME"].map((type) => resolveDoh(type, domain))),
  ]);

  const results = [...local, ...doh];
  const found = results.filter((entry) => entry.found);
  const unknown = found.length === 0 && results.some((entry) => entry.unknown);
  const records = [...new Set(found.map((entry) => entry.type))];

  return {
    subdomain: name,
    domain,
    available: found.length === 0,
    claimed: found.length > 0,
    unknown,
    records,
    checked: results.map((entry) => ({
      source: entry.source,
      type: entry.type,
      found: Boolean(entry.found),
      ...(entry.nxdomain ? { nxdomain: true } : {}),
      ...(entry.unknown ? { unknown: true } : {}),
    })),
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
