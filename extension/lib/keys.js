// language: JavaScript, file: lib/keys.js

import { uid } from "./api.js";

export const SCOPES = [
  "https://www.googleapis.com/auth/admin.directory.user",
  "https://www.googleapis.com/auth/admin.directory.user.security",
  "https://www.googleapis.com/auth/admin.directory.domain.readonly",
];

function fingerprint(value) {
  let hash = 0;
  const text = String(value ?? "");
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 31 + text.charCodeAt(i)) | 0;
  }
  return Math.abs(hash).toString(36);
}

export class KeyError extends Error {
  constructor(message, source) {
    super(source ? `${source}: ${message}` : message);
    this.name = "KeyError";
    this.source = source || "";
  }
}

export function normaliseKey(raw, source = "inline") {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new KeyError("credential is not a JSON object", source);
  }

  // A file straight from the Cloud console arrives wrapped:
  // {"installed":{...}} for Desktop, {"web":{...}} for Web.
  const body = raw.installed || raw.web || raw;
  const type = body.type || raw.type;
  const kind = type === "service_account" ? "service_account" : "oauth";

  const key = {
    id: uid("key"),
    kind,
    domain: (body.domain || raw.domain || "").trim().toLowerCase() || null,
    admin: (body.admin || raw.admin || "").trim().toLowerCase() || null,
    client_id: body.client_id || null,
    client_secret: body.client_secret || null,
    refresh_token: body.refresh_token || null,
    client_email: body.client_email || null,
    private_key: body.private_key || null,
    token_uri: body.token_uri || "https://oauth2.googleapis.com/token",
    scopes: body.scopes || SCOPES,
    project_id: body.project_id || null,
    label: "",
    status: "new",
    message: "",
    seats: null,
    added_at: new Date().toISOString(),
  };

  if (kind === "oauth") {
    if (!key.client_id || !key.client_secret) {
      throw new KeyError(
        "OAuth client needs client_id and client_secret — drop the client_secret_*.json from the console",
        source
      );
    }
  } else if (!key.client_email || !key.private_key) {
    throw new KeyError(
      "service-account credential needs client_email and private_key",
      source
    );
  }

  if (kind === "service_account" && !key.admin && key.domain) {
    key.admin = `admin@${key.domain}`;
  }

  // A bare client is a valid tenant stub: it becomes usable after one consent
  // run, which swaps a refresh token into the same entry.
  key.needs_consent = kind === "oauth" && !key.refresh_token;
  if (key.needs_consent) {
    key.status = "client";
    key.message = "authorize once to mint a refresh token";
  }

  key.fingerprint = fingerprint(
    `${key.client_id || key.client_email}|${key.refresh_token || (key.private_key || "").slice(0, 64)}`
  );
  key.label =
    key.domain ||
    key.client_email ||
    (key.client_id ? `client ${key.client_id.slice(0, 26)}…` : `key ${key.fingerprint}`);
  return key;
}

// Sellers hand out JSON by hand, so a trailing comma is common enough to absorb.
function tryJson(text) {
  try {
    return JSON.parse(text);
  } catch (_) {
    try {
      return JSON.parse(text.replace(/,\s*([}\]])/g, "$1"));
    } catch (_) {
      return undefined;
    }
  }
}

// Accepts: a JSON array, one JSON object per line, a wrapper holding a
// credential, or a line of fields. The domain is optional everywhere -- it is
// detected from the tenant on the first API call.
export function parseKeysText(text) {
  const source = "pasted";
  const trimmed = String(text || "").replace(/^\uFEFF/, "").trim();
  if (!trimmed) throw new KeyError("nothing to import", source);

  const whole = tryJson(trimmed);
  let entries = [];

  if (Array.isArray(whole)) {
    entries = whole.map((entry, index) => ({ entry, source: `${source}[${index}]` }));
  } else if (whole && typeof whole === "object") {
    if (Array.isArray(whole.keys)) {
      entries = whole.keys.map((entry, index) => ({ entry, source: `${source}.keys[${index}]` }));
    } else if (whole.credential && typeof whole.credential === "object") {
      entries = [{
        entry: { ...whole.credential, domain: whole.domain || whole.credential.domain, admin: whole.admin },
        source,
      }];
    } else {
      entries = [{ entry: whole, source }];
    }
  } else {
    const lines = trimmed.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    entries = lines
      .filter((line) => !line.startsWith("#"))
      .map((line, index) => {
        const parsed = tryJson(line);
        if (parsed && typeof parsed === "object") {
          return { entry: parsed, source: `${source} line ${index + 1}` };
        }
        const parts = line.split(/\s+/);
        if (parts.length === 3) {
          return {
            entry: { client_id: parts[0], client_secret: parts[1], refresh_token: parts[2] },
            source: `${source} line ${index + 1}`,
          };
        }
        if (parts.length >= 4) {
          return {
            entry: {
              domain: parts[0],
              client_id: parts[1],
              client_secret: parts[2],
              refresh_token: parts.slice(3).join(" "),
            },
            source: `${source} line ${index + 1}`,
          };
        }
        throw new KeyError(
          "expected JSON, or: [domain] client_id client_secret refresh_token",
          `${source} line ${index + 1}`
        );
      });
  }

  const keys = [];
  const problems = [];
  const seen = new Set();

  for (const { entry, source: where } of entries) {
    try {
      const key = normaliseKey(entry, where);
      if (seen.has(key.fingerprint)) {
        problems.push(`${where}: duplicate key, skipped`);
        continue;
      }
      seen.add(key.fingerprint);
      keys.push(key);
    } catch (err) {
      problems.push(err.message || String(err));
    }
  }

  if (!keys.length) {
    throw new KeyError(problems.join("; ") || "no usable credentials found");
  }
  return { keys, problems };
}
