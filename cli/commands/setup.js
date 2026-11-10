import { parseArgs } from "node:util";
import {
  attachLogging,
  hostOf,
  launchContext,
  openPage,
  saveDebug,
  stayOpen,
  waitUntil,
} from "../browser.js";
import {
  installContentBridge,
  sendGoogleFill,
  sendSetupDomainReady,
  sendSetupFill,
} from "../content-bridge.js";
import { actionNeeded } from "../flow/human.js";
import { authorizeKeyWithContext } from "../consent.js";
import { fullDomain, waitForTxt, writeTxt } from "../../extension/lib/duckdns.js";
import { normaliseKey } from "../../extension/lib/keys.js";
import { driveGoogle, resolveAdminCreds } from "../google.js";
import { checkKeyById } from "../key-flow.js";
import { handleAuthMessage } from "../messages.js";
import { load, mutate } from "../state.js";
import { info, ok, step, warn } from "../util.js";

const AUTO_ADMIN = "https://admin.google.com/";
const AUTO_DOMAINS = "https://admin.google.com/ac/domains/manage";
const AUTO_BILLING = "https://admin.google.com/ac/billing/catalog";
const AUTO_ADMIN_2SV = "https://myaccount.google.com/signinoptions/two-step-verification";
const AUTO_2SV = "https://admin.google.com/ac/security/2sv";
const AUTO_PROJECT = "https://console.cloud.google.com/projectcreate";
const AUTO_ADMIN_SDK = "https://console.cloud.google.com/apis/library/admin.googleapis.com";
const AUTO_CONSENT = "https://console.cloud.google.com/apis/credentials/consent";
const AUTO_CREDENTIALS = "https://console.cloud.google.com/apis/credentials";

const PHASES = [
  {
    id: "tenant.verify",
    label: "verify the domain with a DuckDNS TXT record",
    url: () => AUTO_DOMAINS,
    timeout: 240_000,
    kind: "setup",
  },
  {
    id: "tenant.cloud-identity",
    label: "activate Cloud Identity Free",
    url: () => AUTO_BILLING,
    timeout: 75_000,
    kind: "setup",
  },
  {
    id: "twostep.enroll",
    label: "enrol the admin authenticator app",
    url: () => AUTO_ADMIN_2SV,
    timeout: 210_000,
    kind: "google",
  },
  {
    id: "twostep.policy",
    label: "set the tenant 2SV policy",
    url: () => AUTO_2SV,
    timeout: 95_000,
    kind: "setup",
  },
  {
    id: "key.project",
    label: "create the Cloud project",
    url: () => AUTO_PROJECT,
    timeout: 130_000,
    kind: "setup",
  },
  {
    id: "key.admin-sdk",
    label: "enable the Admin SDK",
    url: (values) => withProject(AUTO_ADMIN_SDK, values.projectId),
    timeout: 100_000,
    kind: "setup",
    needsProject: true,
  },
  {
    id: "key.consent",
    label: "set up the OAuth consent screen",
    url: (values) => withProject(AUTO_CONSENT, values.projectId),
    timeout: 130_000,
    kind: "setup",
    needsProject: true,
  },
  {
    id: "key.scopes",
    label: "add the five OAuth scopes",
    url: (values) => withProject(AUTO_CONSENT, values.projectId),
    timeout: 180_000,
    kind: "setup",
    needsProject: true,
  },
  {
    id: "key.client",
    label: "create the desktop OAuth client",
    url: (values) => withProject(AUTO_CREDENTIALS, values.projectId),
    timeout: 160_000,
    kind: "setup",
    needsProject: true,
  },
  {
    id: "key.authorize",
    label: "authorize the key",
    timeout: 240_000,
    kind: "authorize",
    needsProject: true,
  },
];

const BY_ID = Object.fromEntries(PHASES.map((phase) => [phase.id, phase]));

const HELP = `rac setup [options]

Runs the automatic tenant/key sub-steps in a persistent Patchright profile. The
CLI reuses the extension's content scripts, so it fills and clicks the same
Admin and Cloud console pages as the extension.

This command starts after the tenant and DuckDNS record exist. For the whole
pipeline (signup -> phone/QR gate -> DuckDNS with the tenant admin -> setup ->
Roblox account), use \`rac start\`. If you run this command alone, the domain
and DuckDNS token must already be saved.

Options:
  --phase <id>         run only this sub-step (repeatable)
  --all                run every sub-step not already checked (default)
  --force              run even when the state file marks it done
  --dry-run            print the queue and exit

Values:
  --subdomain <name>   DuckDNS subdomain (default: state/duckdns)
  --token <token>      DuckDNS account token
  --domain <domain>    tenant domain (default: <subdomain>.duckdns.org)
  --admin-email <addr> tenant admin (default: admin@domain)
  --admin-password <v> tenant admin password
  --totp <secret>      base32 authenticator secret
  --business <name>    business name used by the consent screen
  --recovery <email>   consent-screen recovery email
  --project <id>       existing Cloud project id (otherwise create one)

Browser:
  --profile <name>     browser profile (default: setup)
  --headless           run headless (setup pages may need a visible browser)
  --channel <name>     Chrome channel (default: chrome)
  --proxy <url>        proxy
  --login-timeout <s>  admin sign-in timeout (default: 180)
  --keep-open          leave the browser open after the run
  --verbose            print page console output
  --help               show this help

Examples:
  rac setup --subdomain mytenant --token <duckdns-token> --admin-password '<pw>'
  rac setup --phase tenant.verify
  rac setup --phase key.authorize
  rac setup --dry-run
`;

export const summary = "run the automatic tenant and key sub-steps";

function cleanSubdomain(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\.duckdns\.org$/, "")
    .replace(/[^a-z0-9-]/g, "");
}

function cleanDomain(value) {
  const text = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/\/.*$/, "");
  return /^[a-z0-9.-]+\.[a-z]{2,}$/.test(text) ? text : "";
}

function titleCase(value) {
  return String(value || "")
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (ch) => ch.toUpperCase())
    .trim();
}

function withProject(base, projectId) {
  return projectId ? `${base}?project=${encodeURIComponent(projectId)}` : "";
}

function resolveValues(state, values) {
  const saved = state.setup?.auto || {};
  const duck = state.duckdns || {};
  const subdomain = cleanSubdomain(values.subdomain || saved.subdomain || duck.subdomain || "");
  const sameTenant = !saved.subdomain || !subdomain || saved.subdomain === subdomain;
  const domain =
    cleanDomain(values.domain) ||
    (sameTenant ? cleanDomain(saved.domain) : "") ||
    (subdomain ? fullDomain(subdomain) : "");
  const adminEmail = String(
    values["admin-email"] ||
      (sameTenant ? saved.adminEmail : "") ||
      (subdomain ? `admin@${domain}` : "")
  )
    .trim()
    .toLowerCase();
  const recovery = String(
    values.recovery || (sameTenant ? saved.recoveryEmail : "") || adminEmail || ""
  ).trim();
  const projectId = String(values.project || (sameTenant ? saved.projectId : "") || "").trim();
  const token = String(values.token || saved.token || duck.token || "").trim();
  return {
    subdomain,
    domain,
    adminEmail,
    adminPassword: String(
      values["admin-password"] || (sameTenant ? saved.adminPassword : "") || ""
    ),
    recovery,
    token,
    projectId,
    businessName: String(
      values.business ||
        (sameTenant ? saved.businessName : "") ||
        (subdomain ? titleCase(subdomain) : "RAC tenant")
    ),
    projectName: String(
      (sameTenant ? saved.projectName : "") || (subdomain ? `${subdomain}-rac` : "rac-tenant")
    ),
  };
}

function buildQueue(state, values, cfg) {
  const requested = values.phase?.length
    ? values.phase
    : String(values.phases || "")
        .split(",")
        .map((entry) => entry.trim())
        .filter(Boolean);
  const done = state.setup?.done || {};
  const selected = requested.length
    ? requested.map((id) => {
        const phase = BY_ID[id];
        if (!phase) throw new Error(`unknown phase: ${id}`);
        return phase;
      })
    : PHASES;

  const queue = [];
  for (const phase of selected) {
    if (!values.force && done[phase.id]) continue;
    if (phase.id === "key.project" && cfg.projectId) continue;
    queue.push(phase);
  }
  return queue;
}

function requireValue(phase, cfg) {
  if (phase.id === "tenant.verify") {
    if (!cfg.subdomain) throw new Error("tenant.verify needs a DuckDNS subdomain: run `rac duckdns` or pass --subdomain");
    if (!cfg.token) throw new Error("tenant.verify needs the DuckDNS token: run `rac duckdns` or pass --token");
  }
  if (phase.needsProject && !cfg.projectId) {
    throw new Error(`${phase.id} needs a Cloud project id — run \`rac setup --phase key.project\` first`);
  }
}

async function runPhase(page, phase, ctx) {
  const { cfg, creds, signals, state } = ctx;

  if (phase.id === "key.authorize") {
    return authorizePhase(page, phase, ctx);
  }

  requireValue(phase, cfg);

  if (phase.id === "twostep.policy" && state.setup?.auto?.policySafe === false) {
    throw new Error(
      "admin 2SV is already on without a saved authenticator secret — " +
        "policy automation is unsafe; set it manually only if another allowed factor exists"
    );
  }

  const url = phase.url(cfg);
  if (!url) throw new Error(`${phase.id} has no page URL`);
  step(`${phase.id}: ${phase.label}`);

  // The scope picker lives on the same consent page the previous phase just
  // finished. Reloading it would throw away the open dialog for no reason.
  const keepPage = await page
    .evaluate((id) => {
      if (id !== "key.scopes") return false;
      const text = document.body ? document.body.innerText || "" : "";
      return /add or remove scopes/i.test(text);
    }, phase.id)
    .catch(() => false);

  if (!keepPage) {
    info(`open ${url}`);
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 }).catch(() => {});
    await page.waitForTimeout(1200);
  }

  signals.delete(phase.id);
  await mutate((draft) => {
    draft.setup.auto.phase = phase.id;
  });

  let deadline = Date.now() + phase.timeout;
  while (Date.now() < deadline) {
    const signal = signals.get(phase.id);
    if (signal?.done) return signal;
    if (signal?.error) throw new Error(`${phase.id}: ${signal.error}`);
    if (page.isClosed()) throw new Error("the setup page was closed");

    await handoff(page, phase.id, ctx).catch(() => {});

    const challenge = await page
      .evaluate(() => {
        const text = document.body ? document.body.innerText || "" : "";
        if (/captcha|type the text you hear or see/i.test(text)) return "captcha";
        const url = location.href;
        if (
          /accounts\.google\.com|myaccount\.google\.com|challenge|signin/i.test(url) &&
          /verify it.s you|2-step verification|enter (the )?code|try another way/i.test(text)
        ) {
          return "verification";
        }
        return "";
      })
      .catch(() => "");
    if (challenge && !ctx.seenChallenges.has(`${phase.id}:${challenge}`)) {
      ctx.seenChallenges.add(`${phase.id}:${challenge}`);
      deadline = Math.max(deadline, Date.now() + 5 * 60_000);
      await actionNeeded(page, {
        title: challenge === "captcha" ? "Solve the captcha" : "Google needs extra verification",
        lines: ["Finish it in the focused setup window. I'll continue after it."],
      }).catch(() => {});
    }

    await page.waitForTimeout(1200);
  }

  throw new Error(`${phase.id} timed out after ${Math.round(phase.timeout / 1000)}s`);
}

async function handoff(page, phaseId, { creds, cfg, domainReady }) {
  let url = "";
  try {
    url = page.url();
  } catch (_) {
    return;
  }
  const host = hostOf(url);

  if (host === "admin.google.com" || host === "console.cloud.google.com") {
    await sendSetupFill(
      page,
      {
        subdomain: cfg.subdomain,
        domain: cfg.domain,
        token: cfg.token,
        businessName: cfg.businessName,
        projectName: cfg.projectName,
        recoveryEmail: cfg.recovery || creds.email || cfg.adminEmail,
        adminEmail: creds.email || cfg.adminEmail,
        adminPassword: creds.password || cfg.adminPassword,
        projectId: cfg.projectId,
      },
      phaseId
    );
    if (phaseId === "tenant.verify" && domainReady?.value) {
      await sendSetupDomainReady(page);
    }
    return;
  }

  if (host.endsWith("google.com")) {
    await sendGoogleFill(
      page,
      {
        email: creds.email || cfg.adminEmail,
        password: creds.password || cfg.adminPassword,
        otp: creds.otp,
        codes: creds.codes,
      },
      { tries: 2, interval: 250 }
    );
  }
}

async function authorizePhase(page, phase, ctx) {
  const { cfg, creds, context, pendingKey } = ctx;
  const state = await load();
  const key =
    (pendingKey && state.keys.find((entry) => entry.id === pendingKey)) ||
    state.keys.find(
      (entry) =>
        entry.needs_consent &&
        (!cfg.domain || String(entry.domain || "").toLowerCase() === cfg.domain)
    ) ||
    state.keys.find((entry) => entry.needs_consent);
  if (!key) {
    throw new Error("no OAuth client to authorize — run the key.client phase first");
  }

  step(`${phase.id}: ${phase.label} (${key.label || key.id})`);
  const credential = await authorizeKeyWithContext(context, key, {
    creds,
    email: creds.email || cfg.adminEmail,
    timeout: phase.timeout,
    keepOpen: false,
  });

  await mutate((draft) => {
    const target = draft.keys.find((entry) => entry.id === key.id);
    if (!target) return;
    target.refresh_token = credential.refresh_token;
    target.token_uri = credential.token_uri || target.token_uri;
    if (Array.isArray(credential.scopes) && credential.scopes.length) {
      target.scopes = credential.scopes;
    }
    target.needs_consent = false;
    target.status = "new";
    target.message = "";
    target.authorized_at = new Date().toISOString();
  });
  ctx.pendingKey = key.id;
  ok(`${key.label || key.id}: consent granted`);

  const checked = await checkKeyById(key.id).catch((error) => {
    warn(`key check after consent failed: ${error.message || error}`);
    return { ok: false };
  });
  await mutate((draft) => {
    draft.setup.done["run.key"] = true;
    if (checked.ok) draft.setup.done["run.check"] = true;
  });
  if (!checked.ok) warn("consent succeeded, but the Admin SDK check did not pass yet");
  return { done: true };
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      phase: { type: "string", multiple: true },
      phases: { type: "string" },
      all: { type: "boolean", default: false },
      force: { type: "boolean", default: false },
      "dry-run": { type: "boolean", default: false },
      subdomain: { type: "string" },
      token: { type: "string" },
      domain: { type: "string" },
      "admin-email": { type: "string" },
      "admin-password": { type: "string" },
      totp: { type: "string" },
      business: { type: "string" },
      recovery: { type: "string" },
      project: { type: "string" },
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      "login-timeout": { type: "string", default: "180" },
      "keep-open": { type: "boolean", default: false },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  let state = await load();
  let cfg = resolveValues(state, values);
  const previousSubdomain = String(state.setup?.auto?.subdomain || "").toLowerCase();
  let queueState = state;
  if (cfg.subdomain && previousSubdomain && previousSubdomain !== cfg.subdomain) {
    // Switching tenants invalidates every old checkmark and the old project.
    if (values["dry-run"]) {
      queueState = {
        ...state,
        setup: {
          ...state.setup,
          done: {},
          auto: { ...state.setup.auto, projectId: "", policySafe: true },
        },
      };
    } else {
      await mutate((draft) => {
        draft.setup.done = {};
        draft.setup.auto.adminPassword = "";
        draft.setup.auto.projectId = "";
        draft.setup.auto.policySafe = true;
      });
      state = await load();
      cfg = resolveValues(state, values);
      queueState = state;
    }
  }
  const queue = buildQueue(queueState, values, cfg);

  info(`profile: ${values.profile}`);
  info(`domain:  ${cfg.domain || "not set"}`);
  info(`admin:   ${cfg.adminEmail || "not set"}`);
  info(`token:   ${cfg.token ? "saved" : "missing"}`);
  info(`project: ${cfg.projectId || "will be created"}`);

  if (values["dry-run"]) {
    if (!queue.length) {
      ok("nothing to do — every selected sub-step is already checked");
      return 0;
    }
    console.log("\nqueue:");
    for (const phase of queue) console.log(`  ${phase.id.padEnd(24)} ${phase.label}`);
    return 0;
  }
  if (!queue.length) {
    ok("nothing to do — every selected sub-step is already checked");
    return 0;
  }

  const creds = resolveAdminCreds(state, {
    email: cfg.adminEmail,
    password: cfg.adminPassword,
    otp: values.totp,
  });
  if (!cfg.adminEmail && creds.email) cfg.adminEmail = creds.email;
  if (!cfg.adminPassword && creds.password) cfg.adminPassword = creds.password;
  if (cfg.adminEmail || cfg.adminPassword || cfg.token || cfg.subdomain) {
    await mutate((draft) => {
      draft.setup.auto = {
        ...(draft.setup.auto || {}),
        ...(cfg.adminEmail ? { adminEmail: cfg.adminEmail } : {}),
        ...(cfg.adminPassword ? { adminPassword: cfg.adminPassword } : {}),
        ...(cfg.token ? { token: cfg.token } : {}),
        ...(cfg.subdomain ? { subdomain: cfg.subdomain } : {}),
        ...(cfg.domain ? { domain: cfg.domain } : {}),
        ...(cfg.businessName ? { businessName: cfg.businessName } : {}),
        ...(cfg.recovery ? { recoveryEmail: cfg.recovery } : {}),
        ...(cfg.projectId ? { projectId: cfg.projectId } : {}),
      };
      if (cfg.subdomain) draft.duckdns.subdomain = cfg.subdomain;
      if (cfg.token) draft.duckdns.token = cfg.token;
      if (values.totp) {
        draft.totp = {
          ...(draft.totp || {}),
          email: cfg.adminEmail || draft.totp?.email || "",
          secret: values.totp,
        };
      }
    });
    state = await load();
  }

  const context = await launchContext({
    profile: values.profile,
    headless: values.headless,
    channel: values.channel,
    proxy: values.proxy || "",
  });
  let page = null;
  let domainBusy = false;
  const domainReady = { value: false };
  const domainWritten = { token: "" };

  const signals = new Map();
  const mark = (id, error = "") => signals.set(id, { done: !error, error });

  const ctx = {
    cfg,
    creds,
    context,
    domainReady,
    profile: {
      subdomain: cfg.subdomain,
      domain: cfg.domain,
      token: cfg.token,
      businessName: cfg.businessName,
      projectName: cfg.projectName,
      recoveryEmail: cfg.recovery,
      adminEmail: cfg.adminEmail,
      adminPassword: cfg.adminPassword,
      projectId: cfg.projectId,
    },
    pendingKey: null,
    cancelled: false,
    signals,
    seenChallenges: new Set(),
    state,
  };

  const handleDomainToken = async (token, domainFromMessage, sourcePage) => {
    if (domainBusy || domainWritten.token === token) return;
    if (!cfg.token || !cfg.subdomain) {
      mark("tenant.verify", "the DuckDNS token or subdomain is missing");
      return;
    }
    domainBusy = true;
    try {
      info(`writing TXT ${token.slice(0, 48)}… to ${cfg.domain}`);
      await writeTxt(cfg.token, cfg.subdomain, token);
      const result = await waitForTxt(cfg.domain || domainFromMessage, token, 150_000, (answers, rounds) => {
        if (ctx.cancelled) throw new Error("cancelled");
        if (rounds % 6 === 0) info(`waiting for the TXT record (${rounds} lookups)`);
      });
      if (!result.found) {
        mark("tenant.verify", "the TXT record did not appear in DNS within 150s");
        return;
      }
      domainWritten.token = token;
      domainReady.value = true;
      ok("TXT record is visible in DNS");
      const target = page || sourcePage;
      await sendSetupDomainReady(target);
    } catch (error) {
      mark("tenant.verify", error.message || String(error));
    } finally {
      domainBusy = false;
    }
  };

  const onMessage = async (message, sourcePage) => {
    if (!message || typeof message !== "object") return;
    if (message.type === "setup-domain" && message.token) {
      void handleDomainToken(message.token, message.domain || "", sourcePage);
      return;
    }
    if (message.type === "setup-client" && message.client_id) {
      const key = normaliseKey(
        {
          client_id: message.client_id,
          client_secret: message.client_secret,
          domain: cfg.domain,
          admin: cfg.adminEmail,
          project_id: message.project || cfg.projectId,
        },
        "setup"
      );
      await mutate((draft) => {
        const existing = draft.keys.find((entry) => entry.client_id === key.client_id);
        if (existing) {
          ctx.pendingKey = existing.id;
          return;
        }
        draft.keys.push(key);
        ctx.pendingKey = key.id;
      });
      ok(`OAuth client ${message.client_id} saved to keys`);
      mark("key.client");
      return;
    }
    if (message.type === "setup-project" && message.project) {
      cfg.projectId = message.project;
      ctx.profile.projectId = message.project;
      await mutate((draft) => {
        draft.setup.auto.projectId = message.project;
      });
      ok(`Cloud project: ${message.project}`);
      mark("key.project");
      return;
    }
    if (message.type === "setup-domain-status" && message.verified) {
      mark("tenant.verify");
      return;
    }
    if (message.type === "setup-domain-verified") {
      mark("tenant.verify");
      return;
    }
    if (message.type === "setup-billing-done") {
      mark("tenant.cloud-identity");
      return;
    }
    if (message.type === "setup-policy-done") {
      mark("twostep.policy");
      return;
    }
    if (message.type === "setup-api-enabled") {
      mark("key.admin-sdk");
      return;
    }
    if (message.type === "setup-consent-ready") {
      mark("key.consent");
      return;
    }
    if (message.type === "setup-scopes-ready") {
      mark("key.scopes");
      return;
    }
    if (message.type === "setup-scopes-failed") {
      mark("key.scopes", message.error || "scope automation failed");
      return;
    }
    if (message.type === "setup-report" || message.type === "log") {
      await handleAuthMessage(message, sourcePage);
      return;
    }
    if (message.type === "setup-admin" && message.email) {
      cfg.adminEmail = String(message.email).toLowerCase();
      ctx.creds.email = cfg.adminEmail;
      ctx.profile.adminEmail = cfg.adminEmail;
      ctx.profile.recoveryEmail = cfg.recovery || cfg.adminEmail;
      await mutate((draft) => {
        draft.setup.auto.adminEmail = cfg.adminEmail;
        draft.setup.auto.domain = String(message.domain || cfg.domain).toLowerCase();
      });
      return;
    }
    if (message.type === "identity-totp" && message.enrolled) {
      mark("twostep.enroll");
    }
    if (message.type === "google-2sv" && message.enrolled) {
      mark("twostep.enroll");
    }
    if (message.type === "setup-captcha") {
      warn("Google wants a captcha — solve it in the setup window; the run continues after that");
      await actionNeeded(sourcePage, {
        title: "Solve the captcha",
        lines: ["Google wants a captcha in the setup window. I'll continue after it."],
      }).catch(() => {});
      return;
    }
    await handleAuthMessage(message, sourcePage);
  };

  try {
    await installContentBridge(context, { scripts: ["google", "setup"], onMessage });
    page = await openPage(context, AUTO_ADMIN);
    attachLogging(page, { prefix: "setup", verbose: values.verbose });

    if (cfg.adminEmail) {
      info(`signing in as ${cfg.adminEmail}…`);
      await driveGoogle(page, creds, {
        timeout: Number(values["login-timeout"]) * 1000,
        label: "admin sign-in",
        until: () => hostOf(page.url()) === "admin.google.com",
      });
    } else {
      warn("no admin email saved — sign in as the tenant admin in the browser window");
      await waitUntil(() => hostOf(page.url()) === "admin.google.com", {
        timeout: Number(values["login-timeout"]) * 1000,
        interval: 1000,
        label: "admin sign-in",
      });
    }
    ok("admin session ready");
    await page.waitForTimeout(1500);

    for (const phase of queue) {
      state = await load();
      ctx.state = state;
      if (!values.force && state.setup?.done?.[phase.id]) {
        info(`${phase.id}: already checked`);
        continue;
      }
      if (phase.id === "twostep.policy" && state.setup?.auto?.policySafe === false) {
        warn(
          "twostep.policy: skipped — the admin already has 2SV but no authenticator " +
            "secret is saved; changing the policy could lock the admin out"
        );
        continue;
      }
      try {
        const result = await runPhase(page, phase, ctx);
        await mutate((draft) => {
          draft.setup.done[phase.id] = true;
          draft.setup.auto.phase = "";
        });
        ok(`${phase.id}: done${result?.detail ? ` — ${result.detail}` : ""}`);
      } catch (error) {
        await mutate((draft) => {
          draft.setup.auto.phase = phase.id;
          draft.setup.auto.lastError = String(error.message || error).slice(0, 400);
        });
        const dir = await saveDebug(page, `setup-${phase.id}`).catch(() => "");
        if (dir) warn(`debug saved to ${dir}`);
        throw new Error(`${phase.id}: ${error.message || error}`);
      }
    }

    ok("automatic setup finished");
    if (values["keep-open"]) {
      await stayOpen(context, { message: "setup finished — press Ctrl+C to close" });
    }
    return 0;
  } catch (error) {
    if (page) {
      const dir = await saveDebug(page, "setup").catch(() => "");
      if (dir) warn(`debug saved to ${dir}`);
    }
    if (values["keep-open"]) {
      await stayOpen(context, { message: "setup stopped — press Ctrl+C to close" });
    }
    throw error;
  } finally {
    ctx.cancelled = true;
    await context.close().catch(() => {});
  }
}
