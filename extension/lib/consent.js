// language: JavaScript, file: lib/consent.js
// The one-time OAuth consent that turns an OAuth client into a key.
// Redirect target has no server behind it; the navigation URL is what matters.

import { api, sleep } from "./api.js";

export const SCOPES = [
  "https://www.googleapis.com/auth/admin.directory.user",
  "https://www.googleapis.com/auth/admin.directory.user.security",
  "https://www.googleapis.com/auth/admin.directory.domain.readonly",
  // Read the login audit. This is what gives a failed lane Google's own
  // "login failure type" instead of a guess from the challenge page.
  "https://www.googleapis.com/auth/admin.reports.audit.readonly",
  // Read the effective 2SV policy. The Admin console is the source of truth,
  // but the API is what lets the runner check it without a browser.
  "https://www.googleapis.com/auth/cloud-identity.policies.readonly",
];

const AUTH = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN = "https://oauth2.googleapis.com/token";
const REDIRECT = "http://localhost:8765/";

function base64url(bytes) {
  let binary = "";
  for (const byte of new Uint8Array(bytes)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function makeVerifier() {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~";
  const bytes = crypto.getRandomValues(new Uint8Array(64));
  let out = "";
  for (const byte of bytes) out += alphabet[byte % alphabet.length];
  return out;
}

export async function challengeFor(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(digest);
}

export function authoriseUrl(clientId, verifier) {
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: REDIRECT,
    response_type: "code",
    scope: SCOPES.join(" "),
    access_type: "offline",
    prompt: "consent",
    include_granted_scopes: "true",
    code_challenge: verifier,
    code_challenge_method: "S256",
  });
  return `${AUTH}?${params.toString()}`;
}

// Accepts the JSON Google gives you for a Desktop-app OAuth client, or a bare
// {client_id, client_secret} pair.
export function parseClient(text) {
  let data;
  try {
    data = JSON.parse(String(text || "").trim());
  } catch (error) {
    throw new Error(`client JSON is not valid JSON (${error.message})`);
  }
  const source = data.installed || data.web || data;
  if (!source?.client_id) throw new Error("client JSON has no client_id");
  if (!source?.client_secret) {
    throw new Error("this client has no secret; use a Desktop app client");
  }
  return { client_id: source.client_id, client_secret: source.client_secret };
}

export async function waitForCode(clientId, verifier, timeout = 600_000) {
  const url = authoriseUrl(clientId, await challengeFor(verifier));

  const tab = await api.tabs.create({ url, active: true });
  const deadline = Date.now() + timeout;
  let code = null;

  const onNavigate = (details) => {
    if (details.tabId !== tab.id) return;
    if (details.url.startsWith(REDIRECT) && details.url.includes("code=")) {
      code = new URL(details.url).searchParams.get("code");
    }
  };

  api.webNavigation.onBeforeNavigate.addListener(onNavigate);
  try {
    while (Date.now() < deadline && !code) {
      const current = await api.tabs.get(tab.id).catch(() => null);
      if (!current) break;
      if ((current.url || "").startsWith(REDIRECT) && current.url.includes("code=")) {
        code = new URL(current.url).searchParams.get("code");
        break;
      }
      await sleep(600);
    }
  } finally {
    api.webNavigation.onBeforeNavigate.removeListener(onNavigate);
    try {
      await api.tabs.remove(tab.id);
    } catch (_) {
      // already gone
    }
  }

  if (!code) throw new Error("no authorization code captured");
  return code;
}

export async function exchangeCode(client, code, verifier) {
  const body = new URLSearchParams({
    client_id: client.client_id,
    client_secret: client.client_secret,
    code,
    code_verifier: verifier,
    grant_type: "authorization_code",
    redirect_uri: REDIRECT,
  });
  const response = await fetch(TOKEN, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`token exchange failed: ${data.error_description || data.error || response.status}`);
  }
  if (!data.refresh_token) {
    throw new Error("no refresh token returned; re-consent with prompt=consent");
  }
  return {
    client_id: client.client_id,
    client_secret: client.client_secret,
    refresh_token: data.refresh_token,
    scopes: SCOPES,
    token_uri: TOKEN,
  };
}
