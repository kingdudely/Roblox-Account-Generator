// language: JavaScript, file: background.js, target: Firefox MV3 event page / Chrome MV3 worker
// *only two jobs: open the runner, and be the single writer for the log ring*
const api = globalThis.browser ?? globalThis.chrome;

const STATE_KEY = "rac.state";
const LOG_KEY = "rac.log";
const LOG_CAP = 2000;

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
