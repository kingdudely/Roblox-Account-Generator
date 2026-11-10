import {
  authoriseUrl,
  challengeFor,
  exchangeCode,
  makeVerifier,
  parseClient,
} from "../extension/lib/consent.js";
import { hostOf } from "./browser.js";
import { sendGoogleFill } from "./content-bridge.js";
import { info } from "./util.js";

export const REDIRECT = "http://localhost:8765/";

function captureCode(url, set) {
  if (!url || !url.startsWith(REDIRECT)) return;
  try {
    const code = new URL(url).searchParams.get("code");
    if (code) set(code);
  } catch (_) {
    // malformed redirect
  }
}

// Finds one consent control and clicks its centre with the real mouse, so the
// page sees a trusted click. It never presses Cancel/Deny/Back.
async function pressConsent(page, email) {
  const candidate = await page
    .evaluate((wantedEmail) => {
      const visible = (el) => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 1 && rect.height > 1;
      };
      const labelOf = (el) =>
        `${el.getAttribute("aria-label") || ""} ${el.innerText || ""}`
          .replace(/\s+/g, " ")
          .trim()
          .toLowerCase();
      const nodes = [
        ...document.querySelectorAll('button,[role="button"],a,[role="link"],li,[role="menuitem"],tr'),
      ].filter(visible);

      const pick = (el, label) => {
        if (!el) return null;
        try {
          el.scrollIntoView({ block: "center", inline: "center" });
        } catch (_) {
          // ignore
        }
        const rect = el.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) return null;
        return {
          x: rect.x + rect.width / 2,
          y: rect.y + rect.height / 2,
          label: String(label || labelOf(el)).slice(0, 80),
        };
      };

      const email = String(wantedEmail || "").toLowerCase();
      if (email) {
        const row =
          nodes.find((el) => labelOf(el).includes(email)) ||
          [...document.querySelectorAll("[data-email],[data-identifier],[data-authuser]")]
            .filter(visible)
            .find((el) =>
              String(
                el.getAttribute("data-email") ||
                  el.getAttribute("data-identifier") ||
                  el.getAttribute("data-authuser") ||
                  ""
              )
                .toLowerCase()
                .includes(email)
            );
        if (row) return pick(row, `account ${email}`);
      }

      const advanced = nodes.find((el) => labelOf(el) === "advanced");
      if (advanced) return pick(advanced, "advanced");

      const unsafe = nodes.find((el) =>
        /go to .*\(unsafe\)|continue to .*\(unsafe\)/i.test(labelOf(el))
      );
      if (unsafe) return pick(unsafe, "unsafe");

      const wanted = ["continue", "allow", "i understand", "got it", "accept", "next", "confirm"];
      for (const node of nodes) {
        const label = labelOf(node);
        if (!label) continue;
        if (!wanted.some((word) => label === word || label.startsWith(`${word} `))) continue;
        if (/cancel|deny|back/i.test(label)) continue;
        return pick(node, label);
      }
      return null;
    }, email || "")
    .catch(() => null);

  if (!candidate) return null;
  try {
    await page.mouse.click(candidate.x, candidate.y);
    return candidate.label;
  } catch (_) {
    return null;
  }
}

async function pageNeedsAuth(page) {
  return page
    .evaluate(() => {
      const text = document.body ? document.body.innerText || "" : "";
      return Boolean(
        document.querySelector(
          'input[type="email"], input[type="password"], input[type="tel"], input[autocomplete="one-time-code"], input[name="totpPin"], input[name="identifier"], #identifierId'
        ) ||
          /2-step verification|verify it.s you|enter the code|try another way|use your authenticator/i.test(
            text
          )
      );
    })
    .catch(() => false);
}

export async function waitForCodeWithBrowser(
  context,
  clientId,
  verifier,
  {
    creds = null,
    email = "",
    timeout = 600_000,
    keepOpen = false,
    onTick = null,
  } = {}
) {
  let code = null;
  const setCode = (value) => {
    if (value) code = value;
  };

  const page = await context.newPage();
  const routePattern = "http://localhost:8765/**";
  await context.route(routePattern, async (route) => {
    captureCode(route.request().url(), setCode);
    await route
      .fulfill({
        status: 200,
        contentType: "text/html",
        body:
          "<!doctype html><meta charset='utf-8'><title>Authorization complete</title>" +
          "<h1>Authorization complete</h1><p>The CLI captured the code. You can close this tab.</p>",
      })
      .catch(() => {});
  });

  page.on("request", (request) => captureCode(request.url(), setCode));
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) captureCode(frame.url(), setCode);
  });

  const url = authoriseUrl(clientId, await challengeFor(verifier));
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });

  const deadline = Date.now() + timeout;
  let lastNotice = "";
  try {
    while (!code && Date.now() < deadline) {
      const current = page.url();
      const host = hostOf(current);
      if (host.endsWith("google.com")) {
        if (creds) {
          await sendGoogleFill(page, creds, { tries: 2, interval: 250 }).catch(() => {});
        }
        if (!(await pageNeedsAuth(page))) {
          const hit = await pressConsent(page, email || creds?.email || "").catch(() => null);
          if (hit && hit !== lastNotice) {
            lastNotice = hit;
            info(`consent: ${hit}`);
          }
        }
      } else {
        const hit = await pressConsent(page, email || creds?.email || "").catch(() => null);
        if (hit && hit !== lastNotice) {
          lastNotice = hit;
          info(`consent: ${hit}`);
        }
      }
      if (typeof onTick === "function") {
        try {
          await onTick(page);
        } catch (_) {
          // a transient page must not stop the consent loop
        }
      }
      await page.waitForTimeout(1000);
    }
  } finally {
    if (!keepOpen) {
      await page.close().catch(() => {});
    }
    await context.unroute(routePattern).catch(() => {});
  }

  if (!code) {
    throw new Error(
      `no authorization code captured within ${Math.round(timeout / 1000)}s — ` +
        "finish the consent in the browser and try again"
    );
  }
  return code;
}

export async function authorizeKeyWithContext(context, key, options = {}) {
  if (key.kind === "service_account") {
    throw new Error("service accounts do not need OAuth consent");
  }
  if (!key.client_id || !key.client_secret) {
    throw new Error("the key has no client_id/client_secret");
  }
  const client = parseClient(
    JSON.stringify({
      installed: { client_id: key.client_id, client_secret: key.client_secret },
    })
  );
  const verifier = makeVerifier();
  const code = await waitForCodeWithBrowser(context, client.client_id, verifier, options);
  const credential = await exchangeCode(client, code, verifier);
  info(`refresh token captured for ${key.label || key.client_id}`);
  return credential;
}
