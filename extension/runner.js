// language: JavaScript, file: runner.js
// The run loop lives here, in a page — not in the MV3 worker, which sleeps.

import { api, isFirefox, uid, sleep, mapLimit, withTimeout, STATE_KEY } from "./lib/api.js";
import { load, mutate, wipeRecords } from "./lib/store.js";
import { log, stamp } from "./lib/log.js";
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
import { makeIdentityFields } from "./lib/names.js";
import {
  writeTxt,
  clearTxt,
  lookupTxt,
  waitForTxt,
  subdomainFrom,
  fullDomain,
} from "./lib/duckdns.js";
import { parseTotp, totpCode, secondsRemaining } from "./lib/totp.js";

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

// ---------------------------------------------------------------- boot / store

async function refresh() {
  state = await load();
  render();
}

async function remember(message, level = "info") {
  const write = log[level] || log.info;
  write(message);
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
    dot.className = `dot ${key.status === "ok" ? "ok" : key.status === "blocked" ? "blocked" : key.status === "error" ? "error" : ""}`;
    domain.append(dot, document.createTextNode(key.domain || key.label || "unnamed key"));

    const sub = document.createElement("div");
    sub.className = "sub";
    const kind = key.kind === "service_account" ? "service account" : "OAuth";
    sub.textContent = `${kind} · ${used}/50 seats · ${key.message || key.status}`;

    meta.append(domain, sub);

    const actions = document.createElement("div");
    actions.className = "actions";

    if (!key.domain) {
      actions.append(actionButton("Set domain", () => setKeyDomain(key.id)));
    }
    actions.append(
      actionButton("Check", () => checkKey(key.id)),
      actionButton("Delete users", () => deleteKeyUsers(key.id), "danger"),
      actionButton("Remove", () => removeKey(key.id), "ghost")
    );

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
  return [
    `username: ${account.username || ""}`,
    `password: ${account.password || ""}`,
    `email: ${account.email || ""}`,
    `user_id: ${account.user_id || ""}`,
    `cookie: ${account.cookie || ""}`,
    `status: ${account.status || "live"}`,
    `note: Google SSO - sign in with Google using ${account.email || ""}`,
    `created_at: ${account.created_at || ""}`,
  ].join("\n");
}

async function exportAccounts(silent = false) {
  if (!state.accounts.length) {
    if (!silent) await remember("nothing to export", "warn");
    return;
  }
  const text = state.accounts.map(accountText).join("\n\n") + "\n";
  const url = `data:text/plain;charset=utf-8,${encodeURIComponent(text)}`;
  try {
    await api.downloads.download({ url, filename: "roblox-accounts.txt", saveAs: !silent });
    await remember(`exported ${state.accounts.length} account(s)`, "ok");
  } catch (error) {
    await remember(`export failed: ${error.message || error}`, "error");
  }
}

async function downloadText(filename, text) {
  const url = `data:text/plain;charset=utf-8,${encodeURIComponent(text)}`;
  await api.downloads.download({ url, filename, saveAs: true });
}

// ------------------------------------------------------------------- key setup

async function addKeys(text, source = "pasted") {
  const parsed = parseKeysText(text);
  for (const problem of parsed.problems) log.warn(problem);

  await mutate((draft) => {
    for (const key of parsed.keys) {
      if (draft.keys.some((existing) => existing.fingerprint === key.fingerprint)) continue;
      draft.keys.push(key);
    }
  });

  await remember(`${parsed.keys.length} key(s) from ${source}`, "ok");
  await refresh();

  for (const key of parsed.keys) {
    if (key.domain) {
      await checkKey(key.id);
    } else {
      await remember(`${key.label}: no domain yet — detecting`, "step");
      await checkKey(key.id);
    }
  }
}

async function checkKey(id) {
  const key = state.keys.find((entry) => entry.id === id);
  if (!key) return;

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
  const identities = state.identities.filter((identity) => identity.key_id === id);
  if (!identities.length) {
    await remember("no identities recorded for this key", "warn");
    return;
  }
  const ok = confirm(
    `Delete ${identities.length} identities from ${key.domain || key.label}?\n\n` +
      "Seats are freed immediately. Addresses stay reserved for about 20 days and a 412 flag never clears."
  );
  if (!ok) return;

  await remember(`deleting ${identities.length} identities...`, "step");
  try {
    await mapLimit(identities, 8, async (identity) => {
      try {
        await admin.deleteUser(key, identity.email);
      } catch (error) {
        await remember(`${identity.email}: ${error.message || error}`, "warn");
      }
    });
    await mutate((draft) => {
      for (const identity of draft.identities) {
        if (identity.key_id === id) identity.status = "deleted";
      }
    });
    await remember("identities deleted", "ok");
  } catch (error) {
    await remember(`delete failed: ${error.message || error}`, "error");
  }
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
  const planned = [];
  let index = existing.length;

  while (planned.length < count) {
    index += 1;
    const fields = makeIdentityFields(key.domain, index);
    if (taken.has(fields.email)) continue;
    taken.add(fields.email);
    planned.push({
      id: uid("idn"),
      key_id: key.id,
      domain: key.domain,
      email: fields.email,
      first: fields.first,
      last: fields.last,
      password: fields.password,
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
async function mintWithRetry(key, email, attempts = 3) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await admin.mintCodes(key, email);
    } catch (error) {
      lastError = error;
      if (error.status === 412) break;
      await sleep(2500 * (attempt + 1));
    }
  }
  throw lastError || new Error("could not mint a backup code");
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

      await remember(`[lane ${laneIndex + 1}] ${identity.email}`, "step");

      try {
        let provisional = null;
        // Always mint a fresh set: generating invalidates the previous one, so a
        // requeued identity can never present a burned code.
        try {
          const codes = await mintWithRetry(key, identity.email);
          identity.codes = codes;
          await mutate((draft) => {
            const row = draft.identities.find((entry) => entry.id === identity.id);
            if (row) row.codes = codes;
          });
          await remember(`${identity.email}: ${codes.length} backup code(s) minted`, "info");
        } catch (error) {
          await remember(`${identity.email}: backup code mint failed (${error.message || error})`, "warn");
        }

        const result = await withTimeout(
          runIdentity({
            identity,
            lane: laneIndex,
            storeId,
            timeout: settings.timeout || 240,
            shouldStop: () => stopRequested,
            log: (text) => remember(text, "info"),
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
          }
        });

        if (live) {
          made += 1;
          consecutive = 0;
          await remember(`${account.username} (id ${account.user_id}) — saved`, "ok");
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
        await mutate((draft) => {
          const row = draft.identities.find((entry) => entry.id === identity.id);
          if (row) {
            row.status = stopped ? "ready" : "failed";
            row.error = stopped ? null : String(error.message || error);
          }
        });
        if (!stopped) await remember(`${identity.email}: ${error.message || error}`, "error");
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
    const result = await retryFinalise(tab.id, 60, () => false, (text) =>
      remember(text, "info")
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
        const identity = draft.identities.find((entry) => entry.id === account.identity_id);
        if (identity) {
          identity.status = "account";
          identity.error = null;
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

  switchTab("phone");

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
  const key = state.keys.find((entry) => entry.status === "ok") || state.keys[0];
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
    await remember(label, status.enrolled && status.enforced ? "ok" : "warn");
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
        twofa = status.enrolled && status.enforced;
        setText(
          "twofa-status",
          `${status.email}: enrolled=${status.enrolled} enforced=${status.enforced}`
        );
        if (!twofa) {
          await remember(
            `${status.email}: 2SV ${status.enrolled ? "enrolled" : "not enrolled"} \u00b7 ${
              status.enforced ? "enforced" : "not enforced"
            }`,
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

  $("start").addEventListener("click", () => start({ preflight: false }));
  $("preflight").addEventListener("click", () => start({ preflight: true }));
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

  $("authorize").addEventListener("click", async () => {
    try {
      const client = parseClient($("client-input").value);
      const domain = $("client-domain").value.trim().toLowerCase() || null;
      const impersonate = $("client-admin").value.trim().toLowerCase();

      await remember("opening the consent screen — approve it as the tenant admin", "step");
      const verifier = makeVerifier();
      const code = await waitForCode(client.client_id, verifier);
      const credential = await exchangeCode(client, code, verifier);

      const raw = {
        ...credential,
        ...(domain ? { domain } : {}),
        ...(impersonate ? { admin: impersonate } : {}),
      };
      const parsed = parseKeysText(JSON.stringify([raw]));
      await mutate((draft) => {
        for (const key of parsed.keys) {
          if (draft.keys.some((existing) => existing.fingerprint === key.fingerprint)) continue;
          draft.keys.push(key);
        }
      });
      await remember("key authorized and stored", "ok");
      $("client-input").value = "";
      await refresh();
      await checkKey(parsed.keys[0].id);
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
  const phonePanel = $("panel-phone");

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
        switchTab("phone");
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
        switchTab("phone");
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
      switchTab("phone");
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
      switchTab("phone");
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
    if (message?.type === "log-line" && message.line) {
      appendLogNode(message.line);
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

async function boot() {
  state = await load();
  wire();
  renderConsoleLog();
  render();

  setInterval(() => {
    setText("console-clock", new Date().toTimeString().slice(0, 8));
    updateTotp();
  }, 1000);
  setText("console-clock", new Date().toTimeString().slice(0, 8));

  const granted = await hasPermissions();
  $("grant").hidden = granted;

  await refreshChecklist();

  const hash = location.hash.replace("#", "");
  if (hash && document.querySelector(`.tab[data-tab="${hash}"]`)) {
    document.querySelector(`.tab[data-tab="${hash}"]`).click();
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
