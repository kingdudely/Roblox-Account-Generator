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
import { makeIdentityFields, makeUsername, makePerson, makeBusinessName, makeAdminPassword } from "./lib/names.js";
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
// Identities whose authenticator enrolment the content script has confirmed
// (Google accepted the TOTP), as opposed to merely captured the setup key.
const liveEnrolled = new Set();
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
  renderSetup();
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
    // A deleted Google user no longer holds a seat. Counting the tombstone row
    // would make the meter drift down forever as identities are recycled.
    if (identity.status === "deleted") continue;
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

  const deleteGoogleAfter = $("delete-google-after");
  if (deleteGoogleAfter) deleteGoogleAfter.checked = settings.deleteGoogleAfterSuccess !== false;

  const concurrency = $("concurrency");
  if (concurrency) {
    concurrency.value = String(settings.concurrency || 8);
    concurrency.disabled = false;
  }

  const openOnInstall = $("open-on-install");
  if (openOnInstall) openOnInstall.checked = settings.openOnInstall !== false;

  const primeGoogle = $("prime-google");
  if (primeGoogle) primeGoogle.checked = settings.primeGoogle !== false;

  const laneSurface = $("lane-surface");
  if (laneSurface) laneSurface.value = settings.laneSurface === "window" ? "window" : "tab";

  const deleteBatch = $("delete-batch");
  if (deleteBatch) deleteBatch.value = String(settings.deleteBatch || 5);

  const deletePace = $("delete-pace");
  if (deletePace) deletePace.value = String(settings.deletePaceSeconds || 45);

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
    const used = state.identities.filter(
      (identity) => identity.key_id === key.id && identity.status !== "deleted"
    ).length;
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
        // Re-run consent so an existing key can pick up scopes added to
        // SCOPES (the audit and policy reads). The old refresh token keeps
        // working if this is dismissed.
        actionButton("Re-consent", () => authorizeKey(key.id), "ghost"),
        actionButton("Admin", () => showAdmin(key.id), "ghost"),
        actionButton("Recover admin", () => recoverAdmin(key.id), "ghost"),
        actionButton("JSON", () => downloadKeyJson(key.id), "ghost"),
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

function logNode(line) {
  const node = document.createElement("div");
  node.className = `line ${line.level || "info"}`;

  const time = document.createElement("time");
  time.textContent = stamp(line.t);

  const text = document.createElement("span");
  text.className = "text";
  text.textContent = line.text;

  node.append(time, text);
  return node;
}

function renderConsoleLog() {
  const body = $("console-body");
  const fragment = document.createDocumentFragment();
  for (const line of (state.log || []).slice(-300)) {
    fragment.append(logNode(line));
  }
  body.replaceChildren(fragment);
  consoleCount = body.children.length;
  setText("console-stat", `${consoleCount} lines`);
  body.scrollTop = body.scrollHeight;
}

function appendLogNode(line, autoscroll = true) {
  const body = $("console-body");
  const nearBottom = autoscroll
    ? body.scrollHeight - body.scrollTop - body.clientHeight < 90
    : false;

  body.append(logNode(line));
  while (body.children.length > 1400) body.firstElementChild.remove();
  consoleCount = body.children.length;
  if (autoscroll && nearBottom) body.scrollTop = body.scrollHeight;
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
  if (state.keys.length) markSubstepDone("run.key").catch(() => {});

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
async function authorizeKey(id, options = {}) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return false;
  if (key.kind === "service_account") {
    await remember(`${key.label}: service accounts do not need consent`, "warn");
    return false;
  }
  if (!key.client_id || !key.client_secret) {
    await remember(`${key.label}: client_id or client_secret is missing`, "error");
    return false;
  }

  try {
    const client = parseClient(
      JSON.stringify({
        installed: { client_id: key.client_id, client_secret: key.client_secret },
      })
    );
    await remember(`${key.label}: approve the consent tab as the tenant admin`, "step");
    const verifier = makeVerifier();
    const code = await waitForCode(client.client_id, verifier, {
      storeId: options.storeId || null,
      autoConsent: Boolean(options.autoConsent),
      email: options.email || "",
      password: options.password || "",
      otp: options.otp || "",
      timeout: options.timeout || 600_000,
    });
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
    markSubstepDone("run.key").catch(() => {});
    return true;
  } catch (error) {
    await remember(`${key.label}: ${error.message || error}`, "error");
    return false;
  }
}

function adminEmailFor(key) {
  return (
    key.admin ||
    key.admin_email ||
    (key.domain ? `admin@${key.domain}` : "") ||
    state.setup?.auto?.adminEmail ||
    state.totp?.email ||
    ""
  );
}

async function showAdmin(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;
  const email = adminEmailFor(key);
  if (!email) {
    await remember(`${key.label}: no admin email known — use Recover admin`, "warn");
    return;
  }
  const password = key.admin_password || "";
  if (!password) {
    prompt(`Admin email for ${key.label}`, email);
    await remember(`${key.label}: no saved admin password — use Recover admin`, "warn");
    return;
  }
  prompt(`Admin email for ${key.label}`, email);
  prompt(`Saved admin password for ${email}`, password);
  const codes = Array.isArray(key.admin_backup_codes) ? key.admin_backup_codes : [];
  if (codes.length) {
    prompt(`Saved backup codes for ${email}`, codes.join("\n"));
  }
}

async function recoverAdmin(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;
  const email = prompt("Admin email to recover", adminEmailFor(key));
  if (!email) return;
  const target = email.trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(target)) {
    await remember(`admin recovery: not an email address: ${target}`, "error");
    return;
  }

  const ok = confirm(
    `Reset the password for ${target} and mint new backup codes with "${key.label}"?\n\n` +
      "The old password stops working immediately. The key needs " +
      "admin.directory.user and admin.directory.user.security."
  );
  if (!ok) return;

  const password = makeAdminPassword();
  try {
    await admin.setPassword(key, target, password);
    const codes = await admin.mintCodes(key, target);
    await mutate((draft) => {
      const row = draft.keys.find((entry) => entry.id === id);
      if (row) {
        row.admin = target;
        row.admin_email = target;
        row.admin_password = password;
        row.admin_recovery_at = new Date().toISOString();
        row.admin_backup_codes = codes;
      }
      if (draft.setup?.auto) {
        draft.setup.auto.adminEmail = target;
        draft.setup.auto.adminPassword = password;
      }
    });
    await refresh();
    await remember(
      `${target}: admin password reset and ${codes.length} backup code(s) minted`,
      "ok"
    );
    prompt(`New password for ${target} — copy it now`, password);
    if (codes.length) {
      prompt(`Backup codes for ${target} — copy them now`, codes.join("\n"));
    }
  } catch (error) {
    const text = String(error.message || error);
    const hint = /403|insufficient|scope|permission|forbidden/i.test(text)
      ? " — re-consent the key so it has admin.directory.user and admin.directory.user.security"
      : "";
    await remember(`${key.label}: admin recovery failed — ${text}${hint}`, "error");
  }
}

function keyJsonText(key) {
  const base = {
    domain: key.domain || "",
    admin: key.admin || key.admin_email || adminEmailFor(key) || "",
    token_uri: key.token_uri || "https://oauth2.googleapis.com/token",
    scopes: key.scopes || [],
  };
  if (key.kind === "service_account") {
    return JSON.stringify(
      {
        type: "service_account",
        project_id: key.project_id || "",
        private_key: key.private_key || "",
        client_email: key.client_email || "",
        client_id: key.client_id || "",
        ...base,
      },
      null,
      2
    );
  }
  return JSON.stringify(
    {
      client_id: key.client_id || "",
      client_secret: key.client_secret || "",
      refresh_token: key.refresh_token || "",
      ...base,
    },
    null,
    2
  );
}

async function downloadKeyJson(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;
  const name = String(key.domain || key.label || "key").replace(
    /[^a-z0-9._-]+/gi,
    "_"
  );
  await saveTextFile(`${name}-key.json`, keyJsonText(key), true);
  await remember(`${key.label}: key JSON downloaded`, "ok");
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

// ------------------------------------------------------------ auto-delete queue

// A successful signup leaves a Google user behind that holds a Cloud Identity
// seat. The lane should not stop and wait for the deletion, so successes enter
// this queue and one worker walks it with the same jittered pacing as the manual
// Delete users button. A failure leaves the identity as an account so the
// manual path can retry it.
const googleDeleteQueue = [];
let googleDeleteRunning = false;

async function queueGoogleDelete(key, identity) {
  if (!key || !identity?.email) return;
  googleDeleteQueue.push({ key, identity });
  await remember(
    `${identity.email}: queued for Google deletion (${googleDeleteQueue.length} pending)`,
    "info"
  );
  drainGoogleDeleteQueue().catch((error) =>
    remember(`google auto-delete queue: ${error.message || error}`, "warn")
  );
}

async function drainGoogleDeleteQueue() {
  if (googleDeleteRunning) return;
  googleDeleteRunning = true;
  try {
    let first = true;
    while (googleDeleteQueue.length) {
      const settings = state.settings || {};
      const pace = Math.max(5, Math.min(600, Number(settings.deletePaceSeconds) || 45));
      if (!first) {
        const wait = pace * 1000 + Math.floor(Math.random() * pace * 200);
        await remember(
          `google auto-delete: holding ${Math.round(wait / 1000)}s before the next one`,
          "info"
        );
        await sleep(wait);
      }
      first = false;

      const { key, identity } = googleDeleteQueue.shift();
      const email = String(identity.email || "");
      try {
        const result = await admin.deleteUser(key, email);
        await mutate((draft) => {
          const row = draft.identities.find((entry) => entry.id === identity.id);
          if (row) row.status = "deleted";
          const target = draft.keys.find((entry) => entry.id === key.id);
          if (target && result.deleted && typeof target.seats === "number") {
            target.seats = Math.max(0, target.seats - 1);
            target.message = `${target.seats}/50 seats used`;
          }
          const account = draft.accounts.find(
            (entry) => entry.identity_id === identity.id
          );
          if (account) account.google_deleted_at = new Date().toISOString();
        });
        // Keep the in-memory copy in step; otherwise the meter stays stale
        // until the next full refresh.
        const local = state.identities.find((entry) => entry.id === identity.id);
        if (local) local.status = "deleted";
        const localKey = state.keys.find((entry) => entry.id === key.id);
        if (localKey && result.deleted && typeof localKey.seats === "number") {
          localKey.seats = Math.max(0, localKey.seats - 1);
          localKey.message = `${localKey.seats}/50 seats used`;
        }
        await remember(
          `${email}: Google account ${result.missing ? "already gone" : "deleted — seat freed"}`,
          result.missing ? "warn" : "ok"
        );
      } catch (error) {
        await remember(
          `${email}: Google auto-delete failed — ${String(error.message || error).slice(0, 160)}`,
          "warn"
        );
      }
      renderKeys();
      renderQueue();
      renderStats();
    }
  } finally {
    googleDeleteRunning = false;
  }
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
      const identityEmail = String(identity.email || "").toLowerCase();
      activeIdentities.set(identityEmail, identity);
      // A confirmation belongs to one enrollment. A retry of the same address
      // must not inherit the previous run's "already enrolled" answer.
      liveEnrolled.delete(identityEmail);

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
            enrolledFor: (email) => liveEnrolled.has(String(email || "").toLowerCase()),
            username: requestedUsername,
            birthday: profile.birthday,
            gender: profile.gender,
            acceptTerms: settings.acceptTerms !== false,
            laneSurface: settings.laneSurface === "window" ? "window" : "tab",
            primeGoogle: settings.primeGoogle !== false,
            probeIp: Boolean(proxyPool.length && storeId),
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
          // Confirm the identity actually finished 2SV enrolment. The key can
          // report it but cannot toggle it; a false here means the authenticator
          // screen did not complete, even though backup codes still work.
          try {
            const status = await admin.twoStepStatus(key, identity.email);
            await mutate((draft) => {
              const row = draft.identities.find((entry) => entry.id === identity.id);
              if (row) row.twofa_enrolled = Boolean(status.enrolled);
            });
            if (!status.enrolled) {
              await remember(
                `${identity.email}: 2SV not enrolled (enforced=${status.enforced}) — the authenticator screen may not have completed; backup codes still work`,
                "warn"
              );
            }
          } catch (error) {
            if (error.status !== 404) {
              await remember(
                `${identity.email}: 2SV status check failed — ${String(
                  error.message || error
                ).slice(0, 120)}`,
                "info"
              );
            }
          }
          // The Roblox account exists and the cookie is saved. The Google user
          // has done its job; queue it for deletion so the seat comes back.
          if (settings.deleteGoogleAfterSuccess !== false) {
            await queueGoogleDelete(key, identity);
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

    const result = await retryFinalise(tab.id, 120, () => false, (text) =>
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

async function readLoginAudit() {
  const key =
    state.keys.find((entry) => entry.status === "ok") ||
    state.keys.find((entry) => !entry.needs_consent);
  if (!key) {
    await remember("load a key before reading the login audit", "warn");
    return;
  }

  const email = (
    $("audit-email").value.trim() ||
    $("totp-email").value.trim() ||
    state.totp?.email ||
    key.admin ||
    ""
  ).toLowerCase();
  if (!email) {
    await remember("set the identity email in the login audit card", "warn");
    return;
  }

  try {
    const events = await admin.loginEvents(key, email, { maxResults: 50 });
    if (!events.length) {
      setText("audit-status", `${email}: no login events`);
      await remember(`${email}: no login events`, "warn");
      return;
    }
    const lines = events.slice(0, 5).map((event) => {
      const detail = event.failure || event.challengeStatus || event.challenge || event.loginType;
      return `${event.time || "?"} ${event.event || "login"}${detail ? ` (${detail})` : ""}${
        event.ip ? ` ${event.ip}` : ""
      }`;
    });
    const summary = `${email}: ${lines.join(" | ")}`;
    setText("audit-status", summary);
    const failures = events.filter((event) => event.failure);
    await remember(summary, failures.length ? "warn" : "ok");
  } catch (error) {
    const hint =
      error.status === 403
        ? " — re-authorize the key to grant admin.reports.audit.readonly"
        : "";
    setText("audit-status", `${error.message || error}${hint}`);
    await remember(`login audit: ${error.message || error}${hint}`, "error");
  }
}

// -------------------------------------------------------------------- duckdns

async function saveDuck(fields) {
  await mutate((draft) => {
    draft.duckdns = { ...(draft.duckdns || {}), ...fields };
    // Token and subdomain are the same values in Manual step 1 and in the
    // Automatic panel. Keep the automatic copy in step with a manual edit.
    if (fields.token !== undefined || fields.subdomain !== undefined) {
      draft.setup.auto = {
        ...(draft.setup.auto || {}),
        ...(fields.token !== undefined ? { token: fields.token } : {}),
        ...(fields.subdomain !== undefined
          ? { subdomain: fields.subdomain }
          : {}),
      };
    }
  });
  state = await load();
  renderDuckdns();
  renderSetup();
  const credentialsSaved = Boolean(
    state.duckdns?.token && state.duckdns?.subdomain
  );
  const credentialsEntry = substepEntry("dns.credentials");
  if (credentialsSaved && !credentialsEntry?.done) {
    markSubstepDone("dns.credentials").catch(() => {});
  } else if (!credentialsSaved && credentialsEntry?.done) {
    setSubstep("dns.credentials", {
      done: false,
      status: "manual",
      error: "",
    }).catch(() => {});
  }
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
      if (autoSetup.current === "tenant.verify") {
        await signalAutoDomainReady();
      }
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
    if (hit && autoSetup.current === "tenant.verify") {
      await signalAutoDomainReady();
    }
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

function substepEntry(id) {
  return state.setup?.substeps?.[id] || null;
}

function isSubstepDone(id) {
  const entry = substepEntry(id);
  if (entry) return Boolean(entry.done);
  const def = SUBSTEP_BY_ID[id];
  // Older state only had the step-level ticks. A completed step therefore
  // starts with all of its sub-steps checked.
  return Boolean(def && state.setup?.done?.[def.step]);
}

function substepStatus(id) {
  const entry = substepEntry(id);
  if (entry?.status === "running") return "running";
  if (entry?.status === "failed") return "failed";
  if (isSubstepDone(id)) return "done";
  if (
    autoSetup.running &&
    (autoSetup.current === id || autoSetup.queue.includes(id))
  ) {
    return "queued";
  }
  return SUBSTEP_BY_ID[id]?.auto ? "auto" : "manual";
}

function missingSubstepDeps(id) {
  return (SUBSTEP_BY_ID[id]?.requires || []).filter((dep) => !isSubstepDone(dep));
}

function autoProjectId() {
  return autoSetup.projectId || String(state.setup?.auto?.projectId || "");
}

function policyBlocked() {
  return (
    autoSetup.policySafe === false || state.setup?.auto?.policySafe === false
  );
}

function substepBlockedReason(id) {
  const missing = missingSubstepDeps(id);
  if (missing.length) {
    return `needs: ${missing.map((dep) => SUBSTEP_BY_ID[dep]?.label || dep).join(", ")}`;
  }
  if (
    ["key.admin-sdk", "key.consent", "key.scopes", "key.client"].includes(id) &&
    !autoProjectId()
  ) {
    return "run Create the Cloud project with Auto first";
  }
  if (id === "twostep.policy" && policyBlocked()) {
    return "admin 2SV was already on without a saved authenticator secret — set the policy manually only if another allowed factor exists";
  }
  return "";
}

async function migrateSubsteps() {
  const existing = state.setup?.substeps || {};
  const done = state.setup?.done || {};
  const additions = {};
  for (const def of SUBSTEPS) {
    if (existing[def.id]) {
      if (existing[def.id].status === "running") {
        additions[def.id] = {
          ...existing[def.id],
          status: "failed",
          error: "the runner reloaded before this sub-step finished",
        };
      }
      continue;
    }
    if (done[def.step]) {
      additions[def.id] = { done: true, status: "done", error: "" };
      continue;
    }
    if (
      def.id === "dns.credentials" &&
      state.duckdns?.token &&
      state.duckdns?.subdomain
    ) {
      additions[def.id] = { done: true, status: "done", error: "" };
    }
  }
  if (!Object.keys(additions).length) return;
  await mutate((draft) => {
    const merged = { ...(draft.setup.substeps || {}), ...additions };
    draft.setup.substeps = merged;
    draft.setup.done = { ...(draft.setup.done || {}) };
    for (const [step, ids] of Object.entries(STEP_SUBSTEPS)) {
      draft.setup.done[step] = ids.every((id) => Boolean(merged[id]?.done));
    }
  });
  state = await load();
}

async function setSubstep(id, patch) {
  await mutate((draft) => {
    draft.setup.substeps = { ...(draft.setup.substeps || {}) };
    draft.setup.substeps[id] = {
      ...(draft.setup.substeps[id] || {}),
      ...patch,
    };
  });
  state = await load();
  const def = SUBSTEP_BY_ID[id];
  if (def?.step) await syncStepDone(def.step);
  else renderSetup();
}

async function markSubstepDone(id) {
  await setSubstep(id, { done: true, status: "done", error: "" });
}

function substepDoneInDraft(draft, id) {
  const entry = draft.setup?.substeps?.[id];
  if (entry) return Boolean(entry.done);
  const def = SUBSTEP_BY_ID[id];
  return Boolean(def && draft.setup?.done?.[def.step]);
}

// Manual ticks are sequential: checking a sub-step checks everything before it,
// and unchecking one clears everything after it. Automatic task completion uses
// markSubstepDone instead, so it only ticks the task that actually finished.
async function setSubstepCascade(id, checked) {
  const index = SUBSTEPS.findIndex((entry) => entry.id === id);
  if (index < 0) return;
  const affected = checked ? SUBSTEPS.slice(0, index + 1) : SUBSTEPS.slice(index);
  await mutate((draft) => {
    draft.setup.substeps = { ...(draft.setup.substeps || {}) };
    for (const def of affected) {
      draft.setup.substeps[def.id] = {
        ...(draft.setup.substeps[def.id] || {}),
        done: checked,
        status: checked ? "done" : def.auto ? "auto" : "manual",
        error: "",
      };
    }
    draft.setup.done = { ...(draft.setup.done || {}) };
    for (const [step, ids] of Object.entries(STEP_SUBSTEPS)) {
      draft.setup.done[step] = ids.every((subId) =>
        substepDoneInDraft(draft, subId)
      );
    }
  });
  state = await load();
  renderSetup();
}

async function syncStepDone(step) {
  const ids = STEP_SUBSTEPS[step] || [];
  if (!ids.length) {
    renderSetup();
    return;
  }
  const done = ids.every((id) => isSubstepDone(id));
  if (Boolean(state.setup?.done?.[step]) !== done) {
    await mutate((draft) => {
      draft.setup.done = { ...(draft.setup.done || {}), [step]: done };
    });
    state = await load();
  }
  renderSetup();
}

function decorateSubsteps() {
  for (const li of document.querySelectorAll(".step-actions > li[data-substep]")) {
    if (li.querySelector(".substep-head")) continue;
    const id = li.dataset.substep;
    const def = SUBSTEP_BY_ID[id];
    if (!def) continue;

    const head = document.createElement("div");
    head.className = "substep-head";

    const label = document.createElement("label");
    label.className = "substep-tick";
    label.title = "mark this sub-step done";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.dataset.substepDone = id;
    box.setAttribute("aria-label", `${def.label}: done`);
    label.append(box);

    const chip = document.createElement("span");
    chip.className = "substep-state";
    chip.dataset.substepState = id;
    chip.textContent = def.auto ? "auto" : "manual";

    head.append(label, chip);

    if (def.auto) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn tiny ghost auto-substep";
      button.dataset.automateSubstep = id;
      button.textContent = "Auto";
      head.append(button);
    }

    li.prepend(head);
  }
}

function renderSubsteps() {
  for (const li of document.querySelectorAll(".step-actions > li[data-substep]")) {
    const id = li.dataset.substep;
    const def = SUBSTEP_BY_ID[id];
    if (!def) continue;

    const done = isSubstepDone(id);
    li.dataset.done = String(done);

    const box = li.querySelector("[data-substep-done]");
    if (box) box.checked = done;

    const chip = li.querySelector("[data-substep-state]");
    const status = substepStatus(id);
    if (chip) {
      chip.textContent = status;
      chip.dataset.state = status;
    }

    const button = li.querySelector("[data-automate-substep]");
    if (button) {
      const reason = substepBlockedReason(id);
      button.disabled = autoSetup.running || Boolean(reason);
      button.textContent = done ? "Redo" : "Auto";
      button.title = reason
        ? reason
        : done
        ? "run this sub-step again"
        : `run ${def.label}`;
    }

    const entry = substepEntry(id);
    let error = li.querySelector(".substep-error");
    if (!error) {
      error = document.createElement("p");
      error.className = "note substep-error";
      li.querySelector(".substep-head")?.after(error);
    }
    error.textContent = entry?.error || "";
    error.hidden = !entry?.error;
  }
}

function setupState() {
  const setup = state.setup || {};
  return {
    done: setup.done || {},
    open: setup.open || {},
    hideDone: Boolean(setup.hideDone),
    substeps: setup.substeps || {},
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

  renderSubsteps();

  const substepTotal = SUBSTEPS.length;
  const substepDone = SUBSTEPS.filter((entry) => isSubstepDone(entry.id)).length;
  setText(
    "setup-count",
    `${substepDone}/${substepTotal} sub-steps · ${complete}/${SETUP_TICKABLE.length} steps`
  );
  const bar = $("setup-bar");
  if (bar) {
    bar.style.width = `${
      substepTotal ? ((substepDone / substepTotal) * 100).toFixed(1) : 0
    }%`;
  }

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
  renderAutoSetup();
}

// ------------------------------------------------------------- automatic finish

const AUTO_ADMIN = "https://admin.google.com/";
const AUTO_DOMAINS = "https://admin.google.com/ac/domains/manage";
const AUTO_BILLING = "https://admin.google.com/ac/billing/catalog";
const AUTO_ADMIN_2SV =
  "https://myaccount.google.com/signinoptions/two-step-verification";
const AUTO_2SV = "https://admin.google.com/ac/security/2sv";
const AUTO_PROJECT = "https://console.cloud.google.com/projectcreate";
const AUTO_ADMIN_SDK =
  "https://console.cloud.google.com/apis/library/admin.googleapis.com";
const AUTO_CONSENT =
  "https://console.cloud.google.com/apis/credentials/consent";
const AUTO_CREDENTIALS =
  "https://console.cloud.google.com/apis/credentials";

// The setup checklist. Every sub-step is a checkbox in the UI; tasks with
// `auto: true` also get an Auto button. `requires` gates both the button and
// the batch order. `batch: false` keeps a task out of "Automate remaining"
// while still allowing its own Auto button.
const SUBSTEPS = [
  { id: "dns.account", step: "dns", label: "Create the DuckDNS account and subdomain" },
  { id: "dns.token", step: "dns", label: "Copy the DuckDNS account token" },
  { id: "dns.credentials", step: "dns", label: "Save the DuckDNS token and subdomain" },

  { id: "tenant.signup", step: "tenant", label: "Create the Cloud Identity tenant" },
  { id: "tenant.about", step: "tenant", label: "Fill in About you" },
  { id: "tenant.business", step: "tenant", label: "Fill in About your business" },
  { id: "tenant.domain-name", step: "tenant", label: "Enter the DuckDNS domain" },
  { id: "tenant.admin", step: "tenant", label: "Create the admin account / phone gate" },
  {
    id: "tenant.verify",
    step: "tenant",
    label: "Write the TXT record and verify the domain",
    auto: true,
    batch: true,
    requires: ["dns.credentials"],
    timeout: 180_000,
  },
  { id: "tenant.agreement", step: "tenant", label: "Accept the Cloud Identity Agreement" },
  {
    id: "tenant.cloud-identity",
    step: "tenant",
    label: "Activate Cloud Identity Free",
    auto: true,
    batch: true,
    requires: ["tenant.verify"],
    timeout: 75_000,
  },

  { id: "twostep.open", step: "twostep", label: "Open the admin 2SV settings" },
  {
    id: "twostep.enroll",
    step: "twostep",
    label: "Enrol the admin authenticator app",
    auto: true,
    batch: true,
    requires: ["tenant.verify"],
    timeout: 210_000,
  },
  {
    id: "twostep.policy",
    step: "twostep",
    label: "Set the tenant 2SV policy",
    auto: true,
    batch: true,
    requires: ["twostep.enroll"],
    timeout: 95_000,
  },

  {
    id: "key.project",
    step: "key",
    label: "Create the Cloud project",
    auto: true,
    batch: true,
    requires: ["tenant.verify"],
    timeout: 130_000,
  },
  {
    id: "key.admin-sdk",
    step: "key",
    label: "Enable the Admin SDK",
    auto: true,
    batch: true,
    requires: ["key.project"],
    timeout: 100_000,
  },
  {
    id: "key.consent",
    step: "key",
    label: "Set up the OAuth consent screen",
    auto: true,
    batch: true,
    requires: ["key.project"],
    timeout: 130_000,
  },
  {
    id: "key.scopes",
    step: "key",
    label: "Add the five OAuth scopes",
    auto: true,
    batch: true,
    requires: ["key.consent"],
    timeout: 180_000,
  },
  {
    id: "key.client",
    step: "key",
    label: "Create the desktop OAuth client",
    auto: true,
    batch: true,
    requires: ["key.consent", "key.scopes"],
    timeout: 160_000,
  },
  {
    id: "key.authorize",
    step: "key",
    label: "Authorize the key",
    auto: true,
    batch: true,
    requires: ["key.client", "key.admin-sdk"],
    timeout: 240_000,
  },

  { id: "run.key", step: "run", label: "Add the key in Keys" },
  {
    id: "run.check",
    step: "run",
    label: "Check the key",
    auto: true,
    batch: false,
    requires: ["run.key"],
    timeout: 45_000,
    local: true,
  },
  {
    id: "run.first",
    step: "run",
    label: "Run one identity",
    auto: true,
    batch: false,
    requires: ["run.key"],
    local: true,
  },
  {
    id: "run.export",
    step: "run",
    label: "Export accounts",
    auto: true,
    batch: false,
    requires: ["run.first"],
    local: true,
  },
];

const SUBSTEP_BY_ID = Object.fromEntries(SUBSTEPS.map((entry) => [entry.id, entry]));
const STEP_SUBSTEPS = SUBSTEPS.reduce((map, entry) => {
  (map[entry.step] ||= []).push(entry.id);
  return map;
}, {});

// The automatic branch never creates the tenant or the DuckDNS name. It opens
// one worker tab, waits for the user to sign in as the tenant admin, then runs
// the checked sub-steps: domain, Cloud Identity, admin 2SV/policy and the key.
// The user's own Admin console tab is never touched.
const autoSetup = {
  running: false,
  stop: false,
  batch: false,
  queue: [],
  current: "",
  tabId: null,
  storeId: null,
  profile: null,
  phase: "idle",
  status: "idle",
  lastUrl: "",
  adminEmail: "",
  adminAt: 0,
  domainsOpened: false,
  domainReadyPending: false,
  domainWarnAt: 0,
  projectId: "",
  pendingKeyId: "",
  policySafe: true,
  taskStartedAt: 0,
  taskAttempted: false,
  signedIn: false,
  handoffPaused: false,
  blocked: null,
  endedOnKey: false,
};

function autoValues() {
  const saved = state.setup?.auto || {};
  const duck = state.duckdns || {};
  const subdomain = String(
    saved.subdomain || duck.subdomain || suggestedSubdomain() || ""
  )
    .trim()
    .toLowerCase()
    .replace(/\.duckdns\.org$/, "")
    .replace(/[^a-z0-9-]/g, "");
  return {
    subdomain,
    token: String(saved.token || duck.token || "").trim(),
    businessName: subdomain
      ? subdomain.replace(/-+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase())
      : saved.businessName || "RAC tenant",
    projectName: subdomain ? `${subdomain}-rac` : saved.projectName || "",
    recoveryEmail: saved.recoveryEmail || "",
    adminPassword: saved.adminPassword || "",
  };
}

function autoProfile() {
  if (!autoSetup.profile) {
    const values = autoValues();
    autoSetup.profile = {
      ...values,
      domain: values.subdomain ? fullDomain(values.subdomain) : "",
      adminEmail: autoSetup.adminEmail || "",
    };
  }
  return autoSetup.profile;
}

function autoStatus(text) {
  autoSetup.status = text;
  setText("auto-status", text);
}

function autoLog(text, level = "info") {
  remember(`[auto] ${text}`, level).catch(() => {});
}

async function persistAutoState(patch = {}) {
  await mutate((draft) => {
    draft.setup.auto = { ...(draft.setup.auto || {}), ...patch };
    if (patch.token !== undefined) draft.duckdns.token = patch.token || "";
    if (patch.subdomain !== undefined) {
      draft.duckdns.subdomain = patch.subdomain || "";
    }
  });
  state = await load();
}

function autoAdminEmail() {
  return String(autoSetup.adminEmail || autoProfile().adminEmail || "").toLowerCase();
}

function autoAdminOtp() {
  const profile = autoProfile();
  const email = autoAdminEmail();
  const totp = state.totp || {};
  const owner = String(totp.email || "").toLowerCase();
  if (!totp.secret) return "";
  if (!owner) return totp.secret;
  if (email && owner === email) return totp.secret;
  if (profile.domain && owner.endsWith(`@${profile.domain}`)) {
    return totp.secret;
  }
  return "";
}

function ensureAutoAdminLabel() {
  if (autoAdminEmail()) return;
  const profile = autoProfile();
  if (!profile.domain) return;
  autoSetup.adminEmail = `admin@${profile.domain}`;
  profile.adminEmail = autoSetup.adminEmail;
  autoLog(
    `admin address was not visible; using ${autoSetup.adminEmail} as a label`,
    "warn"
  );
  persistAutoState({ adminEmail: autoSetup.adminEmail }).catch(() => {});
  renderAutoSetup();
}

function autoPhaseLabel(phase) {
  return (
    {
      idle: "idle",
      login: "sign-in",
      running: "running",
      paused: "paused",
      done: "done",
      failed: "failed",
      stopped: "stopped",
    }[phase] || phase
  );
}

function renderAutoSetup() {
  const run = $("auto-run");
  if (run) {
    run.hidden =
      !autoSetup.running && !autoSetup.tabId && autoSetup.status === "idle";
  }
  setText("auto-status", autoSetup.status || "idle");
  setText("auto-phase", autoPhaseLabel(autoSetup.phase));

  const done = state.setup?.done || {};
  const tasks = {
    admin:
      autoSetup.signedIn ||
      Boolean(
        autoSetup.tabId && !["idle", "login", "stopped"].includes(autoSetup.phase)
      ),
    tenant: Boolean(done.tenant),
    twostep: Boolean(done.twostep),
    key: Boolean(done.key),
  };
  for (const row of document.querySelectorAll("[data-auto-task]")) {
    const id = row.dataset.autoTask;
    row.dataset.ok = String(Boolean(tasks[id]));
    const label = row.querySelector("em");
    if (label) {
      label.textContent = tasks[id] ? "done" : autoSetup.running ? "waiting" : "idle";
    }
  }

  for (const id of ["auto-start", "auto-start-top"]) {
    const start = $(id);
    if (!start) continue;
    start.disabled = autoSetup.running;
    start.textContent = autoSetup.running ? "Running…" : "Automate remaining";
  }
  const stop = $("auto-stop");
  if (stop) stop.disabled = !autoSetup.running;
  const keys = $("auto-keys");
  if (keys) keys.disabled = !autoSetup.tabId;
  renderSubsteps();
}

async function ensureSetupContainer() {
  if (!isFirefox) return null;
  try {
    const name = "RAC setup";
    const existing = await api.contextualIdentities.query({ name });
    if (existing.length) return existing[0].cookieStoreId;
    const created = await api.contextualIdentities.create({
      name,
      color: "blue",
      icon: "circle",
    });
    return created.cookieStoreId;
  } catch (_) {
    return null;
  }
}

async function ensureAutoWorker() {
  if (autoSetup.tabId) {
    const existing = await api.tabs.get(autoSetup.tabId).catch(() => null);
    if (existing) return autoSetup.tabId;
  }
  if (!autoSetup.storeId) autoSetup.storeId = await ensureSetupContainer();
  const options = { url: AUTO_ADMIN, active: true };
  if (autoSetup.storeId) options.cookieStoreId = autoSetup.storeId;
  const tab = await api.tabs.create(options);
  if (!tab) throw new Error("the setup tab did not open");
  return tab.id;
}

function buildBatchQueue() {
  const willDone = new Set(
    SUBSTEPS.filter((entry) => isSubstepDone(entry.id)).map((entry) => entry.id)
  );
  const queue = [];
  const skipped = [];
  let blocked = null;
  const projectTasks = [
    "key.admin-sdk",
    "key.consent",
    "key.scopes",
    "key.client",
  ];
  for (const def of SUBSTEPS) {
    if (!def.auto || !def.batch || willDone.has(def.id)) continue;
    if (
      projectTasks.includes(def.id) &&
      isSubstepDone("key.project") &&
      !autoProjectId()
    ) {
      blocked = { id: "key.project", missing: [] };
      break;
    }
    if (def.id === "twostep.policy" && policyBlocked()) {
      // The authenticator is not available for this admin, so the policy is
      // unsafe to change automatically. Skip it and keep the key tasks moving.
      skipped.push(def.id);
      continue;
    }
    const missing = (def.requires || []).filter((dep) => !willDone.has(dep));
    if (missing.length) {
      blocked = { id: def.id, missing };
      break;
    }
    queue.push(def.id);
    willDone.add(def.id);
  }
  return { queue, blocked, skipped };
}

function missingSubstepLabels(ids) {
  return ids.map((id) => SUBSTEP_BY_ID[id]?.label || id);
}

async function startAutoSetup() {
  if (autoSetup.running) {
    await remember("automatic finish is already running", "warn");
    return;
  }

  const { queue, blocked, skipped } = buildBatchQueue();
  if (!queue.length) {
    if (blocked) {
      const blockedDef = SUBSTEP_BY_ID[blocked.id];
      const labels = missingSubstepLabels(blocked.missing).join(", ");
      await remember(
        labels
          ? `auto-run is waiting on a manual sub-step: ${labels}`
          : `manual check needed: ${blockedDef?.label || blocked.id}`,
        "warn"
      );
      openStep(
        SUBSTEP_BY_ID[blocked.missing[0]]?.step || blockedDef?.step || "tenant",
        true
      );
    } else if (skipped.length) {
      await remember(
        `manual check needed: ${missingSubstepLabels(skipped).join(", ")}`,
        "warn"
      );
      openStep(SUBSTEP_BY_ID[skipped[0]]?.step || "twostep", true);
    } else {
      await remember("every automatable sub-step is already checked", "info");
    }
    return;
  }

  const values = autoValues();
  if (!values.subdomain) {
    await remember("save the DuckDNS subdomain in step 1 first", "error");
    await setSetup((draft) => {
      draft.open.dns = true;
    });
    openStep("dns", true);
    return;
  }
  if (queue.includes("tenant.verify") && !values.token) {
    await remember(
      "automatic domain verification needs the DuckDNS token from step 1",
      "error"
    );
    openStep("dns", true);
    return;
  }

  await beginAutoRun(queue, true, blocked);
}

async function runSubstep(id) {
  if (autoSetup.running) {
    await remember("automatic finish is already running", "warn");
    return;
  }
  const def = SUBSTEP_BY_ID[id];
  if (!def?.auto) return;

  const blockedReason = substepBlockedReason(id);
  if (blockedReason) {
    await remember(blockedReason, "warn");
    const missing = missingSubstepDeps(id);
    if (missing.length) openStep(SUBSTEP_BY_ID[missing[0]]?.step || def.step, true);
    return;
  }

  if (def.local) {
    await runLocalSubstep(def);
    return;
  }

  const values = autoValues();
  if (!values.subdomain) {
    await remember("save the DuckDNS subdomain in step 1 first", "error");
    openStep("dns", true);
    return;
  }
  if (id === "tenant.verify" && !values.token) {
    await remember("domain verification needs the DuckDNS token from step 1", "error");
    openStep("dns", true);
    return;
  }
  await beginAutoRun([id], false, null);
}

async function beginAutoRun(queue, batch, blocked) {
  const values = autoValues();
  const subdomain = values.subdomain;
  const domain = subdomain ? fullDomain(subdomain) : "";
  const previous = state.setup?.auto || {};
  const previousAdmin = String(previous.adminEmail || "").toLowerCase();
  const seededAdmin = domain && previousAdmin.endsWith(`@${domain}`) ? previousAdmin : "";

  autoSetup.running = true;
  autoSetup.stop = false;
  autoSetup.batch = Boolean(batch);
  autoSetup.queue = [...queue];
  autoSetup.current = "";
  autoSetup.tabId = autoSetup.tabId || null;
  autoSetup.phase = "login";
  autoSetup.status = "opening the Admin console";
  autoSetup.lastUrl = "";
  autoSetup.adminEmail = seededAdmin;
  autoSetup.adminAt = 0;
  autoSetup.domainsOpened = false;
  autoSetup.domainReadyPending = false;
  autoSetup.domainWarnAt = 0;
  const previousProject = String(previous.projectId || "");
  autoSetup.projectId =
    previousProject && String(previous.subdomain || "") === subdomain
      ? previousProject
      : "";
  autoSetup.pendingKeyId = "";
  autoSetup.policySafe =
    String(previous.subdomain || "") === subdomain
      ? previous.policySafe !== false
      : true;
  autoSetup.taskStartedAt = 0;
  autoSetup.taskAttempted = false;
  autoSetup.signedIn = false;
  autoSetup.handoffPaused = false;
  autoSetup.blocked = blocked || null;
  autoSetup.endedOnKey = false;
  autoSetup.profile = {
    ...values,
    token: values.token,
    subdomain,
    domain,
    adminEmail: seededAdmin,
    adminPassword: "",
  };
  autoSetup.storeId = autoSetup.storeId || (await ensureSetupContainer());

  await persistAutoState({
    token: values.token,
    subdomain,
    domain,
    adminEmail: seededAdmin,
    projectName: autoSetup.profile.projectName,
    projectId: autoSetup.projectId,
    policySafe: autoSetup.policySafe,
    phase: queue.length === 1 ? queue[0] : "batch",
  });
  await refresh();
  switchTab("setup");
  autoLog(
    batch
      ? `automatic run started with ${queue.length} sub-step(s)`
      : `automatic sub-step started: ${SUBSTEP_BY_ID[queue[0]]?.label || queue[0]}`
  );
  renderAutoSetup();

  try {
    autoSetup.tabId = await ensureAutoWorker();
    await focusAutoTab();
    renderAutoSetup();
    autoSetupLoop().catch((error) =>
      autoLog(`setup loop stopped: ${error.message || error}`, "error")
    );
  } catch (error) {
    autoSetup.running = false;
    autoSetup.phase = "failed";
    autoSetup.status = `could not open the Admin console tab: ${error.message || error}`;
    autoLog(autoSetup.status, "error");
    renderAutoSetup();
  }
}

function stopAutoSetup(reason = "stopped") {
  const current = autoSetup.current;
  if (running) {
    stopRequested = true;
    remember("stopping the active run after the current identity", "warn").catch(() => {});
  }
  autoSetup.running = false;
  autoSetup.stop = true;
  autoSetup.current = "";
  autoSetup.queue = [];
  autoSetup.phase = "stopped";
  autoSetup.handoffPaused = true;
  if (current) {
    setSubstep(current, {
      status: isSubstepDone(current) ? "done" : SUBSTEP_BY_ID[current]?.auto ? "auto" : "manual",
      error: "",
    }).catch(() => {});
  }
  if (autoSetup.tabId) {
    api.tabs
      .sendMessage(autoSetup.tabId, { type: "setup-stop" })
      .catch(() => {});
  }
  autoStatus(reason);
  autoLog(`automatic finish ${reason}; the worker tab is left open`, "warn");
  renderAutoSetup();
}

async function focusAutoTab() {
  if (!autoSetup.tabId) return;
  const current = await api.tabs.get(autoSetup.tabId).catch(() => null);
  if (!current) return;
  try {
    await api.tabs.update(current.id, { active: true });
    if (current.windowId) await api.windows.update(current.windowId, { focused: true });
  } catch (_) {
    // tab gone
  }
}

async function focusRunnerTab() {
  try {
    const current = await api.tabs.getCurrent?.();
    if (!current) return;
    await api.tabs.update(current.id, { active: true });
    if (current.windowId) await api.windows.update(current.windowId, { focused: true });
  } catch (_) {
    // runner tab gone
  }
}

function autoTabKind(url) {
  if (/accounts\.google\.com/.test(url)) return "sign-in";
  if (/^https:\/\/admin\.google\.com\/ac\/domains/i.test(url)) return "domains";
  if (/^https:\/\/admin\.google\.com\/ac\/billing/i.test(url)) return "billing";
  if (/^https:\/\/admin\.google\.com\/ac\/security\/2sv/i.test(url)) return "policy";
  if (/^https:\/\/admin\.google\.com\//i.test(url)) return "admin";
  if (/^https:\/\/myaccount\.google\.com\//i.test(url)) return "twofa";
  if (/^https:\/\/console\.cloud\.google\.com\//i.test(url)) return "console";
  return "other";
}

async function signalAutoDomainReady() {
  if (autoSetup.current !== "tenant.verify") return;
  autoSetup.domainReadyPending = true;
  autoStatus("verifying the domain");
  autoLog("TXT record is visible; telling Google to verify");
  const tab = await api.tabs.get(autoSetup.tabId).catch(() => null);
  if (
    tab &&
    !/^https:\/\/admin\.google\.com\/ac\/domains/i.test(tab.url || "")
  ) {
    await api.tabs.update(autoSetup.tabId, { url: AUTO_DOMAINS }).catch(() => {});
  }
  try {
    await api.tabs.sendMessage(autoSetup.tabId, { type: "setup-domain-ready" });
    autoSetup.domainReadyPending = false;
  } catch (_) {
    // sendAutoHandoff retries once the content script is listening
  }
}

async function sendAutoHandoff(url) {
  if (autoSetup.handoffPaused) return;
  const profile = autoProfile();
  if (
    /^https:\/\/admin\.google\.com\//i.test(url) ||
    /^https:\/\/console\.cloud\.google\.com\//i.test(url) ||
    /^https:\/\/accounts\.google\.com\//i.test(url)
  ) {
    try {
      await api.tabs.sendMessage(autoSetup.tabId, {
        type: "setup-fill",
        profile: {
          ...profile,
          adminEmail: autoAdminEmail(),
          recoveryEmail: autoAdminEmail() || profile.recoveryEmail,
        },
        phase: autoSetup.current || autoSetup.phase,
      });
      if (autoSetup.domainReadyPending) {
        await api.tabs.sendMessage(autoSetup.tabId, {
          type: "setup-domain-ready",
        });
        autoSetup.domainReadyPending = false;
      }
    } catch (_) {
      // the content script is not ready yet
    }
  }

  if (/^https:\/\/myaccount\.google\.com\//i.test(url)) {
    try {
      await api.tabs.sendMessage(autoSetup.tabId, {
        type: "google-fill",
        email: autoAdminEmail(),
        password: "",
        code: "",
        codes: [],
        otp: autoAdminOtp(),
      });
    } catch (_) {
      // google.js is not ready yet
    }
  }
}

function autoTaskUrl(id) {
  switch (id) {
    case "tenant.verify":
      return AUTO_DOMAINS;
    case "tenant.cloud-identity":
      return AUTO_BILLING;
    case "twostep.enroll":
      return AUTO_ADMIN_2SV;
    case "twostep.policy":
      return AUTO_2SV;
    case "key.project":
      return AUTO_PROJECT;
    case "key.admin-sdk": {
      const project = autoProjectId();
      return project
        ? `${AUTO_ADMIN_SDK}?project=${encodeURIComponent(project)}`
        : "";
    }
    case "key.consent":
    case "key.scopes": {
      const project = autoProjectId();
      return project
        ? `${AUTO_CONSENT}?project=${encodeURIComponent(project)}`
        : "";
    }
    case "key.client": {
      const project = autoProjectId();
      return project
        ? `${AUTO_CREDENTIALS}?project=${encodeURIComponent(project)}`
        : "";
    }
    default:
      return "";
  }
}

async function beginAutoTask(id) {
  const def = SUBSTEP_BY_ID[id];
  if (!def) {
    await failAutoTask(id, "unknown sub-step");
    return;
  }
  ensureAutoAdminLabel();
  autoSetup.current = id;
  autoSetup.taskStartedAt = Date.now();
  autoSetup.taskAttempted = false;
  autoSetup.handoffPaused = id === "key.authorize";
  if (id === "key.project") {
    autoSetup.projectId = "";
    persistAutoState({ projectId: "" }).catch(() => {});
  }
  await setSubstep(id, { status: "running", error: "" });
  autoStatus(`running · ${def.label}`);
  autoLog(`sub-step: ${def.label}`, "step");
  renderAutoSetup();

  if (id === "key.authorize") {
    runAuthorizeTask(id).catch((error) => failAutoTask(id, error.message || error));
    return;
  }

  const url = autoTaskUrl(id);
  if (!url) {
    await failAutoTask(
      id,
      id === "key.admin-sdk" ||
      id === "key.consent" ||
      id === "key.scopes" ||
      id === "key.client"
        ? "no Cloud project id yet — run Create the Cloud project first"
        : "this sub-step has no automatic page"
    );
    return;
  }
  autoSetup.taskAttempted = true;
  await api.tabs.update(autoSetup.tabId, { url }).catch(() => {});
}

async function autoTaskTick() {
  const id = autoSetup.current;
  if (!id) return;
  const def = SUBSTEP_BY_ID[id];
  if (!def) return;

  if (!autoSetup.taskAttempted && id !== "key.authorize") {
    const url = autoTaskUrl(id);
    if (url) {
      autoSetup.taskAttempted = true;
      autoSetup.taskStartedAt = Date.now();
      await api.tabs.update(autoSetup.tabId, { url }).catch(() => {});
    }
  }

  if (def.timeout && Date.now() - autoSetup.taskStartedAt > def.timeout) {
    await failAutoTask(
      id,
      `timed out after ${Math.round(def.timeout / 1000)}s — finish it in the worker tab or retry`
    );
  }
}

async function autoSetupLoop() {
  while (autoSetup.running && !autoSetup.stop) {
    const tab = await api.tabs.get(autoSetup.tabId).catch(() => null);
    if (!tab) {
      if (autoSetup.current) {
        await failAutoTask(autoSetup.current, "the worker tab was closed");
      } else {
        autoSetup.running = false;
        autoSetup.phase = "failed";
        autoStatus("the worker tab was closed");
        renderAutoSetup();
      }
      return;
    }

    const url = tab.url || "";
    const kind = autoTabKind(url);
    if (url !== autoSetup.lastUrl) {
      autoSetup.lastUrl = url;
      autoLog(`at ${url.slice(0, 150)}`);
    }

    const signedIn = kind !== "sign-in" && kind !== "other";
    if (!signedIn) {
      autoSetup.signedIn = false;
      autoStatus("sign in as the tenant admin in the worker tab");
      if (autoSetup.current) {
        autoSetup.taskAttempted = false;
        autoSetup.taskStartedAt = Date.now();
      }
      await sendAutoHandoff(url);
      await sleep(1500);
      continue;
    }

    if (!autoSetup.signedIn) {
      autoSetup.signedIn = true;
      autoStatus("signed in — starting");
      autoLog("signed in to the Admin console", "ok");
      renderAutoSetup();
      await sleep(600);
    }

    await sendAutoHandoff(url);

    if (!autoSetup.current) {
      const next = autoSetup.queue.shift();
      if (!next) {
        await completeAutoRun();
        return;
      }
      await beginAutoTask(next);
    } else {
      await autoTaskTick();
    }

    await sleep(1000);
  }
}

function pauseAutoContent() {
  autoSetup.handoffPaused = true;
  if (!autoSetup.tabId) return;
  api.tabs
    .sendMessage(autoSetup.tabId, { type: "setup-stop" })
    .catch(() => {});
}

async function finishAutoTask(id, { error = "", key = "" } = {}) {
  if (autoSetup.current !== id) return;
  const def = SUBSTEP_BY_ID[id];
  autoSetup.current = "";
  autoSetup.taskStartedAt = 0;

  if (error) {
    await setSubstep(id, { done: false, status: "failed", error: String(error).slice(0, 320) });
    autoSetup.running = false;
    autoSetup.stop = true;
    autoSetup.phase = "failed";
    autoStatus(`failed · ${def?.label || id}: ${error}`);
    autoLog(`${def?.label || id}: ${error}`, "error");
    pauseAutoContent();
    renderAutoSetup();
    await focusRunnerTab();
    return;
  }

  await markSubstepDone(id);
  if (id === "twostep.enroll") {
    await setSubstep("twostep.open", { done: true, status: "done", error: "" });
    if (autoSetup.policySafe) {
      autoSetup.blocked = autoSetup.blocked?.id === "twostep.policy" ? null : autoSetup.blocked;
      await persistAutoState({ policySafe: true }).catch(() => {});
    }
    if (!autoSetup.policySafe) {
      autoSetup.queue = autoSetup.queue.filter((entry) => entry !== "twostep.policy");
      await setSubstep("twostep.policy", {
        done: false,
        status: "failed",
        error:
          "admin 2SV is already on without a saved authenticator secret — set the policy manually only if another allowed factor exists",
      });
      autoLog(
        "2SV policy left manual: an existing admin without a saved authenticator secret would be locked out",
        "warn"
      );
    }
  }
  if (id === "key.authorize") {
    autoSetup.pendingKeyId = key || autoSetup.pendingKeyId;
    await markSubstepDone("run.key");
    autoSetup.endedOnKey = true;
  }
  if (id === "run.check") autoSetup.endedOnKey = true;
  // The consent and client pages are left open for the manual scope work.
  // Stop the page automation there so it cannot click behind the user.
  if (id === "key.consent" || id === "key.scopes" || id === "key.client") {
    pauseAutoContent();
  }

  autoLog(`${def?.label || id} — done`, "ok");
  if (!autoSetup.queue.length) {
    await completeAutoRun();
    return;
  }
  autoStatus("starting the next sub-step");
  renderAutoSetup();
}

async function failAutoTask(id, error) {
  await finishAutoTask(id, { error: String(error || "failed") });
}

async function completeAutoRun() {
  autoSetup.running = false;
  autoSetup.stop = true;
  autoSetup.current = "";
  autoSetup.queue = [];
  if (autoSetup.blocked) {
    const blockedDef = SUBSTEP_BY_ID[autoSetup.blocked.id];
    const labels = missingSubstepLabels(autoSetup.blocked.missing).join(", ");
    autoSetup.phase = "paused";
    autoStatus(
      labels
        ? `paused — tick ${labels}, then run again`
        : `paused — ${blockedDef?.label || autoSetup.blocked.id} needs a manual check`
    );
    autoLog(
      `automatic run paused: ${blockedDef?.label || autoSetup.blocked.id} needs ${
        labels || "a manual check"
      }`,
      "warn"
    );
    if (!substepEntry(autoSetup.blocked.id)?.error) {
      await setSubstep(autoSetup.blocked.id, {
        status: "auto",
        error: `needs: ${labels || "a manual check"}`,
      });
    }
    pauseAutoContent();
  } else {
    const manual = SUBSTEPS.filter(
      (entry) => substepEntry(entry.id)?.status === "failed"
    ).map((entry) => entry.label);
    autoSetup.phase = "done";
    autoStatus(
      manual.length
        ? `done — manual check: ${manual.join(", ")}`
        : "done — selected sub-steps finished"
    );
    autoLog("automatic run finished", "ok");
    pauseAutoContent();
  }
  renderAutoSetup();
  await focusRunnerTab();
  if (autoSetup.endedOnKey && !autoSetup.blocked) switchTab("keys");
}

async function runLocalSubstep(def) {
  autoSetup.running = true;
  autoSetup.stop = false;
  autoSetup.current = "";
  autoSetup.queue = [];
  autoSetup.blocked = null;
  autoSetup.endedOnKey = false;
  autoSetup.phase = "running";
  autoStatus(`running · ${def.label}`);
  renderAutoSetup();
  await setSubstep(def.id, { status: "running", error: "" });
  try {
    if (def.id === "run.first") {
      if (running) throw new Error("a run is already in progress");
      if (!state.keys.length) throw new Error("load a key in Keys first");
      if (!state.keys.some((key) => !key.needs_consent)) {
        throw new Error("authorize the key first");
      }
      const before = state.accounts.length;
      await start({ preflight: true });
      state = await load();
      if (state.accounts.length <= before) {
        throw new Error(
          "the preflight did not create an account — check the Run console for the reason"
        );
      }
      await markSubstepDone("run.key");
      await markSubstepDone(def.id);
      autoSetup.endedOnKey = true;
      autoSetup.phase = "done";
      autoStatus(`first account created — ${state.accounts.length} total`);
      autoLog("preflight created the first account", "ok");
      switchTab("accounts");
    } else if (def.id === "run.export") {
      if (!state.accounts.length) throw new Error("no accounts to export yet");
      await exportAccounts(true);
      await markSubstepDone(def.id);
      autoSetup.endedOnKey = true;
      autoSetup.phase = "done";
      autoStatus(`exported ${state.accounts.length} account(s)`);
      autoLog(`exported ${state.accounts.length} account(s)`, "ok");
    } else {
      const key =
        state.keys.find((entry) => entry.status === "ok") ||
        state.keys.find((entry) => !entry.needs_consent) ||
        state.keys[0];
      if (!key) throw new Error("load a key in Keys first");
      await withTimeout(checkKey(key.id), def.timeout || 45_000, "key check");
      const updated = state.keys.find((entry) => entry.id === key.id);
      if (!updated || updated.status !== "ok") {
        throw new Error(updated?.message || "the key check did not pass");
      }
      await markSubstepDone(def.id);
      await markSubstepDone("run.key");
      autoSetup.endedOnKey = true;
      autoSetup.phase = "done";
      autoStatus("key check passed");
      autoLog("key check passed", "ok");
    }
  } catch (error) {
    await setSubstep(def.id, {
      done: false,
      status: "failed",
      error: String(error.message || error).slice(0, 320),
    });
    autoSetup.phase = "failed";
    autoStatus(`failed · ${def.label}: ${error.message || error}`);
    autoLog(`${def.label} failed: ${error.message || error}`, "error");
  } finally {
    autoSetup.running = false;
    autoSetup.stop = true;
    renderAutoSetup();
    await focusRunnerTab();
  }
}

async function handleAutoAdmin(message) {
  const email = String(message.email || "").trim().toLowerCase();
  if (!email) return;
  const profile = autoProfile();
  const domain = String(profile.domain || "").toLowerCase();
  if (domain && !email.endsWith(`@${domain}`)) {
    const reason = `the worker tab is signed in as ${email}, not @${domain}`;
    if (autoSetup.current) await failAutoTask(autoSetup.current, reason);
    else {
      stopAutoSetup("wrong admin account — sign in as the tenant admin");
      autoLog(reason, "error");
    }
    return;
  }
  if (autoSetup.adminEmail === email) return;
  autoSetup.adminEmail = email;
  autoSetup.adminAt = Date.now();
  profile.adminEmail = email;
  autoLog(`admin account detected: ${email}`, "ok");
  await persistAutoState({ adminEmail: email });
  renderAutoSetup();
}

async function handleAutoDomain(message) {
  if (autoSetup.current !== "tenant.verify") return;
  const token = String(message.token || "");
  const profile = autoProfile();
  if (!token || !profile.subdomain) {
    await failAutoTask("tenant.verify", "DuckDNS token/subdomain missing from step 1");
    return;
  }
  autoStatus("writing the DuckDNS TXT record");
  autoLog(`writing the domain TXT record to ${profile.subdomain}.duckdns.org`);
  try {
    await writeTxt(profile.token, profile.subdomain, token);
    const domain = fullDomain(profile.subdomain);
    const found = await waitForTxt(domain, token, 90_000, (answers, rounds) => {
      if (rounds % 4 === 0) autoLog(`waiting for DNS propagation (${rounds})`);
    });
    if (!found.found) {
      await failAutoTask(
        "tenant.verify",
        "the TXT record did not resolve in 90s — check DuckDNS and press Auto again"
      );
      return;
    }
    await signalAutoDomainReady();
  } catch (error) {
    await failAutoTask("tenant.verify", error.message || error);
  }
}

async function handleAutoDomainStatus(message) {
  if (autoSetup.current !== "tenant.verify") return;
  if (message.found === false) {
    if (Date.now() - autoSetup.taskStartedAt < 25_000) return;
    await failAutoTask(
      "tenant.verify",
      `the tenant does not list ${autoProfile().domain} — sign in as the tenant admin`
    );
    return;
  }
  if (message.verified) {
    await finishAutoTask("tenant.verify");
    return;
  }
  if (!autoSetup.domainWarnAt || Date.now() - autoSetup.domainWarnAt > 15_000) {
    autoSetup.domainWarnAt = Date.now();
    autoLog("the domain is listed but not verified yet — waiting for the TXT record", "warn");
  }
}

async function handleAutoDomainVerified() {
  if (autoSetup.current !== "tenant.verify") return;
  await finishAutoTask("tenant.verify");
}

async function handleAutoBillingDone() {
  if (autoSetup.current !== "tenant.cloud-identity") return;
  await finishAutoTask("tenant.cloud-identity");
}

async function handleAutoPolicyDone() {
  if (autoSetup.current !== "twostep.policy") return;
  await finishAutoTask("twostep.policy");
}

async function handleAdminEnrolled() {
  if (autoSetup.current !== "twostep.enroll") return;
  autoSetup.policySafe = true;
  await persistAutoState({ policySafe: true }).catch(() => {});
  await finishAutoTask("twostep.enroll");
}

async function handleAutoTwoStepExisting(message = {}) {
  if (autoSetup.current !== "twostep.enroll") return;
  autoSetup.policySafe = Boolean(message.secret);
  await persistAutoState({ policySafe: autoSetup.policySafe }).catch(() => {});
  await finishAutoTask("twostep.enroll");
}

async function addAutoKey(clientId, clientSecret) {
  const profile = autoProfile();
  const existing = state.keys.find((entry) => entry.client_id === clientId);
  if (existing) return existing;

  const id = uid("key");
  await mutate((draft) => {
    if (draft.keys.some((entry) => entry.client_id === clientId)) return;
    draft.keys.push({
      id,
      kind: "oauth_client",
      label: profile.domain || "auto setup",
      domain: profile.domain || "",
      admin: autoAdminEmail() || null,
      admin_email: autoAdminEmail() || null,
      admin_password: null,
      client_id: clientId,
      client_secret: clientSecret,
      refresh_token: null,
      needs_consent: true,
      status: "client",
      scopes: [],
      fingerprint: `oauth:${clientId}`,
      created_at: new Date().toISOString(),
    });
  });
  await refresh();
  return state.keys.find((entry) => entry.client_id === clientId) || null;
}

async function handleAutoClient(message) {
  if (autoSetup.current !== "key.client") return;
  const clientId = String(message.client_id || "").trim();
  const clientSecret = String(message.client_secret || "").trim();
  if (!clientId || !clientSecret) {
    await failAutoTask("key.client", "the OAuth client dialog did not expose an id and secret");
    return;
  }
  const key = await addAutoKey(clientId, clientSecret);
  if (!key) {
    await failAutoTask("key.client", "could not add the OAuth client to Keys");
    return;
  }
  autoSetup.pendingKeyId = key.id;
  autoLog(`OAuth client ${clientId} added to Keys`);
  await finishAutoTask("key.client", { key: key.id });
}

function resolvePendingKey() {
  if (autoSetup.pendingKeyId) {
    const pending = state.keys.find((entry) => entry.id === autoSetup.pendingKeyId);
    if (pending) return pending;
  }
  const domain = autoProfile().domain;
  const matching = state.keys.find(
    (entry) =>
      entry.needs_consent &&
      (!domain || String(entry.domain || "").toLowerCase() === domain.toLowerCase())
  );
  return matching || state.keys.find((entry) => entry.needs_consent) || null;
}

async function runAuthorizeTask(id) {
  const key = resolvePendingKey();
  if (!key) {
    await failAutoTask(
      id,
      "no OAuth client to authorize — run Create the desktop OAuth client first"
    );
    return;
  }
  if (!autoSetup.storeId) autoSetup.storeId = await ensureSetupContainer();
  autoStatus(`authorizing ${key.label}`);
  const authorized = await authorizeKey(key.id, {
    storeId: autoSetup.storeId,
    autoConsent: true,
    email: autoAdminEmail(),
    password: "",
    otp: autoAdminOtp(),
    timeout: 180_000,
  });
  if (!authorized) {
    await failAutoTask(id, "consent was not completed — finish it in the consent tab and retry");
    return;
  }
  await finishAutoTask(id, { key: key.id });
}

async function handleAutoCaptcha() {
  autoStatus("captcha — solve it in the worker tab");
  autoLog("Google wants a captcha; the run resumes after you solve it", "warn");
  await focusAutoTab();
}

function wireAutoSetup() {
  for (const id of ["auto-start", "auto-start-top"]) {
    $(id)?.addEventListener("click", () => {
      startAutoSetup().catch((error) =>
        remember(`automatic finish: ${error.message || error}`, "error")
      );
    });
  }
  $("auto-stop")?.addEventListener("click", () => stopAutoSetup("stopped by you"));
  $("auto-keys")?.addEventListener("click", () => switchTab("keys"));
  $("auto-manual")?.addEventListener("click", () => {
    setSetup((draft) => {
      draft.open.tenant = false;
      draft.open.twostep = true;
    })
      .then(() => openStep("twostep", true))
      .catch(() => {});
  });
}

function wireSetup() {
  const panel = $("panel-setup");
  if (!panel) return;

  decorateSubsteps();

  panel.addEventListener("click", (event) => {
    const auto = event.target.closest("[data-automate-substep]");
    if (auto) {
      runSubstep(auto.dataset.automateSubstep).catch((error) =>
        remember(`automatic sub-step: ${error.message || error}`, "error")
      );
      return;
    }
    // The tick is a real checkbox; let its change event do the work.
    if (event.target.closest("[data-step-done]")) return;
    if (event.target.closest("[data-substep-done]")) return;
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
    const sub = event.target.closest("[data-substep-done]");
    if (sub) {
      const id = sub.dataset.substepDone;
      if (autoSetup.running) {
        if (!sub.checked) {
          stopAutoSetup("checklist changed — run stopped");
        } else {
          autoSetup.queue = autoSetup.queue.filter((entry) => entry !== id);
        }
      }
      setSubstepCascade(id, sub.checked)
        .then(() => {
          if (!autoSetup.running) return;
          autoSetup.queue = autoSetup.queue.filter((entry) => !isSubstepDone(entry));
          if (autoSetup.current && isSubstepDone(autoSetup.current)) {
            stopAutoSetup("current sub-step was checked — run stopped");
          } else {
            renderAutoSetup();
          }
        })
        .catch((error) =>
          remember(`checklist: ${error.message || error}`, "error")
        );
      return;
    }

    const box = event.target.closest("[data-step-done]");
    if (!box) return;
    const id = box.dataset.stepDone;
    if (STEP_SUBSTEPS[id]) {
      const ids = STEP_SUBSTEPS[id];
      const target = box.checked ? ids[ids.length - 1] : ids[0];
      if (autoSetup.running && !box.checked) {
        stopAutoSetup("checklist changed — run stopped");
      }
      setSubstepCascade(target, box.checked)
        .then(() => {
          if (!autoSetup.running) return;
          autoSetup.queue = autoSetup.queue.filter((entry) => !isSubstepDone(entry));
          if (autoSetup.current && isSubstepDone(autoSetup.current)) {
            stopAutoSetup("current sub-step was checked — run stopped");
          } else {
            renderAutoSetup();
          }
        })
        .catch((error) =>
          remember(`checklist: ${error.message || error}`, "error")
        );
      return;
    }
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

function wireSecretToggles() {
  const eye =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12Z"/>' +
    '<circle cx="12" cy="12" r="3"/></svg>';

  for (const input of document.querySelectorAll('input[type="password"]')) {
    if (input.dataset.secretWrapped) continue;
    const wrap = document.createElement("span");
    wrap.className = "secret-wrap";
    input.parentNode.insertBefore(wrap, input);
    wrap.append(input);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "secret-eye";
    button.setAttribute("aria-label", "Show value");
    button.title = "Show value";
    button.innerHTML = eye;
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.classList.toggle("revealed", !showing);
      button.setAttribute("aria-label", showing ? "Show value" : "Hide value");
      button.title = showing ? "Show value" : "Hide value";
    });
    wrap.append(button);
    input.dataset.secretWrapped = "1";
  }
}

function wire() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("active", node === tab));
      document.querySelectorAll(".tabpanel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === `panel-${tab.dataset.tab}`);
      });
    });
  });

  $("keys-to-setup")?.addEventListener("click", () => {
    switchTab("setup");
    setSetup((draft) => {
      draft.hideDone = false;
    })
      .then(() => openStep("key", true))
      .catch(() => {});
  });

  wireSetup();
  wireAutoSetup();
  wireSecretToggles();

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

  $("delete-google-after").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.deleteGoogleAfterSuccess = Boolean(event.target.checked);
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

  $("prime-google").addEventListener("change", async (event) => {
    await mutate((draft) => {
      draft.settings.primeGoogle = Boolean(event.target.checked);
    });
  });

  $("lane-surface").addEventListener("change", async (event) => {
    const value = event.target.value === "window" ? "window" : "tab";
    await mutate((draft) => {
      draft.settings.laneSurface = value;
    });
    await refresh();
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
  // Losing the secret costs the account. Save it as soon as it is entered, not
  // only when the button is remembered.
  $("totp-secret").addEventListener("change", () => {
    if ($("totp-secret").value.trim()) saveTotp().catch(() => {});
  });
  $("totp-email").addEventListener("change", () => {
    if (state.totp?.secret) saveTotp().catch(() => {});
  });
  $("totp-check").addEventListener("click", () => checkTwoStep());
  $("audit-check").addEventListener("click", () => readLoginAudit());
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
      if (message.enrolled && email) liveEnrolled.add(email);
      const saved = mutate((draft) => {
        let matched = false;
        const row = draft.identities.find(
          (entry) => String(entry.email || "").toLowerCase() === email
        );
        if (row) {
          row.otp_secret = message.secret;
          matched = true;
        }
        for (const account of draft.accounts) {
          if (String(account.email || "").toLowerCase() === email) {
            account.otp_secret = message.secret;
            matched = true;
          }
        }
        // An enrolment that belongs to no identity is the admin's own account -
        // exactly the secret you cannot afford to drop. It used to be discarded
        // here, which is how a working authenticator got lost.
        if (!matched) {
          // The admin's own settings page has no identity email to send, so
          // keep whatever admin address is already stored or typed rather than
          // blanking it on the way in.
          const adminEmail =
            email ||
            draft.totp?.email ||
            ($("totp-email")?.value || "").trim().toLowerCase() ||
            "";
          draft.totp = {
            ...(draft.totp || {}),
            email: adminEmail,
            secret: message.secret,
            label: "captured from a sign-in",
          };
        }
      }).catch(() => {});
      remember(
        `${email || "account"}: authenticator ${message.enrolled ? "enrolled" : "secret saved"}`,
        "ok"
      );
      const admin = autoAdminEmail();
      const domain = String(autoSetup.profile?.domain || "").toLowerCase();
      const sameAdmin = admin
        ? email === admin
        : Boolean(domain && email.endsWith(`@${domain}`));
      if (message.enrolled && autoSetup.current === "twostep.enroll" && sameAdmin) {
        saved.finally(() =>
          handleAdminEnrolled().catch((error) =>
            autoLog(`admin 2SV: ${error.message || error}`, "error")
          )
        );
      }
    }
    if (message?.type === "google-2sv" && message.enrolled) {
      const email = String(message.email || "").toLowerCase();
      const admin = autoAdminEmail();
      const domain = String(autoSetup.profile?.domain || "").toLowerCase();
      const sameAdmin = admin
        ? email === admin
        : Boolean(domain && email.endsWith(`@${domain}`));
      if (autoSetup.current === "twostep.enroll" && sameAdmin) {
        handleAutoTwoStepExisting(message).catch((error) =>
          autoLog(`admin 2SV: ${error.message || error}`, "error")
        );
      }
      return false;
    }
    if (message?.type === "setup-admin" && message.email) {
      handleAutoAdmin(message).catch((error) =>
        autoLog(`admin account: ${error.message || error}`, "error")
      );
      return false;
    }
    if (message?.type === "setup-report" && message.text) {
      autoLog(String(message.text), message.level || "info");
      return false;
    }
    if (message?.type === "setup-captcha") {
      handleAutoCaptcha().catch(() => {});
      return false;
    }
    if (message?.type === "setup-domain" && message.token) {
      handleAutoDomain(message).catch((error) =>
        autoLog(`domain: ${error.message || error}`, "error")
      );
      return false;
    }
    if (message?.type === "setup-domain-status") {
      handleAutoDomainStatus(message).catch((error) =>
        autoLog(`domain: ${error.message || error}`, "error")
      );
      return false;
    }
    if (message?.type === "setup-domain-verified") {
      handleAutoDomainVerified().catch(() => {});
      return false;
    }
    if (message?.type === "setup-billing-done") {
      handleAutoBillingDone().catch(() => {});
      return false;
    }
    if (message?.type === "setup-policy-done") {
      handleAutoPolicyDone().catch(() => {});
      return false;
    }
    if (message?.type === "setup-client" && message.client_id && message.client_secret) {
      handleAutoClient(message).catch((error) =>
        autoLog(`OAuth client: ${error.message || error}`, "error")
      );
      return false;
    }
    if (message?.type === "setup-project" && message.project) {
      const project = String(message.project);
      const changed = project !== autoSetup.projectId;
      autoSetup.projectId = project;
      if (changed) {
        autoLog(`Cloud project id: ${autoSetup.projectId}`);
        persistAutoState({ projectId: autoSetup.projectId }).catch(() => {});
        renderAutoSetup();
      }
      if (autoSetup.current === "key.project") {
        finishAutoTask("key.project").catch((error) =>
          autoLog(`project: ${error.message || error}`, "error")
        );
      }
      return false;
    }
    if (message?.type === "setup-api-enabled") {
      if (autoSetup.current === "key.admin-sdk") {
        finishAutoTask("key.admin-sdk").catch((error) =>
          autoLog(`Admin SDK: ${error.message || error}`, "error")
        );
      }
      return false;
    }
    if (message?.type === "setup-consent-ready") {
      if (autoSetup.current === "key.consent") {
        finishAutoTask("key.consent").catch((error) =>
          autoLog(`consent: ${error.message || error}`, "error")
        );
      }
      return false;
    }
    if (message?.type === "setup-scopes-ready") {
      if (autoSetup.current === "key.scopes") {
        finishAutoTask("key.scopes").catch((error) =>
          autoLog(`scopes: ${error.message || error}`, "error")
        );
      }
      return false;
    }
    if (message?.type === "setup-scopes-failed") {
      if (autoSetup.current === "key.scopes") {
        failAutoTask(
          "key.scopes",
          message.error ||
            "could not add the scopes automatically — add them manually and tick the sub-step"
        ).catch((error) => autoLog(`scopes: ${error.message || error}`, "error"));
      }
      return false;
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
  // Set the version before any storage or rendering work: the page paints
  // immediately and the build pill never sits on "v---".
  checkBuild();
  state = await load();
  await migrateSubsteps();
  wire();
  renderConsoleLog();
  render();
  // Paint the wizard before the checklist's API calls: step blocks and manual
  // ticks should be right immediately, not after a round-trip to Google.
  renderSetup();
  await remember("runner ready", "info");

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
