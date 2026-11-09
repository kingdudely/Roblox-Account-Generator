import { createRequire } from "node:module";
import { launchContext, findChrome } from "../browser.js";
import { APP_HOME, PROFILE_ROOT, STATE_PATH, profileDir } from "../paths.js";
import { color, info, ok } from "../util.js";
import { parseArgs } from "node:util";

const require = createRequire(import.meta.url);

function patchrightVersion() {
  try {
    const file = require.resolve("patchright/package.json");
    return JSON.parse(require("node:fs").readFileSync(file, "utf8")).version;
  } catch (_) {
    return "unknown";
  }
}

const HELP = `rac doctor [options]

Checks the runtime and launches a real Chrome window long enough to verify:
  - Patchright is installed and can start Chrome
  - navigator.webdriver is false
  - a mouse click produces isTrusted: true

Options:
  --profile <name>    browser profile (default: setup)
  --headless          run headless (headful is the realistic default)
  --channel <name>    Chrome channel (default: chrome; none for bundled Chromium)
  --proxy <url>       proxy for the check
  --help              show this help
`;

export const summary = "check Node, Patchright, Chrome, profiles and trusted input";

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      help: { type: "boolean", default: false },
    },
  });

  const chrome = findChrome();
  console.log(`${color.dim("node")}        ${process.version}`);
  console.log(`${color.dim("patchright")}  ${patchrightVersion()}`);
  console.log(`${color.dim("chrome")}      ${chrome || "not found (will use Patchright Chromium)"}`);
  console.log(`${color.dim("home")}        ${APP_HOME}`);
  console.log(`${color.dim("state")}       ${STATE_PATH}`);
  console.log(`${color.dim("profiles")}    ${PROFILE_ROOT}`);
  console.log(`${color.dim("profile")}     ${profileDir(values.profile)}`);
  console.log(`${color.dim("display")}     ${process.env.DISPLAY || "(none)"}`);
  console.log(`${color.dim("headless")}    ${values.headless}`);

  info("launching a smoke browser…");
  const context = await launchContext({
    profile: values.profile,
    headless: values.headless,
    channel: values.channel,
    proxy: values.proxy || "",
  });
  try {
    const page = context.pages()[0] || (await context.newPage());
    await page.goto(
      "data:text/html,<title>RAC doctor</title><h1>RAC doctor</h1>" +
        "<button id='b' style='width:200px;height:56px'>Click</button>",
      { waitUntil: "domcontentloaded" }
    );
    await page.evaluate(() => {
      window.__racTrusted = null;
      document.addEventListener(
        "click",
        (event) => {
          window.__racTrusted = event.isTrusted;
        },
        { once: true, capture: true }
      );
    });
    const box = await page.locator("#b").boundingBox();
    if (box) await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await page.waitForTimeout(250);
    const result = await page.evaluate(() => ({
      webdriver: navigator.webdriver,
      trusted_click: window.__racTrusted,
      userAgent: navigator.userAgent,
    }));
    console.log(JSON.stringify({ ok: true, ...result }, null, 2));
    if (result.webdriver === false && result.trusted_click === true) {
      ok("Patchright is working: webdriver false, trusted click true");
      return 0;
    }
    throw new Error("Patchright launched, but the trusted-input check did not pass");
  } finally {
    await context.close().catch(() => {});
  }
}
