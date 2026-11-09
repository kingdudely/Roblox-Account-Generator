import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { launchContext } from "../browser.js";
import { DEBUG_ROOT } from "../paths.js";
import { info, ok } from "../util.js";

const HELP = `rac smoke [options]

Launches Patchright, opens a local page (or --url) and verifies a real mouse
click is trusted and navigator.webdriver is false.

Options:
  --url <url>         page to test (default: a local data: page)
  --profile <name>    browser profile (default: setup)
  --headless          run headless
  --channel <name>    Chrome channel (default: chrome)
  --proxy <url>       proxy
  --screenshot <file> screenshot path under debug/
  --help              show this help
`;

export const summary = "verify a trusted click and navigator.webdriver";

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      url: { type: "string" },
      profile: { type: "string", default: "setup" },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      screenshot: { type: "string", default: "patchright-smoke.png" },
      help: { type: "boolean", default: false },
    },
  });

  const context = await launchContext({
    profile: values.profile,
    headless: values.headless,
    channel: values.channel,
    proxy: values.proxy || "",
  });
  try {
    const page = context.pages()[0] || (await context.newPage());
    const url =
      values.url ||
      "data:text/html,<title>RAC smoke</title><h1>RAC smoke</h1>" +
        "<button id='b' style='width:200px;height:56px'>Click me</button>";
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
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
    const box = await page
      .locator("#b, h1")
      .first()
      .boundingBox()
      .catch(() => null);
    if (box) {
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    } else {
      await page.mouse.click(30, 30);
    }
    await page.waitForTimeout(250);

    await fs.mkdir(DEBUG_ROOT, { recursive: true });
    const shot = path.isAbsolute(values.screenshot)
      ? values.screenshot
      : path.join(DEBUG_ROOT, values.screenshot);
    await page.screenshot({ path: shot, fullPage: true });

    const result = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      webdriver: navigator.webdriver,
      trusted_click: window.__racTrusted,
      userAgent: navigator.userAgent,
    }));
    console.log(JSON.stringify(result, null, 2));
    info(`screenshot: ${shot}`);
    if (result.webdriver !== false || result.trusted_click !== true) {
      throw new Error("trusted-input check failed");
    }
    ok("trusted input confirmed");
    return 0;
  } finally {
    await context.close().catch(() => {});
  }
}
