// language: JavaScript, file: lib/phone.js
// Read the destination number and message body out of Google's phone gate.
// The QR carries either an sms: link directly or a link to Google's consent
// page. On the consent page the sms: URI only exists after "Send SMS" is
// pressed, so a MAIN-world hook records it when the user clicks.

import { api, sleep } from "./api.js";

const DATA_RE = /^data:image\/[\w.+-]+;base64,/i;
const SMS_RE = /^sms:([^?&]*)(?:[?&](.*))?$/is;
const UI_WORDS = new Set([
  "GOOGLE", "LOADING", "PHONE", "VERIFY", "NUMBER", "DEVICE", "SECURITY",
  "REASONS", "CARRIER", "STANDARD", "RATES", "SEND", "TEXT", "MESSAGE",
]);

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch (_) {
    return value;
  }
}

export function parseSms(href) {
  const input = String(href || "").trim();
  if (!input) return { number: null, body: null, sms: null };

  let text = input.replace(/^smsto:/i, "sms:");
  if (!/^sms:/i.test(text)) return { number: null, body: null, sms: null };

  // Android also emits sms:number&body=… with no question mark.
  if (!text.includes("?") && text.includes("&")) text = text.replace("&", "?");

  let rawNumber = "";
  let rawBody = "";

  try {
    const url = new URL(text);
    rawNumber = url.hostname || url.pathname || "";
    rawBody = url.searchParams.get("body") || url.searchParams.get("Body") || "";
  } catch (_) {
    const match = SMS_RE.exec(text);
    if (!match) return { number: null, body: null, sms: null };
    rawNumber = match[1] || "";
    const query = new URLSearchParams(match[2] || "");
    rawBody = query.get("body") || query.get("Body") || "";
  }

  // sms://+1… -> hostname, sms:+1… -> pathname, sms:244444;? -> trailing junk
  const number =
    safeDecode(rawNumber)
      .replace(/^\/+/, "")
      .replace(/[\s().-]+/g, "")
      .replace(/[;,]+$/, "")
      .trim() || null;
  const body = String(rawBody).trim() || null;

  return {
    number,
    body,
    sms: number ? `sms:${number}${body ? `?body=${encodeURIComponent(body)}` : ""}` : null,
  };
}

export function guessFromText(text) {
  const source = String(text || "");
  if (!source) return { number: null, body: null };

  const anchored = /(?:send|text|message|saying|texting)\b/i.test(source);
  if (!anchored) return { number: null, body: null };

  const words = source.split(/[^A-Za-z0-9-]+/).filter(Boolean);
  let body = null;

  // A real body almost always mixes letters and digits.
  for (const word of words) {
    if (word.length < 4 || word.length > 16) continue;
    if (!/[A-Za-z]/.test(word) || !/[0-9]/.test(word)) continue;
    body = word;
    break;
  }

  // Otherwise accept an all-caps token, never a sentence word like "Loading".
  if (!body) {
    for (const word of words) {
      if (!/^[A-Z][A-Z0-9-]{3,15}$/.test(word)) continue;
      if (UI_WORDS.has(word)) continue;
      body = word;
      break;
    }
  }

  let number = null;
  const to = /\bto\s+(\+?\d{5,15})\b/i.exec(source);
  if (to) number = to[1];

  return { number, body };
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("image failed to load"));
    image.src = url;
  });
}

export async function decodeImageUrl(url) {
  if (!DATA_RE.test(url) && !/^blob:/.test(url)) return null;
  const image = await loadImage(url);
  const width = image.naturalWidth || image.width;
  const height = image.naturalHeight || image.height;
  if (!width || !height) return null;

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(image, 0, 0);

  const pixels = context.getImageData(0, 0, width, height);
  const decoder = window.jsQR;
  if (typeof decoder !== "function") throw new Error("jsQR bundle not loaded");

  const found = decoder(pixels.data, width, height, { inversionAttempts: "attemptBoth" });
  return found?.data || null;
}

// Runs in the page. Canvas first (Google draws the QR there), then every
// sizeable <img> redrawn through a canvas so tainting is caught locally.
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
      // tainted canvas
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
      // cross-origin image without CORS
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
      // nothing to salvage
    }
    if (out.svgs.length >= 4) break;
  }

  out.hrefs = [...document.querySelectorAll("a,area")]
    .map((anchor) => anchor.href)
    .filter(Boolean)
    .slice(0, 200);
  out.text = (document.body?.innerText || "").slice(0, 8000);
  out.hooked = document.documentElement.getAttribute("data-rac-sms") || "";

  // The consent page builds its sms: URI in the bundle and keeps it in the
  // bootstrap data, so a plain anchor scan misses it. Read the markup too.
  try {
    const markup = document.documentElement.innerHTML;
    const re = /sms:[^"'<>\\\s]{4,300}/gi;
    let match;
    while ((match = re.exec(markup)) && out.smsHits.length < 12) {
      const value = match[0].replace(/&amp;/g, "&");
      if (!out.smsHits.includes(value)) out.smsHits.push(value);
    }
  } catch (_) {
    // huge documents can still be scanned in pieces next pass
  }

  return out;
}

export async function scanTab(tabId) {
  const results = await api.scripting.executeScript({ target: { tabId }, func: collectPage });
  const value = results?.[0]?.result;
  if (!value) throw new Error("no page data returned");
  return value;
}

// Cheap poll for the watcher: no image encoding, just the hook and anchors.
function collectSignal() {
  return {
    url: location.href,
    hooked: document.documentElement.getAttribute("data-rac-sms") || "",
    hrefs: [...document.querySelectorAll('a[href^="sms:"]')]
      .map((anchor) => anchor.getAttribute("href"))
      .filter(Boolean)
      .slice(0, 20),
    text: (document.body?.innerText || "").slice(0, 2000),
  };
}

export async function scanSignal(tabId) {
  try {
    const results = await api.scripting.executeScript({ target: { tabId }, func: collectSignal });
    return results?.[0]?.result ?? null;
  } catch (_) {
    return null;
  }
}

// Records any sms: URI the page hands to window.open or puts on an anchor.
// Installed before the user presses "Send SMS", and re-installed after a
// navigation because a new document starts clean.
function hookSms() {
  if (window.__racSmsHook) {
    return document.documentElement.getAttribute("data-rac-sms") || "";
  }
  window.__racSmsHook = true;

  const record = (value) => {
    if (!value) return;
    try {
      document.documentElement.setAttribute("data-rac-sms", String(value));
    } catch (_) {
      // document gone
    }
  };

  const originalOpen = window.open;
  window.open = function patchedOpen(url, ...rest) {
    if (String(url || "").toLowerCase().startsWith("sms:")) {
      record(url);
      return null;
    }
    return originalOpen.apply(this, [url, ...rest]);
  };

  document.addEventListener(
    "click",
    (event) => {
      const anchor =
        event.target &&
        event.target.closest &&
        event.target.closest('a[href^="sms:"], a[href^="SMS:"]');
      if (anchor) record(anchor.getAttribute("href"));
    },
    true
  );

  return document.documentElement.getAttribute("data-rac-sms") || "";
}

export async function installSmsHook(tabId) {
  try {
    const results = await api.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: hookSms,
    });
    return results?.[0]?.result ?? "";
  } catch (_) {
    return "";
  }
}

function probeSendButtonInPage() {
  const candidates = [...document.querySelectorAll("a,button,[role=button]")];
  const button = candidates.find((element) => {
    const label = `${element.innerText || ""} ${element.getAttribute("aria-label") || ""}`
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
    return /send (sms|text|message)/.test(label) || label === "send sms";
  });
  return {
    url: location.href,
    hasButton: Boolean(button),
  };
}

export async function probeSendButton(tabId) {
  try {
    const results = await api.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: probeSendButtonInPage,
    });
    return results?.[0]?.result ?? null;
  } catch (_) {
    return null;
  }
}

function pressSendSmsInPage() {
  const candidates = [...document.querySelectorAll("a,button,[role=button]")];
  const button = candidates.find((element) => {
    const label = `${element.innerText || ""} ${element.getAttribute("aria-label") || ""}`
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
    return /send (sms|text|message)/.test(label) || label === "send sms";
  });
  if (!button) return { ok: false, reason: "button" };

  const href = button.getAttribute && button.getAttribute("href");
  if (href && String(href).toLowerCase().startsWith("sms:")) {
    document.documentElement.setAttribute("data-rac-sms", href);
  }

  try {
    button.click();
  } catch (_) {
    // event sequence below still fires
  }
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    try {
      button.dispatchEvent(
        new MouseEvent(type, { bubbles: true, cancelable: true, view: window })
      );
    } catch (_) {
      // keep trying
    }
  }

  return { ok: true };
}

export async function pressSendSms(tabId) {
  try {
    const results = await api.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: pressSendSmsInPage,
    });
    return results?.[0]?.result ?? { ok: false, reason: "no result" };
  } catch (error) {
    return { ok: false, reason: String(error.message || error) };
  }
}

export async function captureVisible(windowId) {
  try {
    return await api.tabs.captureVisibleTab(windowId, { format: "png" });
  } catch (_) {
    return null;
  }
}

function findSmsInPage(page) {
  for (const value of [...(page.hooked ? [page.hooked] : []), ...(page.smsHits || []), ...(page.hrefs || [])]) {
    if (String(value).toLowerCase().startsWith("sms:")) return parseSms(value);
  }
  const inline = /sms:(?:&#43;|\+)?[\d][^"'<>\s]{4,}/i.exec(page.text || "");
  if (inline) return parseSms(inline[0]);
  return { number: null, body: null, sms: null };
}

export function looksLikeConsent(page) {
  const url = String(page.url || "");
  const text = String(page.text || "").toLowerCase();
  return (
    url.includes("accounts.google.com") &&
    (url.includes("devicephoneverification") ||
      /send sms|verify your phone|sms message/.test(text))
  );
}

export async function extractFromPage(page) {
  const direct = findSmsInPage(page);
  if (direct.number || direct.body) {
    return { ...direct, page: page.url, text: page.text, source: "page" };
  }

  const payloads = [];
  for (const url of [...(page.canvases || []), ...(page.images || []), ...(page.svgs || [])]) {
    try {
      const decoded = await decodeImageUrl(url);
      if (decoded && !payloads.includes(decoded)) payloads.push(decoded);
    } catch (_) {
      // keep trying the rest
    }
  }

  for (const payload of payloads) {
    if (payload.toLowerCase().startsWith("sms:")) {
      return { ...parseSms(payload), payload, page: page.url, text: page.text, source: "qr" };
    }
  }

  if (payloads.length) {
    return {
      number: null,
      body: null,
      sms: null,
      payload: payloads[0],
      page: page.url,
      text: page.text,
      source: "qr-link",
    };
  }

  const guess = guessFromText(page.text);
  return { ...guess, sms: null, page: page.url, text: page.text, source: "text" };
}

// Opens the consent link in the foreground and installs the hook. Returns as
// soon as the tab is up: the caller watches it, the user presses Send SMS.
export async function openConsentPage(payload, storeId) {
  if (!/^https?:\/\//i.test(payload)) return null;

  // Background tab: the page renders and the hook runs without stealing focus.
  const options = { url: payload, active: false };
  if (storeId) options.cookieStoreId = storeId;
  const tab = await api.tabs.create(options);

  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    const current = await api.tabs.get(tab.id).catch(() => null);
    if (!current) return { tabId: tab.id, url: payload, gone: true };
    if (current.status === "complete") break;
    await sleep(500);
  }

  await sleep(1200);
  await installSmsHook(tab.id);
  const current = await api.tabs.get(tab.id).catch(() => null);
  return { tabId: tab.id, url: current?.url || payload, gone: false };
}
