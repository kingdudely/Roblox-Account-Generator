import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { attachLogging, hostOf, launchContext, openPage, saveDebug, stayOpen, waitUntil } from "../browser.js";
import { installContentBridge } from "../content-bridge.js";
import { driveGoogle, resolveAdminCreds } from "../google.js";
import { handleAuthMessage } from "../messages.js";
import { DEBUG_ROOT } from "../paths.js";
import { load, mutate } from "../state.js";
import { info, ok, warn } from "../util.js";

const HELP = `rac admin [options]

Opens the Google Admin console in a persistent Chrome profile. When an admin
email/password/authenticator secret is stored (or passed), the CLI signs in
automatically. Without a password it leaves the password field to you and
resumes when the Admin console appears.

Options:
  --email <address>     admin email (default: setup.auto.adminEmail/admin@domain)
  --password <value>    admin password (default: setup.auto.adminPassword)
  --totp <secret>       base32 authenticator secret
  --profile <name>      browser profile (default: setup)
  --headless            run headless
  --channel <name>      Chrome channel (default: chrome)
  --proxy <url>         proxy
  --timeout <seconds>   sign-in timeout (default: 180)
  --wait <seconds>      keep the browser open for N seconds, then close
  --screenshot <file>   save a screenshot under debug/
  --verbose             print page console output
  --help                show this help
`;

export const summary = "open or sign in to the Google Admin console";

async function waitForAdminHost(page, timeout) {
  return waitUntil(
    () => {
      const host = hostOf(page.url());
      return host === "admin.google.com" || host === "console.cloud.google.com";
    },
    { timeout, interval: 750, label: "admin console" }
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
      email: { type: "string" },
      password: { type: "string" },
      totp: { type: "string" },
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      timeout: { type: "string", default: "180" },
      wait: { type: "string", default: "0" },
      screenshot: { type: "string" },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  const state = await load();
  const creds = resolveAdminCreds(state, {
    email: values.email,
    password: values.password,
    otp: values.totp,
  });
  if (creds.email) {
    await mutate((draft) => {
      draft.setup.auto.adminEmail = creds.email;
      if (creds.domain) draft.setup.auto.domain = creds.domain;
      if (creds.password) draft.setup.auto.adminPassword = creds.password;
    });
  }

  info(`profile: ${values.profile}`);
  info(`admin:   ${creds.email || "not configured — sign in in the window"}`);
  info(`auth:    ${creds.password ? "password saved" : "manual password"}${creds.otp ? " + authenticator" : ""}`);

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

    page = await openPage(context, "https://admin.google.com/");
    attachLogging(page, { prefix: "admin", verbose: values.verbose });

    if (creds.email) {
      await driveGoogle(page, creds, {
        timeout: Number(values.timeout) * 1000,
        label: "admin sign-in",
        until: () => hostOf(page.url()) === "admin.google.com",
      });
    } else {
      warn("no admin email configured — sign in in the browser window");
      await waitForAdminHost(page, Number(values.timeout) * 1000);
    }

    ok(`signed in: ${page.url()}`);
    if (creds.email) {
      await mutate((draft) => {
        draft.setup.auto.adminEmail = creds.email;
      });
    }

    if (values.screenshot) {
      await fs.mkdir(DEBUG_ROOT, { recursive: true });
      const file = path.isAbsolute(values.screenshot)
        ? values.screenshot
        : path.join(DEBUG_ROOT, values.screenshot);
      await page.screenshot({ path: file, fullPage: true });
      info(`screenshot: ${file}`);
    }

    const wait = Number(values.wait) || 0;
    if (wait > 0) {
      await stayOpen(context, { seconds: wait });
    } else {
      await stayOpen(context, { message: "Admin console is open — press Ctrl+C to close" });
    }
    return 0;
  } catch (error) {
    const dir = await saveDebug(page, "admin").catch(() => "");
    if (dir) warn(`debug saved to ${dir}`);
    throw error;
  } finally {
    await context.close().catch(() => {});
  }
}
