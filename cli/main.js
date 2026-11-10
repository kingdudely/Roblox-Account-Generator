import { color } from "./util.js";
import { STATE_PATH, APP_HOME, PROFILE_ROOT } from "./paths.js";

import * as doctor from "./commands/doctor.js";
import * as smoke from "./commands/smoke.js";
import * as admin from "./commands/admin.js";
import * as duckdns from "./commands/duckdns.js";
import * as signup from "./commands/signup.js";
import * as setup from "./commands/setup.js";
import * as key from "./commands/key.js";
import * as identity from "./commands/identity.js";
import * as runCommand from "./commands/run.js";
import * as exportCommand from "./commands/export.js";
import * as stateCommand from "./commands/state.js";
import * as start from "./commands/start.js";
import * as status from "./commands/status.js";

const COMMANDS = {
  start,
  status,
  doctor,
  smoke,
  admin,
  duckdns,
  signup,
  setup,
  key,
  identity,
  run: runCommand,
  export: exportCommand,
  state: stateCommand,
};

const ALIASES = {
  create: "start",
  up: "start",
  auto: "start",
  next: "start",
  continue: "start",
  progress: "status",
  accounts: "run",
};

function help() {
  console.log(`${color.bold("rac")} — Google tenant → Roblox account

Start here:
  rac start                 create/repair the tenant, key and account(s)
  rac status                show progress and the next step

Everyday:
  rac run --count 5         create Roblox accounts with a ready key
  rac export                write accounts.txt
  rac key ...               manage OAuth keys
  rac identity ...          manage Google identities

One-time setup pieces:
  rac start --tenant-only   stop after the OAuth key is ready
  rac signup                just the Google signup form
  rac duckdns               just the DuckDNS record
  rac setup                 just the tenant/key steps
  rac admin                 open the Admin console

Checks:
  doctor, smoke, state

Paths:
  home       ${APP_HOME}
  state      ${STATE_PATH}
  profiles   ${PROFILE_ROOT}

Run "rac <command> --help" for options.
`);
}

export async function main(argv) {
  const [name, ...rest] = argv;
  if (!name) {
    return status.run([]);
  }
  if (name === "help" || name === "--help" || name === "-h") {
    help();
    return 0;
  }
  if (name === "--version" || name === "version") {
    console.log("rac 0.10.0");
    return 0;
  }
  const resolved = ALIASES[name] || name;
  const command = COMMANDS[resolved];
  if (!command) {
    throw new Error(`unknown command: ${name} (try "rac help")`);
  }
  return command.run(rest);
}
