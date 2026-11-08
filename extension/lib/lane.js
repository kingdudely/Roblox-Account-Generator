// language: JavaScript, file: lib/lane.js
// One identity, one container. The Google cookie jar is the mutex; the container
// is what lets several of them exist in one browser.

import { api, isFirefox, sleep, withTimeout } from "./api.js";
import { makeUsername } from "./names.js";

export const SIGNUP = "https://www.roblox.com/CreateAccount";
export const HOME = "https://www.roblox.com/home";

// Roblox's Google SSO entry point. A plain GET 302s to Google and sets the
// HttpOnly RBXSsoExchangeCookie on auth.roblox.com that binds the OAuth state
// to this browser session. No CreateAccount page and no layout lottery.
export const SSO_INIT =
  "https://auth.roblox.com/v1/external/3161830277039358/sso/oauth/init?postAuthenticationIntentId=web-redirect";
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

// MAIN world, on a roblox.com page. HttpOnly cookies ride along automatically.
async function upgradeAndValidate(birthday, username, gender) {
  async function validate() {
    try {
      const response = await fetch("https://users.roblox.com/v1/users/authenticated", {
        credentials: "include",
        headers: { accept: "application/json" },
      });
      const text = await response.text();
      const out = { status: response.status, body: text.slice(0, 300) };
      try {
        const data = JSON.parse(text);
        out.id = data.id;
        out.name = data.name;
      } catch (_) {
        // 403 "User is moderated" is plain text
      }
      return out;
    } catch (error) {
      return { status: 0, body: String(error).slice(0, 200) };
    }
  }

  let session = await validate();
  if (session.status === 200) {
    return {
      upgrade: { status: 0, body: "already live" },
      session,
      url: location.href,
      username: username || null,
    };
  }

  const headers = { "content-type": "application/json;charset=UTF-8" };
  const body = {
    upgradeType: "OAuth",
    birthday: `${birthday}T00:00:00.000Z`,
    gender: [1, 2, 3].includes(Number(gender)) ? Number(gender) : 1,
  };
  if (username) body.username = username;
  const payload = JSON.stringify(body);

  async function post(token) {
    return fetch("https://auth.roblox.com/v1/account/upgrade", {
      method: "POST",
      credentials: "include",
      headers: token ? { ...headers, "x-csrf-token": token } : headers,
      body: payload,
    });
  }

  let upgrade = { status: 0, body: "" };
  try {
    let response = await post(null);
    if (response.status === 403) {
      const token = response.headers.get("x-csrf-token");
      if (token) response = await post(token);
    }
    const text = (await response.text()).slice(0, 500);
    let code = null;
    try {
      const data = JSON.parse(text);
      code = data?.errors?.[0]?.code ?? data?.code ?? null;
    } catch (_) {
      // plain-text error
    }
    upgrade = { status: response.status, code, body: text };
  } catch (error) {
    upgrade = { status: 0, body: String(error).slice(0, 200) };
  }

  // A fresh upgrade can take a moment to propagate to users.roblox.com.
  for (let attempt = 0; attempt < 4 && session.status !== 200; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    session = await validate();
  }

  const usernameTaken =
    Boolean(username) && upgrade.status === 400 && [9, 11, 12].includes(upgrade.code);

  return { upgrade, session, url: location.href, username: username || null, usernameTaken };
}

// The /not-approved page also mounts an "Updated Agreements" modal. It is not
// part of /v1/account/upgrade; accepting through this endpoint plus the page's
// local cache flag is what makes the modal stop appearing on later loads.
async function acceptUserAgreements() {
  const base = "https://apis.roblox.com/user-agreements";
  const stamp = () => new Date().toISOString();
  const cacheAccepted = () => {
    try {
      localStorage.setItem(
        "Roblox.UserAgreements",
        JSON.stringify({
          doesUserNeedToAcceptAgreements: false,
          lastFetchTimestamp: stamp(),
        })
      );
    } catch (_) {
      // storage disabled; the server-side acceptance still stands
    }
  };

  try {
    const listed = await fetch(`${base}/v1/agreements-resolution/web`, {
      credentials: "include",
      headers: { accept: "application/json" },
    });
    const listText = await listed.text();
    if (!listed.ok) {
      return { listed: 0, accepted: 0, status: listed.status, body: listText.slice(0, 300) };
    }

    let agreements = [];
    try {
      agreements = JSON.parse(listText);
    } catch (_) {
      agreements = [];
    }
    if (!Array.isArray(agreements) || !agreements.length) {
      cacheAccepted();
      return { listed: 0, accepted: 0, status: 200, body: "" };
    }

    const payload = JSON.stringify({
      acceptances: agreements.map((agreement) => ({ agreementId: agreement.id })),
    });
    const post = (token) =>
      fetch(`${base}/v1/acceptances`, {
        method: "POST",
        credentials: "include",
        headers: token
          ? {
              accept: "application/json",
              "content-type": "application/json;charset=UTF-8",
              "x-csrf-token": token,
            }
          : {
              accept: "application/json",
              "content-type": "application/json;charset=UTF-8",
            },
        body: payload,
      });

    let response = await post(null);
    if (response.status === 403) {
      const token = response.headers.get("x-csrf-token");
      if (token) response = await post(token);
    }
    const body = (await response.text()).slice(0, 500);
    let accepted = false;
    if (response.ok) {
      accepted = true;
      try {
        const data = JSON.parse(body);
        if (Array.isArray(data?.results)) {
          accepted = data.results.every((result) => Number(result.errorCode) === 0);
        }
      } catch (_) {
        // a 200 with an unreadable body is still a successful acceptance
      }
    }
    if (accepted) cacheAccepted();

    return {
      listed: agreements.length,
      accepted: accepted ? agreements.length : 0,
      status: response.status,
      body,
    };
  } catch (error) {
    return { listed: 0, accepted: 0, status: 0, body: String(error).slice(0, 200) };
  }
}

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
async function waitForGoogle(tabId, timeout, shouldStop, log) {
  const deadline = Date.now() + timeout;
  let last = "";

  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) throw new Error("stopped");
    const tab = await getTab(tabId);
    if (!tab) throw new Error("tab closed");

    const url = tab.url || "";
    if (url.includes("accounts.google.com")) return tab;
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
  const { codes = [], otpFor = null, surface = null } = options;
  const deadline = Date.now() + timeout;
  const codeList = (Array.isArray(codes) ? codes : []).filter(Boolean);

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

// Hitting SSO init inside the same container rides the Google session that is
// already there, so this is a silent round-trip — no credentials, no form. If
// Google answers with an interactive page instead, the session is gone and we
// stop rather than pretend.
async function resignIn(tabId, timeout, shouldStop, log) {
  try {
    await api.tabs.update(tabId, { url: SSO_INIT });
  } catch (_) {
    return false;
  }
  const deadline = Date.now() + Math.min(timeout, 90_000);
  while (Date.now() < deadline) {
    if (shouldStop && shouldStop()) return false;
    await sleep(1500);
    const tab = await getTab(tabId).catch(() => null);
    if (!tab) return false;
    const url = tab.url || "";
    if (/accounts\.google\.com/.test(url)) {
      log("re-auth hit an interactive Google page — the container session is gone");
      return false;
    }
    if (/not-approved|\/home|sso-complete/.test(url)) {
      log(`re-auth landed on ${shortUrl(url)}`);
      return true;
    }
  }
  return false;
}

async function finaliseOnTab(tabId, timeout, shouldStop, log, options = {}) {
  const landing = (await getTab(tabId).catch(() => null))?.url || "";
  await api.tabs.update(tabId, { url: HOME });
  await waitComplete(tabId, Math.min(timeout, 60_000), shouldStop);
  await sleep(2500);
  log(`finalise from ${shortUrl(landing)}`);

  const acceptTerms = options.acceptTerms !== false;
  let agreement = null;
  if (acceptTerms) {
    try {
      agreement = await withTimeout(
        exec(tabId, acceptUserAgreements, [], "MAIN"),
        30_000,
        "roblox agreements"
      );
      if (agreement) {
        log(
          `agreements ${agreement.listed} listed · accept ${agreement.status}` +
            (agreement.accepted ? ` (${agreement.accepted} accepted)` : "") +
            (agreement.status === 200 && !agreement.accepted ? " (not all accepted)" : "") +
            (agreement.status === 200 ? "" : ` (${agreement.body || "no body"})`)
        );
      }
    } catch (error) {
      log(`agreements failed: ${error.message || error}`);
    }
  }

  let username = options.username || "";
  const birthday = options.birthday || "1996-05-15";

  const attemptUpgrade = async () => {
    let out = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      out = await withTimeout(
        exec(tabId, upgradeAndValidate, [birthday, username, options.gender || 1], "MAIN"),
        60_000,
        "roblox finalise"
      );
      if (!out) throw new Error("finalise returned nothing");
      log(
        `upgrade ${out.upgrade.status}` +
          (out.upgrade.code ? `/${out.upgrade.code}` : "") +
          ` · session ${out.session.status} · ${shortUrl(out.url)}`
      );
      // The body is the only place Roblox says *why*: "User is moderated" vs a
      // validation error vs a dead session all arrive as a status otherwise.
      if (out.upgrade.body) log(`upgrade body: ${String(out.upgrade.body).slice(0, 240)}`);
      if (out.session.status === 200) break;
      if (!out.usernameTaken || attempt >= 3) break;
      username = makeUsername(attempt + 1);
      log(`username ${username} rejected — retrying`);
    }
    return out;
  };

  let result = await attemptUpgrade();
  if (!result) throw new Error("finalise returned nothing");

  // "User is moderated" on a session that is otherwise valid is the fresh
  // account not having been provisioned yet. Signing in again completes it, and
  // the Google session is already in this container, so it costs one round-trip.
  const moderated = /moderat/i.test(String(result.upgrade.body || ""));
  if (
    result.session.status !== 200 &&
    moderated &&
    typeof options.resignIn === "function" &&
    options.retryModerated !== false
  ) {
    log("moderated at finalise — re-running SSO in this container");
    if (await options.resignIn()) {
      await waitComplete(tabId, Math.min(timeout, 45_000), shouldStop).catch(() => {});
      await sleep(2000);
      result = await attemptUpgrade();
    }
  }

  if (result.session.status === 200) {
    if (acceptTerms) {
      // Run the agreement call once more after the page's own checker has
      // settled: its fetch can overwrite the local cache flag we set earlier.
      try {
        const retry = await withTimeout(
          exec(tabId, acceptUserAgreements, [], "MAIN"),
          30_000,
          "roblox agreements retry"
        );
        if (retry) log(`agreements retry · accept ${retry.status}`);
      } catch (error) {
        log(`agreements retry failed: ${error.message || error}`);
      }
    }
    // Replace the stale /not-approved page and its modal before the runner
    // closes or keeps the tab.
    await api.tabs.update(tabId, { url: HOME }).catch(() => {});
    await waitComplete(tabId, Math.min(timeout, 30_000), shouldStop).catch(() => {});
  }

  return { ...result, username: result.session?.name || username || null };
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
    username = "",
    birthday = "1996-05-15",
    gender = 1,
    acceptTerms = true,
    laneSurface = "tab",
    probeIp = false,
    retryModerated = true,
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

    surface = await openLaneSurface(storeId, probeIp ? IP_ECHO : SSO_INIT, lane, laneSurface);
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
      await api.tabs.update(tabId, { url: SSO_INIT }).catch(() => {});
    }
    logFor(
      `${identity.email} · sso init opened` +
        (surface.ownWindow
          ? " in its own window"
          : surface.fallback
            ? " in a background tab (window creation failed)"
            : " in a background tab")
    );

    try {
      googleTab = await waitForGoogle(tabId, Math.min(budget, 30_000), shouldStop, logFor);
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
      codes: identity.codes || [],
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
          retryModerated,
          resignIn: () => resignIn(tabId, budget, shouldStop, logFor),
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
      upgrade: result.upgrade,
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
