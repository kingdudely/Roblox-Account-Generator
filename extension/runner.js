// language: JavaScript, file: runner.js
// The run loop lives here, in a page — not in the MV3 worker, which sleeps.

import { api, isFirefox, uid, sleep, mapLimit, withTimeout, STATE_KEY } from "./lib/api.js";
import { load, mutate, wipeRecords } from "./lib/store.js";
import { log, stamp, CLIENT_ID } from "./lib/log.js";
import { parseKeysText } from "./lib/keys.js";
import * as admin from "./lib/admin.js";
import { ensureContainer, runIdentity, retryFinalise } from "./lib/lane.js";
import { parseClient, makeVerifier, waitForCode, exchangeCode } from "./lib/consent.js";
import {
  scanTab,
  scanSignal,
  extractFromPage,
  decodeImageUrl,
  captureVisible,
  parseSms,
  looksLikeConsent,
  openConsentPage,
  installSmsHook,
  probeSendButton,
  pressSendSms,
} from "./lib/phone.js";
import { makeIdentityFields, makeUsername } from "./lib/names.js";
import {
  writeTxt,
  clearTxt,
  lookupTxt,
  waitForTxt,
  subdomainFrom,
  fullDomain,
} from "./lib/duckdns.js";
import { parseTotp, totpCode, secondsRemaining } from "./lib/totp.js";
import {
  lanesFromSettings,
  proxyForLane,
  publishLaneProxies,
  describeProxy,
} from "./lib/proxy.js";

const ORIGINS = [
  "https://accounts.google.com/*",
  "https://*.google.com/*",
  "https://*.googleapis.com/*",
  "https://*.roblox.com/*",
  "https://dns.google/*",
  "https://www.duckdns.org/*",
];

const $ = (id) => document.getElementById(id);

let state = null;
let running = false;
let stopRequested = false;
let revealed = new Set();
let lastProgress = { done: 0, total: 0 };
let consoleCount = 0;

// Authenticator secrets the content script captures while it enrols a fresh
// identity in 2SV. Kept in memory so the lane can answer a challenge in the same
// run, before the identity object that was queued has been re-read from storage.
const liveOtp = new Map();
// Identities that currently have a lane open, keyed by email. The content script
// can ask for a backup-code set mid-challenge, and the lane holds the only
// object whose `codes` the pump reads.
const activeIdentities = new Map();

// ---------------------------------------------------------------- boot / store

async function refresh() {
  state = await load();
  render();
}

async function remember(message, level = "info") {
  const text = String(message);
  // Append before anything else. The console is the only place a run explains
  // itself, and it used to depend entirely on a round-trip to the background —
  // a background that failed to start left it silent with no clue why.
  const line = { t: Date.now(), level, text, cid: CLIENT_ID };
  state.log = [...(state.log || []), line].slice(-2000);
  appendLogNode(line);

  const write = log[level] || log.info;
  write(text);
}

// ------------------------------------------------------------------ rendering

function setText(id, text) {
  const node = $(id);
  if (node) node.textContent = text;
}

function render() {
  if (!state) return;
  renderStats();
  renderStatus();
  renderKeys();
  renderAccounts();
  renderQueue();
  renderPhone();
  renderDuckdns();
  renderTotp();
  renderSettings();
}

function renderStatus() {
  const pill = $("status");
  const runState = running ? (stopRequested ? "stopping" : "running") : state.run?.state || "idle";
  const label = running ? (stopRequested ? "stopping" : "running") : "idle";
  pill.dataset.state = runState === "running" ? "running" : runState;
  pill.textContent = label;

  $("start").disabled = running;
  $("preflight").disabled = running;
  $("stop").disabled = !running;

  if (running) {
    const done = lastProgress.done;
    const total = lastProgress.total || 1;
    $("bar-fill").style.width = `${Math.min(100, (done / total) * 100)}%`;
    setText("progress-count", `${done}/${lastProgress.total}`);
    setText("progress-text", done ? "working" : "preparing identities");
  } else {
    $("bar-fill").style.width = "0%";
    setText("progress-count", "");
    setText("progress-text", state.run?.reason || (state.accounts.length ? "ready when you are" : "idle"));
  }
}

function renderStats() {
  setText("stat-accounts", String(state.accounts.length));

  const ready = state.identities.filter((identity) => identity.status === "ready").length;
  setText("stat-ready", String(ready));

  const perKey = new Map();
  for (const identity of state.identities) {
    perKey.set(identity.key_id, (perKey.get(identity.key_id) || 0) + 1);
  }
  const seats = state.keys.reduce(
    (total, key) => total + Math.max(0, 50 - (perKey.get(key.id) || 0)),
    0
  );
  setText("stat-seats", String(seats));

  if (state.run?.started_at && running) {
    const started = new Date(state.run.started_at).getTime();
    const baseline = state.run.baseline ?? 0;
    const made = Math.max(0, state.accounts.length - baseline);
    const hours = Math.max(0.001, (Date.now() - started) / 3_600_000);
    setText("stat-rate", made ? `${Math.round(made / hours)}` : "—");
  } else {
    setText("stat-rate", "—");
  }

  setText("batch-hint", state.keys.length ? `${state.keys.length} key(s) loaded` : "add a key first");
}

// ------------------------------------------------------------ random profile

function randomBirthday() {
  const age = 18 + Math.floor(Math.random() * 28); // 18–45
  const year = new Date().getFullYear() - age;
  const month = Math.floor(Math.random() * 12);
  const days = new Date(year, month + 1, 0).getDate();
  const day = 1 + Math.floor(Math.random() * days);
  const pad = (n) => String(n).padStart(2, "0");
  return `${year}-${pad(month + 1)}-${pad(day)}`;
}

function randomGender() {
  return [1, 2, 3][Math.floor(Math.random() * 3)];
}

// Random rolls are stored on the identity so a retry finalises with the same
// numbers. A fixed setting always wins over whatever an older row carries.
function profileFor(identity, settings) {
  const birthday =
    (settings.randomBirthday
      ? identity?.birthday || randomBirthday()
      : settings.birthday) || "1996-05-15";
  const gender =
    (settings.randomGender ? identity?.gender || randomGender() : settings.gender) || 1;
  return { birthday, gender };
}

function renderSettings() {
  const settings = state.settings;
  $("count").value = settings.count;
  $("timeout").value = settings.timeout;
  $("auto-export").checked = Boolean(settings.autoExport);

  const lanes = $("lanes");
  if (isFirefox) {
    lanes.disabled = false;
    lanes.value = String(settings.lanes);
  } else {
    lanes.value = "1";
    lanes.disabled = true;
  }

  const usernameMode = $("username-mode");
  if (usernameMode) usernameMode.value = settings.usernameMode === "ours" ? "ours" : "generated";

  const birthday = $("birthday");
  if (birthday) {
    birthday.value = settings.birthday || "1996-05-15";
    birthday.disabled = Boolean(settings.randomBirthday);
  }
  const birthdayField = $("birthday-field");
  if (birthdayField) birthdayField.classList.toggle("is-random", Boolean(settings.randomBirthday));
  const randomBirthdayBox = $("random-birthday");
  if (randomBirthdayBox) randomBirthdayBox.checked = Boolean(settings.randomBirthday);

  const gender = $("gender");
  if (gender) {
    gender.value = String(settings.gender || 1);
    gender.disabled = Boolean(settings.randomGender);
  }
  const genderField = $("gender-field");
  if (genderField) genderField.classList.toggle("is-random", Boolean(settings.randomGender));
  const randomGenderBox = $("random-gender");
  if (randomGenderBox) randomGenderBox.checked = Boolean(settings.randomGender);

  const acceptTerms = $("accept-terms");
  if (acceptTerms) acceptTerms.checked = settings.acceptTerms !== false;

  const concurrency = $("concurrency");
  if (concurrency) {
    concurrency.value = String(settings.concurrency || 8);
    concurrency.disabled = false;
  }

  const openOnInstall = $("open-on-install");
  if (openOnInstall) openOnInstall.checked = settings.openOnInstall !== false;

  const laneSurface = $("lane-surface");
  if (laneSurface) laneSurface.value = settings.laneSurface === "window" ? "window" : "tab";

  const deleteBatch = $("delete-batch");
  if (deleteBatch) deleteBatch.value = String(settings.deleteBatch || 5);

  const deletePace = $("delete-pace");
  if (deletePace) deletePace.value = String(settings.deletePaceSeconds || 45);

  const retryModerated = $("retry-moderated");
  if (retryModerated) retryModerated.checked = settings.retryModerated !== false;

  const proxyUrl = $("proxy-url");
  if (proxyUrl) proxyUrl.value = settings.proxyUrl || "";

  const proxyList = $("proxy-list");
  if (proxyList) proxyList.value = settings.proxyList || "";

  setText("engine", isFirefox ? "Firefox · containers" : "Chrome · 1 lane");
}

function renderKeys() {
  const list = $("key-list");
  list.replaceChildren();
  setText("key-count", `${state.keys.length} key${state.keys.length === 1 ? "" : "s"}`);

  if (!state.keys.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "no keys yet";
    list.append(empty);
    return;
  }

  for (const key of state.keys) {
    const used = state.identities.filter((identity) => identity.key_id === key.id).length;
    const row = document.createElement("div");
    row.className = "keyrow";

    const meta = document.createElement("div");
    meta.className = "meta";

    const domain = document.createElement("div");
    domain.className = "domain";
    const dot = document.createElement("span");
    const tone = key.needs_consent ? "client" : key.status;
    dot.className = `dot ${tone === "ok" ? "ok" : tone === "blocked" ? "blocked" : tone === "error" ? "error" : tone === "client" ? "client" : ""}`;
    domain.append(dot, document.createTextNode(key.domain || key.label || "unnamed key"));

    const sub = document.createElement("div");
    sub.className = "sub";
    const kind = key.kind === "service_account" ? "service account" : "OAuth";
    sub.textContent = key.needs_consent
      ? `${kind} client · ${key.message || "authorize once to mint a refresh token"}`
      : `${kind} · ${used}/50 seats · ${key.message || key.status}`;

    meta.append(domain, sub);

    const actions = document.createElement("div");
    actions.className = "actions";

    if (key.needs_consent) {
      actions.append(actionButton("Authorize", () => authorizeKey(key.id), "primary"));
    }
    if (!key.domain) {
      actions.append(actionButton("Set domain", () => setKeyDomain(key.id)));
    }
    if (!key.needs_consent) {
      actions.append(
        actionButton("Check", () => checkKey(key.id)),
        actionButton("Delete users", () => deleteKeyUsers(key.id), "danger")
      );
    }
    actions.append(actionButton("Remove", () => removeKey(key.id), "ghost"));

    row.append(meta, actions);
    list.append(row);
  }
}

function actionButton(label, onClick, kind = "") {
  const button = document.createElement("button");
  button.className = `btn tiny ${kind}`.trim();
  button.textContent = label;
  button.addEventListener("click", () => {
    Promise.resolve(onClick()).catch((error) => log.error(String(error.message || error)));
  });
  return button;
}

function renderAccounts() {
  const list = $("account-list");
  list.replaceChildren();
  setText("account-count", `${state.accounts.length} account${state.accounts.length === 1 ? "" : "s"}`);

  if (!state.accounts.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "none yet";
    list.append(empty);
    return;
  }

  for (const account of [...state.accounts].reverse()) {
    const row = document.createElement("div");
    row.className = "arow";

    const name = document.createElement("div");
    const title = document.createElement("div");
    title.className = "name";
    title.textContent = account.username || "(pending username)";
    if (account.status === "pending") {
      const tag = document.createElement("span");
      tag.className = "pending-tag";
      tag.textContent = "pending";
      title.append(tag);
    }
    const mail = document.createElement("div");
    mail.className = "mail";
    mail.textContent = account.email || "";
    name.append(title, mail);

    const secret = document.createElement("div");
    secret.className = "mail mono";
    secret.textContent = revealed.has(account.id)
      ? account.cookie || "(no cookie)"
      : `cookie ${"\u2022".repeat(18)}`;

    const acts = document.createElement("div");
    acts.className = "acts";
    acts.append(
      smallAction("user", () => copyText(account.username || "")),
      smallAction("pass", () => copyText(account.password || "")),
      smallAction("cookie", () => copyText(account.cookie || ""))
    );
    const codes = Array.isArray(account.backup_codes) ? account.backup_codes : [];
    if (codes.length) {
      acts.append(smallAction(`codes ${codes.length}`, () => copyText(codes.join("\n"))));
    }
    if (account.otp_secret) {
      acts.append(smallAction("otp", () => copyText(account.otp_secret)));
    }
    if (account.status === "pending") {
      acts.append(smallAction("finalise", () => finaliseAccount(account)));
    }
    acts.append(
      smallAction(revealed.has(account.id) ? "hide" : "show", () => {
        if (revealed.has(account.id)) revealed.delete(account.id);
        else revealed.add(account.id);
        renderAccounts();
      })
    );

    row.append(name, secret, acts);
    list.append(row);
  }
}

function smallAction(label, onClick) {
  const button = document.createElement("button");
  button.textContent = label;
  button.addEventListener("click", () => Promise.resolve(onClick()).catch(() => {}));
  return button;
}

function renderQueue() {
  const queue = $("queue");
  queue.replaceChildren();

  const ordered = [...state.identities].sort((a, b) => {
    const rank = { working: 0, ready: 1, planned: 2, failed: 3, blocked: 4, account: 5 };
    return (rank[a.status] ?? 6) - (rank[b.status] ?? 6);
  });

  if (!ordered.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "nothing queued";
    queue.append(empty);
    return;
  }

  for (const identity of ordered.slice(0, 80)) {
    const row = document.createElement("div");
    row.className = "qrow";
    row.dataset.state = identity.status;

    const led = document.createElement("span");
    led.className = "led";

    const who = document.createElement("span");
    who.className = "who mono";
    who.textContent = identity.email;
    who.title = identity.error || identity.email;

    const status = document.createElement("span");
    status.className = "state";
    status.textContent = identity.status;

    row.append(led, who, status);
    queue.append(row);
  }
}

function renderPhone() {
  const phone = state.phone || {};
  const number = String(phone.number || "").replace(/^\/+/, "");
  setText("phone-number", number || "—");
  setText("phone-body", phone.body || "—");
  setText("phone-source", phone.source || "no scan yet");

  const payload = $("phone-payload");
  payload.replaceChildren();
  if (phone.payload) {
    payload.append(document.createTextNode("payload: "));
    if (/^https?:\/\//i.test(phone.payload)) {
      const anchor = document.createElement("a");
      anchor.href = phone.payload;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      anchor.textContent =
        phone.payload.length > 130 ? `${phone.payload.slice(0, 130)}…` : phone.payload;
      payload.append(anchor);
    } else {
      payload.append(document.createTextNode(phone.payload.slice(0, 220)));
    }
  }
}

function suggestedSubdomain() {
  const saved = state.duckdns?.subdomain;
  if (saved) return saved;
  const named = state.keys.find((key) => (key.domain || "").endsWith(".duckdns.org"));
  return named ? subdomainFrom(named.domain) || "" : "";
}

function renderDuckdns() {
  const duck = state.duckdns || {};
  $("duck-token").value = duck.token || "";
  $("duck-sub").value = suggestedSubdomain();
  $("duck-value").value = duck.value || "";
  setText("duck-status", duck.answer || "no record written yet");
}

// --------------------------------------------------------------- authenticator

function defaultTotpEmail() {
  const key = state.keys.find((entry) => entry.status === "ok") || state.keys[0];
  return key?.admin || (key?.domain ? `admin@${key.domain}` : "");
}

let totpBusy = false;

async function updateTotp() {
  if (totpBusy) return;
  totpBusy = true;
  try {
    const totp = state?.totp || {};
    const node = $("totp-code");
    if (!node) return;
    if (!totp.secret) {
      node.textContent = "------";
      setText("totp-countdown", "\u2014");
      return;
    }
    node.textContent = await totpCode(totp.secret, totp);
    setText("totp-countdown", `${secondsRemaining(totp.period)}s`);
  } catch (_) {
    setText("totp-code", "------");
    setText("totp-countdown", "bad secret");
  } finally {
    totpBusy = false;
  }
}

function renderTotp() {
  if (!state) return;
  const totp = state.totp || {};
  const email = $("totp-email");
  const secret = $("totp-secret");

  if (email && document.activeElement !== email) {
    email.value = totp.email || defaultTotpEmail();
  }
  if (secret && document.activeElement !== secret) {
    secret.value = totp.secret || "";
  }

  setText(
    "totp-status",
    totp.secret
      ? `saved \u00b7 ${totp.issuer || "authenticator"} \u00b7 ${totp.email || "no admin email"}`
      : "no secret saved"
  );
  updateTotp();
}

function renderConsoleLog() {
  const body = $("console-body");
  body.replaceChildren();
  consoleCount = 0;
  for (const line of (state.log || []).slice(-600)) appendLogNode(line, false);
  body.scrollTop = body.scrollHeight;
}

function appendLogNode(line, autoscroll = true) {
  const body = $("console-body");
  const node = document.createElement("div");
  node.className = `line ${line.level || "info"}`;

  const time = document.createElement("time");
  time.textContent = stamp(line.t);

  const text = document.createElement("span");
  text.className = "text";
  text.textContent = line.text;

  node.append(time, text);
  body.append(node);
  consoleCount = body.children.length;

  const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 90;
  if (autoscroll && nearBottom) body.scrollTop = body.scrollHeight;
  while (body.children.length > 1400) body.firstElementChild.remove();
  consoleCount = body.children.length;

  setText("console-stat", `${consoleCount} lines`);
}

// --------------------------------------------------------------------- helpers

async function copyText(value) {
  if (!value) {
    await remember("nothing to copy", "warn");
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    await remember("copied", "ok");
  } catch (_) {
    await remember("clipboard blocked by the browser", "error");
  }
}

function accountText(account) {
  const codes = Array.isArray(account.backup_codes) ? account.backup_codes : [];
  return [
    `username: ${account.username || ""}`,
    `password: ${account.password || ""}`,
    `email: ${account.email || ""}`,
    `user_id: ${account.user_id || ""}`,
    `cookie: ${account.cookie || ""}`,
    `backup_codes: ${codes.join(", ")}`,
    `otp_secret: ${account.otp_secret || ""}`,
    `status: ${account.status || "live"}`,
    `note: Google SSO - sign in with Google using ${account.email || ""}`,
    `created_at: ${account.created_at || ""}`,
  ].join("\n");
}

async function saveTextFile(filename, text, saveAs = true) {
  // Firefox's downloads API rejects data: URLs ("Access denied for URL
  // data:text/plain..."), so hand it a blob URL and revoke it after the save.
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  try {
    await api.downloads.download({ url, filename, saveAs });
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

async function exportAccounts(silent = false) {
  if (!state.accounts.length) {
    if (!silent) await remember("nothing to export", "warn");
    return;
  }
  const text = state.accounts.map(accountText).join("\n\n") + "\n";
  try {
    await saveTextFile("roblox-accounts.txt", text, !silent);
    await remember(`exported ${state.accounts.length} account(s)`, "ok");
  } catch (error) {
    await remember(`export failed: ${error.message || error}`, "error");
  }
}

// ------------------------------------------------------------------- key setup

async function addKeys(text, source = "pasted") {
  const parsed = parseKeysText(text);
  for (const problem of parsed.problems) log.warn(problem);

  const domain = $("key-domain")?.value.trim().toLowerCase() || null;
  const admin = $("key-admin")?.value.trim().toLowerCase() || null;

  await mutate((draft) => {
    for (const key of parsed.keys) {
      if (draft.keys.some((existing) => existing.fingerprint === key.fingerprint)) continue;
      if (domain && !key.domain) key.domain = domain;
      if (admin && !key.admin) key.admin = admin;
      draft.keys.push(key);
    }
  });

  await remember(`${parsed.keys.length} key(s) from ${source}`, "ok");
  await refresh();

  for (const key of parsed.keys) {
    if (key.needs_consent) {
      await remember(
        `${key.label}: client stored — press Authorize in Tenants to mint its refresh token`,
        "step"
      );
      continue;
    }
    await checkKey(key.id);
  }
}

// A stored client (client_secret_*.json) has no refresh token yet. One consent
// run writes it back into the same entry, so the tenant row never duplicates.
async function authorizeKey(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;
  if (key.kind === "service_account") {
    await remember(`${key.label}: service accounts do not need consent`, "warn");
    return;
  }
  if (!key.client_id || !key.client_secret) {
    await remember(`${key.label}: client_id or client_secret is missing`, "error");
    return;
  }

  try {
    const client = parseClient(
      JSON.stringify({
        installed: { client_id: key.client_id, client_secret: key.client_secret },
      })
    );
    await remember(`${key.label}: approve the consent tab as the tenant admin`, "step");
    const verifier = makeVerifier();
    const code = await waitForCode(client.client_id, verifier);
    const credential = await exchangeCode(client, code, verifier);

    await mutate((draft) => {
      const target = draft.keys.find((entry) => entry.id === id);
      if (!target) return;
      target.refresh_token = credential.refresh_token;
      target.token_uri = credential.token_uri || target.token_uri;
      if (Array.isArray(credential.scopes) && credential.scopes.length) {
        target.scopes = credential.scopes;
      }
      target.needs_consent = false;
      target.status = "new";
      target.message = "";
      target.authorized_at = new Date().toISOString();
    });

    await refresh();
    await remember(`${key.label}: consent granted`, "ok");
    await checkKey(id);
  } catch (error) {
    await remember(`${key.label}: ${error.message || error}`, "error");
  }
}

async function checkKey(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;

  if (key.needs_consent) {
    await remember(`${key.label}: authorize this client before checking it`, "warn");
    return;
  }

  await remember(`checking ${key.label}`, "step");
  try {
    const me = await admin.whoAmI(key);
    const domain = key.domain || me.domain || (await admin.detectDomain(key));
    if (!domain) {
      throw new admin.AdminError(
        "could not detect the tenant domain — set it on the key manually",
        { status: 0 }
      );
    }
    const seats = await admin.countUsers(key, domain);

    await mutate((draft) => {
      const target = draft.keys.find((entry) => entry.id === id);
      if (target) {
        target.status = "ok";
        target.domain = domain;
        target.label = domain;
        target.seats = seats;
        target.message = `${seats}/50 seats used`;
      }
    });

    if (subdomainFrom(domain)) {
      const sub = subdomainFrom(domain);
      await mutate((draft) => {
        if (!draft.duckdns.subdomain) draft.duckdns.subdomain = sub;
      });
    }
    await remember(`${domain} · token ok · ${seats} seats used`, "ok");
  } catch (error) {
    const blocked = error.blocked || error.status === 412 || error.reason === "conditionNotMet";
    await mutate((draft) => {
      const target = draft.keys.find((entry) => entry.id === id);
      if (target) {
        target.status = blocked ? "blocked" : "error";
        target.message = blocked ? "412 conditionNotMet — key is spent" : String(error.message || error).slice(0, 180);
      }
    });
    await remember(`${key.label}: ${blocked ? "blocked by Google (412) — this key cannot create more users" : error.message || error}`, "error");
  }
  await refresh();
  await refreshChecklist();
}

async function setKeyDomain(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;
  const value = prompt(`Domain for this tenant (the part after @)`, key.domain || "example.duckdns.org");
  if (!value) return;
  const domain = value.trim().toLowerCase().replace(/^@/, "");
  if (!/^[a-z0-9.-]+\.[a-z]{2,}$/.test(domain)) {
    await remember(`that does not look like a domain: ${domain}`, "error");
    return;
  }
  await mutate((draft) => {
    const target = draft.keys.find((entry) => entry.id === id);
    if (target) {
      target.domain = domain;
      target.label = domain;
    }
  });
  await refresh();
  await remember(`${key.label}: domain set to ${domain}`, "ok");
  await checkKey(id);
}

async function deleteKeyUsers(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;
  const settings = state.settings || {};
  const batch = Math.max(1, Math.min(50, Number(settings.deleteBatch) || 5));
  const pace = Math.max(5, Math.min(600, Number(settings.deletePaceSeconds) || 45));

  // Oldest first: Google reads a tenant as ordinary staff turnover when the
  // accounts leaving are the ones that have been there longest, and a burst of
  // deletes is what its abuse filter actually reacts to.
  const candidates = state.identities
    .filter((identity) => identity.key_id === id && identity.status !== "deleted")
    .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));

  if (!candidates.length) {
    await remember("no live identities recorded for this key", "warn");
    return;
  }

  const doomed = candidates.slice(0, batch);
  const ok = confirm(
    `Delete ${doomed.length} of ${candidates.length} identities from ${key.domain || key.label}?\n\n` +
      `Paced about ${pace}s apart, oldest first. Both numbers live in Settings — a burst of deletes ` +
      "is what Google's abuse filter answers to. Seats free immediately; addresses stay reserved " +
      "for about 20 days."
  );
  if (!ok) return;

  await remember(`deleting ${doomed.length} identities, ~${pace}s apart...`, "step");
  const removed = new Set();

  for (const [index, identity] of doomed.entries()) {
    if (index) {
      const wait = pace * 1000 + Math.floor(Math.random() * pace * 200);
      await remember(`holding ${Math.round(wait / 1000)}s before the next delete`, "info");
      await sleep(wait);
    }
    try {
      await admin.deleteUser(key, identity.email);
      removed.add(identity.id);
      await remember(`${identity.email}: deleted (${index + 1}/${doomed.length})`, "ok");
    } catch (error) {
      await remember(`${identity.email}: ${error.message || error}`, "warn");
    }
    await mutate((draft) => {
      const row = draft.identities.find((entry) => entry.id === identity.id);
      if (row && removed.has(identity.id)) row.status = "deleted";
    });
  }

  await remember(
    removed.size ? `${removed.size}/${doomed.length} identities deleted` : "nothing deleted",
    removed.size ? "ok" : "warn"
  );
  await refresh();
}

async function removeKey(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;
  if (!confirm(`Remove ${key.domain || key.label} from the list? The credentials stay valid on Google's side.`)) {
    return;
  }
  await mutate((draft) => {
    draft.keys = draft.keys.filter((entry) => entry.id !== id);
    draft.identities = draft.identities.filter((identity) => identity.key_id !== id);
  });
  await refresh();
  await refreshChecklist();
  await remember("key removed", "info");
}

// ---------------------------------------------------------------- identity plan

function planIdentities(draft, key, count) {
  const existing = draft.identities.filter((identity) => identity.key_id === key.id);
  const taken = new Set(existing.map((identity) => identity.email.toLowerCase()));
  const ours = draft.settings?.usernameMode === "ours";
  const randBirthday = Boolean(draft.settings?.randomBirthday);
  const randGender = Boolean(draft.settings?.randomGender);
  const usedNames = new Set(
    draft.identities.map((identity) => identity.roblox_username).filter(Boolean)
  );
  const planned = [];
  let index = existing.length;

  while (planned.length < count) {
    index += 1;
    const fields = makeIdentityFields(key.domain, index);
    if (taken.has(fields.email)) continue;
    taken.add(fields.email);

    let robloxUsername = null;
    if (ours) {
      let attempt = 0;
      do {
        robloxUsername = makeUsername(attempt);
        attempt += 1;
      } while (usedNames.has(robloxUsername) && attempt < 20);
      usedNames.add(robloxUsername);
    }

    planned.push({
      id: uid("idn"),
      key_id: key.id,
      domain: key.domain,
      email: fields.email,
      first: fields.first,
      last: fields.last,
      password: fields.password,
      roblox_username: robloxUsername,
      birthday: randBirthday ? randomBirthday() : draft.settings?.birthday || "1996-05-15",
      gender: randGender ? randomGender() : draft.settings?.gender || 1,
      status: "planned",
      error: null,
      created_at: new Date().toISOString(),
    });
  }
  return planned;
}

async function prepareWork(preflight) {
  const usable = state.keys.filter((key) => key.status !== "blocked" && key.status !== "dead");
  if (!usable.length) throw new Error("no usable keys — add one in the Keys tab");

  // Re-check keys that have not proven themselves this session.
  for (const key of usable) {
    if (key.status !== "ok" || !key.domain) await checkKey(key.id);
  }

  state = await load();
  const keys = state.keys.filter((key) => key.status === "ok" && key.domain);
  if (!keys.length) throw new Error("no key authenticated — see Setup");

  const target = preflight ? 1 : Math.max(1, Number(state.settings.count) || 1);

  let ready = state.identities.filter((identity) => identity.status === "ready");
  if (ready.length >= target) return ready.slice(0, target);

  for (const key of keys) {
    if (ready.length >= target) break;
    const existing = state.identities.filter((identity) => identity.key_id === key.id);
    const capacity = Math.max(0, 50 - existing.length);
    const need = Math.min(target - ready.length, capacity);
    if (need <= 0) continue;

    await remember(`${key.domain}: planning ${need} identities`, "step");
    const planned = await mutate((draft) => {
      const list = planIdentities(draft, key, need);
      for (const identity of list) draft.identities.push(identity);
      return list;
    });

    await mapLimit(planned, state.settings.concurrency || 8, async (identity) => {
      if (stopRequested) return;
      try {
        await admin.insertUser(key, identity);
        await mutate((draft) => {
          const targetRow = draft.identities.find((row) => row.id === identity.id);
          if (targetRow) targetRow.status = "ready";
        });
        ready.push(identity);
        await remember(`${identity.email} created`, "ok");
      } catch (error) {
        const blocked = error.blocked || error.status === 412;
        await mutate((draft) => {
          const targetRow = draft.identities.find((row) => row.id === identity.id);
          if (targetRow) {
            targetRow.status = blocked ? "blocked" : "failed";
            targetRow.error = error.message || String(error);
          }
          if (blocked) {
            const targetKey = draft.keys.find((row) => row.id === key.id);
            if (targetKey) {
              targetKey.status = "blocked";
              targetKey.message = "412 conditionNotMet — key is spent";
            }
          }
        });
        await remember(
          blocked
            ? `${key.domain}: 412 — key is spent, moving on`
            : `${identity.email}: ${error.message || error}`,
          "error"
        );
      }
    });

    state = await load();
  }

  const fresh = (await load()).identities.filter((identity) => identity.status === "ready");
  if (!fresh.length) throw new Error("no identities could be created — check Setup");
  if (fresh.length < target) {
    await remember(
      `only ${fresh.length} of ${target} slots available — seats or keys are exhausted`,
      "warn"
    );
  }
  state = await load();
  return fresh.slice(0, target);
}

// ------------------------------------------------------------------- run loop

// A fresh insert can take a few seconds to propagate to the security service.
async function mintWithRetry(key, email, attempts = 4) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await admin.mintCodes(key, email);
    } catch (error) {
      lastError = error;
      // 412 is the spent-key signal; 403 is the scope/policy signal. Neither
      // gets better with time.
      if (error.status === 412 || error.status === 403) break;
      await sleep(2000 * (attempt + 1));
    }
  }
  throw lastError || new Error("could not mint a backup code");
}

// The tenant policy that blocks a first sign-in, and the switch that fixes it
// without losing the API path.
function mintHint(error) {
  const text = String(error?.message || error || "").toLowerCase();
  if (error?.status === 403) {
    return "the key lacks admin.directory.user.security — re-consent the tenant";
  }
  if (
    /2-?step|two-step|policy|not enrolled|verification code|not allowed/.test(text) ||
    error?.status === 400
  ) {
    return "Admin console → Security → Authentication → 2-Step Verification → Methods: Any except verification codes via text, phone call, Enforcement: On";
  }
  if (error?.quota) return "rate limited — rerun this identity later";
  return "";
}

// The lane is sitting on a backup-code challenge with nothing to type. Mint a
// set for the identity it is driving; the pump re-reads `codes` on every
// handoff, so the next message carries them and the challenge gets answered
// without a restart. If minting is refused, adopt whatever Google already
// holds; if there is nothing at all, mark the identity so the lane fails fast
// instead of waiting out its budget.
async function mintCodesOnDemand(email) {
  const id = String(email || "").toLowerCase();
  const identity = activeIdentities.get(id);
  if (!identity) {
    await remember(`${email}: codes requested, but no lane is driving this identity`, "warn");
    return;
  }
  if (identity.codes?.length) return;

  const key =
    state.keys.find((entry) => entry.id === identity.key_id) ||
    state.keys.find((entry) => entry.status === "ok");
  if (!key) {
    identity.noSecondFactor = "no key available to mint a backup code";
    await remember(`${email}: ${identity.noSecondFactor}`, "error");
    return;
  }

  try {
    const codes = await mintWithRetry(key, email);
    identity.codes = codes;
    await mutate((draft) => {
      const row = draft.identities.find((entry) => entry.id === identity.id);
      if (row) row.codes = codes;
      const account = draft.accounts.find((entry) => entry.identity_id === identity.id);
      if (account) account.backup_codes = codes;
    });
    await remember(`${email}: ${codes.length} backup code(s) minted on demand`, "ok");
  } catch (error) {
    // Minting can be refused (quota, 412, a policy that forbids codes). Adopt a
    // set that already exists before giving up on the identity.
    let existing = [];
    try {
      existing = await admin.listCodes(key, email);
    } catch (_) {
      existing = [];
    }
    if (existing.length) {
      identity.codes = existing;
      await mutate((draft) => {
        const row = draft.identities.find((entry) => entry.id === identity.id);
        if (row) row.codes = existing;
      });
      await remember(`${email}: using ${existing.length} existing backup code(s)`, "warn");
      return;
    }
    identity.noSecondFactor = `no usable second factor for ${email} — ${String(
      error.message || error
    ).slice(0, 120)}`;
    await remember(identity.noSecondFactor, "error");
  }
}

// Explicit button for identities that already exist from an earlier run: mint
// the code now instead of waiting for the next batch to reach them.
async function mintReadyCodes() {
  if (running) {
    await remember("a batch is running — each identity mints its own code", "warn");
    return;
  }

  state = await load();
  const key =
    state.keys.find((entry) => entry.status === "ok" && entry.domain) ||
    state.keys.find((entry) => entry.status === "ok");
  if (!key) {
    await remember("no authenticated key — check a tenant first", "warn");
    return;
  }

  const ready = state.identities.filter((identity) => identity.status === "ready");
  if (!ready.length) {
    await remember("no ready identities — run once so the accounts exist", "warn");
    return;
  }

  await remember(`minting 2SV codes for ${ready.length} identity(ies)`, "step");
  let minted = 0;
  let failed = 0;

  for (const identity of ready) {
    if (identity.codes?.length) continue;
    try {
      const codes = await mintWithRetry(key, identity.email);
      await mutate((draft) => {
        const row = draft.identities.find((entry) => entry.id === identity.id);
        if (row) row.codes = codes;
      });
      minted += 1;
    } catch (error) {
      failed += 1;
      const hint = mintHint(error);
      await remember(
        `${identity.email}: ${error.message || error}${hint ? ` — ${hint}` : ""}`,
        "warn"
      );
      if (hint) break;
    }
  }

  await refresh();
  await remember(
    `${minted} identity(ies) minted${failed ? `, ${failed} failed` : ""}`,
    minted ? "ok" : "warn"
  );
}

async function start({ preflight = false } = {}) {
  if (running) return;

  const granted = await hasPermissions();
  if (!granted) {
    await remember("site access is missing — click Grant site access", "error");
    return;
  }

  running = true;
  stopRequested = false;
  lastProgress = { done: 0, total: 1 };
  revealed = new Set();

  await mutate((draft) => {
    draft.run = {
      state: "running",
      started_at: new Date().toISOString(),
      baseline: draft.accounts.length,
      reason: null,
      preflight,
    };
  });
  await refresh();
  await remember(preflight ? "preflight: one account, start to finish" : "batch started", "step");
  await remember(isFirefox ? `lanes: ${state.settings.lanes}` : "Chrome: one lane, one cookie jar", "info");
  await remember(
    `username: ${
      state.settings.usernameMode === "ours" ? "our generator" : "Roblox generated"
    } · birthday ${
      state.settings.randomBirthday ? "random" : state.settings.birthday || "1996-05-15"
    } · gender ${
      state.settings.randomGender ? "random" : state.settings.gender || 1
    } · terms ${state.settings.acceptTerms !== false ? "auto-accept" : "manual"}`,
    "info"
  );

  let made = 0;
  const startedAt = Date.now();

  try {
    const queue = await prepareWork(preflight);
    await refreshChecklist();
    made = await runBatch(queue, preflight);
  } catch (error) {
    await remember(error.message || String(error), "error");
    await mutate((draft) => {
      draft.run = { state: "idle", started_at: null, reason: String(error.message || error) };
    });
  } finally {
    running = false;
    await refresh();
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    await remember(
      `finished: ${made} account(s) in ${elapsed}s${made && elapsed ? ` (${Math.round((made / elapsed) * 3600)}/hour)` : ""}`,
      made ? "ok" : "warn"
    );
    await mutate((draft) => {
      draft.run = { state: "idle", started_at: null, reason: null, baseline: 0 };
    });
    await refresh();
    if (made && state.settings.autoExport) await exportAccounts(true);
  }
}

async function runBatch(queue, preflight) {
  state = await load();
  const settings = state.settings;
  const laneCount = Math.max(
    1,
    Math.min(
      Number(settings.lanes) || 1,
      isFirefox ? 4 : 1,
      queue.length
    )
  );

  lastProgress = { done: 0, total: queue.length };
  renderStatus();

  const containers = [];
  for (let index = 0; index < laneCount; index += 1) {
    containers.push(await ensureContainer(index));
  }

  // Egress. The router lives in the background page, so the lane -> exit map is
  // published once per batch; container ids are stable for the life of the
  // browser. No proxy configured means nothing is sent and every lane is direct.
  const proxyPool = lanesFromSettings(settings);
  if (proxyPool.length) {
    const map = {};
    for (const [index, container] of containers.entries()) {
      const proxy = proxyForLane(proxyPool, index);
      if (container.storeId && proxy) map[container.storeId] = proxy;
    }
    const taken = await publishLaneProxies(map);
    await remember(
      `egress: ${containers.map((_, index) => describeProxy(proxyForLane(proxyPool, index))).join(" / ")}` +
        (taken ? "" : " — the background did not accept the map, lanes are direct"),
      taken ? "info" : "warn"
    );
  }

  let cursor = 0;
  let made = 0;
  let consecutive = 0;

  async function worker(laneIndex) {
    const { storeId } = containers[laneIndex];
    while (!stopRequested) {
      const identity = queue[cursor];
      if (!identity) break;
      cursor += 1;

      const key = state.keys.find((entry) => entry.id === identity.key_id) ||
        state.keys.find((entry) => entry.status === "ok");
      if (!key) {
        await remember("no key for this identity", "error");
        break;
      }

      await mutate((draft) => {
        const row = draft.identities.find((entry) => entry.id === identity.id);
        if (row) row.status = "working";
      });
      renderQueue();

      // Username, birthday, and gender are fixed before the Roblox tab opens.
      const usernameMode = settings.usernameMode === "ours" ? "ours" : "generated";
      let requestedUsername = usernameMode === "ours" ? identity.roblox_username || "" : "";
      if (usernameMode === "ours" && !requestedUsername) {
        requestedUsername = makeUsername();
        identity.roblox_username = requestedUsername;
        await mutate((draft) => {
          const row = draft.identities.find((entry) => entry.id === identity.id);
          if (row) row.roblox_username = requestedUsername;
        });
      }

      // Random values are rolled per identity and stored on the row, so a
      // manual finalise later reuses the same birthday and gender.
      const profile = profileFor(identity, settings);
      if (identity.birthday !== profile.birthday || identity.gender !== profile.gender) {
        identity.birthday = profile.birthday;
        identity.gender = profile.gender;
        await mutate((draft) => {
          const row = draft.identities.find((entry) => entry.id === identity.id);
          if (row) {
            row.birthday = profile.birthday;
            row.gender = profile.gender;
          }
        });
      }

      await remember(`[lane ${laneIndex + 1}] ${identity.email}`, "step");
      await remember(
        `profile: ${profile.birthday} · gender ${profile.gender}` +
          (settings.randomBirthday || settings.randomGender ? " (random)" : ""),
        "info"
      );

      // The lane object the content script's "need codes" request resolves to.
      identity.noSecondFactor = null;
      activeIdentities.set(String(identity.email || "").toLowerCase(), identity);

      try {
        // An identity with no codes and no authenticator has no second factor at
        // all, and Google answers that with "your sign-in settings don't meet
        // your organization's 2-Step Verification policy" instead of enrolling
        // it. There is no setting for this: a lane must never open an account
        // that cannot possibly authenticate. Mint before the tab, always.
        if (!identity.codes?.length && !identity.otp_secret) {
          try {
            const codes = await mintWithRetry(key, identity.email);
            identity.codes = codes;
            await mutate((draft) => {
              const row = draft.identities.find((entry) => entry.id === identity.id);
              if (row) row.codes = codes;
            });
            await remember(`${identity.email}: ${codes.length} backup code(s) minted`, "info");
          } catch (error) {
            error.mint = true;
            throw error;
          }
        }

        let provisional = null;
        const result = await withTimeout(
          runIdentity({
            identity,
            lane: laneIndex,
            storeId,
            timeout: settings.timeout || 240,
            shouldStop: () => stopRequested,
            log: (text) => remember(text, "info"),
            otpFor: (email) => liveOtp.get(String(email || "").toLowerCase()) || "",
            username: requestedUsername,
            birthday: profile.birthday,
            gender: profile.gender,
            acceptTerms: settings.acceptTerms !== false,
            laneSurface: settings.laneSurface === "window" ? "window" : "tab",
            probeIp: Boolean(proxyPool.length && storeId),
            retryModerated: settings.retryModerated !== false,
            onSession: async (cookie) => {
              provisional = {
                id: uid("acc"),
                identity_id: identity.id,
                key_id: key.id,
                username: null,
                password: identity.password,
                email: identity.email,
                user_id: null,
                display_name: null,
                cookie,
                status: "pending",
                backup_codes: identity.codes || [],
                otp_secret: liveOtp.get(String(identity.email).toLowerCase()) || identity.otp_secret || null,
                tab_id: null,
                store_id: storeId || null,
                created_at: new Date().toISOString(),
              };
              await mutate((draft) => {
                const existing = draft.accounts.findIndex(
                  (row) => row.identity_id === identity.id
                );
                if (existing >= 0) draft.accounts[existing] = provisional;
                else draft.accounts.push(provisional);
              });
              await remember(`${identity.email}: cookie captured and saved`, "ok");
            },
          }),
          (settings.timeout || 240) * 1000 + 30_000,
          "identity"
        );

        const live = result.session?.status === 200;
        const account = {
          id: provisional?.id || uid("acc"),
          identity_id: identity.id,
          key_id: key.id,
          username: result.session?.name || null,
          password: identity.password,
          email: identity.email,
          user_id: result.session?.id || null,
          display_name: result.session?.name || null,
          cookie: result.cookie,
          status: live ? "live" : "pending",
          backup_codes: identity.codes || [],
          otp_secret: liveOtp.get(String(identity.email).toLowerCase()) || identity.otp_secret || null,
          tab_id: result.keepTabId || null,
          store_id: storeId || null,
          created_at: provisional?.created_at || new Date().toISOString(),
        };

        await mutate((draft) => {
          const existing = draft.accounts.findIndex((row) => row.identity_id === identity.id);
          if (existing >= 0) draft.accounts[existing] = account;
          else draft.accounts.push(account);

          const row = draft.identities.find((entry) => entry.id === identity.id);
          if (row) {
            row.status = live ? "account" : "failed";
            row.error = live ? null : "pending — open the tab and finalise, then retry";
            row.account_id = account.id;
            if (live && usernameMode === "ours" && result.username) {
              row.roblox_username = result.username;
            }
          }
        });

        if (live) {
          made += 1;
          consecutive = 0;
          await remember(`${account.username} (id ${account.user_id}) — saved`, "ok");
          // The fallback is minted only now: the account has an authenticator
          // (or Google refused to enrol one), and codes minted here cannot
          // suppress the setup screen that has already been passed.
          if (!identity.codes?.length) {
            try {
              const codes = await mintWithRetry(key, identity.email);
              identity.codes = codes;
              await mutate((draft) => {
                const row = draft.identities.find((entry) => entry.id === identity.id);
                if (row) row.codes = codes;
                const stored = draft.accounts.find((entry) => entry.identity_id === identity.id);
                if (stored) stored.backup_codes = codes;
              });
              await remember(`${identity.email}: ${codes.length} backup code(s) minted after sign-in`, "info");
            } catch (error) {
              await remember(
                `${identity.email}: codes not minted — ${String(error.message || error).slice(0, 120)}`,
                "warn"
              );
            }
          }
        } else {
          consecutive += 1;
          await remember(
            `${identity.email}: session ${result.session?.status} — cookie saved as pending`,
            "warn"
          );
        }
      } catch (error) {
        consecutive += 1;
        const stopped = String(error.message || error).includes("stopped");
        const detail = String(error.message || error);
        const hint = stopped || !error.mint ? "" : mintHint(error);
        await mutate((draft) => {
          const row = draft.identities.find((entry) => entry.id === identity.id);
          if (row) {
            row.status = stopped ? "ready" : "failed";
            row.error = stopped ? null : `${detail}${hint ? ` — ${hint}` : ""}`;
          }
        });
        if (!stopped) {
          await remember(`${identity.email}: ${detail}`, "error");
          if (hint) await remember(hint, "warn");
        }
      } finally {
        activeIdentities.delete(String(identity.email || "").toLowerCase());
      }

      lastProgress.done += 1;
      renderStats();
      renderStatus();

      if (consecutive >= 5) {
        stopRequested = true;
        await remember("circuit breaker: five failures in a row — pausing", "warn");
        break;
      }
    }
  }

  await Promise.all(Array.from({ length: laneCount }, (_, index) => worker(index)));

  if (stopRequested) await remember("stopped between identities", "warn");
  await refresh();
  return made;
}

// -------------------------------------------------------------------- finalise

async function finaliseAccount(account) {
  await remember(`finalising ${account.email}...`, "step");
  let tab = null;
  if (account.tab_id) tab = await api.tabs.get(account.tab_id).catch(() => null);

  if (!tab) {
    const options = { url: "https://www.roblox.com/home", active: false };
    if (account.store_id) options.cookieStoreId = account.store_id;
    try {
      tab = await api.tabs.create(options);
    } catch (_) {
      delete options.cookieStoreId;
      tab = await api.tabs.create(options);
    }
    await sleep(4000);
  }

  try {
    const settings = state.settings || {};
    const usernameMode = settings.usernameMode === "ours" ? "ours" : "generated";
    const identity = state.identities.find((entry) => entry.id === account.identity_id);
    const requestedUsername = usernameMode === "ours"
      ? identity?.roblox_username || makeUsername()
      : "";
    if (usernameMode === "ours" && requestedUsername && requestedUsername !== identity?.roblox_username) {
      await mutate((draft) => {
        const row = draft.identities.find((entry) => entry.id === account.identity_id);
        if (row) row.roblox_username = requestedUsername;
      });
    }

    const profile = profileFor(identity, settings);
    if (identity && (identity.birthday !== profile.birthday || identity.gender !== profile.gender)) {
      await mutate((draft) => {
        const row = draft.identities.find((entry) => entry.id === account.identity_id);
        if (row) {
          row.birthday = profile.birthday;
          row.gender = profile.gender;
        }
      });
    }

    const result = await retryFinalise(tab.id, 60, () => false, (text) =>
      remember(text, "info"), {
        username: requestedUsername,
        birthday: profile.birthday,
        gender: profile.gender,
        acceptTerms: settings.acceptTerms !== false,
      }
    );
    if (result.session?.status === 200) {
      await mutate((draft) => {
        const row = draft.accounts.find((entry) => entry.id === account.id);
        if (row) {
          row.status = "live";
          row.username = result.session.name;
          row.display_name = result.session.name;
          row.user_id = result.session.id;
          row.tab_id = null;
        }
        const identityRow = draft.identities.find((entry) => entry.id === account.identity_id);
        if (identityRow) {
          identityRow.status = "account";
          identityRow.error = null;
          if (usernameMode === "ours") identityRow.roblox_username = result.session.name;
        }
      });
      await remember(`${result.session.name} is live`, "ok");
      await api.tabs.remove(tab.id).catch(() => {});
    } else {
      await remember(`still pending: session ${result.session?.status}`, "warn");
    }
  } catch (error) {
    await remember(`finalise failed: ${error.message || error}`, "error");
  }

  await refresh();
}

// ----------------------------------------------------------------- permission

async function hasPermissions() {
  try {
    return await api.permissions.contains({ origins: ORIGINS });
  } catch (_) {
    return true;
  }
}

async function grantPermissions() {
  try {
    const granted = await api.permissions.request({ origins: ORIGINS });
    await refreshChecklist();
    if (granted) await remember("site access granted", "ok");
    else await remember("site access was declined — the Admin SDK and tabs will not work", "error");
  } catch (error) {
    await remember(`permission request failed: ${error.message || error}`, "error");
  }
}

// -------------------------------------------------------------------- phone

function switchTab(name) {
  const tab = document.querySelector(`.tab[data-tab="${name}"]`);
  if (tab) tab.click();
}

async function handlePayload(raw, source = "pasted") {
  const text = String(raw || "").trim();
  if (!text) return;

  // An authenticator QR is not a phone-gate payload. Route it to the setup card
  // wherever it was pasted or dropped.
  if (/^otpauth:\/\//i.test(text)) {
    switchTab("setup");
    await applyTotp(text, source);
    return;
  }

  switchTab("setup");

  if (/^sms(t?o)?:/i.test(text)) {
    await savePhone({ ...parseSms(text), payload: text, source });
    return;
  }

  if (/^https?:\/\//i.test(text)) {
    await remember(`opening ${text.slice(0, 90)}`, "step");
    try {
      const storeId = await googleStoreId();
      const opened = await openConsentPage(text, storeId);
      if (!opened || opened.gone) {
        await remember("the consent tab closed before it loaded", "warn");
        return;
      }
      await autoPressSendSms(opened.tabId);
    } catch (error) {
      await remember(`could not open that link: ${error.message || error}`, "error");
    }
    return;
  }

  await remember(
    `expected an image, an http link, or an sms: URI (got: ${text.slice(0, 60)})`,
    "warn"
  );
}

async function savePhone(record) {
  await mutate((draft) => {
    draft.phone = {
      number: (record.number || "").replace(/^\/+/, ""),
      body: record.body || "",
      payload: record.payload || record.sms || "",
      source: record.page ? `${record.source || "page"} · ${record.page.slice(0, 90)}` : record.source || "",
    };
  });
  await refresh();
  if (record.number || record.body) {
    await remember(`phone gate: ${record.number || "?"} · ${record.body || "?"}`, "ok");
  } else {
    await remember("decoded, but no number/message found — the page text is in the console below", "warn");
    if (record.text) await remember(record.text.slice(0, 600), "info");
  }
}

async function googleStoreId() {
  try {
    const tabs = await api.tabs.query({});
    const hit = tabs
      .filter((tab) => /accounts\.google\.com/.test(tab.url || ""))
      .pop();
    return hit?.cookieStoreId || null;
  } catch (_) {
    return null;
  }
}

async function finishPhoneCapture(uri, tabId) {
  const parsed = parseSms(uri);
  if (!parsed.number && !parsed.body) {
    await remember("the page handed over an empty sms: URI", "warn");
    return;
  }
  await savePhone({ ...parsed, payload: uri, source: "consent" });
  await remember(`captured ${parsed.number || "?"} · ${parsed.body || "?"}`, "ok");
  if (tabId) api.tabs.remove(tabId).catch(() => {});
}

// The consent page only gives the URI when its Send SMS button is pressed.
// Run it in a background tab and press it from there: no focus, no user step.
async function autoPressSendSms(tabId, { closeOnSuccess = true } = {}) {
  const deadline = Date.now() + 15_000;
  let lastProbe = null;
  const seen = [];

  const navListener = (details) => {
    if (details.tabId === tabId && /^sms:/i.test(details.url || "")) {
      seen.push(details.url);
    }
  };
  try {
    api.webNavigation.onBeforeNavigate.addListener(navListener);
  } catch (_) {
    // listener is a bonus; the hook still records
  }

  try {
    while (Date.now() < deadline) {
      const tab = await api.tabs.get(tabId).catch(() => null);
      if (!tab) {
        await remember("the Google tab was closed before Send SMS", "error");
        return false;
      }

      const probe = await probeSendButton(tabId);
      if (!probe) {
        await sleep(500);
        continue;
      }
      lastProbe = probe;

      if (!probe.hasButton) {
        await sleep(600);
        continue;
      }

      // The document may have changed since the tab opened (begin -> consent).
      await installSmsHook(tabId);
      const pressed = await pressSendSms(tabId);
      if (!pressed?.ok) {
        await sleep(400);
        continue;
      }

      const captureDeadline = Date.now() + 4000;
      while (Date.now() < captureDeadline) {
        if (seen.length) {
          await finishPhoneCapture(seen[0], closeOnSuccess ? tabId : null);
          return true;
        }
        const signal = await scanSignal(tabId);
        const uri =
          signal?.hooked || (signal?.hrefs || []).find((value) => /^sms:/i.test(value));
        if (uri) {
          await finishPhoneCapture(uri, closeOnSuccess ? tabId : null);
          return true;
        }
        await sleep(300);
      }

      await remember("pressed Send SMS but the page handed over nothing — try again", "warn");
      return false;
    }

    if (lastProbe && !lastProbe.hasButton) {
      await remember("Send SMS never appeared — try again", "warn");
    } else {
      await remember("timed out waiting for the phone gate — try again", "warn");
    }
    return false;
  } finally {
    try {
      api.webNavigation.onBeforeNavigate.removeListener(navListener);
    } catch (_) {
      // never registered
    }
  }
}

async function scanGoogleTab() {
  const tabs = await api.tabs.query({});
  let mine = null;
  try {
    mine = await Promise.resolve(api.tabs.getCurrent?.());
  } catch (_) {
    mine = null;
  }
  const candidates = tabs.filter(
    (tab) => tab.id !== mine?.id && /accounts\.google\.com/.test(tab.url || "")
  );
  const target = candidates[candidates.length - 1];
  if (!target) {
    await remember("no Google tab open — open the signup flow first", "warn");
    return;
  }

  await remember(`scanning ${target.url.slice(0, 90)}`, "step");
  try {
    const page = await scanTab(target.id);
    const record = await extractFromPage(page);

    if (record.number || record.body) {
      await savePhone(record);
      return;
    }

    if (record.payload && /^https?:\/\//i.test(record.payload)) {
      const storeId = target.cookieStoreId || (await googleStoreId());
      await remember(`opening ${record.payload.slice(0, 80)}`, "step");
      const opened = await openConsentPage(record.payload, storeId);
      if (!opened || opened.gone) {
        await remember("the consent tab closed before it loaded", "warn");
        return;
      }
      await autoPressSendSms(opened.tabId);
      return;
    }

    if (looksLikeConsent(page)) {
      await autoPressSendSms(target.id, { closeOnSuccess: false });
      return;
    }

    const shot = await captureVisible(target.windowId);
    if (shot) {
      try {
        const decoded = await decodeImageUrl(shot);
        if (decoded) {
          await handlePayload(decoded, "screenshot");
          return;
        }
      } catch (_) {
        // screenshot decode is a fallback
      }
    }

    await remember("no QR and no phone-gate link on that tab", "warn");
  } catch (error) {
    await remember(`scan failed: ${error.message || error}`, "error");
  }
}

async function decodeFile(file) {
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("could not read the image"));
    reader.readAsDataURL(file);
  });
  await decodeDataUrl(dataUrl, file?.name ? `image · ${file.name}` : "image");
}

async function decodeDataUrl(dataUrl, source = "image") {
  const payload = await decodeImageUrl(dataUrl);
  if (!payload) {
    await remember("no barcode found in that image", "warn");
    return;
  }
  await handlePayload(payload, source);
}

function clipboardFiles(data) {
  const files = [...(data?.files || [])];
  for (const item of data?.items || []) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file) files.push(file);
  }
  return files;
}

function isImageFile(file) {
  if (!file) return false;
  return (
    /^image\//i.test(file.type || "") ||
    /\.(png|jpe?g|webp|gif|bmp|avif)$/i.test(file.name || "")
  );
}

// -------------------------------------------------------------------- auth setup

function totpInput() {
  const raw = $("totp-secret").value.trim();
  if (!raw) throw new Error("paste the authenticator secret or otpauth:// URI");
  return parseTotp(raw);
}

async function saveTotp() {
  try {
    const parsed = totpInput();
    await totpCode(parsed.secret, parsed);
    const typed = $("totp-email").value.trim().toLowerCase();
    const email = typed || parsed.email || defaultTotpEmail();
    await mutate((draft) => {
      draft.totp = { ...parsed, email };
    });
    await refresh();
    await refreshChecklist();
    await remember(`authenticator saved${email ? ` for ${email}` : ""}`, "ok");
  } catch (error) {
    setText("totp-status", error.message || String(error));
    await remember(`authenticator: ${error.message || error}`, "error");
  }
}

async function clearTotp() {
  await mutate((draft) => {
    draft.totp = {
      email: "",
      secret: "",
      algorithm: "SHA1",
      digits: 6,
      period: 30,
      issuer: "",
      label: "",
    };
  });
  const secret = $("totp-secret");
  if (secret) secret.value = "";
  await refresh();
  await refreshChecklist();
  await remember("authenticator secret cleared", "info");
}

async function applyTotp(payload, source = "QR") {
  const text = String(payload || "").trim();
  if (!/^otpauth:\/\//i.test(text)) {
    setText("totp-status", `no otpauth:// URI in that ${source}`);
    await remember(`authenticator ${source}: not an otpauth URI`, "warn");
    return false;
  }

  try {
    const parsed = parseTotp(text);
    await totpCode(parsed.secret, parsed);
    const typed = $("totp-email").value.trim().toLowerCase();
    const email = parsed.email || typed || defaultTotpEmail();
    await mutate((draft) => {
      draft.totp = { ...parsed, email };
    });
    await refresh();
    await refreshChecklist();
    await remember(`authenticator QR imported${email ? ` for ${email}` : ""}`, "ok");
    return true;
  } catch (error) {
    setText("totp-status", error.message || String(error));
    await remember(`authenticator QR: ${error.message || error}`, "error");
    return false;
  }
}

async function decodeTotpFile(file) {
  if (!file || !isImageFile(file)) {
    await remember("the authenticator QR must be an image", "warn");
    return;
  }

  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("could not read the image"));
    reader.readAsDataURL(file);
  });

  const payload = await decodeImageUrl(dataUrl);
  if (!payload) {
    setText("totp-status", "no QR code found in that image");
    await remember("no QR code found in that authenticator image", "warn");
    return;
  }
  await applyTotp(payload, file.name ? `QR \u00b7 ${file.name}` : "QR");
}

async function checkTwoStep() {
  const key =
    state.keys.find((entry) => entry.status === "ok") ||
    state.keys.find((entry) => !entry.needs_consent);
  if (!key) {
    await remember("load a key before checking 2SV", "warn");
    return;
  }

  const email =
    $("totp-email").value.trim().toLowerCase() ||
    state.totp?.email ||
    key.admin ||
    defaultTotpEmail();
  if (!email) {
    await remember("set the admin email in the Authenticator setup card", "warn");
    return;
  }

  try {
    const status = await admin.twoStepStatus(key, email);
    const label = `${status.email}: enrolled=${status.enrolled} enforced=${status.enforced}`;
    setText("twofa-status", label);
    await remember(label, status.enrolled ? "ok" : "warn");
    if (status.enforced) {
      await remember(
        "org 2SV enforcement is On — check that Methods excludes verification codes via text/phone call, otherwise every fresh identity is offered SMS",
        "warn"
      );
    } else {
      await remember(
        "org 2SV enforcement is Off — fresh identities fall back to the phone challenge; set Enforcement On with SMS/call disallowed",
        "warn"
      );
    }
    await refreshChecklist();
  } catch (error) {
    setText("twofa-status", error.message || String(error));
    await remember(`2SV check: ${error.message || error}`, "error");
  }
}

// -------------------------------------------------------------------- duckdns

async function saveDuck(fields) {
  await mutate((draft) => {
    draft.duckdns = { ...(draft.duckdns || {}), ...fields };
  });
  state = await load();
  renderDuckdns();
  renderSetup();
}

function duckInputs() {
  const token = $("duck-token").value.trim();
  const sub = ($("duck-sub").value.trim().toLowerCase() || suggestedSubdomain())
    .replace(/\.duckdns\.org$/, "");
  const raw = $("duck-value").value.trim();
  const value =
    raw && !raw.toLowerCase().startsWith("google-site-verification=")
      ? `google-site-verification=${raw}`
      : raw;
  return { token, sub, value };
}

async function duckWrite() {
  const { token, sub, value } = duckInputs();
  if (!token || !sub || !value) {
    await remember("token, subdomain and TXT value are all required", "error");
    return;
  }
  if (!/^[a-z0-9-]+$/.test(sub)) {
    await remember(`subdomain must be a single DuckDNS label: ${sub}`, "error");
    return;
  }

  await saveDuck({ token, subdomain: sub, value, answer: "" });
  try {
    await remember(`writing TXT for ${fullDomain(sub)}`, "step");
    const reply = await writeTxt(token, sub, value);
    await remember(`DuckDNS: ${reply.split("\n")[0]}`, "ok");

    const result = await waitForTxt(fullDomain(sub), value, 90_000, (_answers, rounds) => {
      if (rounds % 3 === 0) {
        setText("duck-status", `waiting for DNS… ${rounds} checks`);
        remember(`waiting for the record to resolve (${rounds})`, "info");
      }
    });

    const answer = result.found
      ? `resolves ✓ ${result.answers.join(" | ")}`
      : `not visible yet — ${result.answers.join(" | ") || "no TXT answer"}`;
    await saveDuck({ answer, checked_at: new Date().toISOString() });

    if (result.found) {
      await remember(`TXT is live on ${fullDomain(sub)}`, "ok");
    } else {
      await remember(
        "TXT was accepted but does not resolve yet — DNS can lag a minute. Press Check now, then Verify in Admin.",
        "warn"
      );
    }
    await refreshChecklist();
  } catch (error) {
    await saveDuck({ answer: String(error.message || error) });
    await remember(`DuckDNS failed: ${error.message || error}`, "error");
  }
}

async function duckCheck() {
  const { sub, value } = duckInputs();
  if (!sub) {
    await remember("a subdomain is required", "error");
    return;
  }
  try {
    const answers = await lookupTxt(fullDomain(sub));
    const hit = value
      ? answers.some((entry) => entry.includes(value))
      : answers.some((entry) => /google-site-verification/i.test(entry));
    const answer = answers.length
      ? `${hit ? "resolves ✓" : "not matching"} — ${answers.join(" | ")}`
      : "no TXT answer";
    await saveDuck({ answer, checked_at: new Date().toISOString() });
    await remember(`${fullDomain(sub)}: ${answer}`, hit ? "ok" : "warn");
    await refreshChecklist();
  } catch (error) {
    await remember(`DNS check failed: ${error.message || error}`, "error");
  }
}

async function duckClear() {
  const { token, sub } = duckInputs();
  if (!token || !sub) {
    await remember("token and subdomain are required to clear the record", "error");
    return;
  }
  try {
    await clearTxt(token, sub);
    await saveDuck({ answer: "record cleared" });
    await remember(`TXT cleared on ${fullDomain(sub)}`, "info");
    await refreshChecklist();
  } catch (error) {
    await remember(`clear failed: ${error.message || error}`, "error");
  }
}

// -------------------------------------------------------------------- checklist

function mark(check, ok) {
  const row = document.querySelector(`[data-check="${check}"]`);
  if (!row) return;
  row.dataset.ok = ok === null ? "" : String(ok);
  const em = row.querySelector("em");
  if (em) em.textContent = ok === null ? "waiting" : ok ? "ok" : "missing";
}

async function refreshChecklist() {
  const granted = await hasPermissions();
  $("grant").hidden = granted;
  mark("perm", granted);

  const key = state.keys[0];
  const liveKey = state.keys.find((entry) => entry.status === "ok");
  const named = state.keys.find((entry) => entry.domain) || key;

  mark("key", Boolean(key));
  mark("auth", liveKey ? true : key ? false : null);
  mark("identity", state.identities.length > 0 ? true : null);

  let twofa = null;
  if (liveKey) {
    const email =
      state.totp?.email ||
      liveKey.admin ||
      (liveKey.domain ? `admin@${liveKey.domain}` : null);
    if (email) {
      try {
        const status = await admin.twoStepStatus(liveKey, email);
        twofa = status.enrolled;
        setText(
          "twofa-status",
          `${status.email}: enrolled=${status.enrolled} enforced=${status.enforced}`
        );
        if (!status.enforced) {
          await remember(
            `${status.email}: 2SV enforcement is Off \u2014 fresh identities fall back to the phone challenge; set Enforcement On with Methods excluding text/call`,
            "warn"
          );
        } else if (!status.enrolled) {
          await remember(
            `${status.email}: 2SV not enrolled \u2014 save the authenticator secret and finish Google's setup`,
            "warn"
          );
        }
      } catch (error) {
        setText("twofa-status", `${email}: ${error.message || error}`);
        if (error.status === 403) {
          await remember(
            `${email}: this key cannot read 2SV status \u2014 re-consent with admin.directory.user`,
            "info"
          );
        } else if (error.status !== 404) {
          await remember(`2SV check failed: ${error.message || error}`, "warn");
        }
      }
    } else {
      setText("twofa-status", "set the admin email in Authenticator setup");
    }
  } else {
    setText("twofa-status", "2SV not checked");
  }
  mark("twofa", twofa);

  if (!named?.domain) {
    mark("dns", null);
    mark("verified", null);
    renderSetup();
    return;
  }

  // Google's own flag first: it survives the TXT record being removed later.
  let verified = null;
  if (liveKey) {
    try {
      const status = await admin.domainStatus(liveKey, named.domain);
      verified = status.verified;
      if (!verified) {
        await remember(
          `${named.domain}: Google has not verified it yet${status.present ? "" : " (not in the tenant list)"}`,
          "warn"
        );
      }
    } catch (error) {
      if (error.status === 403) {
        await remember(
          `${named.domain}: this key has no domain scope — re-consent to read Google's verified flag`,
          "info"
        );
      } else {
        await remember(`domain status failed: ${error.message || error}`, "warn");
      }
    }
  }
  mark("verified", verified);

  const expected = state.duckdns?.value || "";
  try {
    const answers = await lookupTxt(named.domain);
    const hit = expected
      ? answers.some((value) => value.includes(expected))
      : answers.some((value) => /google-site-verification/i.test(value));
    mark("dns", hit);
    if (!hit && verified !== true) {
      await remember(
        `${named.domain}: TXT not visible yet${answers.length ? ` (${answers.join(" | ")})` : ""}`,
        "warn"
      );
    }
  } catch (error) {
    mark("dns", false);
    await remember(`DNS check failed: ${error.message || error}`, "warn");
  }
  renderSetup();
}

// ---------------------------------------------------------------- setup wizard

// Tickable steps. "limits" is reference material and is never counted.
const SETUP_TICKABLE = ["dns", "tenant", "twostep", "key", "run"];

function setupState() {
  const setup = state.setup || {};
  return {
    done: setup.done || {},
    open: setup.open || {},
    hideDone: Boolean(setup.hideDone),
  };
}

async function setSetup(fn) {
  await mutate((draft) => {
    const base = draft.setup || {};
    draft.setup = {
      ...base,
      done: { ...(base.done || {}) },
      open: { ...(base.open || {}) },
    };
    fn(draft.setup);
  });
  state = await load();
  renderSetup();
}

function openStep(id, scroll = false) {
  const step = document.querySelector(`.step[data-step="${id}"]`);
  if (!step) return;
  step.classList.add("open");
  step.querySelector(".step-head")?.setAttribute("aria-expanded", "true");
  setSetup((draft) => {
    draft.open[id] = true;
  }).catch(() => {});
  if (scroll) step.scrollIntoView({ block: "start", behavior: "smooth" });
}

function renderSetup() {
  const steps = [...document.querySelectorAll(".step[data-step]")];
  if (!steps.length) return;

  const setup = setupState();
  const firstOpen = SETUP_TICKABLE.find((id) => !setup.done[id]);
  let complete = 0;

  for (const step of steps) {
    const id = step.dataset.step;
    const tickable = SETUP_TICKABLE.includes(id);
    const done = tickable && Boolean(setup.done[id]);
    if (done) complete += 1;
    step.dataset.done = String(done);

    const tick = step.querySelector("[data-step-done]");
    if (tick) tick.checked = done;

    // Explicit choice wins; otherwise the first unfinished step is the one that
    // is open, so a returning user lands where they left off.
    const open = id in setup.open ? Boolean(setup.open[id]) : id === firstOpen;
    step.classList.toggle("open", open);
    step.querySelector(".step-head")?.setAttribute("aria-expanded", String(open));

    const pill = step.querySelector("[data-step-status]");
    if (pill && tickable) {
      pill.textContent = done ? "done" : "waiting";
      pill.dataset.state = done ? "ok" : "";
    }
  }

  // The automatic checks are evidence, not the user's own progress: they colour
  // the step pill and never tick the box for you.
  for (const row of document.querySelectorAll("#checklist li[data-check]")) {
    if (row.dataset.ok !== "true") continue;
    const id = row.dataset.step;
    if (!id) continue;
    const step = document.querySelector(`.step[data-step="${id}"]`);
    const pill = document.querySelector(`[data-step-status="${id}"]`);
    if (!step || !pill || step.dataset.done === "true") continue;
    pill.textContent = "verified";
    pill.dataset.state = "ok";
  }

  setText("setup-count", `${complete} of ${SETUP_TICKABLE.length} done`);
  const bar = $("setup-bar");
  if (bar) bar.style.width = `${Math.round((complete / SETUP_TICKABLE.length) * 100)}%`;

  const hide = $("setup-hide");
  if (hide) {
    hide.setAttribute("aria-pressed", String(setup.hideDone));
    hide.classList.toggle("primary", setup.hideDone);
  }
  document.getElementById("panel-setup")?.classList.toggle("hide-done", setup.hideDone);

  const duck = state.duckdns || {};
  if ($("duck-saved")) {
    setText(
      "duck-saved",
      duck.token && duck.subdomain
        ? `saved · ${duck.subdomain}.duckdns.org`
        : "not saved yet — step 2 needs these"
    );
  }
}

function wireSetup() {
  const panel = $("panel-setup");
  if (!panel) return;

  panel.addEventListener("click", (event) => {
    // The tick is a real checkbox; let its change event do the work.
    if (event.target.closest("[data-step-done]")) return;
    const head = event.target.closest(".step-head");
    if (!head) return;
    const step = head.closest(".step");
    const id = step?.dataset.step;
    if (!id) return;
    const open = !step.classList.contains("open");
    step.classList.toggle("open", open);
    head.setAttribute("aria-expanded", String(open));
    setSetup((draft) => {
      draft.open[id] = open;
    }).catch(() => {});
  });

  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const head = event.target.closest(".step-head");
    if (!head) return;
    if (event.target.matches("input, button, a, summary")) return;
    event.preventDefault();
    head.click();
  });

  panel.addEventListener("change", (event) => {
    const box = event.target.closest("[data-step-done]");
    if (!box) return;
    const id = box.dataset.stepDone;
    setSetup((draft) => {
      draft.done[id] = box.checked;
    }).catch(() => {});
  });

  $("setup-jump")?.addEventListener("click", () => {
    const setup = setupState();
    openStep(SETUP_TICKABLE.find((id) => !setup.done[id]) || "run", true);
  });

  $("setup-hide")?.addEventListener("click", () => {
    setSetup((draft) => {
      draft.hideDone = !draft.hideDone;
    }).catch(() => {});
  });

  panel.querySelectorAll(".jump-tab").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelector(`.tab[data-tab="${button.dataset.tab}"]`)?.click();
    });
  });

  // External destinations are buttons now, not links inline in a sentence: the
  // action sits under the sub-step it belongs to instead of interrupting it.
  panel.addEventListener("click", (event) => {
    const out = event.target.closest(".link-out");
    if (!out || !out.dataset.href) return;
    window.open(out.dataset.href, "_blank", "noopener,noreferrer");
  });
}

// ---------------------------------------------------------------------- events

function wire() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("active", node === tab));
      document.querySelectorAll(".tabpanel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === `panel-${tab.dataset.tab}`);
      });
    });
  });

  wireSetup();

  $("start").addEventListener("click", () => start({ preflight: false }));
  $("preflight").addEventListener("click", () => start({ preflight: true }));
  $("mint-codes").addEventListener("click", () => mintReadyCodes().catch(async (error) => {
    await remember(error.message || String(error), "error");
  }));
  $("requeue").addEventListener("click", async () => {
    let count = 0;
    await mutate((draft) => {
      for (const identity of draft.identities) {
        if (identity.status === "failed" && !identity.account_id) {
          identity.status = "ready";
          identity.error = null;
          delete identity.codes;
          count += 1;
        }
      }
    });
    await refresh();
    await remember(count ? `${count} identity(ies) requeued` : "nothing to requeue", count ? "ok" : "info");
  });
  $("stop").addEventListener("click", async () => {
    stopRequested = true;
    await remember("stop requested — finishing the current identity", "warn");
    await mutate((draft) => {
      draft.run = { ...(draft.run || {}), state: "stopping" };
    });
    renderStatus();
  });

  $("count").addEventListener("change", async (event) => {
    const value = Math.max(1, Math.min(200, Number(event.target.value) || 1));
    await mutate((draft) => {
      draft.settings.count = value;
    });
    await refresh();
  });

  $("lanes").addEventListener("change", async (event) => {
    const value = Math.max(1, Math.min(isFirefox ? 4 : 1, Number(event.target.value) || 1));
    await mutate((draft) => {
      draft.settings.lanes = value;
    });
    await refresh();
  });

  $("timeout").addEventListener("change", async (event) => {
    const value = Math.max(60, Math.min(900, Number(event.target.value) || 240));
    await mutate((draft) => {
      draft.settings.timeout = value;
    });
    await refresh();
  });

  $("auto-export").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.autoExport = event.target.checked;
    });
  });

  $("username-mode").addEventListener("change", async (event) => {
    const value = event.target.value === "ours" ? "ours" : "generated";
    await mutate((draft) => {
      draft.settings.usernameMode = value;
    });
    await refresh();
  });

  $("birthday").addEventListener("change", async (event) => {
    const value = /^\d{4}-\d{2}-\d{2}$/.test(event.target.value)
      ? event.target.value
      : "1996-05-15";
    await mutate((draft) => {
      draft.settings.birthday = value;
    });
    await refresh();
  });

  $("random-birthday").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.randomBirthday = Boolean(event.target.checked);
    });
    await refresh();
  });

  $("gender").addEventListener("change", async (event) => {
    const value = [1, 2, 3].includes(Number(event.target.value)) ? Number(event.target.value) : 1;
    await mutate((draft) => {
      draft.settings.gender = value;
    });
    await refresh();
  });

  $("random-gender").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.randomGender = Boolean(event.target.checked);
    });
    await refresh();
  });

  $("accept-terms").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.acceptTerms = Boolean(event.target.checked);
    });
  });

  $("concurrency").addEventListener("change", async (event) => {
    const value = Math.max(1, Math.min(20, Number(event.target.value) || 8));
    await mutate((draft) => {
      draft.settings.concurrency = value;
    });
    await refresh();
  });

  $("open-on-install").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.openOnInstall = Boolean(event.target.checked);
    });
  });

  $("lane-surface").addEventListener("change", async (event) => {
    const value = event.target.value === "window" ? "window" : "tab";
    await mutate((draft) => {
      draft.settings.laneSurface = value;
    });
    await refresh();
  });

  $("retry-moderated").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.retryModerated = Boolean(event.target.checked);
    });
  });

  $("proxy-url").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.proxyUrl = event.target.value.trim();
    });
    await refresh();
  });

  $("proxy-list").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.proxyList = event.target.value.trim();
    });
    await refresh();
  });

  $("delete-batch").addEventListener("change", async (event) => {
    const value = Math.max(1, Math.min(50, Number(event.target.value) || 5));
    await mutate((draft) => {
      draft.settings.deleteBatch = value;
    });
    await refresh();
  });

  $("delete-pace").addEventListener("change", async (event) => {
    const value = Math.max(5, Math.min(600, Number(event.target.value) || 45));
    await mutate((draft) => {
      draft.settings.deletePaceSeconds = value;
    });
    await refresh();
  });

  $("add-keys").addEventListener("click", async () => {
    const text = $("key-input").value.trim();
    if (!text) {
      await remember("paste a credential first", "warn");
      return;
    }
    try {
      await addKeys(text);
      $("key-input").value = "";
    } catch (error) {
      await remember(error.message || String(error), "error");
    }
  });

  $("keys-from-file").addEventListener("click", () => $("key-file").click());
  $("key-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      await addKeys(await file.text(), file.name);
    } catch (error) {
      await remember(error.message || String(error), "error");
    }
    event.target.value = "";
  });

  const drop = $("key-drop");
  drop.addEventListener("dragover", (event) => {
    event.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", async (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    const file = event.dataTransfer?.files?.[0];
    if (!file) return;
    try {
      await addKeys(await file.text(), file.name);
    } catch (error) {
      await remember(error.message || String(error), "error");
    }
  });

  $("export").addEventListener("click", () => exportAccounts(false));
  $("copy-all").addEventListener("click", () =>
    copyText(state.accounts.map(accountText).join("\n\n"))
  );
  $("wipe").addEventListener("click", async () => {
    if (!confirm("Wipe every stored account, identity and log line? Keys stay.")) return;
    await wipeRecords();
    await refresh();
    await remember("records wiped", "info");
  });

  $("scan-tab").addEventListener("click", () => scanGoogleTab());
  $("pick-qr").addEventListener("click", () => $("qr-file").click());
  $("qr-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (file) {
      try {
        await decodeFile(file);
      } catch (error) {
        await remember(error.message || String(error), "error");
      }
    }
    event.target.value = "";
  });

  // Paste and drop: a screenshot of the QR, or the consent link as text.
  const qrDrop = $("qr-drop");
  const phonePanel = $("phone-gate");

  if (qrDrop) {
    qrDrop.addEventListener("click", () => $("qr-file").click());
    qrDrop.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        $("qr-file").click();
      }
    });
  }

  if (phonePanel && qrDrop) {
    phonePanel.addEventListener("dragover", (event) => {
      event.preventDefault();
      qrDrop.classList.add("over");
    });
    phonePanel.addEventListener("dragleave", (event) => {
      if (!phonePanel.contains(event.relatedTarget)) qrDrop.classList.remove("over");
    });
    phonePanel.addEventListener("drop", async (event) => {
      event.preventDefault();
      qrDrop.classList.remove("over");

      const data = event.dataTransfer;
      const types = [...(data?.types || [])];
      const files = clipboardFiles(data);
      const image = files.find(isImageFile);
      if (image) {
        switchTab("setup");
        try {
          await decodeFile(image);
        } catch (error) {
          await remember(error.message || String(error), "error");
        }
        return;
      }

      const text = (
        data?.getData("text/uri-list") ||
        data?.getData("text/plain") ||
        ""
      ).trim();

      if (/^data:image\//i.test(text)) {
        switchTab("setup");
        try {
          await decodeDataUrl(text, "dropped");
        } catch (error) {
          await remember(error.message || String(error), "error");
        }
        return;
      }
      if (/^file:\/\//i.test(text)) {
        await remember(
          "the browser hides local file paths — use Decode image to pick the file",
          "warn"
        );
        return;
      }
      if (text) {
        await handlePayload(text, "dropped");
        return;
      }

      if (files.length) {
        await remember(
          `that file is not an image (${files[0].type || files[0].name || "unknown"})`,
          "warn"
        );
      } else {
        await remember(
          `nothing usable in that drop (had: ${types.join(", ") || "no types"})`,
          "warn"
        );
      }
    });
  }

  document.addEventListener("paste", async (event) => {
    const data = event.clipboardData;
    if (!data) return;

    const types = [...(data.types || [])];
    const files = clipboardFiles(data);
    const image = files.find(isImageFile);
    if (image) {
      event.preventDefault();
      switchTab("setup");
      try {
        await decodeFile(image);
      } catch (error) {
        await remember(error.message || String(error), "error");
      }
      return;
    }

    const html = data.getData("text/html") || "";
    const fromHtml = /src\s*=\s*["'](data:image\/[^"']+)["']/i.exec(html)?.[1];
    const text = (data.getData("text/plain") || "").trim();
    const dataImage = fromHtml || (/^data:image\//i.test(text) ? text : null);

    if (dataImage) {
      event.preventDefault();
      switchTab("setup");
      try {
        await decodeDataUrl(dataImage, "pasted");
      } catch (error) {
        await remember(error.message || String(error), "error");
      }
      return;
    }

    if (/^(https?:\/\/|sms:|smsto:|otpauth:\/\/)/i.test(text)) {
      event.preventDefault();
      await handlePayload(text, "pasted");
      return;
    }

    if (/^file:\/\//i.test(text)) {
      event.preventDefault();
      await remember(
        "the browser hides local file paths — use Decode image to pick the file",
        "warn"
      );
      return;
    }

    if (files.length || types.some((type) => type.startsWith("image/"))) {
      await remember(
        `could not read that paste (had: ${types.join(", ") || "no types"}) — use Decode image`,
        "warn"
      );
    }
  });

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = $(button.dataset.copyTarget);
      await copyText(target?.textContent || "");
    });
  });

  $("copy-log").addEventListener("click", () =>
    copyText((state.log || []).map((line) => `[${stamp(line.t)}] ${line.text}`).join("\n"))
  );
  $("clear-log").addEventListener("click", async () => {
    await api.runtime.sendMessage({ type: "clear-log" });
    const body = $("console-body");
    body.replaceChildren();
    consoleCount = 0;
    setText("console-stat", "0 lines");
  });

  $("recheck").addEventListener("click", () => refreshChecklist());
  $("grant").addEventListener("click", () => grantPermissions());

  $("duck-token").addEventListener("change", (event) => {
    saveDuck({ token: event.target.value.trim() }).catch(() => {});
  });
  $("duck-sub").addEventListener("change", (event) => {
    const sub = event.target.value.trim().toLowerCase().replace(/\.duckdns\.org$/, "");
    saveDuck({ subdomain: sub }).catch(() => {});
  });
  $("duck-value").addEventListener("change", (event) => {
    saveDuck({ value: event.target.value.trim() }).catch(() => {});
  });
  $("duck-write").addEventListener("click", () => duckWrite());
  $("duck-check").addEventListener("click", () => duckCheck());
  $("duck-clear").addEventListener("click", () => duckClear());

  $("totp-save").addEventListener("click", () => saveTotp());
  $("totp-check").addEventListener("click", () => checkTwoStep());
  $("totp-clear").addEventListener("click", () => clearTotp());
  $("totp-qr").addEventListener("click", () => $("totp-qr-file").click());
  $("totp-qr-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (file) {
      try {
        await decodeTotpFile(file);
      } catch (error) {
        await remember(error.message || String(error), "error");
      }
    }
    event.target.value = "";
  });

  const totpDrop = $("totp-drop");
  if (totpDrop) {
    totpDrop.addEventListener("click", () => $("totp-qr-file").click());
    totpDrop.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        $("totp-qr-file").click();
      }
    });
    totpDrop.addEventListener("dragover", (event) => {
      event.preventDefault();
      totpDrop.classList.add("over");
    });
    totpDrop.addEventListener("dragleave", () => totpDrop.classList.remove("over"));
    totpDrop.addEventListener("drop", async (event) => {
      event.preventDefault();
      totpDrop.classList.remove("over");
      const file = event.dataTransfer?.files?.[0];
      if (!file) return;
      try {
        await decodeTotpFile(file);
      } catch (error) {
        await remember(error.message || String(error), "error");
      }
    });
  }

  api.runtime.onMessage.addListener((message) => {
    if (message?.type === "google-need-codes" && message.email) {
      mintCodesOnDemand(message.email).catch(() => {});
      return false;
    }
    if (message?.type === "identity-totp" && message.secret) {
      const email = String(message.email || "").toLowerCase();
      liveOtp.set(email, message.secret);
      mutate((draft) => {
        const row = draft.identities.find(
          (entry) => String(entry.email || "").toLowerCase() === email
        );
        if (row) row.otp_secret = message.secret;
        for (const account of draft.accounts) {
          if (String(account.email || "").toLowerCase() === email) {
            account.otp_secret = message.secret;
          }
        }
      }).catch(() => {});
      remember(`${email || "identity"}: authenticator enrolled, secret saved`, "ok");
    }
    if (message?.type === "log-line" && message.line) {
      // Our own lines were appended when they happened; this is the echo.
      if (!message.line.cid || message.line.cid !== CLIENT_ID) appendLogNode(message.line);
    }
    if (message?.type === "log-cleared") {
      $("console-body").replaceChildren();
      consoleCount = 0;
      setText("console-stat", "0 lines");
    }
    if (message?.type === "select-tab" && message.tab) {
      const target = document.querySelector(`.tab[data-tab="${message.tab}"]`);
      if (target) target.click();
    }
    return false;
  });

  api.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes[STATE_KEY]) {
      const next = changes[STATE_KEY].newValue;
      state = { ...state, ...(next || {}), log: state?.log || [] };
      renderStats();
      renderKeys();
      renderAccounts();
      renderQueue();
      renderPhone();
      renderTotp();
    }
  });
}

// ------------------------------------------------------------------------ boot

// A runner tab survives an extension reload, but its module graph does not get
// replaced: the page keeps calling the old code while the new background and
// content scripts are live. That looks exactly like a broken flow, so say it.
//
// The test has to be synchronous. An earlier version asked the background over
// runtime.sendMessage and treated "no reply" as "stale" — a dropped message and
// a dead context are not the same thing, and it flagged healthy pages. A page
// orphaned by a reload loses its runtime object entirely, so getManifest()
// throwing is the real signal.
function checkBuild() {
  try {
    const manifest =
      api.runtime && typeof api.runtime.getManifest === "function"
        ? api.runtime.getManifest()
        : null;
    if (!manifest || !manifest.version) throw new Error("no runtime");
    setText("build", `v${manifest.version}`);
    $("stale")?.classList.remove("show");
    return true;
  } catch (_) {
    setText("build", "stale");
    $("stale")?.classList.add("show");
    return false;
  }
}

async function boot() {
  state = await load();
  wire();
  renderConsoleLog();
  render();
  // Paint the wizard before the checklist's API calls: step blocks and manual
  // ticks should be right immediately, not after a round-trip to Google.
  renderSetup();
  await remember("runner ready", "info");
  await checkBuild();

  setInterval(() => {
    setText("console-clock", new Date().toTimeString().slice(0, 8));
    updateTotp();
  }, 1000);
  setInterval(() => {
    // Cheap heartbeat: a reload invalidates the old page's runtime, and this is
    // the only reliable way for a page with no open connection to notice.
    checkBuild();
  }, 15_000);
  setText("console-clock", new Date().toTimeString().slice(0, 8));

  const granted = await hasPermissions();
  $("grant").hidden = granted;

  await refreshChecklist();

  // #keys, #setup, and #setup/tenant for a specific step block.
  const [hash, stepId] = location.hash.replace("#", "").split("/");
  if (hash && document.querySelector(`.tab[data-tab="${hash}"]`)) {
    document.querySelector(`.tab[data-tab="${hash}"]`).click();
  }
  if (hash === "setup") {
    renderSetup();
    if (stepId) setTimeout(() => openStep(stepId, true), 50);
  }

  if (!state.keys.length) {
    if (!hash) document.querySelector('.tab[data-tab="keys"]')?.click();
    await remember("welcome — add a key first, or read Setup", "info");
  } else {
    await remember(`loaded ${state.keys.length} key(s), ${state.accounts.length} account(s)`, "info");
  }
}

boot().catch((error) => {
  const body = $("console-body");
  if (body) {
    const node = document.createElement("div");
    node.className = "line error";
    node.textContent = `boot failed: ${error.message || error}`;
    body.append(node);
  }
});
