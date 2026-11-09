import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { load } from "../state.js";
import { info, ok } from "../util.js";

const HELP = `rac export [options]

Writes the accounts collected by the CLI to a plain text file.

Options:
  --out <file>    output path (default: accounts.txt)
  --json          write JSON instead of the human-readable block
  --help          show this help
`;

export const summary = "export accounts to a text file";

function accountText(account) {
  return [
    `email: ${account.email || ""}`,
    `password: ${account.password || ""}`,
    `username: ${account.username || ""}`,
    `birthday: ${account.birthday || ""}`,
    `status: ${account.status || "live"}`,
    account.google_deleted
      ? "note: Google identity was deleted after signup; use the Roblox cookie or the username/password"
      : `note: Google SSO - sign in with Google using ${account.email || ""}`,
    `created_at: ${account.created_at || ""}`,
    account.cookie ? `cookie: ${account.cookie}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

export async function exportAccounts({ out = "accounts.txt", json = false } = {}) {
  const state = await load();
  if (!state.accounts.length) {
    info("no accounts to export yet");
    return 0;
  }
  const file = path.resolve(out);
  const text = json
    ? `${JSON.stringify(state.accounts, null, 2)}\n`
    : `${state.accounts.map(accountText).join("\n\n")}\n`;
  await fs.writeFile(file, text, { mode: 0o600 });
  ok(`exported ${state.accounts.length} account(s) to ${file}`);
  return state.accounts.length;
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      out: { type: "string", default: "accounts.txt" },
      json: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });
  await exportAccounts(values);
  return 0;
}
