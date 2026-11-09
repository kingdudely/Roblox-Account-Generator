import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { hostOf, launchContext, openPage, saveDebug, waitUntil } from "../browser.js";
import { DEBUG_ROOT } from "../paths.js";
import { makeAdminPassword } from "../../extension/lib/names.js";
import { load, mutate } from "../state.js";
import { info, ok, warn } from "../util.js";

const SIGNUP = "https://workspace.google.com/gcpidentity/signup?sku=identitybasic";

const HELP = `rac signup [options]

Prefills the Cloud Identity signup form and submits the account step. It stops
at Google's phone/captcha gate and leaves the browser open so you can finish the
verification. It does not bypass that gate.

Options:
  --business <name>    business name (default: from the DuckDNS subdomain)
  --first <name>       contact first name
  --last <name>        contact last name
  --contact <email>    current/recovery email
  --domain <domain>    domain to register (default: <subdomain>.duckdns.org)
  --subdomain <name>   DuckDNS subdomain shortcut
  --admin <username>   admin username local-part (default: admin)
  --password <value>   admin password (default: generated and saved)
  --phone <number>     prefill the phone number at the gate, if a field appears
  --employees <n>      "just-you", "2-9", "10-99", "100-299" or "300+"
  --profile <name>     browser profile (default: setup)
  --headless           run headless (the phone gate needs a visible browser)
  --channel <name>     Chrome channel (default: chrome)
  --proxy <url>        proxy
  --timeout <seconds>  per-step timeout (default: 120)
  --manual             only open the signup page; do not fill anything
  --keep-open          leave the browser open after the gate (default: true
                       when the gate is reached)
  --screenshot <file>  save a screenshot under debug/
  --verbose            print page console output
  --help               show this help
`;

export const summary = "prefill the Cloud Identity signup and stop at the phone gate";

function cleanDomain(value) {
  const text = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/\/.*$/, "");
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(text) ? text : "";
}

function cleanSubdomain(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\.duckdns\.org$/, "")
    .replace(/[^a-z0-9-]/g, "");
}

async function visibleInputs(page) {
  return page.locator(
    'input[type="text"]:visible, input[type="email"]:visible, input[type="password"]:visible, input:not([type]):visible'
  );
}

async function fillFirst(page, value, index = 0) {
  const inputs = await visibleInputs(page);
  const count = await inputs.count();
  if (count <= index) throw new Error(`expected a visible text input at position ${index + 1}, found ${count}`);
  await inputs.nth(index).fill(value);
}

async function fillByAria(page, patterns, value) {
  for (const pattern of patterns) {
    const locator = page.locator(`input[aria-label*="${pattern}" i]:visible`);
    if (await locator.count()) {
      await locator.first().fill(value);
      return true;
    }
  }
  return false;
}

async function clickButton(page, names) {
  for (const name of names) {
    const locator = page.getByRole("button", { name: new RegExp(`^${name}$`, "i") });
    if (await locator.count()) {
      await locator.first().click();
      return true;
    }
  }
  return false;
}

async function waitPath(page, pattern, timeout = 120_000) {
  await page.waitForURL(
    (url) => pattern.test(url.pathname),
    { timeout, waitUntil: "domcontentloaded" }
  );
  await page.waitForTimeout(600);
}

async function maybeFillPhone(page, phone) {
  if (!phone) return false;
  const field = page
    .locator('input[type="tel"]:visible, input[autocomplete="tel"]:visible, input[aria-label*="phone" i]:visible')
    .first();
  if (!(await field.count())) return false;
  await field.fill(String(phone).replace(/[^\d+]/g, "")).catch(() => {});
  await clickButton(page, ["send", "send code", "next", "verify"]).catch(() => {});
  return true;
}

function looksLikeGate(url, text) {
  const body = String(text || "").toLowerCase();
  return (
    /phone|sms|text message|verification code|captcha|verify it.s you/i.test(body) ||
    /phone|verify|challenge|captcha/i.test(url)
  );
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      business: { type: "string" },
      first: { type: "string" },
      last: { type: "string" },
      contact: { type: "string" },
      domain: { type: "string" },
      subdomain: { type: "string" },
      admin: { type: "string", default: "admin" },
      password: { type: "string" },
      phone: { type: "string" },
      employees: { type: "string", default: "just-you" },
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      timeout: { type: "string", default: "120" },
      manual: { type: "boolean", default: false },
      "keep-open": { type: "boolean", default: false },
      screenshot: { type: "string" },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  const state = await load();
  const saved = state.setup?.auto || {};
  const subdomain = cleanSubdomain(
    values.subdomain || state.duckdns?.subdomain || saved.subdomain || ""
  );
  const domain =
    cleanDomain(values.domain) || (subdomain ? `${subdomain}.duckdns.org` : "");
  if (!values.manual && !domain) {
    throw new Error("a domain is required: pass --domain or --subdomain, or run `rac duckdns` first");
  }

  const business =
    values.business ||
    saved.businessName ||
    (subdomain ? subdomain.replace(/-+/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase()) : "RAC tenant");
  const first = values.first || saved.firstName || "Camille";
  const last = values.last || saved.lastName || "Bianchi";
  const contact = values.contact || saved.recoveryEmail || saved.adminEmail || "";
  const adminUser = String(values.admin || saved.adminUser || "admin")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, "");
  const sameTenant = !saved.subdomain || !subdomain || saved.subdomain === subdomain;
  const password =
    values.password || (sameTenant ? saved.adminPassword : "") || makeAdminPassword();
  const timeout = Number(values.timeout) * 1000;

  if (subdomain && saved.subdomain && saved.subdomain !== subdomain) {
    await mutate((draft) => {
      draft.setup.done = {};
      draft.setup.auto.projectId = "";
      draft.setup.auto.policySafe = true;
    });
  }

  await mutate((draft) => {
    draft.setup.auto = {
      ...(draft.setup.auto || {}),
      businessName: business,
      firstName: first,
      lastName: last,
      adminUser,
      adminPassword: password,
      recoveryEmail: contact,
      domain,
      subdomain,
    };
    if (subdomain) draft.duckdns.subdomain = subdomain;
  });

  info(`profile:  ${values.profile}`);
  info(`business: ${business}`);
  info(`domain:   ${domain}`);
  info(`admin:    ${adminUser}@${domain}`);
  if (phoneGatePreview(values.phone)) info(`phone:    ${values.phone}`);

  const context = await launchContext({
    profile: values.profile,
    headless: values.headless,
    channel: values.channel,
    proxy: values.proxy || "",
  });
  let page = null;
  try {
    page = await openPage(context, SIGNUP);
    page.setDefaultTimeout(30_000);
    await page.waitForTimeout(2500);

    if (values.manual) {
      console.log("signup page is open — press Ctrl+C to close");
      await new Promise((resolve) => {
        process.once("SIGINT", resolve);
        process.once("SIGTERM", resolve);
        context.once("close", resolve);
      });
      return 0;
    }

    // Step 1: business name, employee count.
    await waitUntil(() => page.locator("#ucc-0, input[aria-label*='Business name' i]").count(), {
      timeout,
      label: "business-name step",
    });
    await fillByAria(page, ["Business name"], business).catch(() => {});
    if (!(await page.locator("#ucc-0").inputValue().catch(() => ""))) {
      await fillFirst(page, business, 0).catch(() => {});
    }
    const employeeMap = {
      "just-you": "#c1",
      "2-9": "#c2",
      "10-99": "#c3",
      "100-299": "#c4",
      "300+": "#c5",
    };
    const radio = employeeMap[String(values.employees).toLowerCase()] || "#c1";
    await page.locator(radio).check().catch(() => {});
    await clickButton(page, ["Next"]);
    await waitPath(page, /\/signup\/contact/, timeout);

    // Step 2: contact name + current email.
    await fillFirst(page, first, 0);
    await fillFirst(page, last, 1);
    if (contact) {
      const email = page.locator('input[type="email"]:visible').first();
      if (await email.count()) await email.fill(contact);
      else await fillFirst(page, contact, 2);
    }
    await clickButton(page, ["Next"]);
    await waitPath(page, /\/signup\/bring(?!confirm)/, timeout);

    // Step 3: domain.
    await fillByAria(page, ["domain"], domain).catch(() => {});
    if (!(await page.locator("#ucc-11").inputValue().catch(() => ""))) {
      await fillFirst(page, domain, 0);
    }
    await clickButton(page, ["Next"]);
    await waitPath(page, /\/signup\/bringconfirm/, timeout);

    // Step 4: confirm the domain.
    await clickButton(page, ["Next"]);
    await waitPath(page, /\/signup\/account/, timeout);

    // Step 5: admin username + password + agreement.
    await fillFirst(page, adminUser, 0);
    await fillFirst(page, password, 1);
    await fillFirst(page, password, 2);

    if (values.phone) await maybeFillPhone(page, values.phone);
    const agreed = await clickButton(page, ["Agree and continue"]);
    if (!agreed) warn("could not find the Agree and continue button");

    // The gate: phone, SMS, captcha, or a rejection message.
    await page.waitForTimeout(4000);
    const text = await page.evaluate(() => (document.body ? document.body.innerText : "")).catch(() => "");
    const url = page.url();
    if (hostOf(url) === "admin.google.com") {
      await mutate((draft) => {
        draft.setup.done["tenant.about"] = true;
        draft.setup.done["tenant.business"] = true;
        draft.setup.done["tenant.domain-name"] = true;
        draft.setup.done["tenant.signup"] = true;
        draft.setup.done["tenant.agreement"] = true;
      });
      ok("tenant created — landed in the Admin console");
    } else if (looksLikeGate(url, text)) {
      await mutate((draft) => {
        draft.setup.done["tenant.about"] = true;
        draft.setup.done["tenant.business"] = true;
        draft.setup.done["tenant.domain-name"] = true;
      });
      warn("Google's phone/captcha gate is up — finish it in the browser window");
      await maybeFillPhone(page, values.phone);
    } else {
      warn("signup did not reach the Admin console; check the browser for the next screen");
    }

    if (values.screenshot) {
      await fs.mkdir(DEBUG_ROOT, { recursive: true });
      const file = path.isAbsolute(values.screenshot)
        ? values.screenshot
        : path.join(DEBUG_ROOT, values.screenshot);
      await page.screenshot({ path: file, fullPage: true }).catch(() => {});
      info(`screenshot: ${file}`);
    }

    if (values.headless) {
      warn("headless mode cannot complete the phone gate — closing the browser");
      return 0;
    }
    console.log("finish the gate in the browser — press Ctrl+C to close");
    const monitor = setInterval(async () => {
      let host = "";
      try {
        host = hostOf(page.url());
      } catch (_) {
        clearInterval(monitor);
        return;
      }
      if (host !== "admin.google.com") return;
      clearInterval(monitor);
      await mutate((draft) => {
        draft.setup.done["tenant.signup"] = true;
        draft.setup.done["tenant.agreement"] = true;
      }).catch(() => {});
      ok("tenant created — Admin console detected");
    }, 2000);
    try {
      await new Promise((resolve) => {
        process.once("SIGINT", resolve);
        process.once("SIGTERM", resolve);
        context.once("close", resolve);
      });
    } finally {
      clearInterval(monitor);
    }
    return 0;
  } catch (error) {
    if (page) {
      const dir = await saveDebug(page, "signup").catch(() => "");
      if (dir) warn(`debug saved to ${dir}`);
    }
    throw error;
  } finally {
    await context.close().catch(() => {});
  }
}

function phoneGatePreview(phone) {
  return Boolean(phone);
}
