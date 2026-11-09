import { parseArgs } from "node:util";
import * as admin from "../../extension/lib/admin.js";
import { makeIdentityFields } from "../../extension/lib/names.js";
import { checkKeyById, resolveKeyArg } from "../key-flow.js";
import { load, mutate, requireKey } from "../state.js";
import { confirm, info, ok, table, warn } from "../util.js";

const HELP = `rac identity <subcommand> [options]

Subcommands:
  create [options]     create one or more Google identities with the Admin SDK
  list                 list identities created by this CLI
  delete <email|all>   delete identities from the tenant
  status <email>       show the Google 2SV status for a user
  codes <email>        mint and print verification codes

Create options:
  --key <id>           key to use (default: first usable key)
  --count <n>          number of identities (default: 1, max: 50)
  --first <name>       override the given name
  --last <name>        override the family name
  --email <address>    override the generated email (count must be 1)
  --password <value>   override the generated password
  --prefix <text>      email local-part prefix (default: first.last)
`;

export const summary = "create and list Google identities with the Admin SDK";

function subcommand(argv) {
  const [name, ...rest] = argv;
  return { name: name || "list", rest };
}

async function cmdCreate(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      key: { type: "string" },
      count: { type: "string", default: "1" },
      first: { type: "string" },
      last: { type: "string" },
      email: { type: "string" },
      password: { type: "string" },
      prefix: { type: "string" },
      help: { type: "boolean", default: false },
    },
  });

  const key = values.key ? await requireKey(values.key) : await resolveKeyArg([], { usable: true });
  if (key.needs_consent) {
    throw new Error(`${key.label || key.id} needs authorization first: rac key authorize ${key.id}`);
  }
  const state = await load();
  const domain = String(key.domain || "").trim().toLowerCase() || (await admin.detectDomain(key));
  if (!domain) throw new Error("could not detect the tenant domain; set it with `rac key check`");

  const count = Math.max(1, Math.min(50, Number(values.count) || 1));
  if (values.email && count !== 1) throw new Error("--email only works with --count 1");
  if (!key.domain) await checkKeyById(key.id, { quiet: true });

  const created = [];
  for (let index = 0; index < count; index += 1) {
    const generated = makeIdentityFields(domain, state.identities.length + index + 1);
    const first = values.first || generated.first;
    const last = values.last || generated.last;
    const prefix = values.prefix || `${first}.${last}`.toLowerCase().replace(/[^a-z0-9._-]/g, "");
    const email = (values.email || `${prefix}${count > 1 ? state.identities.length + index + 1 : ""}@${domain}`).toLowerCase();
    const password = values.password || generated.password;
    const identity = {
      id: `id_${Date.now().toString(36)}${index.toString(36)}`,
      key_id: key.id,
      email,
      first,
      last,
      password,
      domain,
      status: "created",
      created_at: new Date().toISOString(),
    };
    await admin.insertUser(key, identity);
    await mutate((draft) => {
      draft.identities.push(identity);
    });
    created.push(identity);
    ok(`${email}  ${password}`);
  }

  info(`${created.length} identity/identities created in ${domain}`);
  return 0;
}

async function cmdList(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac identity list");
    return 0;
  }
  const state = await load();
  if (!state.identities.length) {
    info("no identities created by this CLI yet");
    return 0;
  }
  console.log(
    table(
      state.identities.map((identity) => ({
        email: identity.email,
        first: identity.first || "",
        last: identity.last || "",
        status: identity.status || "",
        created: String(identity.created_at || "").slice(0, 19),
      })),
      [
        { key: "email", label: "email", max: 36 },
        { key: "first", label: "first", max: 14 },
        { key: "last", label: "last", max: 14 },
        { key: "status", label: "status", max: 10 },
        { key: "created", label: "created", max: 19 },
      ]
    )
  );
  return 0;
}

async function cmdDelete(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac identity delete <email|all> [--key id] [--yes]");
    return 0;
  }
  const { values, positionals } = parseArgs({
    args: argv,
    options: {
      key: { type: "string" },
      yes: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
    allowPositionals: true,
  });
  const target = positionals[0];
  if (!target) throw new Error("give an email or `all`");
  const state = await load();
  const key = values.key
    ? await requireKey(values.key)
    : await resolveKeyArg([], { usable: true });
  const matches =
    target === "all"
      ? state.identities
      : state.identities.filter((identity) => identity.email.toLowerCase() === target.toLowerCase());
  if (!matches.length) throw new Error(`no identity matches ${target}`);
  if (!values.yes) {
    const yes = await confirm(`delete ${matches.length} identity/identities from Google?`);
    if (!yes) return 0;
  }
  const emails = new Set(matches.map((identity) => identity.email));
  let deleted = 0;
  for (const identity of matches) {
    const result = await admin.deleteUser(key, identity.email).catch((error) => {
      warn(`${identity.email}: ${error.message || error}`);
      return null;
    });
    if (result?.deleted) deleted += 1;
  }
  await mutate((draft) => {
    draft.identities = draft.identities.filter((identity) => !emails.has(identity.email));
  });
  ok(`deleted ${deleted}/${matches.length} identity/identities`);
  return 0;
}

async function cmdStatus(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac identity status <email> [--key id]");
    return 0;
  }
  const { values, positionals } = parseArgs({
    args: argv,
    options: { key: { type: "string" }, help: { type: "boolean", default: false } },
    allowPositionals: true,
  });
  const email = positionals[0];
  if (!email) throw new Error("give an email");
  const key = values.key ? await requireKey(values.key) : await resolveKeyArg([], { usable: true });
  const status = await admin.twoStepStatus(key, email);
  console.log(JSON.stringify(status, null, 2));
  return 0;
}

async function cmdCodes(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log("rac identity codes <email> [--key id]");
    return 0;
  }
  const { values, positionals } = parseArgs({
    args: argv,
    options: { key: { type: "string" }, help: { type: "boolean", default: false } },
    allowPositionals: true,
  });
  const email = positionals[0];
  if (!email) throw new Error("give an email");
  const key = values.key ? await requireKey(values.key) : await resolveKeyArg([], { usable: true });
  const codes = await admin.mintCodes(key, email);
  ok(`${codes.length} code(s) for ${email}`);
  console.log(codes.join("\n"));
  return 0;
}

export async function run(argv) {
  if (!argv.length || argv[0] === "--help" || argv[0] === "-h") {
    console.log(HELP);
    return 0;
  }
  const { name, rest } = subcommand(argv);
  switch (name) {
    case "create":
      return cmdCreate(rest);
    case "list":
      return cmdList(rest);
    case "delete":
    case "remove":
      return cmdDelete(rest);
    case "status":
      return cmdStatus(rest);
    case "codes":
      return cmdCodes(rest);
    case "--help":
    case "-h":
      console.log(HELP);
      return 0;
    default:
      throw new Error(`unknown identity subcommand: ${name} (try "rac identity --help")`);
  }
}
