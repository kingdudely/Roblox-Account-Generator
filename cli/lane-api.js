import fs from "node:fs/promises";
import { api } from "../extension/lib/api.js";
import { parseProxy, parseProxyList } from "../extension/lib/proxy.js";
import { attachLogging, launchContext } from "./browser.js";
import { dispatchMessage, injectContentScripts, installContentBridge } from "./content-bridge.js";
import { profileDir } from "./paths.js";

function proxyString(proxy) {
  if (!proxy) return "";
  const scheme = proxy.type === "socks" || proxy.type === "socks4" ? "socks5" : proxy.type;
  const auth = proxy.username
    ? `${encodeURIComponent(proxy.username)}:${encodeURIComponent(proxy.password || "")}@`
    : "";
  return `${scheme}://${auth}${proxy.host}:${proxy.port}`;
}

function buildProxyPool(settings = {}) {
  const listed = parseProxyList(settings.proxyList || "");
  const single = parseProxy(settings.proxyUrl || "");
  return listed.length ? listed : single ? [single] : [];
}

export async function createLaneApi({
  headless = false,
  channel = "chrome",
  verbose = false,
  settings = {},
  onMessage = null,
  onLog = null,
} = {}) {
  const contexts = new Map();
  const names = new Map();
  const tabs = new Map();
  const pageTabs = new Map();
  const hooked = new WeakSet();
  const proxyPool = buildProxyPool(settings);
  let nextTabId = 1;
  let nextLane = 1;
  let closed = false;

  const log = (text) => {
    if (typeof onLog === "function") onLog(String(text));
  };

  function nextIndexForName(name) {
    const match = /^RAC lane (\d+)$/i.exec(String(name || ""));
    if (match) return Math.max(1, Number(match[1]));
    return nextLane++;
  }

  async function profileExists(index) {
    try {
      await fs.access(profileDir(`lane-${index}`));
      return true;
    } catch (_) {
      return false;
    }
  }

  async function contextFor(storeId) {
    if (!storeId) return null;
    const existing = contexts.get(storeId);
    if (existing) return existing;

    const index = Number(String(storeId).replace(/^rac-lane-/, "")) || nextLane++;
    const proxy = proxyPool.length ? proxyString(proxyPool[(index - 1) % proxyPool.length]) : "";
    const context = await launchContext({
      profile: `lane-${index}`,
      headless,
      channel,
      proxy,
    });

    const lane = { storeId, index, context, name: names.get(storeId)?.name || `RAC lane ${index}` };
    if (onMessage) {
      await installContentBridge(context, {
        scripts: ["google", "awake"],
        onMessage: (message, page) => onMessage(message, page, lane),
      });
      context.on("page", (page) => hookPage(page, lane));
      for (const page of context.pages()) hookPage(page, lane);
    }
    contexts.set(storeId, lane);
    log(`lane ${index}: browser context ready${proxy ? " with a proxy" : ""}`);
    return lane;
  }

  function hookPage(page, lane) {
    if (hooked.has(page)) return;
    hooked.add(page);
    attachLogging(page, { prefix: `lane ${lane.index}`, verbose });
    page.on("domcontentloaded", () => {
      const tab = pageTabs.get(page);
      if (tab) tab.status = "loading";
      injectContentScripts(page).catch(() => {});
    });
    page.on("load", () => {
      const tab = pageTabs.get(page);
      if (tab) tab.status = "complete";
      injectContentScripts(page).catch(() => {});
    });
    page.on("close", () => {
      const tab = pageTabs.get(page);
      if (!tab) return;
      tabs.delete(tab.id);
      pageTabs.delete(page);
    });
  }

  function registerPage(page, lane) {
    hookPage(page, lane);
    const tab = {
      id: nextTabId++,
      page,
      lane,
      storeId: lane.storeId,
      status: "loading",
    };
    tabs.set(tab.id, tab);
    pageTabs.set(page, tab);
    return tab;
  }

  async function info(tab) {
    if (!tab || tab.page.isClosed()) throw new Error("tab closed");
    return {
      id: tab.id,
      index: tab.id,
      windowId: 0,
      active: false,
      status: tab.status,
      url: tab.page.url(),
      title: await tab.page.title().catch(() => ""),
      cookieStoreId: tab.storeId,
    };
  }

  async function pageFor(id) {
    const tab = tabs.get(id);
    if (!tab || tab.page.isClosed()) throw new Error(`no tab ${id}`);
    return tab;
  }

  async function navigate(page, url, lane) {
    if (!url) return;
    try {
      // Commit is early enough to install the awake shim before Google's
      // bundles start their animation loop, and late enough for evaluate() to
      // have a document to run in.
      await page.goto(url, { waitUntil: "commit", timeout: 60_000 });
      await injectContentScripts(page).catch(() => {});
      await page.waitForLoadState("domcontentloaded", { timeout: 30_000 }).catch(() => {});
    } catch (error) {
      log(`lane ${lane.index}: navigation failed: ${error.message}`);
    }
  }

  async function createTab(options = {}) {
    const lane = await contextFor(options.cookieStoreId || null);
    if (!lane) throw new Error("a lane needs a storeId");
    let page = lane.context.pages().find((candidate) => !candidate.isClosed() && !pageTabs.has(candidate));
    if (!page) page = await lane.context.newPage();
    const tab = registerPage(page, lane);
    if (options.url) await navigate(page, options.url, lane);
    return info(tab);
  }

  const adapter = {
    tabs: {
      async create(options = {}) {
        return createTab(options);
      },
      async get(id) {
        return info(await pageFor(id));
      },
      async update(id, options = {}) {
        const tab = await pageFor(id);
        if (options.url) {
          tab.status = "loading";
          await navigate(tab.page, options.url, tab.lane);
        }
        return info(tab);
      },
      async reload(id) {
        const tab = await pageFor(id);
        tab.status = "loading";
        await tab.page.reload({ waitUntil: "commit", timeout: 60_000 }).catch(() => {});
        await injectContentScripts(tab.page).catch(() => {});
        await tab.page.waitForLoadState("domcontentloaded", { timeout: 30_000 }).catch(() => {});
      },
      async remove(id) {
        const tab = tabs.get(id);
        if (!tab) return;
        tabs.delete(id);
        pageTabs.delete(tab.page);
        await tab.page.close().catch(() => {});
      },
      async query(filter = {}) {
        const list = [];
        for (const tab of tabs.values()) {
          if (tab.page.isClosed()) continue;
          if (filter.cookieStoreId && tab.storeId !== filter.cookieStoreId) continue;
          list.push(await info(tab).catch(() => null));
        }
        return list.filter(Boolean);
      },
      async sendMessage(id, message) {
        const tab = await pageFor(id);
        const ok = await dispatchMessage(tab.page, message, { tries: 8, interval: 250 });
        if (!ok) throw new Error("no content script in the tab");
        return { ok: true };
      },
    },
    scripting: {
      async executeScript(injection = {}) {
        const tab = await pageFor(injection.target?.tabId);
        const args = Array.isArray(injection.args) ? injection.args : [];
        // Playwright's page.evaluate takes exactly one argument; Chrome's
        // scripting.executeScript spreads args. Pass an array when there are
        // several, and let the page function unpack it.
        const arg = args.length === 0 ? undefined : args.length === 1 ? args[0] : args;
        const result = await tab.page.evaluate(injection.func, arg);
        return [{ result: result === undefined ? null : result }];
      },
    },
    cookies: {
      async getAll(filter = {}) {
        const lane = await contextFor(filter.storeId || null);
        if (!lane) return [];
        const cookies = await lane.context.cookies();
        return cookies.map((cookie) => ({
          ...cookie,
          storeId: lane.storeId,
        }));
      },
      async remove(details = {}) {
        const lane = await contextFor(details.storeId || null);
        if (!lane) return;
        const cookies = await lane.context.cookies();
        const host = (() => {
          try {
            return new URL(details.url || "").hostname;
          } catch (_) {
            return "";
          }
        })();
        const matches = cookies.filter((cookie) => {
          if (details.name && cookie.name !== details.name) return false;
          if (!host) return true;
          const domain = String(cookie.domain || "").replace(/^\./, "");
          return cookie.domain === host || host === domain || host.endsWith(`.${domain}`);
        });
        for (const cookie of matches) {
          await lane.context
            .clearCookies({ name: cookie.name, domain: cookie.domain, path: cookie.path })
            .catch(() => {});
        }
      },
    },
    contextualIdentities: {
      async query(filter = {}) {
        const name = String(filter.name || "");
        for (const [storeId, entry] of names) {
          if (entry.name === name) return [{ cookieStoreId: storeId, name, color: entry.color || "blue", icon: entry.icon || "circle" }];
        }
        const match = /^RAC lane (\d+)$/i.exec(name);
        if (match) {
          const index = Number(match[1]);
          if (await profileExists(index)) {
            const storeId = `rac-lane-${index}`;
            names.set(storeId, { name, color: "blue", icon: "circle" });
            return [{ cookieStoreId: storeId, name, color: "blue", icon: "circle" }];
          }
        }
        return [];
      },
      async create(options = {}) {
        const name = String(options.name || `RAC lane ${nextLane}`);
        const index = nextIndexForName(name);
        nextLane = Math.max(nextLane, index + 1);
        const storeId = `rac-lane-${index}`;
        names.set(storeId, { name, color: options.color || "blue", icon: options.icon || "circle" });
        return {
          cookieStoreId: storeId,
          name,
          color: options.color || "blue",
          icon: options.icon || "circle",
        };
      },
    },
    windows: {
      async create() {
        throw new Error("own-window lanes are not supported by the CLI adapter");
      },
      async remove() {},
      async update() {},
    },
    runtime: {
      async sendMessage() {
        return { ok: true };
      },
      onMessage: {
        addListener() {},
      },
    },
    webNavigation: {
      onBeforeNavigate: {
        addListener() {},
        removeListener() {},
      },
    },
    downloads: {
      async download() {
        throw new Error("downloads are not supported in the CLI lane adapter");
      },
    },
  };

  async function close() {
    if (closed) return;
    closed = true;
    for (const lane of contexts.values()) {
      await lane.context.close().catch(() => {});
    }
    contexts.clear();
    tabs.clear();
    pageTabs.clear();
  }

  return {
    api: adapter,
    contexts,
    close,
    proxyPool,
  };
}

export function installLaneApi(adapter) {
  Object.assign(api, adapter);
  return api;
}
