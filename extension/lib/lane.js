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

// A lane needs a *visible* document, not merely an open tab. Google's sign-in
// UI is an rAF-driven SPA: a background tab never receives animation frames, so
// the field fills, the click dispatches, and the transition never runs. One
// small window per lane keeps the tab selected, which is the only thing Firefox
// treats as "visible" — occlusion is not part of its visibility model, so these
// windows are free to overlap or sit behind the runner.
export async function openLaneSurface(storeId, url, lane, mode) {
  if (mode !== "windows" || !api.windows) {
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

export async function laneVisibility(tabId) {
  try {
    const results = await api.scripting.executeScript({
      target: { tabId },
      func: () => ({
        state: document.visibilityState,
        focus: typeof document.hasFocus === "function" ? document.hasFocus() : false,
        url: location.href,
      }),
    });
    return results?.[0]?.result ?? null;
  } catch (_) {
    return null;
  }
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
  let closedNoted = false;
  let visibility = null;
  let raised = false;

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
      } catch (_) {
        // The content script may not be in this document yet (host permission
        // granted after the tab opened, or a frame navigation). Inject it once.
        try {
          await api.scripting.executeScript({
            target: { tabId: tab.id },
            files: ["content/google.js"],
          });
        } catch (_) {
          // keep waiting; the declared content script may still arrive
        }
      }
    }

    // Visibility is the thing that decides whether a Google page can advance at
    // all, so record it rather than guessing from a stalled screen.
    if (Date.now() - lastProbe > 6000) {
      lastProbe = Date.now();
      const seen = await laneVisibility(tab.id);
      if (seen && seen.state !== visibility) {
        visibility = seen.state;
        log(`google tab is ${visibility}${seen.focus ? " · focused" : ""}`);
        if (visibility === "hidden" && surface?.windowId && !raised) {
          raised = true;
          log("hidden lane — raising its window; switch lane mode to windows to stop this");
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

async function finaliseOnTab(tabId, timeout, shouldStop, log, options = {}) {
  await api.tabs.update(tabId, { url: HOME });
  await waitComplete(tabId, Math.min(timeout, 60_000), shouldStop);
  await sleep(2500);

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
  let result = null;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    result = await withTimeout(
      exec(tabId, upgradeAndValidate, [birthday, username, options.gender || 1], "MAIN"),
      60_000,
      "roblox finalise"
    );
    if (!result) throw new Error("finalise returned nothing");
    log(
      `upgrade ${result.upgrade.status}` +
        (result.upgrade.code ? `/${result.upgrade.code}` : "") +
        ` · session ${result.session.status}`
    );
    if (result.session.status !== 200 && result.upgrade.body) {
      log(`upgrade body: ${String(result.upgrade.body).slice(0, 180)}`);
    }
    if (result.session.status === 200) break;
    if (!result.usernameTaken || attempt >= 3) break;
    username = makeUsername(attempt + 1);
    log(`username ${username} rejected — retrying`);
  }
  if (!result) throw new Error("finalise returned nothing");

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
    laneMode = "windows",
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

    surface = await openLaneSurface(storeId, SSO_INIT, lane, laneMode);
    tabId = surface.tabId;
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
