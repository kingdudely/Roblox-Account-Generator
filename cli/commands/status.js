import { parseArgs } from "node:util";
import { APP_HOME, STATE_PATH } from "../paths.js";
import { load } from "../state.js";
import { statusModel, setupProgress } from "../flow/plan.js";
import { color, info, mask } from "../util.js";

const HELP = `rac status [options]

Shows what is set up, what is missing, and the one command to run next.

Options:
  --json       machine-readable status
  --secrets    show the saved admin password
  --help       show this help
`;

export const summary = "show setup progress and the next step";

function mark(done) {
  return done ? color.green("✓") : color.dim("○");
}

function printHuman(state, model, { secrets = false } = {}) {
  console.log("");
  console.log(color.bold("Roblox Alt Creator"));
  if (model.domain) console.log(`  tenant   ${model.domain}`);
  if (model.admin) console.log(`  admin    ${model.admin}`);
  console.log(`  accounts ${model.accounts.live} live${model.accounts.pending ? `, ${model.accounts.pending} pending` : ""}`);
  console.log(`  state    ${STATE_PATH}`);

  console.log("");
  console.log(color.bold("Progress"));
  for (const row of model.rows) {
    console.log(`  ${mark(row.done)} ${row.label.padEnd(24)} ${color.dim(row.detail)}`);
  }

  const progress = setupProgress(state);
  if (progress.done || progress.total) {
    console.log("");
    console.log(color.dim(`Tenant steps (${progress.done}/${progress.total})`));
    for (const phase of model.phases) {
      const active = state.setup?.auto?.phase === phase.id ? color.cyan("→") : " ";
      if (!phase.done && !state.setup?.done?.[phase.id]) {
        console.log(`  ${active} ${color.dim("○")} ${phase.label}`);
      } else {
        console.log(`  ${active} ${color.green("✓")} ${phase.label}`);
      }
    }
    if (state.setup?.auto?.lastError) {
      console.log(color.yellow(`  ! ${state.setup.auto.lastError}`));
    }
  }

  if (model.key) {
    console.log("");
    console.log(`  key      ${model.key.label || model.key.id}`);
  }
  if (secrets && state.setup?.auto?.adminPassword) {
    console.log(`  password ${state.setup.auto.adminPassword}`);
    if (state.totp?.secret) console.log(`  totp     ${state.totp.secret}`);
  } else if (state.setup?.auto?.adminPassword) {
    console.log(`  password ${mask(state.setup.auto.adminPassword, 4)} ${color.dim("(--secrets to show)")}`);
  }

  console.log("");
  console.log(`${color.bold("Next")}  ${model.next}`);
  if (!model.rows.some((row) => row.done)) {
    console.log(color.dim(`Start with: npm run create-tenant   (home: ${APP_HOME})`));
  }
  console.log("");
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      json: { type: "boolean", default: false },
      secrets: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  const state = await load();
  const model = statusModel(state);
  if (values.json) {
    console.log(
      JSON.stringify(
        {
          domain: model.domain,
          admin: model.admin,
          rows: model.rows,
          phases: model.phases,
          accounts: model.accounts,
          next: model.next,
          key: model.key
            ? { id: model.key.id, label: model.key.label, domain: model.key.domain, status: model.key.status }
            : null,
        },
        null,
        2
      )
    );
    return 0;
  }

  if (!model.subdomain && !model.key && !state.accounts.length) {
    info("nothing set up yet");
  }
  printHuman(state, model, values);
  return 0;
}
