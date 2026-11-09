// language: JavaScript, file: lib/lane.js
// One identity, one container. The Google cookie jar is the mutex; the container
// is what lets several of them exist in one browser.

import { api, isFirefox, sleep, withTimeout } from "./api.js";

export const SIGNUP = "https://www.roblox.com/CreateAccount";
export const HOME = "https://www.roblox.com/home";

// Roblox's Google SSO entry point. A plain GET 302s to Google and sets the
// HttpOnly RBXSsoExchangeCookie on auth.roblox.com that binds the OAuth state
// to this browser session. No CreateAccount page and no layout lottery.
export const SSO_INIT =
  "https://auth.roblox.com/v1/external/3161830277039358/sso/oauth/init?postAuthenticationIntentId=web-redirect";
// Direct Google sign-in. The Roblox OAuth flow only offers a challenge to a
// brand-new identity; this entry point is what shows the account-setup wizard,
// including the authenticator enrollment. Once signed in, the container keeps
// the Google session and the SSO init reuses it.
export const GOOGLE_PRIME =
  "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fmyaccount.google.com%2F";
export const COOKIE = ".ROBLOSECURITY";
// Answers with the bare exit address as plain text. Read from inside the lane
// so it reflects that container's proxy, not the browser's.
export const IP_ECHO = "https://api.ipify.org/";

export const LANE_COLUMNS = ["blue", "turquoise", "green", "purple", "orange", "pink"];

// ------------------------------------------------------------------ containers

export async function ensureContainer(index) {
  if (!isFirefox) return { storeId: null, name: "default" };
  const name = `RAC lane ${index + 1}`;
  const existing = await api.contextualIdentities.query({ name });
  if (existing.length) return { storeId: existing[0].cookieStoreId, name };

  const created = await api.contextualIdentities.create({
    name,
    color: LANE_COLUMNS[index % LANE_COLUMNS.length],
    icon: "circle",
  });
  return { storeId: created.cookieStoreId, name };
}

// ---------------------------------------------------------------- tab plumbing

function tabOptions(storeId, extra) {
  const options = { ...extra };
  if (storeId) options.cookieStoreId = storeId;
  return options;
}

export async function closeTab(tabId) {
  if (!tabId) return;
  try {
    await api.tabs.remove(tabId);
  } catch (_) {
    // already gone
  }
}

async function getTab(tabId) {
  try {
    return await api.tabs.get(tabId);
  } catch (_) {
    return null;
  }
}

async function reloadTab(tabId) {
  try {
    await api.tabs.reload(tabId);
  } catch (_) {
    // tab gone or reload refused
  }
}

// A lane's surface. "tab" is a plain background tab: nothing on screen, and
// content/awake.js (MAIN world) makes the page believe it is selected and gives
// it a frame clock that does not depend on being painted. "window" is the
// always-visible fallback — one small window per lane with its tab selected,
// which is the only thing Firefox treats as "visible"; occlusion is not part of
// its visibility model, so those windows may overlap or sit behind the runner.
export async function openLaneSurface(storeId, url, lane, surface = "tab") {
  if (surface !== "window" || !api.windows) {
    const tab = await api.tabs.create(tabOptions(storeId, { url, active: false }));
    return { tabId: tab.id, windowId: null, ownWindow: false };
  }

  const step = 26 * (lane % 8);
  try {
    const created = await api.windows.create({
      url,
      focused: false,
      type: "normal",
      width: 460,
      height: 420,
      left: 48 + step,
      top: 48 + step,
      ...(storeId ? { cookieStoreId: storeId } : {}),
    });
    const tab = created?.tabs?.[0];
    if (!tab) throw new Error("window opened without a tab");
    return { tabId: tab.id, windowId: created.id, ownWindow: true };
  } catch (_) {
    // A headless session, a single-window window manager, or a policy that
    // forbids new windows: fall back to a background tab rather than failing
    // the identity.
    const tab = await api.tabs.create(tabOptions(storeId, { url, active: false }));
    return { tabId: tab.id, windowId: null, ownWindow: false, fallback: true };
  }
}

export async function closeLaneSurface(surface) {
  if (!surface) return;
  if (surface.windowId) {
    try {
      await api.windows.remove(surface.windowId);
    } catch (_) {
      // already gone
    }
    return;
  }
  await closeTab(surface.tabId);
}

// A lane in "tab" mode is a background tab: nothing appears on screen, and the
// awake shim (content/awake.js, MAIN world) supplies the frame clock and the
// visibility answer the page expects. "window" mode is the old, always-visible
// fallback for a machine where the shim cannot win — a page that froze its own
// visibility getters, or a browser that refuses MAIN-world injection.
export async function laneVisibility(tabId) {
  let seen = null;
  try {
    const results = await api.scripting.executeScript({
      target: { tabId },
      func: () => ({
        state: document.visibilityState,
        focus: typeof document.hasFocus === "function" ? document.hasFocus() : false,
        url: location.href,
      }),
    });
    seen = results?.[0]?.result ?? null;
  } catch (_) {
    return null;
  }

  // The shim lives in the page's own world, where an isolated content script
  // cannot see it. Asking the main world for its frame count is the only honest
  // measure of whether a hidden lane is actually advancing.
  try {
    const main = await api.scripting.executeScript({
      target: { tabId },
      world: "MAIN",
      func: () => ({
        patched: Boolean(window.__racAwake && window.__racAwake.patched),
        frames: (window.__racAwake && window.__racAwake.frames) || 0,
        native:
          window.__racAwake && window.__racAwake.nativeVisibility
            ? window.__racAwake.nativeVisibility()
            : "unknown",
      }),
    });
    seen.awake = main?.[0]?.result ?? null;
  } catch (_) {
    seen.awake = null;
  }
  return seen;
}

export async function waitComplete(tabId, timeout, shouldStop) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");
    const tab = await getTab(tabId);
    if (!tab) throw new Error("tab closed");
    if (tab.status === "complete") return tab;
    await sleep(400);
  }
  throw new Error("page did not finish loading");
}

async function exec(tabId, func, args = [], world = null) {
  const injection = { target: { tabId }, func, args };
  if (world) injection.world = world;

  try {
    const results = await api.scripting.executeScript(injection);
    return results?.[0]?.result ?? null;
  } catch (error) {
    // Older Firefox builds can reject `world: "MAIN"`. The isolated world still
    // shares the DOM and the page's cookies, so retry there before giving up.
    if (world) {
      try {
        const results = await api.scripting.executeScript({ target: { tabId }, func, args });
        return results?.[0]?.result ?? null;
      } catch (_) {
        // fall through to the original error
      }
    }
    throw new Error(`page script failed: ${error.message || error}`);
  }
}

async function queryGoogleTab(storeId) {
  const tabs = await api.tabs.query(storeId ? { cookieStoreId: storeId } : {});
  return tabs.find((tab) => (tab.url || "").includes("accounts.google.com")) || null;
}

// ------------------------------------------------------------------- cookies

export async function grabCookie(storeId) {
  const cookies = await api.cookies.getAll(storeId ? { storeId } : {});
  const found = cookies.filter((cookie) => cookie.name === COOKIE && cookie.value);
  if (!found.length) return null;
  found.sort((a, b) => {
    const wide = Number(b.domain.startsWith(".")) - Number(a.domain.startsWith("."));
    return wide || b.value.length - a.value.length;
  });
  return found[0].value;
}

// The signup page can leave a site-scoped cookie before the domain-wide session
// is written. Give the domain-wide one a moment to replace it.
async function settleSessionCookie(storeId, first) {
  const deadline = Date.now() + 6000;
  let best = first;
  while (Date.now() < deadline) {
    const cookies = await api.cookies.getAll(storeId ? { storeId } : {});
    const wide = cookies
      .filter((cookie) => cookie.name === COOKIE && cookie.value && cookie.domain.startsWith("."))
      .sort((a, b) => b.value.length - a.value.length)[0];
    if (wide) return wide.value;
    await sleep(500);
  }
  return best;
}

export async function clearSiteCookies(storeId, needles = ["google.com", "roblox.com"]) {
  const cookies = await api.cookies.getAll(storeId ? { storeId } : {});
  let removed = 0;
  for (const cookie of cookies) {
    if (!needles.some((needle) => cookie.domain.includes(needle))) continue;
    const host = cookie.domain.startsWith(".") ? cookie.domain.slice(1) : cookie.domain;
    const url = `${cookie.secure ? "https" : "http"}://${host}${cookie.path || "/"}`;
    try {
      await api.cookies.remove({
        url,
        name: cookie.name,
        ...(storeId ? { storeId } : {}),
      });
      removed += 1;
    } catch (_) {
      // HttpOnly / partitioned cookie the API cannot address
    }
  }
  return removed;
}

// -------------------------------------------------------------- page functions

// Runs in the MAIN world. Returns candidates by rank; never clicks here.
function scanSignupPage() {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  const buttons = [...document.querySelectorAll("button,a,[role=button],div[role=button]")]
    .filter(visible)
    .map((el) => {
      const rect = el.getBoundingClientRect();
      const label = `${el.getAttribute("aria-label") || ""} ${el.innerText || ""}`
        .replace(/\s+/g, " ")
        .trim();
      return {
        label: label.slice(0, 80),
        tag: el.tagName,
        href: (el.getAttribute("href") || "").slice(0, 300),
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
        w: Math.round(rect.width),
        h: Math.round(rect.height),
      };
    });

  const google = buttons.filter((candidate) => /google/i.test(candidate.label));
  const rank = (candidate) => {
    const label = candidate.label.toLowerCase();
    const href = candidate.href.toLowerCase();
    let score = 0;
    if (label.includes("sign up with google")) score += 100;
    if (label.includes("signup") || label.includes("create")) score += 40;
    if (href.includes("sso") || href.includes("oauth") || href.includes("auth.roblox")) score += 60;
    if (candidate.w * candidate.h > 4000) score += 10;
    return score;
  };
  google.sort((a, b) => rank(b) - rank(a));

  return {
    url: location.href,
    hasUsername: Boolean(document.querySelector("#signup-username")),
    hasGoogle: google.length > 0,
    candidates: google.slice(0, 5),
  };
}

// MAIN world. Fires the real handler at coordinates while capturing window.open,
// so the SSO URL is recovered even when the button is not an anchor.
function activateSignupCta(x, y) {
  const captured = [];
  const originalOpen = window.open;
  window.open = function patchedOpen(url) {
    if (url) captured.push(String(url));
    return null;
  };

  let element = document.elementFromPoint(x, y);
  let probe = element;
  while (probe && probe !== document.body) {
    const role = probe.getAttribute && probe.getAttribute("role");
    if (probe.tagName === "BUTTON" || probe.tagName === "A" || role === "button") {
      element = probe;
      break;
    }
    probe = probe.parentElement;
  }

  if (element) {
    const href = element.getAttribute && element.getAttribute("href");
    if (href && href !== "#" && !href.toLowerCase().startsWith("javascript")) {
      try {
        captured.push(new URL(href, location.href).href);
      } catch (_) {
        // malformed href
      }
    }
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
      try {
        element.dispatchEvent(
          new MouseEvent(type, {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: x,
            clientY: y,
          })
        );
      } catch (_) {
        // keep going
      }
    }
  }

  window.open = originalOpen;
  return captured;
}

// MAIN world, on the /not-approved page. The UI is authoritative here: the same
// operations sent over HTTP can answer 200 while the account stays pending, but
// clicking through always moves it. Idempotent — the lane calls this in a loop
// until the page leaves /not-approved.
async function uiFinaliseStep(birthday, username, gender, acceptTerms) {
  const date = String(birthday || "1996-05-15");
  const [year, month, day] = date.split("-").map((part) => Number(part));
  const MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  const GENDERS = { 1: "Unspecified", 2: "Male", 3: "Female" };
  const MONTH = MONTHS[Math.max(0, month - 1)];
  const DAY = String(day);
  const YEAR = String(year);

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const shown = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  const nameOf = (el) =>
    String(el.getAttribute("aria-label") || el.innerText || el.value || "")
      .replace(/\s+/g, " ")
      .trim();
  const click = (el) => {
    if (!el) return false;
    try {
      el.click();
      return true;
    } catch (_) {
      return false;
    }
  };
  const findButton = (pattern, scope) =>
    Array.from((scope || document).querySelectorAll('button,[role="button"],a'))
      .filter(shown)
      .find((el) => pattern.test(nameOf(el)));

  const report = {
    agree: false,
    username: false,
    month: false,
    day: false,
    year: false,
    gender: false,
    next: false,
    nextDisabled: false,
    controls: "",
    buttons: "",
    text: "",
  };

  // 1. "Updated Agreements" modal. It can mount a moment after the page.
  const dialog = document.querySelector("#modal-dialog,[role=dialog]") || document;
  const agree =
    findButton(/^(i agree|agree|accept|got it)$/i, dialog) ||
    findButton(/^(i agree|agree|accept|got it)$/i);
  if (agree) {
    if (acceptTerms !== false) {
      report.agree = click(agree);
      await sleep(1000);
    } else {
      report.blocked = "updated-agreements modal left open by settings";
    }
  }

  // 2. Username. The page pre-fills the name Roblox generated; it is editable,
  // which is what keeps "Our generator" working without the upgrade endpoint.
  const wanted = String(username || "").trim();
  let usernameField = null;
  let originalUsername = null;
  if (wanted) {
    const inputs = Array.from(document.querySelectorAll("input"))
      .filter(shown)
      .filter((el) => {
        const type = String(el.type || "text").toLowerCase();
        return type === "text" || type === "";
      });
    const named = inputs.find((el) =>
      /user/i.test(`${el.name} ${el.id} ${el.getAttribute("aria-label") || ""}`)
    );
    const field = named || (inputs.length === 1 ? inputs[0] : null);
    if (field && field.value !== wanted) {
      try {
        usernameField = field;
        originalUsername = field.value;
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(field, wanted);
        field.dispatchEvent(new Event("input", { bubbles: true }));
        field.dispatchEvent(new Event("change", { bubbles: true }));
        field.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "a" }));
        field.blur();
        report.username = true;
      } catch (_) {
        // leave Roblox's name in place rather than fail the step
      }
    }
  }

  // 3. Birthday. Prefer a real <select> when one is visible — a controlled
  // React select needs the prototype setter, not `el.value = …`. Otherwise use
  // the Radix combobox and click the item in its portal.
  const KIND_LABEL = { month: "Month", day: "Day", year: "Year" };
  const triggerFor = (kind) =>
    Array.from(
      document.querySelectorAll(
        '[role="combobox"],[aria-haspopup="listbox"],button,select'
      )
    )
      .filter(shown)
      .find((el) => {
        const label = String(
          el.getAttribute("aria-label") || el.getAttribute("name") || ""
        ).toLowerCase();
        return label === kind;
      });

  function setSelect(select, value) {
    try {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
      setter.call(select, value);
      select.dispatchEvent(new Event("input", { bubbles: true }));
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    } catch (_) {
      return false;
    }
  }

  // The birthday compliance form renders a Radix combobox *and* a hidden
  // native select for each field. The hidden select is the reliable input: a
  // synthetic click on the combobox does not always open the portal, and the
  // select's options have the labels the form validates against.
  function hiddenSelect(kind) {
    const selects = Array.from(document.querySelectorAll("select"));
    if (kind === "month") {
      return selects.find((sel) => /january/i.test(sel.options[0]?.text || ""));
    }
    if (kind === "day") {
      return selects.find((sel) => /^0?1$/.test((sel.options[0]?.text || "").trim()));
    }
    if (kind === "year") {
      return selects.find((sel) => /^\d{4}$/.test((sel.options[0]?.text || "").trim()));
    }
    return null;
  }

  async function choose(kind, value, alternates) {
    const target = String(value);
    const select = hiddenSelect(kind);
    if (select) {
      const wanted = [target, ...(alternates || [])].map((item) =>
        String(item).trim().toLowerCase()
      );
      const option = Array.from(select.options).find(
        (item) =>
          wanted.includes(item.text.trim().toLowerCase()) ||
          wanted.includes(String(item.value).trim().toLowerCase())
      );
      if (option && setSelect(select, option.value)) {
        await sleep(200);
        if (String(select.value) === String(option.value)) return true;
      }
    }

    const trigger = triggerFor(kind);
    if (!trigger) return false;
    const current = nameOf(trigger);
    if (current === target) return true;

    click(trigger);
    await sleep(450);

    // Radix items are buttons with data-radix-collection-item, not role=option.
    const options = Array.from(
      document.querySelectorAll(
        '[role="option"], [data-radix-collection-item], [role="listbox"] li, [role="listbox"] button'
      )
    ).filter(shown);
    const pool = [target, ...(alternates || [])];
    const hit = pool
      .map((want) => options.find((el) => nameOf(el) === want))
      .find(Boolean);
    if (!hit) {
      // Close the listbox (not the dialog) so the next field is not blocked.
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      await sleep(250);
      return false;
    }
    click(hit);
    await sleep(350);

    // Confirm the trigger now shows the value; a click that did not take is the
    // difference between "trying" and "done".
    return nameOf(trigger) === target;
  }

  report.month = await choose("month", MONTH, [String(month)]);
  report.day = await choose("day", DAY, [DAY.padStart(2, "0")]);
  report.year = await choose("year", YEAR, []);
  report.gender = await choose("gender", GENDERS[Number(gender)] || GENDERS[1], []);
  if (!report.gender) {
    // The compliance form offers Female/Male only; "Unspecified" from the
    // settings has no control there. Pick the closest visible option.
    const wanted = (GENDERS[Number(gender)] || "Male").toLowerCase();
    const buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(shown);
    const hit =
      buttons.find((el) => nameOf(el).toLowerCase() === wanted) ||
      buttons.find((el) => /^(female|male|unspecified|prefer not to say)$/i.test(nameOf(el)));
    if (hit) {
      report.gender = click(hit);
    }
  }
  // Let React react to the selects and gender before looking at the submit
  // button's disabled state.
  await sleep(600);

  // 4. Create account. The birthday form is not inside the empty #modal-dialog,
  // so search the whole page; the submit control is labelled "Create account".
  const candidates = Array.from(
    document.querySelectorAll('button,[role="button"],input[type="submit"],a')
  ).filter(shown);
  const next = candidates.find(
    (el) =>
      /^(create account|continue|submit|done|next|finish)$/i.test(nameOf(el)) ||
      /^(continue|submit|done|next|finish)$/i.test(String(el.value || ""))
  );
  report.nextDisabled = Boolean(next && next.disabled);

  // A rejected username keeps Continue disabled. Put Roblox's own name back so
  // the account can still finish; the run keeps the generated name.
  if (report.nextDisabled && report.username && originalUsername !== null) {
    try {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(usernameField, originalUsername);
      usernameField.dispatchEvent(new Event("input", { bubbles: true }));
      usernameField.dispatchEvent(new Event("change", { bubbles: true }));
      report.username = false;
      report.usernameReverted = true;
    } catch (_) {
      // leave it; the diagnostic below will show the disabled button
    }
  }

  if (next && !next.disabled) {
    report.next = click(next);
    await sleep(1200);
  }

  report.controls = ["month", "day", "year"]
    .map((kind) => {
      const el = triggerFor(kind);
      return el ? `${kind}=${nameOf(el)}` : `${kind}=?`;
    })
    .join(" · ");
  report.buttons = candidates.slice(0, 10).map(nameOf).filter(Boolean).join(" | ");
  report.text = String(document.body ? document.body.innerText : "")
    .replace(/\s+/g, " ")
    .slice(0, 220);
  return report;
}

// MAIN world. Read-only proof that the session is usable; this is the only HTTP
// left in the finalise path.
async function verifySession() {
  try {
    const response = await fetch("https://users.roblox.com/v1/users/authenticated", {
      credentials: "include",
      headers: { accept: "application/json" },
    });
    const text = await response.text();
    let data = null;
    try {
      data = JSON.parse(text);
    } catch (_) {
      // 403 "User is moderated" is plain text
    }
    return {
      status: response.status,
      id: data?.id ?? null,
      name: data?.name ?? null,
      body: text.slice(0, 200),
    };
  } catch (error) {
    return { status: 0, id: null, name: null, body: String(error).slice(0, 200) };
  }
}

// ---------------------------------------------------------------- sign-in flow
// ---------------------------------------------------------------- sign-in flow

async function waitForSignup(tabId, storeId, timeout, shouldStop, log) {
  const deadline = Date.now() + timeout;
  let attempt = 0;
  let last = null;

  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");
    attempt += 1;
    try {
      await waitComplete(tabId, 45_000, shouldStop);
      await sleep(1200);
      last = await exec(tabId, scanSignupPage, [], "MAIN");
      if (last?.hasUsername && last?.hasGoogle) {
        log(`layout ${attempt}: google offered`);
        return last;
      }
      log(`layout ${attempt}: google=${Boolean(last?.hasGoogle)} username=${Boolean(last?.hasUsername)} - reloading`);
    } catch (error) {
      if (String(error.message) === "stopped") throw error;
      log(`layout ${attempt}: ${error.message}`);
    }

    try {
      // Python re-rolled this per attempt: the layout variant follows the
      // session, so a plain reload is not always enough.
      await clearSiteCookies(storeId, ["roblox.com"]);
      await api.tabs.update(tabId, { url: SIGNUP });
      await sleep(1500);
    } catch (_) {
      throw new Error("signup tab closed");
    }
  }
  throw new Error("no Google button in any layout");
}

async function openGoogleTab(tabId, scan, storeId, timeout, shouldStop, log) {
  const best = scan.candidates[0];
  if (best) log(`cta: ${JSON.stringify(best.label)} <${best.tag}> ${best.w}x${best.h}`);

  if (best) {
    const href = String(best.href || "");
    const looksLikeSso = /^(\/|https?:)/i.test(href) && /sso|oauth|auth|google|signup/i.test(href);
    let navigated = false;

    // A real anchor does not need a synthetic click: navigating the tab is both
    // trusted and popup-free. Only fire the handler when there is no usable href.
    if (looksLikeSso) {
      try {
        const target = new URL(href, "https://www.roblox.com").href;
        log(`sso href -> ${target.slice(0, 110)}`);
        await api.tabs.update(tabId, { url: target });
        navigated = true;
      } catch (_) {
        navigated = false;
      }
    }

    if (!navigated) {
      const captured = await exec(tabId, activateSignupCta, [best.x, best.y], "MAIN");
      const url = (captured || []).find((value) => /^(https?:)?\/\//.test(value));
      if (url) {
        log(`sso url captured -> ${url.slice(0, 110)}`);
        await api.tabs.update(tabId, { url });
      } else {
        log("cta fired without a captured url; waiting for a google tab");
      }
    }
  }

  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");
    const googleTab = await queryGoogleTab(storeId);
    if (googleTab) return googleTab;
    const tab = await getTab(tabId);
    if (!tab) throw new Error("signup tab closed before Google loaded");
    if ((tab.url || "").includes("accounts.google.com")) return tab;
    await sleep(800);
  }
  throw new Error("Google sign-in never opened");
}

// Direct counterpart to openGoogleTab: the init request is already in flight in
// this tab, so all that is left is to wait for the 302 to land on Google.
async function waitForGoogle(tabId, timeout, shouldStop, log, options = {}) {
  const { ignorePrime = false } = options;
  const deadline = Date.now() + timeout;
  let last = "";

  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");
    const tab = await getTab(tabId);
    if (!tab) throw new Error("tab closed");

    const url = tab.url || "";
    // After a prime, the tab is already on accounts.google.com. Returning here
    // would hand the old prime URL to pumpCredentials and never open Roblox's
    // SSO init. The prime URL carries continue=…myaccount.google.com; the real
    // OAuth URL carries Roblox's own continue target.
    if (url.includes("accounts.google.com") && !(ignorePrime && url.includes("myaccount.google.com"))) {
      return tab;
    }
    // With a live Google session the SSO init can skip Google entirely and land
    // straight on Roblox. That is a success, not a failed Google redirect.
    if (/roblox\.com\/(sso-complete|not-approved|home)/.test(url)) {
      return tab;
    }
    if (url && url !== last) {
      last = url;
      log(`sso init: ${url.slice(0, 90)}`);
    }
    if (tab.status === "complete" && /\/sso\/oauth\/init/.test(url)) {
      throw new Error(`sso init refused (${url.slice(0, 80)})`);
    }
    await sleep(500);
  }
  throw new Error("sso init did not reach Google");
}

async function pumpCredentials(tab, identity, timeout, shouldStop, storeId, log, options = {}) {
  const { codesFor = null, abortIf = null, otpFor = null, surface = null } = options;
  const deadline = Date.now() + timeout;

  // Read per handoff, not once: a backup code can be minted *while* the lane is
  // sitting on the challenge, and the next message has to carry it.
  const readCodes = () => {
    const list = typeof codesFor === "function" ? codesFor() : null;
    return (Array.isArray(list) ? list : []).filter(Boolean);
  };

  let lastNote = 0;
  let lastProbe = 0;
  let sent = 0;
  let misses = 0;
  let closedNoted = false;
  let visibility = null;
  let raised = false;
  let warnedStall = false;
  let zeroFrames = 0;

  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");

    const cookie = await grabCookie(storeId);
    if (cookie) {
      const settled = await settleSessionCookie(storeId, cookie);
      log(`session cookie landed (${settled.length} chars)`);
      return settled;
    }

    const current = await getTab(tab.id);
    if (!current) {
      if (!closedNoted) {
        closedNoted = true;
        log("Google tab closed — waiting for the cookie to settle");
      }
    } else {
      // A lane that cannot get a second factor will never succeed. Stop in
      // seconds with the reason instead of burning the whole time budget.
      const abort = typeof abortIf === "function" ? abortIf() : null;
      if (abort) throw new Error(abort);

      const codeList = readCodes();
      try {
        // Every handoff also runs one tick inside the page, so the flow keeps
        // moving even when background-timer throttling would have slowed the
        // content script's own interval.
        await api.tabs.sendMessage(tab.id, {
          type: "google-fill",
          email: identity.email,
          password: identity.password,
          code: codeList[0] || "",
          codes: codeList,
          // Recomputed per handoff: a fresh identity's secret is captured by the
          // content script mid-run and lands in liveOtp, not in storage yet.
          otp:
            (typeof otpFor === "function" ? otpFor(identity.email) : "") ||
            identity.otp_secret ||
            "",
        });
        sent += 1;
        misses = 0;
      } catch (_) {
        // The declared content script runs at document_idle, so the first
        // handoff can arrive before it does. The old fallback injected the file
        // by hand — which is a *second* state machine in the same document,
        // two fills and two submits on one field. Wait for the real one.
        misses += 1;
        if (misses === 15) log("no google agent in the tab yet — still waiting for it");
      }
    }

    // The point of the awake shim is that a hidden lane still advances, so log
    // both halves: what the tab claims and how many frames the page actually
    // received from the shim.
    if (Date.now() - lastProbe > 6000) {
      lastProbe = Date.now();
      const seen = await laneVisibility(tab.id);
      if (seen) {
        const awake = seen.awake;
        const signature = `${seen.state}:${awake?.patched ? awake.frames > 0 : "none"}`;
        if (signature !== visibility) {
          visibility = signature;
          const parts = [`google tab is ${seen.state}`];
          if (awake) {
            parts.push(
              awake.patched
                ? `awake shim · ${awake.frames} frames`
                : `no awake shim · native ${awake.native}`
            );
          }
          log(parts.join(" · "));
        }
        if (seen.state === "hidden" && awake && awake.patched && awake.frames === 0) {
          zeroFrames += 1;
          if (zeroFrames === 3 && !warnedStall) {
            warnedStall = true;
            log("hidden lane with no frames — the shim is installed but never ticked");
          }
        }
        if (seen.state === "hidden" && surface?.windowId && !raised) {
          raised = true;
          log("hidden lane — raising its window; switch lane surface to a tab to stop this");
          try {
            await api.windows.update(surface.windowId, { focused: true });
          } catch (_) {
            // focus is best-effort
          }
        }
      }
    }

    if (Date.now() - lastNote > 20_000) {
      lastNote = Date.now();
      const left = Math.ceil((deadline - Date.now()) / 1000);
      log(`waiting on Google... ${left}s left, ${sent} handoffs`);
    }
    await sleep(2000);
  }
  throw new Error("no .ROBLOSECURITY within the time budget");
}

// The SSO callback sets .ROBLOSECURITY before the browser has finished the
// redirect chain that provisions the account. Navigate away in that window and
// the account is left half-created, which is what makes /v1/account/upgrade
// answer "User is moderated" forever. So wait for a terminal page first.
function shortUrl(url) {
  return String(url || "").split("?")[0].slice(-70);
}

async function awaitSsoSettled(tabId, timeout, shouldStop, log) {
  const deadline = Date.now() + Math.min(timeout, 30_000);
  let last = "";
  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) return;
    const tab = await getTab(tabId).catch(() => null);
    if (!tab) return;
    last = tab.url || "";
    if (/not-approved|\/home|sso-complete/.test(last)) {
      log(`sso settled on ${shortUrl(last)}`);
      return;
    }
    await sleep(700);
  }
  log(`sso did not settle in time (last: ${shortUrl(last)})`);
}

// MAIN world. The /sso-complete page sometimes renders a Continue button
// instead of redirecting on its own. Clicking it is safe; if there is no
// button the caller simply waits for the redirect.
function clickSsoContinue() {
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };
  const label = (el) =>
    `${el.getAttribute("aria-label") || ""} ${el.innerText || ""}`
      .replace(/\s+/g, " ")
      .trim();
  const hit = [...document.querySelectorAll('button,[role="button"],a,input[type="submit"]')]
    .filter(visible)
    .find((el) => /^(continue|continue to roblox|go to roblox|ok|done|next)$/i.test(label(el)));
  if (!hit) return null;
  try {
    hit.click();
    return label(hit).slice(0, 60);
  } catch (_) {
    return null;
  }
}

async function finaliseOnTab(tabId, timeout, shouldStop, log, options = {}) {
  const landing = (await getTab(tabId).catch(() => null))?.url || "";
  log(`finalise from ${shortUrl(landing)}`);

  const birthday = options.birthday || "1996-05-15";
  const username = options.username || "";
  const budget = Math.min(timeout, 150_000);
  const deadline = Date.now() + budget;
  let step = null;
  let lastText = "";
  let lastDid = "";
  let lastDiag = "";
  let attempts = 0;
  let homeWaits = 0;
  let ssoClicks = 0;
  let lastUrl = "";
  let stuck = 0;
  let reloads = 0;
  let nextClickedAt = 0;
  let ssoSince = 0;
  let ssoReloads = 0;

  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");

    const tab = await getTab(tabId).catch(() => null);
    if (!tab) throw new Error("finalise tab closed");
    const url = tab.url || "";
    if (url !== lastUrl) {
      lastUrl = url;
      if (/sso-complete/.test(url)) ssoSince = Date.now();
      else ssoSince = 0;
      if (!/not-approved/.test(url)) stuck = 0;
      nextClickedAt = 0;
      log(`finalise page ${shortUrl(url)}`);
    }

    // The username/birthday form lives on /not-approved. Never navigate away
    // from it: that page is the one the site uses to mark the account usable.
    if (/\/not-approved/.test(url)) {
      if (attempts >= 20) break;
      attempts += 1;

      step = await withTimeout(
        exec(
          tabId,
          uiFinaliseStep,
          [birthday, username, options.gender || 1, options.acceptTerms !== false],
          "MAIN"
        ),
        25_000,
        "ui finalise"
      ).catch((error) => {
        log(`ui finalise step failed: ${error.message || error}`);
        return null;
      });

      if (step) {
        if (step.blocked) {
          log(`ui finalise blocked: ${step.blocked}`);
          break;
        }
        lastText = step.text || lastText;
        const did = ["agree", "username", "month", "day", "year", "gender", "next"]
          .filter((key) => step[key])
          .join(", ");
        if (did && did !== lastDid) {
          lastDid = did;
          log(`ui finalise: ${did}`);
        }
        if (step.usernameReverted) {
          log("ui finalise: chosen username was rejected — kept Roblox's generated name");
        }
        if (!step.next) {
          const diag =
            `ui finalise stuck · ${step.controls || "no birthday controls"}` +
            `${step.nextDisabled ? " · continue disabled" : ""}` +
            ` · buttons: ${step.buttons || "none"}`;
          if (diag !== lastDiag) {
            lastDiag = diag;
            log(diag);
          }
        }
        stuck = step.next ? 0 : stuck + 1;
        if (step.next) {
          if (!nextClickedAt) nextClickedAt = Date.now();
        } else {
          nextClickedAt = 0;
        }
      } else {
        stuck += 1;
        nextClickedAt = 0;
      }

      // A reload clears a frozen React tree / stale DOM. The form is
      // idempotent, so re-running it after the reload is safe; if the account
      // actually finished, /not-approved redirects to /home and the loop
      // verifies the session there.
      if (stuck >= 2 && reloads < 3) {
        reloads += 1;
        stuck = 0;
        nextClickedAt = 0;
        log(`ui finalise: reloading /not-approved (${reloads}/3)`);
        await reloadTab(tabId);
        await sleep(2500);
        continue;
      }

      // Continue was clicked but the page did not move. Give it 8 seconds,
      // then reload rather than clicking a dead button forever.
      if (nextClickedAt && Date.now() - nextClickedAt > 8000 && reloads < 3) {
        reloads += 1;
        nextClickedAt = 0;
        log(`ui finalise: still on /not-approved after submit — reloading (${reloads}/3)`);
        await reloadTab(tabId);
        await sleep(2500);
        continue;
      }

      await sleep(2200);
      continue;
    }

    // Already signed in and finished provisioning: /home is the success page.
    // Verify the session here instead of running the birthday form on it.
    if (/\/home/.test(url)) {
      const session = await exec(tabId, verifySession, [], "MAIN").catch(() => null);
      if (session && session.status === 200) {
        log(`ui finalise · session 200 · ${session.name || session.id}`);
        return { session, ui: step, username: session.name || username || null };
      }
      homeWaits += 1;
      if (homeWaits === 1 || homeWaits % 8 === 0) {
        log(`finalise: at /home, session ${session?.status ?? 0} — waiting for provisioning`);
      }
      await sleep(2500);
      continue;
    }

    // The SSO callback can park here for a moment. Give it a Continue click
    // and, more importantly, do not navigate away while it provisions.
    if (/sso-complete/.test(url)) {
      if (ssoClicks < 3) {
        const hit = await exec(tabId, clickSsoContinue, [], "MAIN").catch(() => null);
        if (hit) {
          ssoClicks += 1;
          log(`sso-complete: clicked ${hit}`);
        }
      }
      if (!ssoSince) ssoSince = Date.now();
      if (Date.now() - ssoSince > 12_000 && ssoReloads < 2) {
        ssoReloads += 1;
        ssoSince = Date.now();
        log(`sso-complete stuck — reloading (${ssoReloads}/2)`);
        await reloadTab(tabId);
        await sleep(2500);
        continue;
      }
      await sleep(1500);
      continue;
    }

    // Blank or unknown page: wait for the next navigation.
    await sleep(1500);
  }

  const session = await exec(tabId, verifySession, [], "MAIN").catch(() => null);
  log(
    `ui finalise gave up · session ${session?.status ?? 0}` +
      (lastText ? ` · page: ${lastText}` : "")
  );
  return { session: session || { status: 0 }, ui: step, username: session?.name || username || null };
}

// Sign the identity in to Google directly, before Roblox's OAuth flow. This is
// the only entry point that shows the account-setup wizard to a brand-new user;
// the OAuth flow goes straight to a challenge, which is why codes-only identities
// were hit-or-miss. A failure here is not fatal: the caller falls back to the
// normal SSO lane with whatever Google session the prime left behind.
async function primeGoogleSession(tabId, identity, timeout, shouldStop, storeId, log, options = {}) {
  const { codesFor = () => [], otpFor = () => "", enrolledFor = () => false } = options;
  const email = String(identity.email || "").toLowerCase();
  const initialSecret =
    (typeof otpFor === "function" ? otpFor(identity.email) : "") || identity.otp_secret || "";
  const deadline = Date.now() + Math.min(timeout, 120_000);

  let lastUrl = "";
  let lastBody = "";
  let signedInAt = 0;
  let sent = 0;
  let secretAt = 0;

  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");

    const tab = await getTab(tabId);
    if (!tab) throw new Error("prime tab closed");
    const url = tab.url || "";
    if (url !== lastUrl) {
      lastUrl = url;
      log(`prime: ${shortUrl(url)}`);
    }

    const secret =
      (typeof otpFor === "function" ? otpFor(identity.email) : "") || "";
    if (secret && !initialSecret && !secretAt) {
      secretAt = Date.now();
      log("prime: authenticator secret captured — waiting for Google to accept the code");
    }
    if (secret && secretAt) {
      // The key is scraped before the wizard has advanced to the code entry,
      // let alone accepted it. Leaving the tab at that moment navigates away
      // mid-enrolment, so wait for the content script's confirmation.
      const enrolled =
        typeof enrolledFor === "function" ? enrolledFor(identity.email) : false;
      if (enrolled) {
        log("prime: authenticator enrolled");
        await sleep(1500);
        return true;
      }
      if (Date.now() - secretAt > 20_000) {
        log(
          "prime: secret captured but enrollment was not confirmed — keeping the secret and continuing"
        );
        return true;
      }
    }

    if (/^https:\/\/myaccount\.google\.com\//.test(url)) {
      if (!signedInAt) {
        signedInAt = Date.now();
        log(
          initialSecret
            ? "prime: signed in; authenticator already on file"
            : "prime: signed in; waiting for the 2SV setup card"
        );
      }
      // A stored secret means there is nothing to enrol — the session is the
      // product. A fresh account gets a setup card, which the content script
      // needs a few ticks to drive. The QR/setup page can take longer than the
      // first page, so each setup screen restarts this window.
      if (initialSecret || Date.now() - signedInAt > 60_000) {
        log(
          initialSecret
            ? "prime: session ready"
            : "prime: signed in, but no authenticator secret appeared"
        );
        return Boolean(initialSecret);
      }
    }

    const body = await exec(
      tabId,
      () => String(document.body ? document.body.innerText : "").slice(0, 5000),
      [],
      "MAIN"
    ).catch(() => "");

    if (body && body !== lastBody) {
      lastBody = body;
      // A setup screen can take longer than the landing page. Keep the 60s
      // window alive while the QR/key wizard is actually on screen.
      if (
        signedInAt &&
        /scan the qr|can.t scan|setup key|enter a setup key|enter your email address and this key|tap add to finish|add authenticator app/i.test(
          body
        )
      ) {
        signedInAt = Date.now();
      }
      if (/don.?t meet your organization|2-step verification policy/i.test(body)) {
        log("prime: policy wall on direct sign-in — falling back to the SSO lane");
        return false;
      }
      if (/type the text you hear or see|captcha image/i.test(body)) {
        log("prime: captcha on direct sign-in — falling back to the SSO lane");
        return false;
      }
      // The risk-based "Verify it's you" phone page cannot be answered here. The
      // SSO lane below can still use a backup code, and it takes seconds rather
      // than waiting out the prime budget.
      if (/challenge\/iap|enter a phone number to get a text message/i.test(`${url} ${body}`)) {
        log("prime: phone verification gate — falling back to the SSO lane");
        return false;
      }
    }

    try {
      const codes = (typeof codesFor === "function" ? codesFor() : []).filter(Boolean);
      await api.tabs.sendMessage(tabId, {
        type: "google-fill",
        email: identity.email,
        password: identity.password,
        code: codes[0] || "",
        codes,
        otp:
          (typeof otpFor === "function" ? otpFor(identity.email) : "") ||
          identity.otp_secret ||
          "",
      });
      sent += 1;
    } catch (_) {
      // The content script is declared at document_idle; the first handoff can
      // arrive before it does. The next tick will land.
    }

    await sleep(2000);
  }

  log(`prime: timed out after ${sent} handoffs — falling back to the SSO lane`);
  return false;
}

export async function runIdentity(options) {
  const {
    identity,
    lane,
    storeId,
    timeout = 240,
    shouldStop,
    log,
    keepTabs = false,
    onSession,
    otpFor,
    enrolledFor,
    username = "",
    birthday = "1996-05-15",
    gender = 1,
    acceptTerms = true,
    laneSurface = "tab",
    probeIp = false,
    primeGoogle = true,
  } = options;

  const started = Date.now();
  const budget = timeout * 1000;
  let tabId = null;
  let googleTab = null;
  let surface = null;
  let holdTab = Boolean(keepTabs);

  const logFor = (text) => log(`[lane ${lane + 1}] ${text}`);

  try {
    const wiped = await clearSiteCookies(storeId);
    if (wiped) logFor(`cleared ${wiped} stale cookies`);

    const startUrl = probeIp ? IP_ECHO : primeGoogle ? GOOGLE_PRIME : SSO_INIT;
    surface = await openLaneSurface(storeId, startUrl, lane, laneSurface);
    tabId = surface.tabId;
    if (probeIp) {
      // The exit IP has to be read from inside the lane, because the proxy is
      // bound to this container. api.ipify.org answers with the bare address as
      // a top-level navigation, so no CORS or CSP is involved.
      await sleep(1400);
      const seen = await exec(
        tabId,
        () => String(document.body ? document.body.innerText : "").trim().slice(0, 60),
        [],
        "MAIN"
      ).catch(() => "");
      logFor(`lane exit ip ${seen || "unknown"}`);
      if (seen && identity.exit_ip && identity.exit_ip !== seen) {
        logFor(`exit ip changed ${identity.exit_ip} -> ${seen} — rotating proxies break sign-in`);
      }
      await api.tabs.update(tabId, { url: primeGoogle ? GOOGLE_PRIME : SSO_INIT }).catch(() => {});
    }
    logFor(
      `${identity.email} · ${primeGoogle ? "google prime opened" : "sso init opened"}` +
        (surface.ownWindow
          ? " in its own window"
          : surface.fallback
            ? " in a background tab (window creation failed)"
            : " in a background tab")
    );

    if (primeGoogle) {
      let primed = false;
      try {
        primed = await primeGoogleSession(tabId, identity, budget, shouldStop, storeId, logFor, {
          codesFor: () => identity.codes || [],
          otpFor,
          enrolledFor,
        });
      } catch (error) {
        if (String(error.message || error) === "stopped") throw error;
        logFor(`google prime failed (${error.message || error}) — continuing with the SSO lane`);
      }
      logFor(
        primed
          ? "google prime: authenticator ready — starting Roblox SSO"
          : "google prime: no new factor — starting Roblox SSO with the session it left"
      );
      await api.tabs.update(tabId, { url: SSO_INIT }).catch(() => {});
    }

    try {
      googleTab = await waitForGoogle(tabId, Math.min(budget, 30_000), shouldStop, logFor, {
        ignorePrime: primeGoogle,
      });
      logFor("sso init -> google (direct)");
    } catch (error) {
      if (String(error.message) === "stopped") throw error;
      logFor(`direct sso init failed (${error.message}) - falling back to the signup CTA`);
      await api.tabs.update(tabId, { url: SIGNUP });
      const scan = await waitForSignup(tabId, storeId, Math.min(budget, 120_000), shouldStop, logFor);
      googleTab = await openGoogleTab(
        tabId,
        scan,
        storeId,
        Math.min(budget, 90_000),
        shouldStop,
        logFor
      );
    }
    logFor(`google: ${(googleTab.url || "").slice(0, 90)}`);

    const cookie = await pumpCredentials(googleTab, identity, budget, shouldStop, storeId, logFor, {
      // Read through a function: a set can be minted mid-lane when Google asks
      // for a code the runner did not have yet.
      codesFor: () => identity.codes || [],
      // Set by the runner when the challenge arrived and no set could be minted.
      abortIf: () => (identity.noSecondFactor ? identity.noSecondFactor : null),
      otpFor,
      surface,
    });

    if (googleTab.id !== tabId) await closeTab(googleTab.id);
    googleTab = null;

    // Persist before finalising: the cookie is the product, and a crash between
    // here and the upgrade must not lose it.
    if (typeof onSession === "function") {
      try {
        await onSession(cookie);
      } catch (error) {
        logFor(`session save failed: ${error.message || error}`);
      }
    }

    // The SSO callback has set the cookie, but the account may still be
    // provisioning. Let the redirect chain finish before touching the page.
    await awaitSsoSettled(tabId, 25_000, shouldStop, logFor);

    const result = await (async () => {
      try {
        return await finaliseOnTab(tabId, budget, shouldStop, logFor, {
          username,
          birthday,
          gender,
          acceptTerms,
        });
      } catch (error) {
        if (String(error.message || error).includes("stopped")) throw error;
        logFor(`finalise failed: ${error.message || error}`);
        holdTab = true;
        const failure = { status: 0, body: String(error.message || error) };
        return { upgrade: failure, session: failure };
      }
    })();
    const ok = result.session.status === 200;

    if (!ok && !holdTab) {
      logFor("account still pending - keeping the tab for a manual finalise");
      holdTab = true;
    }

    return {
      cookie,
      session: result.session,
      username: result.username || (ok ? result.session?.name : null),
      finalised: ok,
      elapsed: Math.round((Date.now() - started) / 1000),
      keepTabId: holdTab ? tabId : null,
    };
  } finally {
    if (googleTab && googleTab.id !== tabId) await closeTab(googleTab.id);
    if (!holdTab && surface) await closeLaneSurface(surface);
  }
}

export async function retryFinalise(tabId, timeout, shouldStop, log, options = {}) {
  const result = await finaliseOnTab(tabId, timeout, shouldStop, log, options);
  return result;
}
