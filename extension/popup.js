// language: JavaScript, file: popup.js

const api = globalThis.browser ?? globalThis.chrome;

async function render() {
  const bag = await api.storage.local.get("rac.state");
  const state = bag["rac.state"] || {};
  const keys = state.keys || [];
  const accounts = state.accounts || [];
  const identities = state.identities || [];

  document.getElementById("accounts").textContent = String(accounts.length);
  document.getElementById("keys").textContent = String(keys.length);

  const perKey = new Map();
  for (const identity of identities) {
    perKey.set(identity.key_id, (perKey.get(identity.key_id) || 0) + 1);
  }
  const seats = keys.reduce((total, key) => total + Math.max(0, 50 - (perKey.get(key.id) || 0)), 0);
  document.getElementById("seats").textContent = String(seats);

  const run = state.run || {};
  const label = document.getElementById("state");
  if (run.state === "running") {
    label.innerHTML = "running · <b>leave the tab open</b>";
  } else {
    label.textContent = accounts.length ? "ready when you are" : "no accounts yet";
  }
}

async function openRunner(tab = null) {
  const url = api.runtime.getURL("runner.html");
  const tabs = await api.tabs.query({});
  const existing = tabs.find((entry) => (entry.url || "").startsWith(url));
  if (existing) {
    await api.tabs.update(existing.id, { active: true });
    await api.windows.update(existing.windowId, { focused: true }).catch(() => {});
    if (tab) {
      await api.tabs.sendMessage(existing.id, { type: "select-tab", tab }).catch(() => {});
    }
    return;
  }
  await api.tabs.create({ url: tab ? `${url}#${tab}` : url });
}

document.getElementById("open").addEventListener("click", () => openRunner());
document.getElementById("keys-tab").addEventListener("click", () => openRunner("keys"));

// Paint the static popup first; the storage read and seat math can land a frame
// later without making the first click feel dead.
const scheduleRender = () => {
  const run = () => render().catch(() => {});
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
  else setTimeout(run, 0);
};
scheduleRender();
