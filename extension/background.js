// language: JavaScript, file: background.js, target: Firefox MV3 event page / Chrome MV3 worker
// *only two jobs: open the runner, and be the single writer for the log ring*
const api = globalThis.browser ?? globalThis.chrome;

const STATE_KEY = "rac.state";
const LOG_KEY = "rac.log";
const LOG_CAP = 2000;

// Which container exits through which proxy. Filled by the runner at the start
// of a batch and persisted, because the background is an event page that can be
// suspended mid-run — losing the map would silently drop every lane back to the
// home IP halfway through.
const PROXY_KEY = "rac.proxies";
let laneProxies = new Map();
let proxiesHydrated = false;

async function hydrateProxies() {
  if (proxiesHydrated) return laneProxies;
  try {
    const bag = await api.storage.local.get(PROXY_KEY);
    const map = bag && bag[PROXY_KEY] ? bag[PROXY_KEY] : {};
    laneProxies = new Map(Object.entries(map).filter(([, value]) => Boolean(value)));
  } catch (_) {
    laneProxies = new Map();
  }
  proxiesHydrated = true;
  return laneProxies;
}

hydrateProxies().catch(() => {});

if (api.proxy && api.proxy.onRequest) {
  api.proxy.onRequest.addListener((details) => {
    const storeId = details && details.cookieStoreId;
    if (!storeId) return undefined;
    if (laneProxies.has(storeId)) return laneProxies.get(storeId);
    if (proxiesHydrated) return undefined;
    // First request after a cold start: answer from storage rather than let the
    // lane out through the home address.
    return hydrateProxies().then((map) => map.get(storeId) || undefined);
  });

  if (api.proxy.onError) {
    api.proxy.onError.addListener((error) => {
      appendLog({
        level: "warn",
        text: `proxy: ${error && error.message ? error.message : error}`,
      });
    });
  }
}

async function openRunner() {
  const url = api.runtime.getURL("runner.html");
  const tabs = await api.tabs.query({});
  const existing = tabs.find((tab) => (tab.url || "").startsWith(url));
  if (existing) {
    await api.tabs.update(existing.id, { active: true });
    try {
      await api.windows.update(existing.windowId, { focused: true });
    } catch (_) {
      // window may be gone between query and update
    }
    return;
  }
  await api.tabs.create({ url });
}

api.runtime.onInstalled.addListener((details) => {
  if (details.reason !== "install") return;
  api.storage.local
    .get(STATE_KEY)
    .then((bag) => {
      const settings = bag?.[STATE_KEY]?.settings || {};
      if (settings.openOnInstall === false) return null;
      return openRunner();
    })
    .catch(() => openRunner());
});

async function appendLog(message) {
  const bag = await api.storage.local.get(LOG_KEY);
  const log = Array.isArray(bag[LOG_KEY]) ? bag[LOG_KEY] : [];
  const line = {
    t: Date.now(),
    level: message.level || "info",
    text: String(message.text ?? "").slice(0, 4000),
  };
  log.push(line);
  await api.storage.local.set({ [LOG_KEY]: log.slice(-LOG_CAP) });
  try {
    await api.runtime.sendMessage({ type: "log-line", line });
  } catch (_) {
    // no listener open; storage still has it
  }
}

async function clearLog() {
  await api.storage.local.set({ [LOG_KEY]: [] });
  try {
    await api.runtime.sendMessage({ type: "log-cleared" });
  } catch (_) {
    // no listener
  }
}

api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || typeof message !== "object") return false;

  if (message.type === "ping") {
    // The runner uses this to notice that it is an orphaned page: after the
    // extension is reloaded, the old page keeps running its old module graph
    // and every sendMessage rejects.
    sendResponse({ ok: true, version: api.runtime.getManifest().version });
    return false;
  }

  if (message.type === "proxy-config") {
    const map = message.map || {};
    laneProxies = new Map(Object.entries(map).filter(([, value]) => Boolean(value)));
    proxiesHydrated = true;
    api.storage.local
      .set({ [PROXY_KEY]: map })
      .then(
        () => sendResponse({ ok: true, lanes: laneProxies.size }),
        (err) => sendResponse({ ok: false, error: String(err) })
      );
    return true;
  }

  if (message.type === "open-runner") {
    openRunner().then(
      () => sendResponse({ ok: true }),
      (err) => sendResponse({ ok: false, error: String(err) })
    );
    return true;
  }

  if (message.type === "log") {
    appendLog(message).then(
      () => sendResponse({ ok: true }),
      (err) => sendResponse({ ok: false, error: String(err) })
    );
    return true;
  }

  if (message.type === "clear-log") {
    clearLog().then(
      () => sendResponse({ ok: true }),
      (err) => sendResponse({ ok: false, error: String(err) })
    );
    return true;
  }

  return false;
});
