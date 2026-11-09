import fs from "node:fs/promises";
import { APP_HOME, STATE_PATH } from "./paths.js";
import { emptyState } from "../extension/lib/store.js";

const LOG_CAP = 500;

function normalise(raw) {
  const base = emptyState();
  const state = raw && typeof raw === "object" ? raw : {};
  return {
    ...base,
    ...state,
    keys: Array.isArray(state.keys) ? state.keys : [],
    identities: Array.isArray(state.identities) ? state.identities : [],
    accounts: Array.isArray(state.accounts) ? state.accounts : [],
    settings: { ...base.settings, ...(state.settings || {}) },
    run: { ...base.run, ...(state.run || {}) },
    phone: { ...base.phone, ...(state.phone || {}) },
    duckdns: { ...base.duckdns, ...(state.duckdns || {}) },
    totp: { ...base.totp, ...(state.totp || {}) },
    setup: {
      ...base.setup,
      ...(state.setup || {}),
      done: { ...(state.setup?.done || {}) },
      open: { ...(state.setup?.open || {}) },
      auto: { ...base.setup.auto, ...(state.setup?.auto || {}) },
    },
    log: Array.isArray(state.log) ? state.log.slice(-LOG_CAP) : [],
  };
}

export async function ensureHome() {
  await fs.mkdir(APP_HOME, { recursive: true, mode: 0o700 });
}

export async function load() {
  await ensureHome();
  try {
    const text = await fs.readFile(STATE_PATH, "utf8");
    if (!text.trim()) return normalise({});
    return normalise(JSON.parse(text));
  } catch (error) {
    if (error.code === "ENOENT") return normalise({});
    throw new Error(`could not read ${STATE_PATH}: ${error.message}`);
  }
}

export async function save(state) {
  await ensureHome();
  const clean = normalise(state);
  clean.log = clean.log.slice(-LOG_CAP);
  const tmp = `${STATE_PATH}.${process.pid}.tmp`;
  await fs.writeFile(tmp, `${JSON.stringify(clean, null, 2)}\n`, { mode: 0o600 });
  await fs.rename(tmp, STATE_PATH);
  return clean;
}

let chain = Promise.resolve();

// One writer per process. The CLI is the only writer in normal use, but a
// second command in another terminal still gets a consistent file.
export function mutate(fn) {
  const next = chain.then(async () => {
    const state = await load();
    const result = await fn(state);
    const saved = await save(state);
    return result === undefined ? saved : result;
  });
  chain = next.catch(() => {});
  return next;
}

export async function setDone(id, done = true) {
  return mutate((state) => {
    state.setup.done = { ...(state.setup.done || {}), [id]: Boolean(done) };
    if (done) {
      state.setup.open = { ...(state.setup.open || {}), [id.split(".")[0]]: false };
    }
  });
}

export async function appendLog(level, text) {
  return mutate((state) => {
    state.log.push({
      at: new Date().toISOString(),
      level: String(level || "info"),
      text: String(text || ""),
    });
  });
}

export async function findKey(idOrPrefix) {
  const state = await load();
  if (!state.keys.length) return null;
  const needle = String(idOrPrefix || "").trim();
  if (!needle) return null;
  return (
    state.keys.find((key) => key.id === needle) ||
    state.keys.find((key) => key.id.startsWith(needle)) ||
    state.keys.find((key) => String(key.label || "").toLowerCase() === needle.toLowerCase()) ||
    null
  );
}

export async function requireKey(idOrPrefix) {
  const key = await findKey(idOrPrefix);
  if (!key) {
    throw new Error(
      idOrPrefix
        ? `no key matches ${idOrPrefix}`
        : "no key configured — run `rac key import <file>` first"
    );
  }
  return key;
}

export function keyUsable(key) {
  return Boolean(key && !key.needs_consent && (key.refresh_token || (key.kind === "service_account" && key.private_key)));
}
