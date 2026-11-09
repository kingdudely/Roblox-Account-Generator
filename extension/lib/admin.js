// language: JavaScript, file: lib/admin.js
// Admin SDK over HTTPS. Nothing here touches a browser tab.

import { Semaphore, sleep } from "./api.js";

const TOKEN_URL = "https://oauth2.googleapis.com/token";
const ADMIN = "https://admin.googleapis.com/admin/directory/v1";
const JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer";

const tokenCache = new Map();
const globalGate = new Semaphore(8);
const domainGates = new Map();

export class AdminError extends Error {
  constructor(message, { status = 0, reason = "", payload = null } = {}) {
    super(message);
    this.name = "AdminError";
    this.status = status;
    this.reason = reason;
    this.payload = payload;
  }

  get blocked() {
    return this.status === 412 || this.reason === "conditionNotMet";
  }

  get quota() {
    return (
      this.status === 429 ||
      this.reason === "quotaExceeded" ||
      this.reason === "rateLimitExceeded" ||
      this.reason === "userRateLimitExceeded"
    );
  }
}

async function parseError(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = null;
  }
  const error = payload?.error || {};
  const first = Array.isArray(error.errors) ? error.errors[0] : null;
  const reason = first?.reason || error.status || "";
  const message = error.message || first?.message || `HTTP ${response.status}`;
  return new AdminError(message, { status: response.status, reason, payload });
}

// Google documents 10 user inserts per domain per second. Staying at ~8/s
// leaves headroom for the retries a real batch always produces.
function domainGate(domain) {
  const id = domain || "default";
  if (!domainGates.has(id)) {
    domainGates.set(id, { chain: Promise.resolve(), last: 0 });
  }
  return domainGates.get(id);
}

async function pace(domain, spacing = 120) {
  const gate = domainGate(domain);
  const turn = gate.chain.then(async () => {
    const wait = Math.max(0, gate.last + spacing - Date.now());
    if (wait) await sleep(wait);
    gate.last = Date.now();
  });
  gate.chain = turn.catch(() => {});
  await turn;
}

function base64url(input) {
  const bytes =
    typeof input === "string" ? new TextEncoder().encode(input) : new Uint8Array(input);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function pemToBuffer(pem) {
  const body = String(pem)
    .replace(/-----BEGIN PRIVATE KEY-----/, "")
    .replace(/-----END PRIVATE KEY-----/, "")
    .replace(/\s+/g, "");
  const binary = atob(body);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

async function serviceAssertion(key) {
  const header = { alg: "RS256", typ: "JWT" };
  const now = Math.floor(Date.now() / 1000);
  const claims = {
    iss: key.client_email,
    scope: (key.scopes || []).join(" "),
    aud: TOKEN_URL,
    iat: now,
    exp: now + 3600,
  };
  if (key.admin) claims.sub = key.admin;

  const unsigned = `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(claims))}`;
  const cryptoKey = await crypto.subtle.importKey(
    "pkcs8",
    pemToBuffer(key.private_key),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    cryptoKey,
    new TextEncoder().encode(unsigned)
  );
  return `${unsigned}.${base64url(signature)}`;
}

export async function accessToken(key, force = false) {
  const cached = tokenCache.get(key.id);
  if (!force && cached && cached.expires > Date.now() + 60_000) {
    return cached.token;
  }

  const body = new URLSearchParams();
  if (key.kind === "service_account") {
    body.set("grant_type", JWT_GRANT);
    body.set("assertion", await serviceAssertion(key));
  } else {
    body.set("grant_type", "refresh_token");
    body.set("client_id", key.client_id);
    body.set("client_secret", key.client_secret);
    body.set("refresh_token", key.refresh_token);
  }

  const response = await fetch(TOKEN_URL, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });

  if (!response.ok) {
    const failure = await parseError(response);
    throw new AdminError(
      `token refresh failed (${failure.reason || failure.status}): ${failure.message}`,
      failure
    );
  }

  const data = await response.json();
  const ttl = Number(data.expires_in || 3600) * 1000;
  tokenCache.set(key.id, { token: data.access_token, expires: Date.now() + ttl });
  return data.access_token;
}

async function call(key, path, { method = "GET", body = null, spacing = 0, retries = 2 } = {}) {
  if (spacing) await pace(key.domain, spacing);

  return globalGate.run(async () => {
    let attempt = 0;
    for (;;) {
      const token = await accessToken(key, attempt > 0);
      const response = await fetch(`${ADMIN}${path}`, {
        method,
        headers: {
          authorization: `Bearer ${token}`,
          ...(body ? { "content-type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });

      if (response.ok) {
        if (response.status === 204) return null;
        const text = await response.text();
        return text ? JSON.parse(text) : null;
      }

      const error = await parseError(response);
      if (error.status === 401 && attempt < 1) {
        attempt += 1;
        continue;
      }
      if (error.quota && attempt < retries) {
        attempt += 1;
        await sleep(600 * attempt + Math.random() * 400);
        continue;
      }
      throw error;
    }
  });
}

export async function whoAmI(key) {
  const data = await call(key, "/users?customer=my_customer&maxResults=1");
  const user = data?.users?.[0];
  return {
    email: user?.primaryEmail || null,
    domain: user?.primaryEmail ? user.primaryEmail.split("@").pop() : null,
    total: data?.users?.length || 0,
  };
}

export async function detectDomain(key) {
  try {
    const me = await whoAmI(key);
    if (me.domain) return me.domain;
  } catch (_) {
    // fall through to the customer record
  }
  try {
    const customer = await call(key, "/customers/my_customer");
    if (customer?.customerDomain) return customer.customerDomain;
  } catch (_) {
    // scope may not allow it
  }
  return key.domain || null;
}

export async function countUsers(key, domain) {
  let total = 0;
  let page = null;
  let pages = 0;
  do {
    const query = new URLSearchParams({
      customer: "my_customer",
      maxResults: "200",
      ...(domain ? { domain } : {}),
      ...(page ? { pageToken: page } : {}),
    });
    const data = await call(key, `/users?${query.toString()}`);
    total += data?.users?.length || 0;
    page = data?.nextPageToken || null;
    pages += 1;
  } while (page && pages < 5);
  return total;
}

export async function twoStepStatus(key, email) {
  const data = await call(
    key,
    `/users/${encodeURIComponent(email)}?projection=full`
  );
  return {
    email: data?.primaryEmail || email,
    enrolled: Boolean(data?.isEnrolledIn2Sv),
    enforced: Boolean(data?.isEnforcedIn2Sv),
    suspended: Boolean(data?.suspended),
    archived: Boolean(data?.archived),
  };
}

// Login audit. The Admin console's "Login failure type" is Google's own
// diagnosis, and it is the only way to tell a policy refusal from a bad code,
// an expired challenge, or an account that never had a factor. The Admin SDK
// Reports API is a different host from the Directory API, hence the separate
// fetch rather than call().
export async function loginEvents(key, email, { maxResults = 50 } = {}) {
  const query = new URLSearchParams({ maxResults: String(maxResults) });
  const path = `/activity/users/${encodeURIComponent(
    email
  )}/applications/login?${query.toString()}`;
  const url = `https://admin.googleapis.com/admin/reports/v1${path}`;

  let token = await accessToken(key);
  let response = await fetch(url, {
    headers: { authorization: `Bearer ${token}` },
  });
  if (response.status === 401) {
    token = await accessToken(key, true);
    response = await fetch(url, {
      headers: { authorization: `Bearer ${token}` },
    });
  }
  if (!response.ok) throw await parseError(response);

  const data = await response.json();
  const events = [];
  for (const item of data.items || []) {
    for (const event of item.events || []) {
      const parameters = Object.fromEntries(
        (event.parameters || []).map((entry) => [entry.name, entry.value])
      );
      events.push({
        time: item.id?.time || "",
        actor: item.actor?.email || email,
        event: event.name || "",
        failure: parameters.login_failure_type || "",
        challenge: parameters.login_challenge_method || "",
        challengeStatus: parameters.login_challenge_status || "",
        loginType: parameters.login_type || "",
        ip: parameters.actor_ip_address || parameters.ip_address || "",
        parameters,
      });
    }
  }
  return events;
}

export async function insertUser(key, identity) {
  const body = {
    primaryEmail: identity.email,
    name: { givenName: identity.first, familyName: identity.last },
    password: identity.password,
    changePasswordAtNextLogin: false,
  };
  try {
    return await call(key, "/users", { method: "POST", body, spacing: 120 });
  } catch (error) {
    if (error.status === 409 || error.reason === "duplicate") {
      await call(key, `/users/${encodeURIComponent(identity.email)}`, {
        method: "PUT",
        body: { password: identity.password, changePasswordAtNextLogin: false },
      });
      return { reset: true };
    }
    throw error;
  }
}

export async function deleteUser(key, email) {
  try {
    await call(key, `/users/${encodeURIComponent(email)}`, { method: "DELETE" });
    return { deleted: true };
  } catch (error) {
    if (error.status === 404) return { deleted: false, missing: true };
    throw error;
  }
}

// Admin recovery: the key can set a new password without knowing the old one.
// It is the management credential of last resort when the original admin
// password is forgotten.
export async function setPassword(key, email, password) {
  const data = await call(key, `/users/${encodeURIComponent(email)}`, {
    method: "PATCH",
    body: { password, changePasswordAtNextLogin: false },
  });
  return {
    email: data?.primaryEmail || email,
    updated: Boolean(data),
  };
}

export async function domainStatus(key, domain) {
  const data = await call(key, "/customer/my_customer/domains");
  const list = data?.domains || [];
  const wanted = String(domain || "").toLowerCase();
  const target = list.find((entry) => (entry.domainName || "").toLowerCase() === wanted);
  return {
    present: Boolean(target),
    verified: Boolean(target?.verified),
    primary: Boolean(target?.isPrimary),
    domains: list.map((entry) => entry.domainName).filter(Boolean),
  };
}

// POST .../verificationCodes/generate mints a fresh set of ten and invalidates
// the previous one. The collection itself has no POST method, which is why the
// bare .../verificationCodes URL answers 404.
export async function mintCodes(key, email) {
  const base = `/users/${encodeURIComponent(email)}/verificationCodes`;

  try {
    await call(key, `${base}/generate`, { method: "POST", spacing: 200 });
  } catch (error) {
    // A user that already holds a full set can answer 400 "too many codes";
    // listing below still returns the valid ones. Anything else is real.
    if (error.status !== 400) throw error;
  }

  const data = await call(key, base, { spacing: 200 });
  const codes = (data?.items || [])
    .map((item) => item.verificationCode)
    .filter((code) => typeof code === "string" && code.length);

  if (!codes.length) {
    throw new AdminError(
      "Google returned no backup codes — the user is not enrolled in 2SV, or the tenant policy forbids verification codes",
      { status: 0, reason: "noCodes", payload: data }
    );
  }
  return codes;
}

export async function listCodes(key, email) {
  const data = await call(key, `/users/${encodeURIComponent(email)}/verificationCodes`, {
    spacing: 200,
  });
  return (data?.items || []).map((item) => item.verificationCode).filter(Boolean);
}
