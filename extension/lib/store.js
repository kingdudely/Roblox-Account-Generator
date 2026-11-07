// language: JavaScript, file: lib/store.js

import { api, STATE_KEY, LOG_KEY } from "./api.js";

export const DEFAULT_SETTINGS = {
  count: 1,
  lanes: 1,
  concurrency: 8,
  timeout: 240,
  autoExport: true,
  openOnInstall: true,
};

export function emptyState() {
  return {
    keys: [],
    identities: [],
    accounts: [],
    settings: { ...DEFAULT_SETTINGS },
    run: { state: "idle", started_at: null, reason: null },
    log: [],
    phone: { number: "", body: "", payload: "", source: "" },
    duckdns: { token: "", subdomain: "", value: "", answer: "", checked_at: null },
  };
}

function normalise(state) {
  const base = emptyState();
  return {
    ...base,
    ...state,
    settings: { ...base.settings, ...(state.settings || {}) },
    run: { ...base.run, ...(state.run || {}) },
    phone: { ...base.phone, ...(state.phone || {}) },
    duckdns: { ...base.duckdns, ...(state.duckdns || {}) },
  };
}

export async function load() {
  const bag = await api.storage.local.get([STATE_KEY, LOG_KEY]);
  const state = normalise(bag[STATE_KEY] || {});
  state.log = Array.isArray(bag[LOG_KEY]) ? bag[LOG_KEY] : [];
  return state;
}

// Single-writer queue for records. The log ring lives under its own key so a
// record write can never roll newer log lines backwards.
let chain = Promise.resolve();

export function mutate(fn) {
  const next = chain.then(async () => {
    const state = await load();
    const result = await fn(state);
    delete state.log;
    await api.storage.local.set({ [STATE_KEY]: state });
    return result;
  });
  chain = next.catch(() => {});
  return next;
}

export async function wipeRecords() {
  await mutate((state) => {
    state.identities = [];
    state.accounts = [];
    state.run = { state: "idle", started_at: null, reason: null };
    state.phone = { number: "", body: "", payload: "", source: "" };
  });
  await api.storage.local.set({ [LOG_KEY]: [] });
}
