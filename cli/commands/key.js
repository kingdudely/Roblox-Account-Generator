import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { attachLogging, launchContext, saveDebug, stayOpen } from "../browser.js";
import { installContentBridge } from "../content-bridge.js";
import { authorizeKeyWithContext } from "../consent.js";
import { resolveAdminCreds } from "../google.js";
import { handleAuthMessage } from "../messages.js";
import { load, mutate, requireKey } from "../state.js";
import { confirm, info, ok, readStdin, table, warn } from "../util.js";
import {
  checkAllKeys,
  checkKeyById,
  importKeysText,
  keyJson,
  keySummary,
  recoverAdminWithKey,
  redactKey,
} from "../key-flow.js";

const HELP = `rac key <subcommand> [options]

Subcommands:
  import [file|-]     import OAuth clients, refresh tokens or service accounts
  list                list stored keys
  show [id]           show one key (secrets masked unless --secrets)
  check [id|all]      verify a key against the Admin SDK
  authorize [id]      run the OAuth consent flow and store a refresh token
                      (use --manual to sign in yourself)
  recover [id]        reset the admin password and mint backup codes
  remove <id|all>     delete a key
  export [id|all]     print or write usable key JSON

Import options:
  --domain <domain>   set the domain when the key JSON has none
  --admin <address>   set the admin address when the key JSON has none
  --no-check          do not call the Admin SDK after import

Authorize options:
  --email, --password, --totp      admin credentials for the consent page
  --profile <name>    browser profile (default: setup)
  --headless          run headless
  --channel <name>    Chrome channel (default: chrome)
  --proxy <url>       proxy
  --timeout <seconds> consent timeout (default: 600)
  --keep-open         leave the browser open after success
  --verbose           print page console output

Show/export options:
  --secrets           include client_secret/refresh_token/private_key
  --json              machine-readable output

Run "rac key <subcommand> --help" for the full option list.
`;

const SUBHELP = {
  import: `rac key import [file|-] [options]

Reads a JSON credential (installed/web wrapper), a JSON array, one JSON object
per line, or "client_id client_secret refresh_token" lines.

  --domain <domain>   fallback tenant domain
  --admin <address>   fallback admin address
  --no-check          skip the Admin SDK check after import
`,
  authorize: `rac key authorize [id] [options]

Runs the OAuth consent flow in a persistent Patchright profile. The key must be
an OAuth client with client_id and client_secret.

  --email <address>   admin email
  --password <value>  admin password
  --totp <secret>     base32 authenticator secret
  --manual            ignore saved admin credentials; sign in in the browser
  --profile <name>    browser profile (default: setup)
  --headless          run headless
  --channel <name>    Chrome channel (default: chrome)
  --proxy <url>       proxy
  --timeout <seconds> consent timeout (default: 600)
  --keep-open         leave the browser open after success
  --verbose           print page console output
`,
  recover: `rac key recover [id] [--email admin@domain]

Resets the target admin password with the key's Admin SDK access and mints new
backup codes. The new password and codes are saved to the local state file.
`,
};

function parse(argv, options) {
  return parseArgs({ args: argv, options, allowPositionals: true });
}

function subcommand(argv) {
  const [name, ...rest] = argv;
  return { name: name || "list", rest };
}

async function readInput(file) {
  if (!file || file === "-") return readStdin();
  return fs.readFile(path.resolve(file), "utf8");
}

async function cmdImport(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(SUBHELP.import);
    return 0;
  }
  const { values, positionals } = parse(argv, {
    domain: { type: "string" },
    admin: { type: "string" },
    "no-check": { type: "boolean", default: false },
    help: { type: "boolean", default: false },
  });
  const text = await readInput(positionals[0]);
  const result = await importKeysText(text, {
    domain: values.domain || "",
    adminEmail: values.admin || "",
    source: positionals[0] ? path.basename(positionals[0]) : "stdin",
  });
  if (!values["no-check"]) {
    let failed = 0;
    for (const id of result.ids) {
      const key = await requireKey(id);
      if (!key.needs_consent) {
        const checked = await checkKeyById(key);
        if (!checked.ok) failed += 1;
      }
    }
    if (failed) return 1;
  }
  return 0;
}

async function cmdList(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac key list\n\nLists stored keys without secrets.");
    return 0;
  }
  const state = await load();
  if (!state.keys.length) {
    info("no keys stored — run `rac key import <file>`");
    return 0;
  }
  console.log(
    table(state.keys.map(keySummary), [
      { key: "id", label: "id", max: 18 },
      { key: "label", label: "label", max: 34 },
      { key: "kind", label: "kind", max: 15 },
      { key: "status", label: "status", max: 12 },
      { key: "domain", label: "domain", max: 28 },
      { key: "seats", label: "seats", max: 6 },
      { key: "consent", label: "consent", max: 16 },
    ])
  );
  return 0;
}

async function cmdShow(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac key show [id] [--secrets] [--json]");
    return 0;
  }
  const { values, positionals } = parse(argv, {
    secrets: { type: "boolean", default: false },
    json: { type: "boolean", default: false },
    help: { type: "boolean", default: false },
  });
  const key = positionals[0]
    ? await requireKey(positionals[0])
    : await load().then((state) => state.keys[state.keys.length - 1]);
  if (!key) throw new Error("no keys stored");
  if (values.json) {
    const payload = values.secrets
      ? { ...key, ...keyJson(key, { secrets: true }) }
      : { ...redactKey(key), ...keyJson(key, { secrets: false }) };
    console.log(JSON.stringify(payload, null, 2));
    return 0;
  }
  const publicKey = keyJson(key, { secrets: values.secrets });
  console.log(`${colorLabel("id")}          ${key.id}`);
  console.log(`${colorLabel("label")}       ${key.label || ""}`);
  console.log(`${colorLabel("kind")}        ${key.kind || "oauth"}`);
  console.log(`${colorLabel("status")}      ${key.status || ""}`);
  console.log(`${colorLabel("domain")}      ${key.domain || ""}`);
  console.log(`${colorLabel("admin")}       ${key.admin || key.admin_email || ""}`);
  console.log(`${colorLabel("seats")}       ${key.seats ?? ""}`);
  console.log(`${colorLabel("consent")}     ${key.needs_consent ? "needs authorize" : "ready"}`);
  console.log(`${colorLabel("client_id")}   ${publicKey.client_id || key.client_email || ""}`);
  console.log(`${colorLabel("client_secret")} ${publicKey.client_secret || ""}`);
  console.log(`${colorLabel("refresh")}     ${publicKey.refresh_token || ""}`);
  if (key.message) console.log(`${colorLabel("message")}     ${key.message}`);
  return 0;
}

async function cmdCheck(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac key check [id|all]");
    return 0;
  }
  const { positionals } = parse(argv, {});
  const target = positionals[0];
  if (!target || target === "all") {
    const results = await checkAllKeys();
    return results.some((entry) => !entry.ok) ? 1 : 0;
  }
  const result = await checkKeyById(target);
  return result.ok ? 0 : 1;
}

async function cmdAuthorize(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(SUBHELP.authorize);
    return 0;
  }
  const { values, positionals } = parse(argv, {
    email: { type: "string" },
    password: { type: "string" },
    totp: { type: "string" },
    manual: { type: "boolean", default: false },
    profile: { type: "string", default: "setup" },
    headless: { type: "boolean", default: false },
    channel: { type: "string", default: "chrome" },
    proxy: { type: "string" },
    timeout: { type: "string", default: "600" },
    "keep-open": { type: "boolean", default: false },
    verbose: { type: "boolean", default: false },
    help: { type: "boolean", default: false },
  });

  const state = await load();
  const key = positionals[0]
    ? await requireKey(positionals[0])
    : state.keys.find((entry) => entry.needs_consent) || state.keys[state.keys.length - 1];
  if (!key) throw new Error("no key configured — run `rac key import <file>` first");
  if (key.kind === "service_account") throw new Error("service accounts do not need consent");
  if (!key.client_id || !key.client_secret) throw new Error("this key has no client_id/client_secret");

  const creds = values.manual
    ? { email: "", password: "", otp: "", codes: [], domain: "", adminUser: "" }
    : resolveAdminCreds(state, {
        email: values.email,
        password: values.password,
        otp: values.totp,
      });
  if (creds.email) {
    await mutate((draft) => {
      draft.setup.auto.adminEmail = creds.email;
      if (creds.password) draft.setup.auto.adminPassword = creds.password;
      if (creds.domain) draft.setup.auto.domain = creds.domain;
    });
  }
  info(`authorizing ${key.label || key.id}`);
  info(`admin: ${creds.email || "not configured — sign in in the browser"}`);

  const context = await launchContext({
    profile: values.profile,
    headless: values.headless,
    channel: values.channel,
    proxy: values.proxy || "",
  });
  try {
    await installContentBridge(context, {
      scripts: ["google"],
      onMessage: (message, page) => handleAuthMessage(message, page),
    });

    const logged = new WeakSet();
    const credential = await authorizeKeyWithContext(context, key, {
      creds: values.manual ? null : creds,
      email: values.manual ? "" : creds.email,
      timeout: Number(values.timeout) * 1000,
      keepOpen: values["keep-open"],
      onTick: (page) => {
        if (logged.has(page)) return;
        logged.add(page);
        attachLogging(page, { prefix: "consent", verbose: values.verbose });
      },
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

    ok(`${key.label || key.id}: consent granted`);
    await checkKeyById(key.id);
    if (values["keep-open"]) {
      await stayOpen(context, { message: "consent completed — press Ctrl+C to close" });
    }
    return 0;
  } catch (error) {
    const page = context.pages()[0];
    if (page) {
      const dir = await saveDebug(page, "key-authorize").catch(() => "");
      if (dir) warn(`debug saved to ${dir}`);
    }
    throw error;
  } finally {
    await context.close().catch(() => {});
  }
}

async function cmdRecover(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(SUBHELP.recover);
    return 0;
  }
  const { values, positionals } = parse(argv, {
    email: { type: "string" },
    json: { type: "boolean", default: false },
    help: { type: "boolean", default: false },
  });
  const key = positionals[0]
    ? await requireKey(positionals[0])
    : (await load()).keys.find((entry) => !entry.needs_consent);
  if (!key) throw new Error("no usable key — import and authorize one first");
  const result = await recoverAdminWithKey(key, values.email);
  if (values.json) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(`admin:    ${result.target}`);
    console.log(`password: ${result.password}`);
    if (result.codes.length) console.log(`codes:\n${result.codes.map((code) => `  ${code}`).join("\n")}`);
    warn("store these now — the old password no longer works");
  }
  return 0;
}

async function cmdRemove(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac key remove <id|all> [--yes]");
    return 0;
  }
  const { values, positionals } = parse(argv, {
    yes: { type: "boolean", default: false },
    help: { type: "boolean", default: false },
  });
  const target = positionals[0];
  if (!target) throw new Error("give a key id, a unique prefix, or `all`");
  const state = await load();
  const matches =
    target === "all"
      ? state.keys
      : state.keys.filter((key) => key.id === target || key.id.startsWith(target));
  if (!matches.length) throw new Error(`no key matches ${target}`);
  if (!values.yes) {
    const yes = await confirm(`delete ${matches.length} key(s)?`);
    if (!yes) return 0;
  }
  const ids = new Set(matches.map((key) => key.id));
  await mutate((draft) => {
    draft.keys = draft.keys.filter((key) => !ids.has(key.id));
  });
  ok(`deleted ${matches.length} key(s)`);
  return 0;
}

async function cmdExport(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac key export [id|all] [--out <file>] [--no-secrets]");
    return 0;
  }
  const { values, positionals } = parse(argv, {
    out: { type: "string" },
    "no-secrets": { type: "boolean", default: false },
    help: { type: "boolean", default: false },
  });
  const state = await load();
  const target = positionals[0];
  const keys =
    !target || target === "all"
      ? state.keys
      : state.keys.filter((key) => key.id === target || key.id.startsWith(target));
  if (!keys.length) throw new Error("no keys to export");
  const includeSecrets = !values["no-secrets"];
  const payload =
    keys.length === 1
      ? keyJson(keys[0], { secrets: includeSecrets })
      : keys.map((key) => keyJson(key, { secrets: includeSecrets }));
  const text = `${JSON.stringify(payload, null, 2)}\n`;
  if (values.out) {
    await fs.writeFile(path.resolve(values.out), text, { mode: 0o600 });
    ok(`wrote ${keys.length} key(s) to ${path.resolve(values.out)}`);
  } else {
    process.stdout.write(text);
  }
  return 0;
}

function colorLabel(label) {
  return `${label}:`.padEnd(14);
}

export const summary = "import, list, check, authorize, recover and remove keys";

export async function run(argv) {
  const { name, rest } = subcommand(argv);
  if (!argv.length || name === "--help" || name === "-h") {
    console.log(HELP);
    return 0;
  }
  switch (name) {
    case "import":
      return cmdImport(rest);
    case "list":
      return cmdList(rest);
    case "show":
      return cmdShow(rest);
    case "check":
      return cmdCheck(rest);
    case "authorize":
      return cmdAuthorize(rest);
    case "recover":
      return cmdRecover(rest);
    case "remove":
    case "delete":
      return cmdRemove(rest);
    case "export":
      return cmdExport(rest);
    default:
      throw new Error(`unknown key subcommand: ${name} (try "rac key --help")`);
  }
}
