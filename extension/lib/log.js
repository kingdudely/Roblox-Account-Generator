// language: JavaScript, file: lib/log.js

import { api } from "./api.js";

export function send(text, level = "info") {
  const message = { type: "log", level, text: String(text) };
  try {
    const result = api.runtime.sendMessage(message);
    if (result && typeof result.catch === "function") result.catch(() => {});
  } catch (_) {
    // extension shutting down
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
