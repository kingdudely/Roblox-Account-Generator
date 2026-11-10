import fs from "node:fs/promises";
import { parseSms, guessFromText } from "../../extension/lib/phone.js";
import { actionNeeded } from "./human.js";
import { info, warn } from "../util.js";

const JSQR_PATH = new URL("../../extension/lib/jsqr.js", import.meta.url);

let jsqrSource = null;

// The QR lives in a canvas/image on Google's page. Pull every plausible image,
// decode it with the same jsQR bundle the extension ships, and return whatever
// the QR actually contains.
function collectPage() {
  const out = {
    url: location.href,
    title: document.title,
    canvases: [],
    images: [],
    svgs: [],
    hrefs: [],
    smsHits: [],
    hooked: "",
    text: "",
  };

  for (const canvas of document.querySelectorAll("canvas")) {
    if (canvas.width < 80 || canvas.height < 80) continue;
    try {
      out.canvases.push(canvas.toDataURL("image/png"));
    } catch (_) {
      // tainted
    }
    if (out.canvases.length >= 6) break;
  }

  for (const image of document.querySelectorAll("img")) {
    if ((image.naturalWidth || 0) < 80) continue;
    try {
      const scale = Math.min(1, 1000 / Math.max(image.naturalWidth, image.naturalHeight));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(image.naturalWidth * scale);
      canvas.height = Math.round(image.naturalHeight * scale);
      canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
      out.images.push(canvas.toDataURL("image/png"));
    } catch (_) {
      // cross-origin image
    }
    if (out.images.length >= 8) break;
  }

  for (const svg of document.querySelectorAll("svg")) {
    const box = svg.getBoundingClientRect();
    if (box.width < 80 || box.height < 80) continue;
    try {
      const source = new XMLSerializer().serializeToString(svg);
      out.svgs.push(`data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(source)))}`);
    } catch (_) {
      // ignore
    }
    if (out.svgs.length >= 4) break;
  }

  out.hrefs = [...document.querySelectorAll("a,area")]
    .map((anchor) => anchor.href)
    .filter(Boolean)
    .slice(0, 200);
  out.text = (document.body?.innerText || "").slice(0, 8000);
  out.hooked = document.documentElement.getAttribute("data-rac-sms") || "";

  try {
    const markup = document.documentElement.innerHTML;
    const re = /sms:[^"'<>\\\s]{4,300}/gi;
    let match;
    while ((match = re.exec(markup)) && out.smsHits.length < 12) {
      const value = match[0].replace(/&amp;/g, "&");
      if (!out.smsHits.includes(value)) out.smsHits.push(value);
    }
  } catch (_) {
    // ignore
  }

  return out;
}

function decodeDataUrl(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      try {
        const width = image.naturalWidth || image.width;
        const height = image.naturalHeight || image.height;
        if (!width || !height) {
          resolve(null);
          return;
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(image, 0, 0);
        const pixels = context.getImageData(0, 0, width, height);
        if (typeof window.jsQR !== "function") {
          reject(new Error("jsQR is not loaded"));
          return;
        }
        const found = window.jsQR(pixels.data, width, height, {
          inversionAttempts: "attemptBoth",
        });
        resolve(found?.data || null);
      } catch (error) {
        reject(error);
      }
    };
    image.onerror = () => reject(new Error("image failed to load"));
    image.src = url;
  });
}

async function ensureJsQr(page) {
  const loaded = await page
    .evaluate(() => typeof window.jsQR === "function")
    .catch(() => false);
  if (loaded) return true;
  if (!jsqrSource) jsqrSource = await fs.readFile(JSQR_PATH, "utf8");
  await page.evaluate(jsqrSource).catch(() => {});
  return page
    .evaluate(() => typeof window.jsQR === "function")
    .catch(() => false);
}

function findSmsInData(data) {
  const candidates = [
    ...(data.hooked ? [data.hooked] : []),
    ...(data.smsHits || []),
    ...(data.hrefs || []),
  ];
  for (const value of candidates) {
    if (/^sms:/i.test(String(value))) return parseSms(value);
  }
  const inline = /sms:(?:\+|\d)[^"'<>\s]{4,}/i.exec(data.text || "");
  return inline ? parseSms(inline[0]) : { number: null, body: null, sms: null };
}

export function classifyPhoneText(text = "", data = {}) {
  const body = String(text || "").toLowerCase();
  if (/sorry,? we can.?t complete your signup|we can.?t create your account|too many unsuccessful attempts|phone number cannot be used|this phone number can.?t be used|something went wrong/i.test(body)) {
    return "rejected";
  }
  if (/captcha|type the text you hear or see|not a robot/i.test(body)) return "captcha";
  if (/scan (the|this)? ?qr|qr code|verify your device|scan with your phone/i.test(body)) {
    return "qr";
  }
  if (
    /send\b[^.!?\n]{0,50}\b(sms|text|message)\b|send it to|text this message|message the number/i.test(body) ||
    (data.smsHits || []).length
  ) {
    return "send-sms";
  }
  if (/enter (the )?(verification )?code|6-digit|check your phone|we sent (you )?a code|sent you a code/i.test(body)) {
    return "code-entry";
  }
  if (/phone number|enter a phone|mobile number|get a verification code|we.?ll send .*code/i.test(body)) {
    return "phone-entry";
  }
  if (/verify it.s you|2-step verification|confirm it.s you|try another way/i.test(body)) {
    return "verification";
  }
  return "unknown";
}

export async function scanPhonePage(page) {
  const data = await page.evaluate(collectPage).catch(() => null);
  if (!data) return { kind: "unknown", number: null, body: null, payload: "", text: "" };

  const direct = findSmsInData(data);
  const qrLikely = /scan|qr|verify your device/i.test(data.text || "");
  if ((direct.number || direct.body) && !qrLikely) {
    return {
      kind: "send-sms",
      ...direct,
      payload: direct.sms || "",
      text: data.text,
      source: "page",
      url: data.url,
    };
  }

  await ensureJsQr(page);
  const payloads = [];
  for (const url of [...data.canvases, ...data.images, ...data.svgs]) {
    const payload = await page.evaluate(decodeDataUrl, url).catch(() => null);
    if (payload && !payloads.includes(payload)) payloads.push(payload);
  }

  // Some Google QR images are cross-origin and taint the canvas. A viewport
  // screenshot is the fallback that always works.
  if (!payloads.length && /qr|scan|verify your device/i.test(data.text || "")) {
    const shot = await page.screenshot({ type: "png" }).catch(() => null);
    if (shot) {
      const dataUrl = `data:image/png;base64,${shot.toString("base64")}`;
      const payload = await page.evaluate(decodeDataUrl, dataUrl).catch(() => null);
      if (payload) payloads.push(payload);
    }
  }

  for (const payload of payloads) {
    if (/^sms:/i.test(payload)) {
      return {
        kind: "send-sms",
        ...parseSms(payload),
        payload,
        text: data.text,
        source: "qr",
        url: data.url,
      };
    }
  }

  const link = payloads.find((value) => /^https?:\/\//i.test(value));
  if (link) {
    return {
      kind: "consent-link",
      payload: link,
      number: null,
      body: null,
      text: data.text,
      source: "qr",
      url: data.url,
    };
  }

  if (direct.number || direct.body) {
    return {
      kind: "send-sms",
      ...direct,
      payload: direct.sms || "",
      text: data.text,
      source: "page",
      url: data.url,
    };
  }

  const kind = classifyPhoneText(data.text, data);
  return {
    kind,
    ...guessFromText(data.text),
    payload: payloads[0] || "",
    text: data.text,
    source: "text",
    url: data.url,
  };
}

// Google's consent page only emits the sms: URI after "Send SMS" is pressed.
// Hook window.open and anchors first, then click the real button.
export async function pressSendSms(page) {
  await page
    .evaluate(() => {
      window.__racSms = "";
      const record = (value) => {
        if (!value || !/^sms:/i.test(String(value))) return;
        window.__racSms = String(value);
        try {
          document.documentElement.setAttribute("data-rac-sms", String(value));
        } catch (_) {
          // document may be navigating
        }
      };
      const original = window.open;
      window.open = function patchedOpen(url, ...rest) {
        record(url);
        return original.apply(this, [url, ...rest]);
      };
      document.addEventListener(
        "click",
        (event) => {
          const anchor = event.target?.closest?.('a[href^="sms:"], a[href^="SMS:"]');
          if (anchor) {
            record(anchor.getAttribute("href"));
            // Do not let Chrome hand the sms: URI to the desktop's phone app.
            event.preventDefault();
          }
        },
        true
      );
      for (const anchor of document.querySelectorAll('a[href^="sms:"], a[href^="SMS:"]')) {
        record(anchor.getAttribute("href"));
      }
    })
    .catch(() => {});

  const candidate = await page
    .evaluate(() => {
      const visible = (element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 1 && rect.height > 1;
      };
      const nodes = [...document.querySelectorAll('button,[role="button"],a')].filter(visible);
      const hit = nodes.find((element) => {
        const label = `${element.innerText || ""} ${element.getAttribute("aria-label") || ""}`
          .replace(/\s+/g, " ")
          .trim()
          .toLowerCase();
        return /send (sms|text|message)/.test(label) || label === "send sms" || label === "send";
      });
      if (!hit) return null;
      try {
        hit.scrollIntoView({ block: "center", inline: "center" });
      } catch (_) {
        // ignore
      }
      const rect = hit.getBoundingClientRect();
      return {
        x: rect.x + rect.width / 2,
        y: rect.y + rect.height / 2,
        label: (hit.innerText || hit.getAttribute("aria-label") || "send").slice(0, 40),
      };
    })
    .catch(() => null);

  if (!candidate) return null;
  await page.mouse.click(candidate.x, candidate.y).catch(() => {});
  await page.waitForTimeout(800);

  const value = await page
    .evaluate(
      () =>
        window.__racSms ||
        document.documentElement.getAttribute("data-rac-sms") ||
        [...document.querySelectorAll('a[href^="sms:"]')]
          .map((anchor) => anchor.getAttribute("href"))
          .find(Boolean) ||
        ""
    )
    .catch(() => "");
  return { value: value || "", label: candidate.label };
}

export async function extractFromConsentLink(context, url, { timeout = 15_000 } = {}) {
  let page = null;
  try {
    page = await context.newPage();
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const direct = await page
        .evaluate(() => {
          const anchor = document.querySelector('a[href^="sms:"]');
          return anchor ? anchor.getAttribute("href") : "";
        })
        .catch(() => "");
      if (direct) return parseSms(direct);
      const pressed = await pressSendSms(page).catch(() => null);
      if (pressed?.value) return parseSms(pressed.value);
      await page.waitForTimeout(800);
    }
    return null;
  } catch (error) {
    warn(`phone gate: could not read the consent page (${error.message || error})`);
    return null;
  } finally {
    if (page) await page.close().catch(() => {});
  }
}

function gateKey(scan) {
  return [scan.kind, scan.number || "", scan.body || "", scan.payload || ""].join("|");
}

async function bringQrIntoView(page) {
  await page
    .evaluate(() => {
      const nodes = [...document.querySelectorAll("canvas,img,svg")];
      const best = nodes
        .map((element) => ({ element, rect: element.getBoundingClientRect() }))
        .filter(({ rect }) => rect.width >= 100 && rect.height >= 100)
        .sort((a, b) => b.rect.width * b.rect.height - a.rect.width * a.rect.height)[0];
      if (best) best.element.scrollIntoView({ block: "center", inline: "center" });
    })
    .catch(() => {});
}

// Focus the browser, make a noise, and put Google's number/message on screen.
// The caller keeps polling; this function never waits for the user.
export async function showPhoneGate(page, context, { seen = new Set() } = {}) {
  const signature = await page
    .evaluate(() => `${location.href}|${(document.body?.innerText || "").slice(0, 300)}`)
    .catch(() => "");
  if (signature && seen.has(`sig:${signature}`)) {
    return { kind: "waiting", number: null, body: null, payload: "", text: "" };
  }
  if (signature) seen.add(`sig:${signature}`);

  let scan = await scanPhonePage(page);

  if (scan.kind === "consent-link" && scan.payload && context) {
    info("phone gate: reading the consent link to extract the number/message…");
    const extracted = await extractFromConsentLink(context, scan.payload).catch(() => null);
    if (extracted?.number || extracted?.body) {
      scan = { ...scan, ...extracted, kind: "send-sms", source: "consent" };
    }
  }

  // The consent page can be the current tab, with a "Send SMS" button that
  // reveals the sms: URI only after it is pressed.
  if (scan.kind === "send-sms" && !scan.number && !scan.body) {
    const pressed = await pressSendSms(page).catch(() => null);
    const parsed = pressed?.value ? parseSms(pressed.value) : null;
    if (parsed?.number || parsed?.body) {
      scan = { ...scan, ...parsed, kind: "send-sms", source: "button" };
    }
  }

  const key = gateKey(scan);
  if (seen.has(key)) return scan;
  seen.add(key);

  if (scan.number || scan.body) {
    await actionNeeded(page, {
      title: "Send this text from your phone",
      lines: [
        `To:      ${scan.number || "see the Google page"}`,
        `Message: ${scan.body || "see the Google page"}`,
        "Open your SMS app, send that exact message, then come back here.",
      ].map((line) => line.replace(/\n/g, " ")),
    });
    return scan;
  }

  if (scan.kind === "consent-link" && scan.payload) {
    await bringQrIntoView(page);
    await actionNeeded(page, {
      title: "Scan the QR code with your phone",
      lines: [
        "The QR page is focused in the browser.",
        "If the QR will not scan, open this link on your phone:",
        scan.payload,
      ],
    });
    return scan;
  }

  if (scan.kind === "qr" || /qr|scan|verify your device/i.test(scan.text || "")) {
    await bringQrIntoView(page);
    await actionNeeded(page, {
      title: "Scan the QR code with your phone",
      lines: [
        "Use your phone camera on the QR code in the focused browser window.",
        "Approve the sign-in, then come back here.",
      ],
    });
    return scan;
  }

  if (scan.kind === "phone-entry") {
    await actionNeeded(page, {
      title: "Google wants your phone number",
      lines: [
        "Type your number in the focused browser window.",
        "Google will text you a code; enter it on the next page.",
      ],
    });
    return scan;
  }

  if (scan.kind === "code-entry") {
    await actionNeeded(page, {
      title: "Enter the code Google sent you",
      lines: ["Check your phone for the code and type it into the focused browser window."],
    });
    return scan;
  }

  if (scan.kind === "captcha") {
    await actionNeeded(page, {
      title: "Solve the captcha",
      lines: ["Google wants a captcha. Solve it in the focused browser window."],
    });
    return scan;
  }

  if (scan.kind === "rejected") {
    return scan;
  }

  await actionNeeded(page, {
    title: "Google needs you",
    lines: [
      "Finish the verification in the focused browser window.",
      "I'll continue automatically when the page moves on.",
    ],
  });
  return scan;
}

export async function hasVisiblePhoneInput(page) {
  return page
    .locator(
      'input[type="tel"]:visible, input[autocomplete="tel"]:visible, input[aria-label*="phone" i]:visible'
    )
    .count()
    .then((count) => count > 0)
    .catch(() => false);
}

export async function hasVisibleCodeInput(page) {
  return page
    .locator(
      'input[autocomplete="one-time-code"]:visible, input[name="totpPin"]:visible, input[aria-label*="code" i]:visible, input[inputmode="numeric"]:visible'
    )
    .count()
    .then((count) => count > 0)
    .catch(() => false);
}

export async function fillVisiblePhone(page, phone) {
  if (!phone) return false;
  const field = page
    .locator(
      'input[type="tel"]:visible, input[autocomplete="tel"]:visible, input[aria-label*="phone" i]:visible'
    )
    .first();
  if (!(await field.count().catch(() => 0))) return false;
  const current = await field.inputValue().catch(() => "");
  if (!current) await field.fill(String(phone).replace(/[^\d+]/g, "")).catch(() => {});
  return true;
}

export async function clickVisibleButton(page, names) {
  for (const name of names) {
    const button = page.getByRole("button", { name: new RegExp(`^${name}$`, "i") });
    if (await button.count().catch(() => 0)) {
      await button.first().click().catch(() => {});
      return true;
    }
  }
  return false;
}
