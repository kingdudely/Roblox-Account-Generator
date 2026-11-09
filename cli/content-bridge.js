import fs from "node:fs/promises";

const scriptUrl = (relative) => new URL(relative, import.meta.url);

const SHIM = `
(() => {
  if (globalThis.__racShim) return;
  globalThis.__racShim = true;
  globalThis.__racMessageListeners = globalThis.__racMessageListeners || [];
  const root = globalThis.chrome || (globalThis.chrome = {});
  const runtime = {
    onMessage: {
      addListener(fn) {
        if (typeof fn === "function" && !globalThis.__racMessageListeners.includes(fn)) {
          globalThis.__racMessageListeners.push(fn);
        }
      },
    },
    sendMessage(message) {
      try {
        if (typeof globalThis.__racSend === "function") {
          Promise.resolve(globalThis.__racSend(message)).catch(() => {});
        }
      } catch (_) {
        // the CLI may have closed its binding
      }
      return Promise.resolve();
    },
    getURL(value) { return value; },
  };
  try {
    root.runtime = runtime;
  } catch (_) {
    try {
      Object.defineProperty(root, "runtime", { value: runtime, configurable: true });
    } catch (_) {
      // page has frozen chrome.runtime; the content scripts will report it
    }
  }
  if (!globalThis.browser) globalThis.browser = root;
})();
`;

const installed = new WeakSet();
const bridgeContent = new WeakMap();

async function readScript(relative) {
  return fs.readFile(scriptUrl(relative), "utf8");
}

export async function installContentBridge(context, { scripts = ["google", "setup"], onMessage } = {}) {
  if (installed.has(context)) return;
  installed.add(context);

  const parts = [SHIM];
  if (scripts.includes("awake")) parts.push(await readScript("../extension/content/awake.js"));
  if (scripts.includes("google")) parts.push(await readScript("../extension/content/google.js"));
  if (scripts.includes("setup")) parts.push(await readScript("../extension/content/setup.js"));
  bridgeContent.set(context, parts.join("\n;\n"));

  await context.exposeBinding("__racSend", async (source, message) => {
    if (!message || typeof message !== "object") return { ok: false };
    try {
      if (onMessage) await onMessage(message, source.page || source.frame?.page());
    } catch (error) {
      console.error(`content bridge: ${error.message || error}`);
    }
    return { ok: true };
  });
}

// Patchright intentionally does not run Page.addScriptToEvaluateOnNewDocument
// init scripts. Injecting on demand is equivalent for these content scripts:
// they are message-driven, and every dispatch happens after the navigation.
export async function injectContentScripts(page) {
  const context = page.context();
  const content = bridgeContent.get(context);
  if (!content) return false;
  const already = await page
    .evaluate(() => Boolean(globalThis.__racShim))
    .catch(() => false);
  if (already) return true;
  await page.evaluate(content).catch(() => {});
  return page
    .evaluate(() => Boolean(globalThis.__racShim) && (globalThis.__racMessageListeners || []).length > 0)
    .catch(() => false);
}

export async function dispatchMessage(page, message, { tries = 20, interval = 250 } = {}) {
  for (let attempt = 0; attempt < tries; attempt += 1) {
    await injectContentScripts(page);
    const delivered = await page
      .evaluate((payload) => {
        const listeners = globalThis.__racMessageListeners || [];
        for (const listener of [...listeners]) {
          try {
            listener(payload, {}, () => {});
          } catch (_) {
            // one bad content script must not hide the others
          }
        }
        return listeners.length;
      }, message)
      .catch(() => 0);
    if (delivered > 0) return true;
    await page.waitForTimeout(interval);
  }
  return false;
}

export async function sendGoogleFill(
  page,
  { email, password = "", otp = "", codes = [] },
  options = {}
) {
  return dispatchMessage(
    page,
    {
      type: "google-fill",
      email: email || "",
      password: password || "",
      code: "",
      codes: Array.isArray(codes) ? codes : [],
      otp: otp || "",
    },
    options
  );
}

export async function sendSetupFill(page, profile, phase) {
  return dispatchMessage(page, {
    type: "setup-fill",
    profile,
    phase,
  });
}

export async function sendSetupDomainReady(page) {
  return dispatchMessage(page, { type: "setup-domain-ready" });
}
