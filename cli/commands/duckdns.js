import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { attachLogging, hostOf, launchContext, openPage, saveDebug, waitUntil } from "../browser.js";
import { DEBUG_ROOT } from "../paths.js";
import { installContentBridge } from "../content-bridge.js";
import { driveGoogle, resolveAdminCreds } from "../google.js";
import { handleAuthMessage } from "../messages.js";
import { load, mutate } from "../state.js";
import { info, ok, warn } from "../util.js";

const DUCKDNS = "https://www.duckdns.org/";
const LOGIN_GOOGLE = "https://www.duckdns.org/login?generateRequest=google";

const HELP = `rac duckdns [options]

Opens DuckDNS in the persistent setup profile. With saved admin credentials it
can sign in through Google, add the subdomain and capture the account token.
If no credentials are configured it opens the page and waits for you to sign in.

Options:
  --subdomain <name>    subdomain to create (without .duckdns.org)
  --token <token>       save a token directly, without using the UI
  --email <address>     Google account for the DuckDNS login
  --password <value>    Google password
  --totp <secret>       base32 authenticator secret
  --profile <name>      browser profile (default: setup)
  --headless            run headless
  --channel <name>      Chrome channel (default: chrome)
  --proxy <url>         proxy
  --timeout <seconds>   login/automation timeout (default: 180)
  --keep-open           leave the browser open after success
  --screenshot <file>   save a screenshot under debug/
  --verbose             print page console output
  --help                show this help
`;

export const summary = "open DuckDNS, add a subdomain and capture the token";

function cleanSubdomain(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\.duckdns\.org$/, "")
    .replace(/[^a-z0-9-]/g, "");
}

async function pageState(page) {
  return page
    .evaluate(() => {
      const visible = (el) => {
        const rect = el.getBoundingClientRect();
        return rect.width > 1 && rect.height > 1;
      };
      const text = document.body ? document.body.innerText || "" : "";
      const tokenPattern = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
      const inputs = [...document.querySelectorAll("input")].filter(visible);
      const subdomain = inputs.find((input) => {
        const label = `${input.id} ${input.name} ${input.placeholder} ${input.getAttribute("aria-label") || ""}`;
        return /sub\s*domain|subdomain/i.test(label);
      });
      const body = text.toLowerCase();
      return {
        url: location.href,
        title: document.title,
        text: text.slice(0, 1200),
        subdomain: subdomain
          ? { id: subdomain.id, name: subdomain.name, type: subdomain.type }
          : null,
        loggedIn:
          /add domain|account information|your token|delete domain/i.test(body) ||
          Boolean(subdomain) ||
          inputs.some((input) => tokenPattern.test(input.value || "")),
      };
    })
    .catch(() => ({ url: page.url(), title: "", text: "", subdomain: null, loggedIn: false }));
}

async function captureToken(page) {
  return page
    .evaluate(() => {
      const pattern = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
      const values = [];
      for (const input of document.querySelectorAll("input,textarea")) {
        if (input.value && pattern.test(input.value)) values.push(input.value.trim());
      }
      const text = document.body ? document.body.innerText || "" : "";
      for (const match of text.matchAll(
        /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi
      )) {
        values.push(match[0]);
      }
      const tokenInput =
        [...document.querySelectorAll("input")].find((input) =>
          /token/i.test(
            `${input.id} ${input.name} ${input.placeholder} ${input.getAttribute("aria-label") || ""}`
          )
        ) || null;
      const preferred = tokenInput && pattern.test(tokenInput.value || "") ? tokenInput.value.trim() : "";
      return { token: preferred || values[0] || "", candidates: [...new Set(values)] };
    })
    .catch(() => ({ token: "", candidates: [] }));
}

async function addDomain(page, subdomain) {
  const candidate = await page
    .evaluate((name) => {
      const visible = (el) => {
        const rect = el.getBoundingClientRect();
        return rect.width > 1 && rect.height > 1;
      };
      const inputs = [...document.querySelectorAll("input")].filter(visible);
      const input = inputs.find((el) => {
        const label = `${el.id} ${el.name} ${el.placeholder} ${el.getAttribute("aria-label") || ""}`;
        return /sub\s*domain|subdomain/i.test(label);
      });
      if (!input) return null;
      input.focus();
      return {
        x: input.getBoundingClientRect().x + input.getBoundingClientRect().width / 2,
        y: input.getBoundingClientRect().y + input.getBoundingClientRect().height / 2,
        name,
      };
    }, subdomain)
    .catch(() => null);
  if (!candidate) return false;

  await page.mouse.click(candidate.x, candidate.y);
  await page.keyboard.press("Control+A").catch(() => {});
  await page.keyboard.type(subdomain, { delay: 35 });

  const button = await page
    .evaluate(() => {
      const visible = (el) => {
        const rect = el.getBoundingClientRect();
        return rect.width > 1 && rect.height > 1;
      };
      const nodes = [
        ...document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"],a'),
      ].filter(visible);
      const hit = nodes.find((el) =>
        /^add domain$/i.test(
          `${el.innerText || ""} ${el.value || ""} ${el.getAttribute("aria-label") || ""}`
            .replace(/\s+/g, " ")
            .trim()
        )
      );
      if (!hit) return null;
      const rect = hit.getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, label: hit.innerText || hit.value };
    })
    .catch(() => null);
  if (!button) return false;
  await page.mouse.click(button.x, button.y);
  info(`clicked "add domain" for ${subdomain}`);
  return true;
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      subdomain: { type: "string" },
      token: { type: "string" },
      email: { type: "string" },
      password: { type: "string" },
      totp: { type: "string" },
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      timeout: { type: "string", default: "180" },
      "keep-open": { type: "boolean", default: false },
      screenshot: { type: "string" },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  const state = await load();
  const subdomain = cleanSubdomain(
    values.subdomain || state.duckdns.subdomain || state.setup?.auto?.subdomain || ""
  );
  const directToken = String(values.token || "").trim();

  if (directToken && subdomain) {
    await mutate((draft) => {
      draft.duckdns = { ...draft.duckdns, token: directToken, subdomain, checked_at: new Date().toISOString() };
      draft.setup.auto = { ...(draft.setup.auto || {}), token: directToken, subdomain };
      draft.setup.done["dns.account"] = true;
      draft.setup.done["dns.token"] = true;
      draft.setup.done["dns.credentials"] = true;
    });
    ok(`saved ${subdomain}.duckdns.org and its token`);
    return 0;
  }

  const creds = resolveAdminCreds(state, {
    email: values.email,
    password: values.password,
    otp: values.totp,
  });

  const context = await launchContext({
    profile: values.profile,
    headless: values.headless,
    channel: values.channel,
    proxy: values.proxy || "",
  });
  let page = null;
  try {
    await installContentBridge(context, {
      scripts: ["google", "setup"],
      onMessage: (message, target) => handleAuthMessage(message, target),
    });

    page = await openPage(context, creds.email ? LOGIN_GOOGLE : DUCKDNS);
    attachLogging(page, { prefix: "duckdns", verbose: values.verbose });

    if (creds.email && hostOf(page.url()).endsWith("google.com")) {
      info(`signing in to DuckDNS as ${creds.email}…`);
      await driveGoogle(page, creds, {
        timeout: Number(values.timeout) * 1000,
        label: "DuckDNS Google login",
        until: () => hostOf(page.url()).endsWith("duckdns.org"),
      });
    }

    const timeout = Number(values.timeout) * 1000;
    await waitUntil(async () => (await pageState(page)).loggedIn, {
      timeout,
      interval: 1000,
      label: "DuckDNS login",
    });

    let current = await pageState(page);
    if (subdomain) {
      info(`adding ${subdomain}.duckdns.org…`);
      const added = await addDomain(page, subdomain);
      if (!added) warn("could not find the subdomain input or the add-domain button");
      await page.waitForTimeout(2500);
      current = await pageState(page);
      if (!current.text.toLowerCase().includes(subdomain)) {
        warn(`DuckDNS did not show ${subdomain} yet — check the page before retrying`);
      }
    }

    const captured = await captureToken(page);
    const token = directToken || captured.token;
    if (!token) {
      warn("could not find the DuckDNS token on the page");
    }

    if (subdomain || token) {
      await mutate((draft) => {
        draft.duckdns = {
          ...draft.duckdns,
          ...(subdomain ? { subdomain } : {}),
          ...(token ? { token } : {}),
          checked_at: new Date().toISOString(),
        };
        draft.setup.auto = {
          ...(draft.setup.auto || {}),
          ...(subdomain ? { subdomain } : {}),
          ...(token ? { token } : {}),
        };
        if (subdomain) draft.setup.done["dns.account"] = true;
        if (token) draft.setup.done["dns.token"] = true;
        if (subdomain && token) draft.setup.done["dns.credentials"] = true;
      });
    }

    if (subdomain && token) {
      ok(`${subdomain}.duckdns.org · token ${token.slice(0, 8)}… saved`);
    } else if (subdomain) {
      ok(`subdomain ${subdomain} saved; token still missing`);
    } else if (token) {
      ok(`token saved${captured.candidates.length > 1 ? ` (${captured.candidates.length} candidates on the page)` : ""}`);
    }

    if (values.screenshot) {
      await fs.mkdir(DEBUG_ROOT, { recursive: true });
      const file = path.isAbsolute(values.screenshot)
        ? values.screenshot
        : path.join(DEBUG_ROOT, values.screenshot);
      await page.screenshot({ path: file, fullPage: true }).catch(() => {});
      info(`screenshot: ${file}`);
    }

    if (values["keep-open"] || !token || !subdomain) {
      console.log("DuckDNS is open — press Ctrl+C to close");
      await new Promise((resolve) => {
        process.once("SIGINT", resolve);
        process.once("SIGTERM", resolve);
        context.once("close", resolve);
      });
    }
    return 0;
  } catch (error) {
    if (page) {
      const dir = await saveDebug(page, "duckdns").catch(() => "");
      if (dir) warn(`debug saved to ${dir}`);
    }
    throw error;
  } finally {
    await context.close().catch(() => {});
  }
}
