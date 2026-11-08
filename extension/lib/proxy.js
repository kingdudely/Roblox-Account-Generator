// language: JavaScript, file: lib/proxy.js
// Per-lane egress. Firefox's proxy API is the only one that can hand each
// container its own exit, so this is Firefox-only by construction: Chrome has
// one proxy for the whole profile.
//
// ProxyInfo carries username/password for SOCKS, so an authenticated SOCKS5
// needs no local relay and no prompt.

import { api, isFirefox } from "./api.js";

export function parseProxy(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;

  // Accepted: socks5://user:pass@host:1080 , socks://host:1080 ,
  // http://user:pass@host:8080 , host:1080
  let scheme = "socks";
  let rest = raw;
  const match = raw.match(/^([a-z0-9]+):\/\/(.*)$/i);
  if (match) {
    scheme = match[1].toLowerCase();
    rest = match[2];
  }
  if (scheme === "socks5" || scheme === "socks5h") scheme = "socks";
  if (!["socks", "socks4", "http", "https"].includes(scheme)) return null;

  let username;
  let password;
  const at = rest.lastIndexOf("@");
  if (at > 0) {
    const creds = rest.slice(0, at);
    rest = rest.slice(at + 1);
    const colon = creds.indexOf(":");
    username = decodeURIComponent(colon >= 0 ? creds.slice(0, colon) : creds);
    password = colon >= 0 ? decodeURIComponent(creds.slice(colon + 1)) : undefined;
  }

  const tail = rest.replace(/\/+$/, "");
  const split = tail.lastIndexOf(":");
  if (split <= 0) return null;
  const host = tail.slice(0, split);
  const port = Number(tail.slice(split + 1));
  if (!host || !Number.isInteger(port) || port <= 0) return null;

  return {
    type: scheme,
    host,
    port,
    ...(username ? { username } : {}),
    ...(password ? { password } : {}),
    // Resolve names at the proxy, otherwise DNS leaks the real one and the
    // exit country disagrees with the resolver country. That mismatch is a
    // fingerprint all by itself.
    ...(scheme === "socks" || scheme === "socks4" ? { proxyDNS: true } : {}),
  };
}

export function parseProxyList(text) {
  return String(text || "")
    .split(/[\n,]/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map(parseProxy)
    .filter(Boolean);
}

// One proxy per lane, cycling. A lane that outlives the list simply wraps.
export function lanesFromSettings(settings) {
  const single = parseProxy(settings.proxyUrl);
  const listed = parseProxyList(settings.proxyList);
  const pool = listed.length ? listed : single ? [single] : [];
  return pool;
}

export function proxyForLane(pool, lane) {
  if (!pool.length) return null;
  return pool[lane % pool.length];
}

export function canProxy() {
  return Boolean(isFirefox && api.proxy && api.proxy.onRequest);
}

// The router lives in the background page, so the lane -> exit mapping has to
// be sent there rather than read from here. Container ids are stable for the
// life of the browser, so one message at the start of a run is enough.
export async function publishLaneProxies(map) {
  if (!api.runtime || !api.runtime.sendMessage) return false;
  try {
    await api.runtime.sendMessage({ type: "proxy-config", map });
    return true;
  } catch (_) {
    return false;
  }
}

export function describeProxy(proxy) {
  if (!proxy) return "direct";
  const auth = proxy.username ? `${proxy.username}@` : "";
  return `${proxy.type}://${auth}${proxy.host}:${proxy.port}`;
}
