import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { hostOf, launchContext, openPage, saveDebug, stayOpen, waitUntil } from "../browser.js";
import { DEBUG_ROOT } from "../paths.js";
import { makeAdminPassword } from "../../extension/lib/names.js";
import { actionNeeded } from "../flow/human.js";
import { cleanDomain, cleanSubdomain } from "../flow/domain.js";
import {
  classifyPhoneText,
  hasVisibleCodeInput,
  hasVisiblePhoneInput,
  showPhoneGate,
} from "../flow/phone.js";
import { load, mutate } from "../state.js";
import { info, ok, oneLine, sleep, warn } from "../util.js";

const SIGNUP = "https://workspace.google.com/gcpidentity/signup?sku=identitybasic";

const HELP = `rac signup [options]

Fills the Cloud Identity signup form and drives the phone/QR gate. The tenant
domain does not need to exist yet; DuckDNS is created afterwards with the new
tenant admin account.

Options:
  --business <name>       business name (default: from the DuckDNS subdomain)
  --first <name>          contact first name
  --last <name>           contact last name
  --contact <email>       current/recovery email
  --domain <domain>       domain to register (default: <subdomain>.duckdns.org)
  --subdomain <name>      DuckDNS subdomain shortcut
  --admin <username>      admin username local-part (default: admin)
  --password <value>      admin password (default: generated and saved)
  --phone <number>        YOUR number, only for the gate that texts you a code
                          (not the number Google asks you to text)
  --employees <n>         "just-you", "2-9", "10-99", "100-299" or "300+"
  --gate-timeout <s>      how long to wait at the phone/QR gate (default: 1800)
  --profile <name>        browser profile (default: setup)
  --headless              run headless (the phone gate needs a visible browser)
  --channel <name>       Chrome channel (default: chrome)
  --proxy <url>           proxy
  --timeout <seconds>     per-step form timeout (default: 120)
  --manual                only open the signup page; do not fill anything
  --check-form            fill through the admin step, verify, then stop
  --fast                  instant fill (debug only; default is human-like typing)
  --keep-open             leave the browser open after the tenant is created
  --screenshot <file>     save a screenshot under debug/
  --verbose               print page console output
  --help                  show this help
`;

export const summary = "fill the Cloud Identity signup and drive the phone/QR gate";

export function placeholderEmail(subdomain) {
  const name = String(subdomain || "tenant")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "")
    .slice(0, 40);
  return `rac-${name || "tenant"}@example.com`;
}

let humanInput = true;

function rnd(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

async function humanPause(min = 250, max = 800, fast = humanInput) {
  if (fast) return;
  await sleep(rnd(min, max));
}

async function humanClick(page, locator, { fast = humanInput } = {}) {
  if (fast) {
    await locator.click();
    return;
  }
  await locator.scrollIntoViewIfNeeded().catch(() => {});
  const box = await locator.boundingBox();
  if (!box) {
    await locator.click();
    return;
  }
  const x = box.x + box.width * (0.25 + Math.random() * 0.5);
  const y = box.y + box.height * (0.3 + Math.random() * 0.4);
  await page.mouse.move(x, y, { steps: rnd(8, 22) });
  await humanPause(90, 260, fast);
  await page.mouse.click(x, y);
  await humanPause(180, 620, fast);
}

async function humanType(
  page,
  locator,
  text,
  { fast = humanInput, clear = true } = {}
) {
  const value = String(text ?? "");
  if (fast) {
    await locator.fill(value);
    return;
  }
  await humanClick(page, locator, { fast });
  const current = await locator.inputValue().catch(() => "");
  if (clear && current) {
    const modifier = process.platform === "darwin" ? "Meta" : "Control";
    await page.keyboard.press(`${modifier}+A`).catch(() => {});
    await page.keyboard.press("Backspace").catch(() => {});
    await humanPause(100, 300, fast);
  }
  const chars = [...value];
  for (let index = 0; index < chars.length; index += 1) {
    await page.keyboard.type(chars[index]);
    await sleep(rnd(45, 135));
    if (index > 0 && index % rnd(5, 9) === 0) {
      await sleep(rnd(120, 420));
    }
  }
  await humanPause(250, 750, fast);
}

async function editableInputs(page) {
  return page.locator(
    'input[type="text"]:visible:not([readonly]):not([disabled]), ' +
      'input[type="email"]:visible:not([readonly]):not([disabled]), ' +
      'input[type="password"]:visible:not([readonly]):not([disabled]), ' +
      'input:not([type]):visible:not([readonly]):not([disabled])'
  );
}

async function fillFirst(page, value, index = 0) {
  const inputs = await editableInputs(page);
  const count = await inputs.count();
  if (count <= index) throw new Error(`expected an editable input at position ${index + 1}, found ${count}`);
  await humanType(page, inputs.nth(index), value);
}

async function fillByAria(page, patterns, value) {
  for (const pattern of patterns) {
    const locator = page.locator(
      `input[aria-label*="${pattern}" i]:visible:not([readonly]):not([disabled])`
    );
    if (await locator.count()) {
      await humanType(page, locator.first(), value);
      return true;
    }
  }
  return false;
}

// The account step has a readonly username preview before the real field.
// Prefer the stable IDs from Google's form, then fall back to editable inputs.
export async function fillAccountStep(
  page,
  adminUser,
  password,
  { fast = humanInput } = {}
) {
  const username = page
    .locator(
      '#ucc-14:visible, input[autocomplete="off"][maxlength="64"]:visible, input[type="text"]:visible:not([readonly]):not([disabled])'
    )
    .first();
  const passwords = page.locator(
    '#ucc-15:visible, #ucc-16:visible, input[type="password"]:visible:not([readonly]):not([disabled])'
  );

  if (!(await username.count())) {
    throw new Error("could not find the editable username field on the signup account step");
  }
  await humanType(page, username, adminUser, { fast });
  const userValue = await username.inputValue().catch(() => "");
  if (userValue !== adminUser) {
    throw new Error(`username field did not accept the value (got ${JSON.stringify(userValue)})`);
  }

  const count = await passwords.count();
  if (count < 2) {
    throw new Error(`expected two editable password fields, found ${count}`);
  }
  await humanType(page, passwords.nth(0), password, { fast });
  await humanType(page, passwords.nth(1), password, { fast });
  const firstPassword = await passwords.nth(0).inputValue().catch(() => "");
  const secondPassword = await passwords.nth(1).inputValue().catch(() => "");
  if (firstPassword !== password || secondPassword !== password) {
    throw new Error("one of the password fields did not accept the value");
  }
  return { username: userValue, passwordLength: firstPassword.length };
}

async function clickButton(page, names) {
  for (const name of names) {
    const locator = page.getByRole("button", { name: new RegExp(`^${name}$`, "i") });
    if (await locator.count()) {
      await humanClick(page, locator.first());
      return true;
    }
  }
  return false;
}

async function waitPath(page, pattern, timeout = 120_000) {
  await page.waitForURL((url) => pattern.test(url.pathname), {
    timeout,
    waitUntil: "domcontentloaded",
  });
  await page.waitForTimeout(600);
}

async function bodyText(page) {
  return page
    .evaluate(() => (document.body ? document.body.innerText || "" : ""))
    .catch(() => "");
}

async function maybeFillPassword(page, password) {
  if (!password) return false;
  const fields = page.locator(
    'input[type="password"]:visible:not([readonly]):not([disabled])'
  );
  const count = await fields.count().catch(() => 0);
  // The signup account step has password + confirm. A Google sign-in page has
  // one password field. Do not touch the signup form after Agree.
  if (count !== 1) return false;
  const field = fields.first();
  const current = await field.inputValue().catch(() => "");
  if (!current) {
    await humanType(page, field, password).catch(() => {});
    info("admin password filled; continuing");
  }
  await clickButton(page, ["Next", "Sign in", "Continue"]).catch(() => {});
  return true;
}

async function maybeFillPhone(page, phone) {
  if (!phone) return false;
  const field = page
    .locator(
      'input[type="tel"]:visible, input[autocomplete="tel"]:visible, input[aria-label*="phone" i]:visible'
    )
    .first();
  if (!(await field.count().catch(() => 0))) return false;
  const current = await field.inputValue().catch(() => "");
  if (current) return false;
  await humanType(page, field, String(phone).replace(/[^\d+]/g, "")).catch(() => {});
  await clickButton(page, ["send", "send code", "next", "verify"]).catch(() => {});
  info("phone number filled; waiting for Google");
  return true;
}

function looksLikeSuccess(url, text) {
  if (hostOf(url) === "admin.google.com") return true;
  if (/\/signup\/(success|complete|welcome)/i.test(url)) return true;
  if (/create a username|confirm password|agree and continue/i.test(text)) return false;
  try {
    const parsed = new URL(url);
    if (
      parsed.hostname === "workspace.google.com" &&
      !/\/gcpidentity\/signup/i.test(parsed.pathname)
    ) {
      return true;
    }
  } catch (_) {
    // malformed URL
  }
  return /your account is ready|your google workspace account is ready|tenant is ready|you.re all set up/i.test(
    text
  );
}

function looksLikeRejection(text) {
  return /sorry,? we can.?t complete your signup|we can.?t create your account|too many unsuccessful attempts|this phone number (?:can.?t|cannot) be used|signup was blocked|domain.*(?:already|registered|in use)|already.*(?:registered|in use).*domain/i.test(
    text
  );
}

async function markTenantCreated() {
  await mutate((draft) => {
    draft.setup.done["tenant.signup"] = true;
    draft.setup.done["tenant.agreement"] = true;
    draft.setup.done["tenant.about"] = true;
    draft.setup.done["tenant.business"] = true;
    draft.setup.done["tenant.domain-name"] = true;
    draft.setup.done["tenant.admin"] = true;
    draft.setup.auto.phase = "";
  });
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
      "gate-timeout": { type: "string", default: "1800" },
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      timeout: { type: "string", default: "120" },
      manual: { type: "boolean", default: false },
      "check-form": { type: "boolean", default: false },
      fast: { type: "boolean", default: false },
      "keep-open": { type: "boolean", default: false },
      screenshot: { type: "string" },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  humanInput = !values.fast;
  const state = await load();
  const saved = state.setup?.auto || {};
  const subdomain = cleanSubdomain(
    values.subdomain || state.duckdns?.subdomain || saved.subdomain || ""
  );
  const domain =
    cleanDomain(values.domain) || (subdomain ? `${subdomain}.duckdns.org` : "");
  if (!values.manual && !domain) {
    throw new Error("a domain is required: pass --domain or --subdomain, or run `rac start`");
  }

  const business =
    values.business ||
    saved.businessName ||
    (subdomain ? subdomain.replace(/-+/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase()) : "RAC tenant");
  const first = values.first || saved.firstName || "Camille";
  const last = values.last || saved.lastName || "Bianchi";
  const contact =
    values.contact || saved.recoveryEmail || placeholderEmail(subdomain);
  const adminUser = String(values.admin || saved.adminUser || "admin")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, "");
  const sameTenant = !saved.subdomain || !subdomain || saved.subdomain === subdomain;
  const password =
    values.password || (sameTenant ? saved.adminPassword : "") || makeAdminPassword();
  const timeout = Number(values.timeout) * 1000;
  const gateTimeout = Math.max(60, Number(values["gate-timeout"]) || 1800) * 1000;

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
      adminEmail: `${adminUser}@${domain}`,
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
  if (/@example\.(com|org)$/i.test(contact)) {
    info(`recovery: ${contact} (placeholder; pass --contact for a real one)`);
  }

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
    if (humanInput) {
      await page.mouse.move(rnd(250, 900), rnd(180, 520), { steps: rnd(10, 22) });
      await humanPause(700, 1600);
    }

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
    await humanPause(700, 1600);
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
    const radioLocator = page.locator(radio);
    if (await radioLocator.count()) {
      await humanClick(page, radioLocator.first()).catch(async () => {
        await radioLocator.first().check().catch(() => {});
      });
    }
    await clickButton(page, ["Next"]);
    await waitPath(page, /\/signup\/contact/, timeout);
    await humanPause(800, 1800);

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
    await humanPause(700, 1600);

    // Step 3: domain.
    await fillByAria(page, ["domain"], domain).catch(() => {});
    if (!(await page.locator("#ucc-11").inputValue().catch(() => ""))) {
      await fillFirst(page, domain, 0);
    }
    await clickButton(page, ["Next"]);
    await waitPath(page, /\/signup\/bringconfirm/, timeout);
    await humanPause(600, 1400);

    // Step 4: confirm the domain.
    await clickButton(page, ["Next"]);
    await waitPath(page, /\/signup\/account/, timeout);
    await humanPause(1200, 2600);

    // Step 5: admin username + password + agreement.
    await fillAccountStep(page, adminUser, password);

    if (values["check-form"]) {
      ok("signup form filled correctly; stopping before Agree");
      if (values.screenshot) {
        await fs.mkdir(DEBUG_ROOT, { recursive: true });
        const file = path.isAbsolute(values.screenshot)
          ? values.screenshot
          : path.join(DEBUG_ROOT, values.screenshot);
        await page.screenshot({ path: file, fullPage: true }).catch(() => {});
        info(`screenshot: ${file}`);
      }
      return 0;
    }

    if (values.phone) await maybeFillPhone(page, values.phone);
    await humanPause(1200, 2600);
    const agreed = await clickButton(page, ["Agree and continue"]);
    if (!agreed) warn("could not find the Agree and continue button");

    // Step 6: phone/QR gate. Everything here is automatic except the actual
    // phone action; the CLI focuses the window, beeps and extracts any number
    // and message Google wants you to send.
    const seen = new Set();
    const deadline = Date.now() + gateTimeout;
    let lastNotice = "";
    let created = false;

    while (Date.now() < deadline) {
      if (page.isClosed()) throw new Error("the browser was closed before the tenant was created");
      let url = "";
      try {
        url = page.url();
      } catch (_) {
        throw new Error("the browser was closed before the tenant was created");
      }
      const text = await bodyText(page);
      const kind = classifyPhoneText(text);

      if (/passwords? don.?t match|username (?:is )?(?:not available|already taken)|enter a valid email/i.test(text)) {
        throw new Error(`signup form rejected the values: ${oneLine(text, 220)}`);
      }

      if (looksLikeRejection(text)) {
        await saveDebug(page, "signup-rejected").catch(() => {});
        const domainBusy = /domain.*(?:already|registered|in use)|already.*(?:registered|in use).*domain/i.test(
          text
        );
        throw new Error(
          `Google rejected the signup: ${oneLine(text, 220) || "no message"} — ` +
            (domainBusy
              ? "that domain is already registered; rerun with `--subdomain <new-name>`"
              : "wait a while or try a different network/phone; this is Google's anti-abuse check, not a CLI bug")
        );
      }

      if (looksLikeSuccess(url, text)) {
        created = true;
        break;
      }

      if (kind === "captcha") {
        if (lastNotice !== "captcha") {
          lastNotice = "captcha";
          await actionNeeded(page, {
            title: "Solve the captcha",
            lines: ["Google wants a captcha. Solve it in the focused browser window."],
          });
        }
        await page.waitForTimeout(1800);
        continue;
      }

      if (await maybeFillPassword(page, password)) {
        await page.waitForTimeout(1800);
        continue;
      }

      const qrish =
        kind === "qr" ||
        kind === "send-sms" ||
        kind === "consent-link" ||
        /qr|scan|send\b[^.!?\n]{0,50}\b(?:sms|text|message)|verify your device/i.test(text);

      if (!qrish && (kind === "code-entry" || (kind !== "phone-entry" && (await hasVisibleCodeInput(page))))) {
        if (values.headless) {
          throw new Error("Google's verification code page needs a visible browser; rerun without --headless");
        }
        if (lastNotice !== "code-entry") {
          lastNotice = "code-entry";
          await actionNeeded(page, {
            title: "Enter the code Google sent you",
            lines: [
              "Check your phone and type the code into the focused browser window.",
              values.phone ? `Number used: ${values.phone}` : "",
            ].filter(Boolean),
          });
        }
        await page.waitForTimeout(2200);
        continue;
      }

      if (!qrish && (kind === "phone-entry" || (kind !== "code-entry" && (await hasVisiblePhoneInput(page))))) {
        if (values.headless) {
          throw new Error("Google's phone page needs a visible browser; rerun without --headless");
        }
        if (values.phone) {
          const filled = await maybeFillPhone(page, values.phone);
          if (filled) {
            lastNotice = "phone-filled";
            await page.waitForTimeout(1800);
            continue;
          }
        }
        if (lastNotice !== "phone-entry") {
          lastNotice = "phone-entry";
          await actionNeeded(page, {
            title: "Google wants your phone number",
            lines: [
              "Type your number in the focused browser window (or rerun with --phone).",
              "Google will text you a code; enter it on the next page.",
            ],
          });
        }
        await page.waitForTimeout(1800);
        continue;
      }

      if (qrish || kind === "verification") {
        if (values.headless) {
          throw new Error("Google's phone/QR gate needs a visible browser; rerun without --headless");
        }
        const scan = await showPhoneGate(page, context, { seen });
        if (scan.kind === "rejected") {
          throw new Error(`Google rejected the signup: ${oneLine(scan.text, 220)}`);
        }
        lastNotice = scan.kind;
        await page.waitForTimeout(2200);
        continue;
      }

      if (
        kind === "unknown" &&
        hostOf(url) === "accounts.google.com" &&
        /verify|qr|scan|phone|security|challenge|code/i.test(text)
      ) {
        await showPhoneGate(page, context, { seen });
        await page.waitForTimeout(1800);
        continue;
      }

      if (/verify|phone|sms|code|qr|recovery/i.test(text) && lastNotice !== "unknown-gate") {
        lastNotice = "unknown-gate";
        await actionNeeded(page, {
          title: "Google needs you",
          lines: ["Finish the verification in the focused browser window."],
        });
      }

      await page.waitForTimeout(1600);
    }

    if (!created) {
      await saveDebug(page, "signup-gate-timeout").catch(() => {});
      throw new Error(
        `Google verification did not finish within ${Math.round(gateTimeout / 1000)}s — ` +
          "finish it in the browser and run `rac start` again"
      );
    }

    await markTenantCreated();
    ok("tenant created — continuing");

    if (values.screenshot) {
      await fs.mkdir(DEBUG_ROOT, { recursive: true });
      const file = path.isAbsolute(values.screenshot)
        ? values.screenshot
        : path.join(DEBUG_ROOT, values.screenshot);
      await page.screenshot({ path: file, fullPage: true }).catch(() => {});
      info(`screenshot: ${file}`);
    }

    if (values["keep-open"]) {
      await stayOpen(context, { message: "tenant created — press Ctrl+C to close" });
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
