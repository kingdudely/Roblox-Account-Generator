// language: JavaScript, file: lib/log.js

import { api } from "./api.js";

// Every page that emits log lines stamps them with this. The runner appends its
// own lines to the console immediately, and the background broadcasts them back
// for persistence; the id is what lets the runner ignore its own echo.
export const CLIENT_ID = `c_${Math.random().toString(36).slice(2, 10)}`;

export function send(text, level = "info") {
  const message = { type: "log", level, text: String(text), cid: CLIENT_ID };
  try {
    const result = api.runtime.sendMessage(message);
    if (result && typeof result.catch === "function") result.catch(() => {});
    return true;
  } catch (_) {
    // extension shutting down, or a background that never registered: the
    // caller still owns the local copy, so the console is not blind.
    return false;
  }
}

export const log = {
  info: (text) => send(text, "info"),
  ok: (text) => send(text, "ok"),
  warn: (text) => send(text, "warn"),
  error: (text) => send(text, "error"),
  step: (text) => send(text, "step"),
};

export function stamp(ms) {
  const date = new Date(ms);
  return date.toTimeString().slice(0, 8);
}
