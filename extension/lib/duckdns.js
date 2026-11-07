// language: JavaScript, file: lib/duckdns.js
// DuckDNS keeps TXT records behind an API, not in its web UI:
//   /update?domains=<sub>&token=<token>&txt=<value>[&verbose=true][&clear=true]
// The record covers <sub>.duckdns.org and every sub-subdomain beneath it.

import { sleep } from "./api.js";

const UPDATE = "https://www.duckdns.org/update";
const RESOLVE = "https://dns.google/resolve";

export function subdomainFrom(domain) {
  const match = /^([a-z0-9-]+)\.duckdns\.org$/i.exec(String(domain || "").trim());
  return match ? match[1].toLowerCase() : null;
}

export function fullDomain(subdomain) {
  return `${String(subdomain || "").trim().toLowerCase()}.duckdns.org`;
}

function query(params) {
  return Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== null)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
    .join("&");
}

async function callUpdate(params) {
  const response = await fetch(`${UPDATE}?${query(params)}`, { cache: "no-store" });
  const text = (await response.text()).trim();
  if (!response.ok) {
    throw new Error(`DuckDNS answered HTTP ${response.status}`);
  }
  if (!text.startsWith("OK")) {
    throw new Error(
      `DuckDNS refused the update (${text.slice(0, 120) || "empty reply"}) — check the token and subdomain`
    );
  }
  return text;
}

export async function writeTxt(token, subdomain, value) {
  return callUpdate({ domains: subdomain, token, txt: value, verbose: "true" });
}

export async function clearTxt(token, subdomain) {
  return callUpdate({ domains: subdomain, token, txt: "", clear: "true", verbose: "true" });
}

function clean(value) {
  return String(value || "")
    .replace(/^"+|"+$/g, "")
    .replace(/\\"/g, '"');
}

export async function lookupTxt(domain) {
  const response = await fetch(
    `${RESOLVE}?name=${encodeURIComponent(domain)}&type=TXT`,
    { cache: "no-store" }
  );
  if (!response.ok) throw new Error(`DNS lookup answered HTTP ${response.status}`);
  const data = await response.json();
  return (data.Answer || []).map((answer) => clean(answer.data)).filter(Boolean);
}

export async function waitForTxt(domain, expected, timeout = 90_000, onTick) {
  const deadline = Date.now() + timeout;
  let answers = [];
  let rounds = 0;

  while (Date.now() < deadline) {
    answers = await lookupTxt(domain).catch(() => []);
    rounds += 1;
    const hit = expected
      ? answers.some((value) => value === expected || value.includes(expected))
      : answers.some((value) => /google-site-verification/i.test(value));
    if (hit) return { found: true, answers, rounds };
    if (typeof onTick === "function") onTick(answers, rounds);
    await sleep(4000);
  }
  return { found: false, answers, rounds };
}
