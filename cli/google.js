import { dispatchMessage, sendGoogleFill } from "./content-bridge.js";
import { hostOf, saveDebug } from "./browser.js";
import { actionNeeded } from "./flow/human.js";
import { showPhoneGate } from "./flow/phone.js";
import { info, warn } from "./util.js";

export function isGoogleHost(url) {
  const host = hostOf(url);
  return host === "accounts.google.com" || host === "myaccount.google.com" || host.endsWith(".google.com") && host.includes("accounts");
}

export function resolveAdminCreds(state, overrides = {}) {
  const saved = state.setup?.auto || {};
  const duck = state.duckdns || {};
  const totp = state.totp || {};
  const key = (state.keys || []).find((entry) => entry.admin_email || entry.admin) || null;
  const domain = String(
    overrides.domain ||
      saved.domain ||
      duck.subdomain && `${duck.subdomain}.duckdns.org` ||
      key?.domain ||
      ""
  )
    .trim()
    .toLowerCase();
  const adminUser = String(saved.adminUser || "admin").trim();
  const savedEmail = String(
    saved.adminEmail ||
      (saved.adminUser && domain ? `${saved.adminUser}@${domain}` : "") ||
      key?.admin_email ||
      key?.admin ||
      totp.email ||
      ""
  )
    .trim()
    .toLowerCase();

  const email = String(
    overrides.email ||
      process.env.RAC_ADMIN_EMAIL ||
      savedEmail ||
      (domain ? `${adminUser}@${domain}` : "")
  )
    .trim()
    .toLowerCase();
  const password = String(
    overrides.password ||
      process.env.RAC_ADMIN_PASSWORD ||
      saved.adminPassword ||
      key?.admin_password ||
      ""
  );
  const totpOwner = String(totp.email || "").toLowerCase();
  const totpMatches =
    !totpOwner ||
    !email ||
    totpOwner === email ||
    Boolean(domain && totpOwner.endsWith(`@${domain}`));
  const otp = String(
    overrides.otp ||
      overrides.totp ||
      process.env.RAC_TOTP ||
      (totpMatches ? totp.secret : "") ||
      ""
  );
  const codes = Array.isArray(overrides.codes)
    ? overrides.codes
    : Array.isArray(key?.admin_backup_codes)
      ? key.admin_backup_codes
      : [];

  return { email, password, otp, codes, domain, adminUser };
}

export function credsForPage(creds, overrides = {}) {
  return {
    email: overrides.email || creds.email,
    password: overrides.password ?? creds.password,
    otp: overrides.otp ?? creds.otp,
    codes: overrides.codes || creds.codes || [],
  };
}

export async function fillGooglePage(page, creds) {
  return sendGoogleFill(page, creds, { tries: 3, interval: 200 });
}

export async function driveGoogle(
  page,
  creds,
  {
    until = null,
    timeout = 180_000,
    label = "Google sign-in",
    onTick = null,
    screenshot = true,
  } = {}
) {
  const isDone =
    until ||
    ((current) => {
      const host = hostOf(current.url());
      return Boolean(host) && !host.endsWith("google.com");
    });

  const deadline = Date.now() + timeout;
  let lastUrl = "";
  let passwordNotice = false;
  let stuckSince = Date.now();
  let lastNotice = "";
  const humanSeen = new Set();

  async function challenge() {
    return page
      .evaluate(() => {
        const text = document.body ? document.body.innerText || "" : "";
        if (/captcha|type the text you hear or see|not a robot/i.test(text)) return "captcha";
        if (/enter (the )?code|verification code|check your phone|we sent a code/i.test(text)) {
          return "code";
        }
        if (/2-step verification|verify it.s you|confirm it.s you|try another way/i.test(text)) {
          return "verification";
        }
        if (document.querySelector('input[type="password"]')) return "password";
        return "";
      })
      .catch(() => "");
  }

  while (Date.now() < deadline) {
    let done = false;
    try {
      done = Boolean(await isDone(page));
    } catch (_) {
      // page is navigating
    }
    if (done) return true;

    let url = "";
    try {
      url = page.url();
    } catch (_) {
      // closed
      break;
    }
    if (url !== lastUrl) {
      lastUrl = url;
      stuckSince = Date.now();
      lastNotice = "";
      info(`${label}: ${url.slice(0, 160)}`);
    }

    if (isGoogleHost(url)) {
      if (!creds.password && !passwordNotice) {
        passwordNotice = true;
        warn("no admin password saved — type it in the browser window; the CLI resumes after it");
      }
      const issue = await challenge();
      if (issue && Date.now() - stuckSince > 12_000 && lastNotice !== issue) {
        lastNotice = issue;
        const copy = {
          password: ["Google wants the admin password", "Type it in the focused browser window."],
          code: ["Enter the code Google sent you", "Check the phone and type the code in the focused browser window."],
          verification: ["Google wants extra verification", "Finish it in the focused browser window; I'll continue after it."],
          captcha: ["Solve the captcha", "Solve it in the focused browser window; I'll continue after it."],
        };
        if (issue === "verification") {
          await showPhoneGate(page, page.context(), { seen: humanSeen }).catch(() => {});
        } else {
          await actionNeeded(page, { title: copy[issue][0], lines: [copy[issue][1]] });
        }
        stuckSince = Date.now();
      } else if (
        !issue &&
        Date.now() - stuckSince > 20_000 &&
        lastNotice !== "stuck"
      ) {
        lastNotice = "stuck";
        await actionNeeded(page, {
          title: "Finish the Google sign-in",
          lines: [
            "The page is waiting for you in the focused browser window.",
            "Pick the admin account or finish the prompt; I'll continue after it.",
          ],
        });
        stuckSince = Date.now();
      }
      await fillGooglePage(page, creds).catch(() => {});
    }

    if (typeof onTick === "function") {
      try {
        await onTick(page);
      } catch (_) {
        // a transient page must not stop the login loop
      }
    }
    await page.waitForTimeout(1200);
  }

  const url = (() => {
    try {
      return page.url();
    } catch (_) {
      return "(closed)";
    }
  })();
  if (screenshot) {
    const dir = await saveDebug(page, `google-${label}`).catch(() => "");
    throw new Error(`${label} timed out after ${Math.round(timeout / 1000)}s at ${url}${dir ? ` — debug: ${dir}` : ""}`);
  }
  throw new Error(`${label} timed out after ${Math.round(timeout / 1000)}s at ${url}`);
}

export async function waitForAdminConsole(page, creds, { timeout = 180_000, requirePassword = false } = {}) {
  return driveGoogle(page, creds, {
    timeout,
    label: "admin sign-in",
    until: (current) => {
      const host = hostOf(current.url());
      return host === "admin.google.com" || host === "console.cloud.google.com";
    },
    onTick: async (current) => {
      const host = hostOf(current.url());
      if (host === "admin.google.com" && requirePassword && !creds.password) {
        // The console is reachable; no need to keep warning.
      }
    },
  });
}

export async function ensureAdminSession(page, creds, { timeout = 180_000 } = {}) {
  const host = hostOf(page.url());
  if (host === "admin.google.com" || host === "console.cloud.google.com") return true;
  return waitForAdminConsole(page, creds, { timeout });
}

export function isManualGate(url, body = "") {
  const text = `${url} ${body}`.toLowerCase();
  return (
    /captcha|recaptcha|verify (?:your )?phone|phone number|enter the code|text message|sms/i.test(text) ||
    /challenge\/pwd|challenge\/ipp|challenge\/selection/i.test(url)
  );
}

export async function sendGoogleMessage(page, message) {
  return dispatchMessage(page, message, { tries: 3, interval: 200 });
}
