import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { makeAdminPassword } from "../../extension/lib/names.js";
import { buildPlan, statusModel, usableKey } from "../flow/plan.js";
import {
  checkSubdomainAvailability,
  cleanDomain,
  cleanSubdomain,
  fullDomain,
  pickSubdomain,
  titleCase,
} from "../flow/domain.js";
import { load, mutate } from "../state.js";
import { ask, color, error, info, ok, phase, warn } from "../util.js";

const BIN = fileURLToPath(new URL("../../bin/rac.js", import.meta.url));

const HELP = `rac start [options]

The one command that builds a tenant from nothing:
  1. picks a free DuckDNS subdomain and checks DNS
  2. creates the Cloud Identity tenant with admin@<sub>.duckdns.org
     (the DuckDNS record does not need to exist yet)
  3. pauses only for Google's phone/QR check
  4. uses the tenant admin account to create the DuckDNS record and token
  5. verifies the domain, finishes the tenant and authorizes the OAuth key
  6. optionally creates Roblox account(s)

Options:
  --tenant-only          stop after the OAuth key is ready
  --accounts <n>         Roblox accounts to create after setup (default: 1)
  --no-accounts          same as --tenant-only
  --subdomain <name>     DuckDNS name to use (default: generated and DNS-checked)
  --domain <domain>      override the tenant domain
  --token <token>        existing DuckDNS account token
  --contact <email>      recovery email (required once; saved after that)
  --admin <user>         admin local-part (default: admin)
  --password <value>     admin password (default: generated and saved)
  --phone <number>       your phone, only for the gate that texts YOU a code
  --totp <secret>        existing admin authenticator secret
  --project <id>         reuse an existing Cloud project
  --from <step>          force a step: signup, duckdns, setup, accounts
  --dry-run              print the plan and exit
  --yes                  use saved values without prompting
  --force-subdomain      keep a subdomain even if its DuckDNS record exists
  --profile <name>       browser profile (default: setup)
  --channel <name>       Chrome channel (default: chrome)
  --proxy <url>          proxy for every browser step
  --headless             not recommended on the first run (phone gates need a window)
  --keep-open            keep the lane browsers open after the Roblox run
  --verbose              print page console output
  --help                 show this help

Examples:
  npm run create-tenant
  rac start --tenant-only
  rac start --accounts 5
  rac start --subdomain myname --contact me@gmail.com
  rac start --from setup
`;

export const summary = "create a tenant, key and Roblox account in one command";

function validEmail(value) {
  return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(String(value || "").trim());
}

function cleanAdminUser(value) {
  return String(value || "admin")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]/g, "") || "admin";
}

function runChild(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [BIN, command, ...args], {
      stdio: "inherit",
      env: process.env,
    });
    const onInt = () => {
      try {
        child.kill("SIGINT");
      } catch (_) {
        // already gone
      }
    };
    const onTerm = () => {
      try {
        child.kill("SIGTERM");
      } catch (_) {
        // already gone
      }
    };
    const cleanup = () => {
      process.removeListener("SIGINT", onInt);
      process.removeListener("SIGTERM", onTerm);
    };
    process.once("SIGINT", onInt);
    process.once("SIGTERM", onTerm);
    child.once("error", (err) => {
      cleanup();
      reject(err);
    });
    child.once("exit", (code, signal) => {
      cleanup();
      resolve(signal ? 130 : code ?? 1);
    });
  });
}

async function resolveConfig(values, { persist = true } = {}) {
  const state = await load();
  const saved = state.setup?.auto || {};
  const previousSubdomain = cleanSubdomain(
    state.duckdns?.subdomain ||
      saved.subdomain ||
      (/\.duckdns\.org$/i.test(String(saved.domain || "")) ? saved.domain : "")
  );
  const tenantExists = Boolean(
    state.setup?.done?.["tenant.signup"] ||
      state.setup?.done?.["tenant.verify"] ||
      (state.keys || []).length
  );

  let token = String(values.token || state.duckdns?.token || saved.token || "").trim();
  let subdomain = cleanSubdomain(values.subdomain || previousSubdomain);
  if (!subdomain) {
    const savedDomain = String(saved.domain || state.keys?.[0]?.domain || "");
    if (/\.duckdns\.org$/i.test(savedDomain)) subdomain = cleanSubdomain(savedDomain);
  }
  if (tenantExists && !subdomain) {
    throw new Error(
      "a tenant/key is already saved but no DuckDNS subdomain is recorded — " +
        "use `rac run` with the existing key, or use a separate RAC_HOME for a new tenant"
    );
  }
  if (token && !subdomain) {
    throw new Error("a DuckDNS token also needs its --subdomain");
  }

  if (subdomain && tenantExists && values.subdomain && subdomain !== previousSubdomain) {
    throw new Error(
      `a tenant already exists for ${previousSubdomain || "the saved domain"}; ` +
        "use a separate RAC_HOME (or delete state.json) to create another tenant"
    );
  }

  if (!subdomain) {
    const picked = await pickSubdomain({ requested: "", token });
    subdomain = picked.subdomain;
    info(`DuckDNS subdomain: ${picked.domain} (generated, DNS-checked)`);
    if (picked.check?.unknown) warn("DNS check was inconclusive; continuing with the generated name");
  } else if (!token && !tenantExists && !values["force-subdomain"] && !values.from) {
    const check = await checkSubdomainAvailability(subdomain);
    if (!check.available && !check.unknown) {
      const picked = await pickSubdomain({ requested: "", token: "" });
      warn(
        `${subdomain}.duckdns.org already has ${check.records.join(", ")} records; ` +
          `using ${picked.domain} instead`
      );
      subdomain = picked.subdomain;
    } else if (check.unknown) {
      warn(`could not confirm whether ${subdomain}.duckdns.org is free; continuing`);
    } else {
      info(`DuckDNS subdomain: ${subdomain}.duckdns.org (free)`);
    }
  }

  const domain = cleanDomain(values.domain) || fullDomain(subdomain);
  if (!/\.duckdns\.org$/i.test(domain)) {
    throw new Error(
      "rac start uses DuckDNS for automatic TXT verification; " +
        "custom domains need manual DNS, so run `rac signup` and `rac setup` separately"
    );
  }

  let contact = String(
    values.contact || saved.recoveryEmail || process.env.RAC_CONTACT_EMAIL || ""
  ).trim();
  if (!validEmail(contact)) {
    if (process.stdin.isTTY && !values.yes) {
      console.log("");
      console.log("Google needs a recovery email that is not inside the tenant domain.");
      while (!validEmail(contact)) {
        contact = String(await ask("Recovery email: ")).trim();
      }
    } else {
      throw new Error(
        "a real recovery email is required for the first run: " +
          "pass --contact you@example.com or set RAC_CONTACT_EMAIL"
      );
    }
  }

  const adminUser = cleanAdminUser(values.admin || saved.adminUser || "admin");
  const switchingTenant = Boolean(
    previousSubdomain && subdomain && previousSubdomain !== subdomain && !tenantExists
  );
  const generatedPassword =
    !values.password && (!saved.adminPassword || switchingTenant);
  const password = String(
    values.password || (!switchingTenant && saved.adminPassword) || makeAdminPassword()
  );
  const business = String(
    values.business || saved.businessName || titleCase(subdomain) || "RAC tenant"
  );
  const first = String(values.first || saved.firstName || "Camille");
  const last = String(values.last || saved.lastName || "Bianchi");
  const phone = String(values.phone || process.env.RAC_PHONE || state.settings?.phoneNumber || "").trim();
  const totp = String(values.totp || process.env.RAC_TOTP || state.totp?.secret || "").trim();
  const project = String(values.project || saved.projectId || "").trim();
  const adminEmail = `${adminUser}@${domain}`;

  const nextAuto = {
    ...(state.setup?.auto || {}),
    businessName: business,
    firstName: first,
    lastName: last,
    adminUser,
    adminEmail,
    adminPassword: password,
    recoveryEmail: contact,
    domain,
    subdomain,
    ...(token ? { token } : {}),
    ...(project ? { projectId: project } : {}),
  };
  if (switchingTenant) {
    nextAuto.projectId = "";
    nextAuto.policySafe = true;
  }
  const nextDuckdns = {
    ...(state.duckdns || {}),
    subdomain,
    ...(token ? { token } : {}),
  };
  const planState = {
    ...state,
    duckdns: nextDuckdns,
    settings: {
      ...(state.settings || {}),
      ...(phone ? { phoneNumber: phone } : {}),
    },
    setup: {
      ...(state.setup || {}),
      ...(switchingTenant ? { done: {}, open: {} } : {}),
      auto: nextAuto,
    },
  };

  if (persist) {
    await mutate((draft) => {
      if (switchingTenant) {
        draft.setup.done = {};
        draft.setup.auto.projectId = "";
        draft.setup.auto.policySafe = true;
      }
      draft.setup.auto = nextAuto;
      draft.duckdns = nextDuckdns;
      if (phone) draft.settings.phoneNumber = phone;
    });
  }

  return {
    state: persist ? await load() : planState,
    subdomain,
    domain,
    token,
    contact,
    adminUser,
    adminEmail,
    password,
    generatedPassword,
    business,
    first,
    last,
    phone,
    totp,
    project,
  };
}

function commonBrowserArgs(values) {
  const args = [];
  if (values.profile) args.push("--profile", values.profile);
  if (values.channel) args.push("--channel", values.channel);
  if (values.proxy) args.push("--proxy", values.proxy);
  if (values.headless) args.push("--headless");
  if (values.verbose) args.push("--verbose");
  return args;
}

function signupArgs(cfg, values) {
  return [
    "--subdomain",
    cfg.subdomain,
    "--domain",
    cfg.domain,
    "--contact",
    cfg.contact,
    "--admin",
    cfg.adminUser,
    "--business",
    cfg.business,
    "--first",
    cfg.first,
    "--last",
    cfg.last,
    "--employees",
    values.employees || "just-you",
    "--gate-timeout",
    String(values["gate-timeout"] || 1800),
    ...(cfg.phone ? ["--phone", cfg.phone] : []),
    ...commonBrowserArgs(values),
  ];
}

function duckdnsArgs(cfg, values) {
  return [
    "--subdomain",
    cfg.subdomain,
    "--email",
    cfg.adminEmail,
    "--timeout",
    String(values["login-timeout"] || 600),
    ...(cfg.totp ? ["--totp", cfg.totp] : []),
    ...commonBrowserArgs(values),
  ];
}

function setupArgs(cfg, values) {
  return [
    "--subdomain",
    cfg.subdomain,
    "--token",
    cfg.token,
    "--domain",
    cfg.domain,
    "--admin-email",
    cfg.adminEmail,
    "--business",
    cfg.business,
    "--recovery",
    cfg.contact,
    "--login-timeout",
    String(values["login-timeout"] || 600),
    ...(cfg.project ? ["--project", cfg.project] : []),
    ...(cfg.totp ? ["--totp", cfg.totp] : []),
    ...commonBrowserArgs(values),
  ];
}

function runArgs(cfg, values, accounts) {
  const args = ["--count", String(accounts), "--lanes", String(values.lanes || 1)];
  if (values.channel) args.push("--channel", values.channel);
  if (values.proxy) args.push("--proxy", values.proxy);
  if (values.headless) args.push("--headless");
  if (values.verbose) args.push("--verbose");
  if (values["keep-open"]) args.push("--keep-open");
  return args;
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      "tenant-only": { type: "boolean", default: false },
      "no-accounts": { type: "boolean", default: false },
      accounts: { type: "string", default: "1" },
      subdomain: { type: "string" },
      domain: { type: "string" },
      token: { type: "string" },
      contact: { type: "string" },
      admin: { type: "string" },
      password: { type: "string" },
      phone: { type: "string" },
      totp: { type: "string" },
      project: { type: "string" },
      business: { type: "string" },
      first: { type: "string" },
      last: { type: "string" },
      employees: { type: "string", default: "just-you" },
      from: { type: "string" },
      "dry-run": { type: "boolean", default: false },
      yes: { type: "boolean", default: false },
      "force-subdomain": { type: "boolean", default: false },
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      "gate-timeout": { type: "string", default: "1800" },
      "login-timeout": { type: "string", default: "600" },
      lanes: { type: "string", default: "1" },
      "keep-open": { type: "boolean", default: false },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  if (values.headless) {
    warn("headless cannot complete Google's phone/QR gate; use a visible browser for the first run");
  }

  const cfg = await resolveConfig(values, { persist: !values["dry-run"] });
  const tenantOnly = values["tenant-only"] || values["no-accounts"];
  const requestedAccounts = Number(values.accounts);
  const accounts = tenantOnly
    ? 0
    : Math.max(0, Math.min(50, Number.isFinite(requestedAccounts) ? requestedAccounts : 1));
  const plan = buildPlan(cfg.state, {
    accounts,
    tenantOnly,
    from: values.from,
    fromProvided: Boolean(values.from),
  });
  if (plan[0]?.id === "accounts" && !usableKey(cfg.state)) {
    throw new Error("no authorized key yet — run `rac start` without `--from accounts` first");
  }

  console.log("");
  console.log(color.bold("rac start"));
  console.log(`  tenant   ${cfg.domain}`);
  console.log(`  admin    ${cfg.adminEmail}`);
  console.log(`  contact  ${cfg.contact}`);
  console.log(`  key      ${usableKey(cfg.state)?.label || "will be created"}`);
  console.log(
    `  plan     ${
      plan.length ? plan.map((step) => step.id).join(" → ") : "nothing to do"
    }`
  );
  console.log(
    color.dim("  Only Google's phone/QR check needs you; everything else is automatic.")
  );

  if (values["dry-run"]) {
    console.log("");
    for (const [index, step] of plan.entries()) {
      console.log(`  ${index + 1}. ${step.label}${step.forced ? " (forced)" : ""}`);
    }
    return 0;
  }

  if (!plan.length) {
    ok("everything selected is already done");
    const model = statusModel(cfg.state);
    console.log(`next: ${model.next}`);
    return 0;
  }

  let state = cfg.state;
  for (const [index, step] of plan.entries()) {
    phase(index + 1, plan.length, step.label);
    let code = 0;

    if (step.id === "signup") {
      code = await runChild("signup", signupArgs(cfg, values));
    } else if (step.id === "duckdns") {
      code = await runChild("duckdns", duckdnsArgs(cfg, values));
      state = await load();
      cfg.token = String(state.duckdns?.token || cfg.token || "").trim();
    } else if (step.id === "setup") {
      state = await load();
      cfg.token = String(state.duckdns?.token || cfg.token || "").trim();
      if (!cfg.token) {
        error("the DuckDNS token is still missing; finish the DuckDNS step first");
        return 2;
      }
      code = await runChild("setup", setupArgs(cfg, values));
    } else if (step.id === "accounts") {
      code = await runChild("run", runArgs(cfg, values, accounts));
    } else if (step.id === "domain") {
      // resolved before the plan was built
      code = 0;
    }

    if (code !== 0) {
      console.log("");
      if (step.id === "signup") {
        warn(
          "if you already created this tenant, run `rac start --from duckdns`; " +
            "if the name belongs to someone else, rerun with `--subdomain <new-name>`"
        );
      }
      if (step.id === "duckdns") {
        warn(
          "if DuckDNS rejected the tenant admin, run `rac duckdns` and sign in with any Google account, " +
            "then run `rac start` again"
        );
      }
      if (code === 2) {
        warn("that step needs you. Finish it in the browser, then run `npm run create-tenant` again.");
      } else {
        error(`${step.id} stopped with exit code ${code}`);
      }
      return code;
    }
    state = await load();
  }

  const finalState = await load();
  const model = statusModel(finalState);
  console.log("");
  console.log(color.green("━━━━━━━━━━━━━━━━ tenant ready ━━━━━━━━━━━━━━━━"));
  console.log(`  tenant   ${model.domain || cfg.domain}`);
  console.log(`  admin    ${model.admin || cfg.adminEmail}`);
  if (cfg.generatedPassword) {
    console.log(`  password ${cfg.password}`);
    warn("save that admin password and your state.json somewhere safe");
  }
  if (model.key) console.log(`  key      ${model.key.label || model.key.id}`);
  console.log(`  accounts ${model.accounts.live} live${model.accounts.pending ? `, ${model.accounts.pending} pending` : ""}`);
  if (accounts > 0) console.log(`  export   accounts.txt`);
  console.log("");
  console.log(`${color.bold("Next")}  ${model.next}`);
  console.log("");
  return 0;
}
