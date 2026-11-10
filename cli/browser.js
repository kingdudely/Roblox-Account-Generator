import { existsSync } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "patchright";
import { parseProxy } from "../extension/lib/proxy.js";
import { DEBUG_ROOT, profileDir } from "./paths.js";

export function findChrome() {
  const names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"];
  for (const name of names) {
    for (const dir of String(process.env.PATH || "").split(path.delimiter)) {
      if (!dir) continue;
      const candidate = path.join(dir, name);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

export async function chromeExists() {
  return findChrome();
}

function proxyOptions(raw) {
  if (!raw) return null;
  const proxy = parseProxy(raw);
  if (!proxy) throw new Error(`could not parse proxy: ${raw}`);
  const scheme = proxy.type === "socks" || proxy.type === "socks4" ? "socks5" : proxy.type;
  return {
    server: `${scheme}://${proxy.host}:${proxy.port}`,
    ...(proxy.username ? { username: proxy.username } : {}),
    ...(proxy.password ? { password: proxy.password } : {}),
  };
}

export async function launchContext({
  profile = "setup",
  headless = false,
  proxy = "",
  channel = "chrome",
  slowMo = 0,
  args = [],
} = {}) {
  const dir = profileDir(profile);
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });

  const options = {
    headless: Boolean(headless),
    viewport: null,
    locale: "en-US",
    acceptDownloads: true,
  };
  if (channel && channel !== "none") options.channel = channel;
  if (slowMo) options.slowMo = slowMo;
  const proxied = proxyOptions(proxy);
  if (proxied) options.proxy = proxied;
  if (args.length) options.args = args;

  let lastError = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const context = await chromium.launchPersistentContext(dir, options);
      context.setDefaultTimeout(30_000);
      context.setDefaultNavigationTimeout(60_000);
      return context;
    } catch (error) {
      lastError = error;
      const locked = /ProcessSingleton|profile.*(?:lock|use)|already in use|Target page, context or browser has been closed/i.test(
        String(error.message || error)
      );
      if (!locked || attempt === 2) throw error;
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
  }
  throw lastError;
}

export async function firstPage(context) {
  const pages = context.pages();
  const blank = pages.find((page) => !page.isClosed() && page.url() === "about:blank");
  if (blank) return blank;
  const open = pages.find((page) => !page.isClosed());
  return open || context.newPage();
}

export async function openPage(context, url, { reuse = true } = {}) {
  const page = reuse ? await firstPage(context) : await context.newPage();
  if (url) {
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
  }
  return page;
}

export function attachLogging(page, { prefix = "page", verbose = false } = {}) {
  page.on("pageerror", (error) => {
    console.error(`${prefix}: page error: ${error.message}`);
  });
  page.on("requestfailed", (request) => {
    const error = request.failure()?.errorText || "failed";
    if (/net::ERR_ABORTED/.test(error)) return;
    if (verbose) console.error(`${prefix}: request failed ${error}: ${request.url().slice(0, 180)}`);
  });
  page.on("console", (message) => {
    if (!verbose) return;
    const text = message.text();
    if (!text) return;
    console.log(`${prefix}: [console:${message.type()}] ${text.slice(0, 500)}`);
  });
}

export async function saveDebug(page, label = "failure") {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dir = path.join(DEBUG_ROOT, `${stamp}-${String(label).replace(/[^a-zA-Z0-9._-]+/g, "-")}`);
  await fs.mkdir(dir, { recursive: true, mode: 0o700 });
  const meta = {
    saved_at: new Date().toISOString(),
    label,
    url: "",
    title: "",
    error: "",
  };
  try {
    meta.url = page.url();
    meta.title = await page.title();
  } catch (error) {
    meta.error = error.message;
  }
  try {
    await page.screenshot({ path: path.join(dir, "screenshot.png"), fullPage: true });
  } catch (error) {
    meta.error = meta.error || error.message;
  }
  try {
    await fs.writeFile(path.join(dir, "page.html"), await page.content(), "utf8");
  } catch (_) {
    // page may be closed
  }
  try {
    const text = await page.evaluate(() => (document.body ? document.body.innerText : ""));
    await fs.writeFile(path.join(dir, "page.txt"), text || "", "utf8");
  } catch (_) {
    // page may be closed
  }
  await fs.writeFile(path.join(dir, "meta.json"), `${JSON.stringify(meta, null, 2)}\n`, "utf8");
  return dir;
}

export async function waitUntil(check, { timeout = 60_000, interval = 500, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const result = await check();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
  throw new Error(
    `${label} timed out after ${Math.round(timeout / 1000)}s${lastError ? ` (${lastError.message})` : ""}`
  );
}

export function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch (_) {
    return "";
  }
}

export async function stayOpen(context, { seconds = 0, message = "browser open — press Ctrl+C to close" } = {}) {
  if (seconds > 0) {
    await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
    await context.close();
    return;
  }
  console.log(message);
  await new Promise((resolve) => {
    const close = async () => {
      try {
        await context.close();
      } catch (_) {
        // already closed
      }
      resolve();
    };
    process.once("SIGINT", close);
    process.once("SIGTERM", close);
    context.once("close", () => resolve());
  });
}
