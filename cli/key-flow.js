import * as admin from "../extension/lib/admin.js";
import { normaliseKey, parseKeysText } from "../extension/lib/keys.js";
import { subdomainFrom } from "../extension/lib/duckdns.js";
import { makeAdminPassword } from "../extension/lib/names.js";
import { findKey, load, mutate, requireKey } from "./state.js";
import { info, mask, ok, warn } from "./util.js";

export async function importKeysText(text, { domain = "", adminEmail = "", source = "pasted" } = {}) {
  const parsed = parseKeysText(text);
  for (const problem of parsed.problems) warn(problem);

  let added = 0;
  let skipped = 0;
  const ids = [];

  await mutate((state) => {
    for (const key of parsed.keys) {
      if (state.keys.some((existing) => existing.fingerprint === key.fingerprint)) {
        skipped += 1;
        continue;
      }
      if (domain && !key.domain) key.domain = String(domain).trim().toLowerCase();
      if (adminEmail && !key.admin) key.admin = String(adminEmail).trim().toLowerCase();
      state.keys.push(key);
      ids.push(key.id);
      added += 1;
    }
  });

  ok(`${added} key(s) imported from ${source}${skipped ? `, ${skipped} duplicate(s) skipped` : ""}`);
  return { added, skipped, ids, parsed };
}

export async function checkKeyById(idOrKey, { quiet = false } = {}) {
  const key = typeof idOrKey === "string" ? await requireKey(idOrKey) : idOrKey;
  if (!key) throw new Error("key not found");
  if (key.needs_consent) {
    warn(`${key.label || key.id}: authorize this client before checking it`);
    return { ok: false, key };
  }

  if (!quiet) info(`checking ${key.label || key.id}…`);
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

    await mutate((state) => {
      const target = state.keys.find((entry) => entry.id === key.id);
      if (!target) return;
      target.status = "ok";
      target.domain = domain;
      target.label = domain;
      target.seats = seats;
      target.message = `${seats}/50 seats used`;
      target.checked_at = new Date().toISOString();
      const sub = subdomainFrom(domain);
      if (sub && !state.duckdns.subdomain) state.duckdns.subdomain = sub;
    });

    ok(`${domain} · token ok · ${seats} seats used`);
    return { ok: true, key: { ...key, domain, seats, status: "ok" } };
  } catch (error) {
    const blocked =
      error.blocked || error.status === 412 || error.reason === "conditionNotMet";
    const message = blocked
      ? "blocked by Google (412) — this key cannot create more users"
      : String(error.message || error);
    await mutate((state) => {
      const target = state.keys.find((entry) => entry.id === key.id);
      if (!target) return;
      target.status = blocked ? "blocked" : "error";
      target.message = message.slice(0, 240);
      target.checked_at = new Date().toISOString();
    });
    warn(`${key.label || key.id}: ${message}`);
    return { ok: false, key: { ...key, status: blocked ? "blocked" : "error", message } };
  }
}

export async function checkAllKeys() {
  const state = await load();
  const results = [];
  for (const key of state.keys) {
    results.push(await checkKeyById(key.id));
  }
  return results;
}

export async function recoverAdminWithKey(idOrKey, targetEmail) {
  const key = typeof idOrKey === "string" ? await requireKey(idOrKey) : idOrKey;
  const target = String(targetEmail || key.admin_email || key.admin || (key.domain ? `admin@${key.domain}` : ""))
    .trim()
    .toLowerCase();
  if (!target || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(target)) {
    throw new Error("a valid admin email is required");
  }

  const password = makeAdminPassword();
  await admin.setPassword(key, target, password);
  let codes = [];
  try {
    codes = await admin.mintCodes(key, target);
  } catch (error) {
    warn(`password reset worked, but backup codes failed: ${error.message || error}`);
  }

  await mutate((state) => {
    const row = state.keys.find((entry) => entry.id === key.id);
    if (row) {
      row.admin = target;
      row.admin_email = target;
      row.admin_password = password;
      row.admin_recovery_at = new Date().toISOString();
      row.admin_backup_codes = codes;
    }
    state.setup.auto = {
      ...(state.setup.auto || {}),
      adminEmail: target,
      adminPassword: password,
    };
  });

  ok(`${target}: admin password reset${codes.length ? ` and ${codes.length} backup code(s) minted` : ""}`);
  return { target, password, codes };
}

export async function latestKey({ usable = false } = {}) {
  const state = await load();
  const list = state.keys || [];
  return (
    list.find((key) => (usable ? !key.needs_consent : true)) ||
    list[list.length - 1] ||
    null
  );
}

export function keyJson(key, { secrets = false } = {}) {
  const base = {
    domain: key.domain || "",
    admin: key.admin || key.admin_email || (key.domain ? `admin@${key.domain}` : ""),
    token_uri: key.token_uri || "https://oauth2.googleapis.com/token",
    scopes: key.scopes || [],
  };
  if (key.kind === "service_account") {
    return {
      type: "service_account",
      project_id: key.project_id || "",
      private_key: secrets ? key.private_key || "" : mask(key.private_key, 8),
      client_email: key.client_email || "",
      client_id: key.client_id || "",
      ...base,
    };
  }
  return {
    client_id: key.client_id || "",
    client_secret: secrets ? key.client_secret || "" : mask(key.client_secret, 8),
    refresh_token: secrets ? key.refresh_token || "" : mask(key.refresh_token, 8),
    ...base,
  };
}

export function redactKey(key) {
  const copy = { ...key };
  if (copy.client_secret) copy.client_secret = mask(copy.client_secret, 8);
  if (copy.refresh_token) copy.refresh_token = mask(copy.refresh_token, 8);
  if (copy.private_key) copy.private_key = mask(copy.private_key, 8);
  if (copy.admin_password) copy.admin_password = mask(copy.admin_password, 4);
  if (Array.isArray(copy.admin_backup_codes)) copy.admin_backup_codes = copy.admin_backup_codes.map(() => "…");
  return copy;
}

export function keySummary(key) {
  return {
    id: key.id,
    label: key.label || key.domain || key.id,
    kind: key.kind || "oauth",
    status: key.status || (key.needs_consent ? "client" : "new"),
    domain: key.domain || "",
    seats: key.seats === null || key.seats === undefined ? "" : String(key.seats),
    consent: key.needs_consent ? "needs authorize" : "ready",
  };
}

export async function resolveKeyArg(positionals = [], { usable = false } = {}) {
  const first = positionals.find((value) => !String(value).startsWith("-"));
  if (first) return requireKey(first);
  const key = await latestKey({ usable });
  if (!key) throw new Error("no key configured — run `rac key import <file>` first");
  return key;
}

export async function findKeyLoose(value) {
  return findKey(value);
}

export { normaliseKey };
