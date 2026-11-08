// language: JavaScript, file: lib/store.js

import { api, STATE_KEY, LOG_KEY } from "./api.js";

export const DEFAULT_SETTINGS = {
  count: 1,
  lanes: 1,
  concurrency: 8,
  timeout: 240,
  autoExport: true,
  openOnInstall: true,
  // How a lane presents its page. "tab" is a plain background tab: nothing
  // appears on screen, and content/awake.js supplies the frame clock and the
  // visibility answer Google's sign-in UI waits for. "window" gives every lane
  // its own small window with the tab selected — the always-visible fallback
  // for a machine where the shim cannot win.
  laneSurface: "tab",
  // Deletion pacing. Google treats a burst of deletes as an abuse signal, so a
  // "delete users" run walks a small batch at a time.
  deleteBatch: 5,
  deletePaceSeconds: 45,
  // finalise defaults
  usernameMode: "generated",
  birthday: "1996-05-15",
  randomBirthday: false,
  gender: 1,
  randomGender: false,
  acceptTerms: true,
  // Google counts a freshly minted backup code as an enrolled 2SV method, so a
  // pre-minted account is never offered authenticator setup — it is challenged
  // for a code instead and ends up with single-use codes as its only factor.
  // Off means: enrol first, mint the codes afterwards as the fallback.
  mintBeforeSignIn: false,
  // Re-run SSO once when finalise comes back "User is moderated". The Google
  // session is still in the container, so it costs one silent round-trip.
  retryModerated: true,
  // Egress. Empty means direct. One SOCKS5/HTTP proxy per line; lanes cycle.
  proxyUrl: "",
  proxyList: "",
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
    totp: { email: "", secret: "", algorithm: "SHA1", digits: 6, period: 30, issuer: "", label: "" },
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
    totp: { ...base.totp, ...(state.totp || {}) },
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
