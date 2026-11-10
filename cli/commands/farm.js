import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { exportAccounts } from "./export.js";
import { keyUsable, load } from "../state.js";
import { color, error, info, ok, sleep, warn } from "../util.js";

const BIN = fileURLToPath(new URL("../../bin/rac.js", import.meta.url));

const HELP = `rac farm [options]

Runs Roblox accounts forever, one at a time, until Ctrl+C.

Options:
  --pause <seconds>      wait between successful accounts (default: 60)
  --timeout <seconds>    per-account budget (default: 300)
  --lanes <n>            lane profiles (default: 1)
  --out <file>           export file (default: accounts.txt)
  --max-failures <n>     stop after n consecutive failures (default: 0 = never)
  --keep-google          keep each Google identity after success
  --proxy <url>          proxy every lane
  --channel <name>       Chrome channel (default: chrome)
  --headless             run lanes headless
  --verbose              print page console output
  --help                 show this help

Examples:
  npm run farm -- --out /home/john/Downloads/roblox-accounts-rac.txt
  node bin/rac.js farm --pause 120 --timeout 300
`;

export const summary = "generate accounts forever, one at a time";

function runOnce(args, onChild) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [BIN, "run", ...args], {
      stdio: "inherit",
      env: process.env,
    });
    onChild(child);
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      onChild(null);
      resolve(signal ? 130 : code ?? 1);
    });
  });
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      pause: { type: "string", default: "60" },
      timeout: { type: "string", default: "300" },
      lanes: { type: "string", default: "1" },
      out: { type: "string", default: "accounts.txt" },
      "max-failures": { type: "string", default: "0" },
      "keep-google": { type: "boolean", default: false },
      proxy: { type: "string" },
      channel: { type: "string", default: "chrome" },
      headless: { type: "boolean", default: false },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  const state = await load();
  if (!state.keys.some((key) => keyUsable(key))) {
    throw new Error("no authorized key — run `rac key import` and `rac key authorize` first");
  }

  const pause = Math.max(5, Number(values.pause) || 60);
  const timeout = Math.max(60, Number(values.timeout) || 300);
  const lanes = Math.max(1, Math.min(4, Number(values.lanes) || 1));
  const maxFailures = Math.max(0, Number(values["max-failures"]) || 0);
  const runArgs = ["--count", "1", "--lanes", String(lanes), "--new", "--timeout", String(timeout)];
  if (values["keep-google"]) runArgs.push("--keep-google");
  if (values.proxy) runArgs.push("--proxy", values.proxy);
  if (values.channel) runArgs.push("--channel", values.channel);
  if (values.headless) runArgs.push("--headless");
  if (values.verbose) runArgs.push("--verbose");

  let stopping = false;
  let child = null;
  let made = 0;
  let failures = 0;
  const onInt = () => {
    stopping = true;
    warn("stopping after the current account (Ctrl+C again to force)");
    if (child) {
      try {
        child.kill("SIGINT");
      } catch (_) {
        // already gone
      }
    }
  };
  process.once("SIGINT", onInt);

  console.log("");
  console.log(color.bold("rac farm"));
  console.log(`  pause    ${pause}s between accounts`);
  console.log(`  timeout  ${timeout}s per account`);
  console.log(`  lanes    ${lanes}`);
  console.log(`  export   ${values.out}`);
  console.log(color.dim("  Ctrl+C stops after the current account."));
  console.log("");

  try {
    while (!stopping) {
      const code = await runOnce(runArgs, (next) => {
        child = next;
      });

      if (code === 0) {
        made += 1;
        failures = 0;
        await exportAccounts({ out: values.out }).catch((error) => {
          warn(`export failed: ${error.message || error}`);
        });
        ok(`farm: ${made} live account(s) this session`);
      } else {
        failures += 1;
        if (code === 2) {
          warn("account is pending; it was exported but not finalized");
          await exportAccounts({ out: values.out }).catch(() => {});
        } else {
          warn(`run exited with code ${code}`);
        }
        if (maxFailures && failures >= maxFailures) {
          error(`stopping after ${failures} consecutive failure(s)`);
          break;
        }
      }

      if (stopping) break;
      const delay = code === 0 ? pause : Math.min(600, pause * 2 ** Math.min(failures, 4));
      info(`waiting ${delay}s before the next account…`);
      await sleep(delay * 1000);
    }
  } finally {
    process.removeListener("SIGINT", onInt);
  }

  ok(`farm stopped: ${made} live account(s) this session`);
  return 0;
}
