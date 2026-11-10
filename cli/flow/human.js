import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { color, sleep } from "../util.js";

const execFileAsync = promisify(execFile);

function quiet() {
  return Boolean(process.env.RAC_QUIET);
}

export function bell(times = 2) {
  if (quiet() || process.env.RAC_NO_SOUND) return;
  try {
    process.stdout.write("\x07".repeat(Math.max(1, Math.min(3, times))));
  } catch (_) {
    // no tty
  }
}

export async function desktopNotify(title, body = "") {
  if (quiet() || process.env.RAC_NO_NOTIFY) return;
  try {
    if (process.platform === "linux") {
      await execFileAsync(
        "notify-send",
        ["-a", "Roblox Alt Creator", "-u", "critical", title, body],
        { timeout: 4000 }
      );
    } else if (process.platform === "darwin") {
      const script = `display notification ${JSON.stringify(body)} with title ${JSON.stringify(title)}`;
      await execFileAsync("osascript", ["-e", script], { timeout: 4000 });
    }
  } catch (_) {
    // notifications are best effort
  }
}

export async function focusPage(page) {
  if (!page || (typeof page.isClosed === "function" && page.isClosed())) return false;
  try {
    await page.bringToFront();
    return true;
  } catch (_) {
    return false;
  }
}

export async function actionNeeded(
  page,
  { title = "Google needs you", lines = [], sound = true } = {}
) {
  const focused = await focusPage(page);
  if (sound) bell(2);
  void desktopNotify("RAC needs you", title);

  console.log("");
  console.log(color.yellow("━━━━━━━━━━━━━━━━ ACTION NEEDED ━━━━━━━━━━━━━━━━"));
  console.log(` ${color.bold(title)}`);
  for (const line of lines) console.log(` ${line}`);
  console.log(color.dim(" I'll continue automatically when the page moves on."));
  if (!focused) console.log(color.dim(" (Could not raise the browser window — switch to it manually.)"));
  console.log(color.yellow("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"));
  console.log("");
}

export async function waitForHuman(
  page,
  check,
  {
    title = "Google needs you",
    lines = [],
    timeout = 30 * 60_000,
    interval = 1500,
    remindEvery = 60_000,
    sound = true,
  } = {}
) {
  await actionNeeded(page, { title, lines, sound });
  const deadline = Date.now() + timeout;
  let lastReminder = Date.now();

  while (Date.now() < deadline) {
    if (page && typeof page.isClosed === "function" && page.isClosed()) {
      throw new Error("the browser was closed before the step finished");
    }
    let done = false;
    try {
      done = Boolean(await check());
    } catch (_) {
      // the page may be navigating
    }
    if (done) return true;

    if (Date.now() - lastReminder > remindEvery) {
      lastReminder = Date.now();
      if (!quiet()) bell(1);
      console.log(
        color.dim(
          ` still waiting for ${title.toLowerCase()} — finish it in the browser and I'll continue`
        )
      );
    }
    await sleep(interval);
  }

  throw new Error(`timed out waiting for ${title.toLowerCase()}`);
}

export async function notifyOnce(page, seen, key, title, lines) {
  const marker = String(key || title);
  if (seen.has(marker)) return false;
  seen.add(marker);
  await actionNeeded(page, { title, lines });
  return true;
}
