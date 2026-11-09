import readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";

const useColor =
  process.stdout.isTTY && !process.env.NO_COLOR && process.env.TERM !== "dumb";

function paint(code, text) {
  if (!useColor) return text;
  return `\u001b[${code}m${text}\u001b[0m`;
}

export const color = {
  dim: (text) => paint("2", text),
  bold: (text) => paint("1", text),
  red: (text) => paint("31", text),
  green: (text) => paint("32", text),
  yellow: (text) => paint("33", text),
  blue: (text) => paint("34", text),
  magenta: (text) => paint("35", text),
  cyan: (text) => paint("36", text),
};

function stamp() {
  return new Date().toTimeString().slice(0, 8);
}

export function info(text) {
  console.log(`${color.dim(stamp())} ${text}`);
}
export function step(text) {
  console.log(`${color.dim(stamp())} ${color.cyan(">")} ${text}`);
}
export function ok(text) {
  console.log(`${color.dim(stamp())} ${color.green("ok")} ${text}`);
}
export function warn(text) {
  console.error(`${color.dim(stamp())} ${color.yellow("warn")} ${text}`);
}
export function error(text) {
  console.error(`${color.dim(stamp())} ${color.red("error")} ${text}`);
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function mask(value, keep = 6) {
  const text = String(value || "");
  if (!text) return "";
  if (text.length <= keep * 2) return `${text.slice(0, 2)}…`;
  return `${text.slice(0, keep)}…${text.slice(-keep)}`;
}

export function oneLine(value, max = 120) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

export function table(rows, columns) {
  if (!rows.length) return "";
  const widths = columns.map((column) => {
    const header = String(column.label || "");
    const width = rows.reduce(
      (max, row) => Math.max(max, String(row[column.key] ?? "").length),
      header.length
    );
    return Math.min(column.max || width, Math.max(header.length, width));
  });
  const line = (cells) =>
    cells
      .map((cell, index) => String(cell).slice(0, widths[index]).padEnd(widths[index]))
      .join("  ")
      .trimEnd();
  const head = line(columns.map((column) => column.label));
  const body = rows.map((row) => line(columns.map((column) => row[column.key] ?? "")));
  return [color.dim(head), ...body].join("\n");
}

export async function ask(question, fallback = "") {
  const rl = readline.createInterface({ input, output });
  try {
    const answer = await rl.question(question);
    return answer.trim() || fallback;
  } finally {
    rl.close();
  }
}

export async function askSecret(question) {
  if (!process.stdin.isTTY) return ask(question);
  const rl = readline.createInterface({ input, output });
  try {
    const answer = await rl.question(question);
    return answer.trim();
  } finally {
    rl.close();
  }
}

export async function confirm(question) {
  const answer = (await ask(`${question} [y/N] `)).toLowerCase();
  return answer === "y" || answer === "yes";
}

export async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

export function parseInteger(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : fallback;
}

export function duration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes ? `${minutes}m ${rest}s` : `${rest}s`;
}

export function isoNow() {
  return new Date().toISOString();
}
