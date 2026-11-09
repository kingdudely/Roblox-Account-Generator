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

const COMMANDS = {
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

function help() {
  console.log(`${color.bold("rac")} — Roblox Alt Creator CLI

Usage:
  rac <command> [options]

Commands:
  doctor      check Node, Patchright, Chrome, profiles and trusted input
  smoke       verify a trusted click and navigator.webdriver
  admin       open or sign in to the Google Admin console
  duckdns     open DuckDNS, add a subdomain and capture the token
  signup      prefill the Cloud Identity signup and stop at the phone gate
  setup       run the automatic tenant and key sub-steps
  key         import, list, check, authorize, recover and remove keys
  identity    create and list Google identities with the Admin SDK
  run         create identities and run Roblox Google-SSO lanes
  export      export accounts to a text file
  state       show the state path and a short summary

Global paths:
  home        ${APP_HOME}
  state       ${STATE_PATH}
  profiles    ${PROFILE_ROOT}

Run "rac <command> --help" for command options.
`);
}

export async function main(argv) {
  const [name, ...rest] = argv;
  if (!name || name === "help" || name === "--help" || name === "-h") {
    help();
    return 0;
  }
  if (name === "--version" || name === "version") {
    console.log("rac 0.9.0");
    return 0;
  }
  const command = COMMANDS[name];
  if (!command) {
    throw new Error(`unknown command: ${name} (try "rac help")`);
  }
  return command.run(rest);
}
