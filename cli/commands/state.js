import { parseArgs } from "node:util";
import { APP_HOME, PROFILE_ROOT, STATE_PATH } from "../paths.js";
import { load, mutate } from "../state.js";
import { confirm, ok, table, warn } from "../util.js";

const HELP = `rac state [options]

Options:
  --json       print the raw state document
  --help       show this help

Subcommands:
  rac state scrub [--yes]   clear admin password/company/name/phone
                            (keeps the key, admin email, domain and TOTP)
`;

export const summary = "show the state path and a short summary";

async function scrub(argv) {
  const { values } = parseArgs({
    args: argv,
    options: {
      yes: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });
  if (values.help) {
    console.log("rac state scrub [--yes]\n\nClears the admin password, company name, first/last name and phone. Keeps keys, admin email, domain, DuckDNS token and TOTP secret.");
    return 0;
  }
  if (!values.yes) {
    if (!process.stdin.isTTY) {
      throw new Error("pass --yes to scrub state in a non-interactive session");
    }
    const yes = await confirm(
      "clear the admin password, company name, first/last name and phone?"
    );
    if (!yes) return 0;
  }
  await mutate((draft) => {
    draft.setup.auto.adminPassword = "";
    draft.setup.auto.businessName = "";
    draft.setup.auto.firstName = "";
    draft.setup.auto.lastName = "";
    draft.settings.phoneNumber = "";
    draft.phone = { number: "", body: "", payload: "", source: "" };
  });
  ok("tenant secrets cleared; key JSON, admin email, domain, DuckDNS token and TOTP kept");
  warn("the admin password is gone; use `rac key recover` if you need it again");
  return 0;
}

export async function run(argv) {
  if (argv[0] === "scrub") return scrub(argv.slice(1));
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }

  const { values } = parseArgs({
    args: argv,
    options: {
      json: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });
  const state = await load();
  if (values.json) {
    console.log(JSON.stringify(state, null, 2));
    return 0;
  }

  console.log(`home:      ${APP_HOME}`);
  console.log(`state:     ${STATE_PATH}`);
  console.log(`profiles:  ${PROFILE_ROOT}`);
  console.log(`keys:      ${state.keys.length}`);
  console.log(`identities: ${state.identities.length}`);
  console.log(`accounts:  ${state.accounts.length}`);

  const done = Object.entries(state.setup?.done || {})
    .filter(([, value]) => value)
    .map(([id]) => id);
  console.log(`setup:     ${done.length ? done.join(", ") : "nothing checked"}`);
  console.log(`duckdns:   ${state.duckdns?.subdomain || "not set"}${state.duckdns?.token ? " (token saved)" : ""}`);
  console.log(`admin:     ${state.setup?.auto?.adminEmail || "not set"}`);
  console.log(`2sv:       ${state.totp?.secret ? `secret saved${state.totp.email ? ` for ${state.totp.email}` : ""}` : "no secret"}`);

  if (state.keys.length) {
    console.log("");
    console.log(
      table(
        state.keys.map((key) => ({
          label: key.label || key.id,
          status: key.status || (key.needs_consent ? "needs authorize" : "new"),
          domain: key.domain || "",
        })),
        [
          { key: "label", label: "label", max: 34 },
          { key: "status", label: "status", max: 16 },
          { key: "domain", label: "domain", max: 30 },
        ]
      )
    );
  }
  return 0;
}
