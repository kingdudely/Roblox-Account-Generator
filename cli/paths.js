import os from "node:os";
import path from "node:path";

function resolveHome() {
  const override = process.env.RAC_HOME;
  if (override && override.trim()) return path.resolve(override.trim());
  return path.join(os.homedir(), ".local", "share", "roblox-alt-creator");
}

export const APP_HOME = resolveHome();
export const STATE_PATH = path.join(APP_HOME, "state.json");
export const PROFILE_ROOT = path.join(APP_HOME, "profiles");
export const DEBUG_ROOT = path.join(process.cwd(), "debug");

export function profileDir(name = "setup") {
  const safe = String(name || "setup")
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!safe || safe === "." || safe === "..") {
    throw new Error(`invalid profile name: ${name}`);
  }
  return path.join(PROFILE_ROOT, safe);
}

export function debugDir(label) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  return path.join(DEBUG_ROOT, `${stamp}-${String(label || "debug").replace(/[^a-zA-Z0-9._-]+/g, "-")}`);
}
