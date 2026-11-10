import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { hostOf, launchContext, openPage } from "../browser.js";
import { DEBUG_ROOT } from "../paths.js";
import { placeholderEmail } from "./signup.js";
import {
  cleanSubdomain,
  randomSubdomain,
  titleCase,
} from "../flow/domain.js";
import { color, info, ok, sleep, warn } from "../util.js";

const SIGNUP = "https://workspace.google.com/gcpidentity/signup?sku=identitybasic";

const HELP = `rac record-signup [options]

Opens a visible Chrome window on the Cloud Identity signup page, prints dummy
values for a manual signup, and records the session:

  video.webm        the whole browser window
  trace.zip         Playwright trace
  network.har       request/response log
  timeline.log      URL/text changes over time
  step-*.png        screenshot whenever the URL changes
  page.txt/html     final page

It stops automatically when it reaches admin.google.com, when Google shows its
rejection message, when the browser closes, or after --timeout.

Options:
  --subdomain <name>   dummy DuckDNS domain (default: generated)
  --business <name>    dummy business name
  --first <name>       dummy first name (default: Alex)
  --last <name>        dummy last name (default: Morgan)
  --contact <email>    dummy recovery email (default: generated @example.com)
  --admin <user>       dummy admin username (default: alex.morgan)
  --password <value>   dummy admin password
  --profile <name>     browser profile under RAC_HOME (default: manual-record)
  --user-data-dir <path>  use a real Chrome profile directory instead
  --profile-directory <name>  Chrome profile inside it (default: Default)
  --channel <name>     Chrome channel (default: chrome)
  --proxy <url>        proxy
  --timeout <seconds>  recording timeout (default: 1800)
  --headless           not useful for a manual recording
  --help               show this help
`;

export const summary = "record a manual Cloud Identity signup for comparison";

function stamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

async function assertProfileFree(dir) {
  const lock = path.join(dir, "SingletonLock");
  let target = "";
  try {
    target = await fs.readlink(lock);
  } catch (_) {
    return;
  }
  const pid = Number(String(target).split("-").pop());
  if (!Number.isInteger(pid) || pid <= 0) return;
  try {
    process.kill(pid, 0);
    throw new Error(`Chrome is using this profile (pid ${pid}); close Chrome completely first`);
  } catch (error) {
    if (error.code !== "ESRCH") throw error;
  }
  // Chrome died without cleaning its singleton files. They make a new launch
  // say "Opening in existing browser session" forever.
  for (const name of ["SingletonLock", "SingletonSocket", "SingletonCookie"]) {
    await fs.rm(path.join(dir, name), { force: true }).catch(() => {});
  }
  info(`removed stale Chrome profile lock from ${dir}`);
}

async function append(file, text) {
  await fs.appendFile(file, `${new Date().toISOString()} ${text}\n`).catch(() => {});
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      subdomain: { type: "string" },
      business: { type: "string" },
      first: { type: "string", default: "Alex" },
      last: { type: "string", default: "Morgan" },
      contact: { type: "string" },
      admin: { type: "string", default: "alex.morgan" },
      password: { type: "string", default: "CedarHarbor-4821" },
      profile: { type: "string", default: "manual-record" },
      "user-data-dir": { type: "string" },
      "profile-directory": { type: "string", default: "Default" },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      timeout: { type: "string", default: "1800" },
      headless: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  const subdomain = cleanSubdomain(values.subdomain || randomSubdomain());
  const domain = `${subdomain}.duckdns.org`;
  const business = values.business || `${titleCase(subdomain)} Studio`;
  const contact = values.contact || placeholderEmail(subdomain);
  const adminEmail = `${values.admin}@${domain}`;
  const password = values.password;

  const dir = path.join(DEBUG_ROOT, `${stamp()}-manual-signup`);
  const videoDir = path.join(dir, "video");
  const harPath = path.join(dir, "network.har");
  const tracePath = path.join(dir, "trace.zip");
  const timelinePath = path.join(dir, "timeline.log");
  const summaryPath = path.join(dir, "summary.json");
  await fs.mkdir(videoDir, { recursive: true });

  if (values["user-data-dir"]) {
    await assertProfileFree(path.resolve(values["user-data-dir"]));
  }

  console.log("");
  console.log(color.bold("Manual signup recording"));
  console.log(`  URL       ${SIGNUP}`);
  console.log(
    `  Profile   ${
      values["user-data-dir"]
        ? `${path.resolve(values["user-data-dir"])} (${values["profile-directory"]})`
        : values.profile
    }`
  );
  console.log(`  Business  ${business}`);
  console.log(`  Name      ${values.first} ${values.last}`);
  console.log(`  Recovery  ${contact}`);
  console.log(`  Domain    ${domain}`);
  console.log(`  Username  ${values.admin}`);
  console.log(`  Password  ${password}`);
  console.log("");
  console.log(color.yellow("Use these dummy values. If Google asks for a phone, use your own."));
  console.log(color.dim("The recording includes everything visible in the browser; it is stored under debug/."));
  console.log("");
  console.log("Recording starts now. Do the signup manually in the Chrome window.");
  console.log("");

  const context = await launchContext({
    profile: values.profile,
    userDataDir: values["user-data-dir"] || "",
    headless: values.headless,
    channel: values.channel,
    proxy: values.proxy || "",
    args:
      values["user-data-dir"] && values["profile-directory"]
        ? [`--profile-directory=${values["profile-directory"]}`]
        : [],
    recordVideo: { dir: videoDir, size: { width: 1280, height: 800 } },
    recordHar: { path: harPath, mode: "full", content: "embed" },
  });

  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });

  const attach = (target) => {
    target.on("console", (message) => {
      const text = message.text();
      if (text) void append(timelinePath, `[console:${message.type()}] ${text.slice(0, 1000)}`);
    });
    target.on("pageerror", (error) => {
      void append(timelinePath, `[pageerror] ${error.message}`);
    });
  };
  context.on("page", attach);
  for (const target of context.pages()) attach(target);

  let page = null;
  let reason = "timeout";
  let finalUrl = "";
  let finalText = "";
  let shots = 0;
  let lastUrl = "";
  let lastText = "";

  try {
    page = await openPage(context, SIGNUP);
    await append(timelinePath, `start ${SIGNUP}`);
    const deadline = Date.now() + Math.max(60, Number(values.timeout) || 1800) * 1000;

    while (Date.now() < deadline) {
      const pages = context.pages().filter((target) => !target.isClosed());
      if (!pages.length) {
        reason = "closed";
        break;
      }
      if (!page || page.isClosed()) page = pages[pages.length - 1];

      let url = "";
      try {
        url = page.url();
      } catch (_) {
        reason = "closed";
        break;
      }
      let text = "";
      try {
        text = await page.evaluate(() => (document.body ? document.body.innerText || "" : ""));
      } catch (_) {
        // navigating
      }

      if (url !== lastUrl) {
        lastUrl = url;
        finalUrl = url;
        await append(timelinePath, `url ${url}`);
        const shot = path.join(dir, `step-${String(++shots).padStart(2, "0")}.png`);
        await page.screenshot({ path: shot, fullPage: true }).catch(() => {});
        console.log(`${color.dim(new Date().toTimeString().slice(0, 8))} ${url}`);
      }

      if (text && text !== lastText) {
        lastText = text;
        finalText = text;
        await append(timelinePath, `text ${text.replace(/\s+/g, " ").slice(0, 1200)}`);
      }

      if (hostOf(url) === "admin.google.com") {
        reason = "success";
        break;
      }
      if (
        /sorry,? we can.?t complete your signup|we can.?t create your account|too many unsuccessful attempts|this phone number (?:can.?t|cannot) be used|signup was blocked/i.test(
          text
        )
      ) {
        reason = "rejected";
        break;
      }

      await sleep(1000);
    }
  } catch (error) {
    reason = `error: ${error.message || error}`;
  } finally {
    for (const target of context.pages()) {
      if (target.isClosed()) continue;
      try {
        finalUrl = target.url() || finalUrl;
        finalText = await target.evaluate(() => (document.body ? document.body.innerText || "" : ""));
        await fs.writeFile(path.join(dir, "page.txt"), finalText, "utf8");
        await fs.writeFile(path.join(dir, "page.html"), await target.content(), "utf8");
        await target.screenshot({ path: path.join(dir, "final.png"), fullPage: true }).catch(() => {});
      } catch (_) {
        // page closed
      }
    }

    await context.tracing.stop({ path: tracePath }).catch(() => {});
    await context.close().catch(() => {});

    const summary = {
      reason,
      final_url: finalUrl,
      final_text: finalText.slice(0, 4000),
      values: {
        business,
        first: values.first,
        last: values.last,
        contact,
        domain,
        admin: adminEmail,
        password,
      },
      artifacts: {
        timeline: timelinePath,
        trace: tracePath,
        har: harPath,
        video_dir: videoDir,
      },
    };
    await fs.writeFile(summaryPath, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
    await fs.writeFile(
      path.join(dir, "values.txt"),
      [
        `business: ${business}`,
        `first: ${values.first}`,
        `last: ${values.last}`,
        `contact: ${contact}`,
        `domain: ${domain}`,
        `admin: ${adminEmail}`,
        `password: ${password}`,
        "",
      ].join("\n"),
      "utf8"
    );

    console.log("");
    if (reason === "success") {
      ok("recording stopped: Admin console reached");
    } else if (reason === "rejected") {
      warn("recording stopped: Google's rejection message appeared");
    } else if (reason === "closed") {
      warn("recording stopped: browser closed");
    } else {
      warn(`recording stopped: ${reason}`);
    }
    info(`final URL: ${finalUrl}`);
    info(`artifacts: ${dir}`);
    info(`  timeline: ${timelinePath}`);
    info(`  trace:    ${tracePath}`);
    info(`  har:      ${harPath}`);
    info(`  video:    ${videoDir}`);
    info(`  summary:  ${summaryPath}`);
    console.log("");
  }
  return 0;
}
