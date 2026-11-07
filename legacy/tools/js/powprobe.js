// Reveal exactly which fields Arkose's PoW worker reads from the `start` message.
//
// The worker is string-array obfuscated, so static reading is a dead end. A
// Proxy around `msg.data` logs every property access with the value the worker
// received — authoritative, and immune to renaming.

const fs = require("fs");
const vm = require("vm");

const [seqPath, setupPath] = process.argv.slice(2);
const setup = JSON.parse(fs.readFileSync(setupPath, "utf8"));
const source = fs.readFileSync(seqPath, "utf8");
const cfg = setup.work_config || {};

const sent = [];
const access = [];

const sandbox = {
  console: { log() {}, warn() {}, error() {}, debug() {} },
  TextEncoder, TextDecoder, crypto: globalThis.crypto, performance: globalThis.performance,
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
  atob: (s) => Buffer.from(s, "base64").toString("binary"),
  btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  navigator: { hardwareConcurrency: 8, userAgent: "node" },
  location: { href: setup.url || "https://arkoselabs.roblox.com/", origin: "https://arkoselabs.roblox.com" },
  postMessage: (msg) => sent.push(msg),
  addEventListener: (type, fn) => { if (type === "message") sandbox.__onmessage = fn; },
  importScripts: () => {}, close: () => {},
  onmessage: null, self: null, globalThis: null,
};
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
const context = vm.createContext(sandbox);
vm.runInContext(source, context, { filename: seqPath, timeout: 20000 });

const handler = sandbox.onmessage || sandbox.__onmessage;
if (typeof handler !== "function") {
  console.log(JSON.stringify({ error: "no handler" }));
  process.exit(1);
}

const trim = (v) => {
  try {
    const s = typeof v === "string" ? v : JSON.stringify(v);
    return s.length > 80 ? s.slice(0, 80) + "…" : s;
  } catch (e) { return String(v); }
};

const wrap = (obj, label) => new Proxy(obj, {
  get(target, prop, recv) {
    const value = Reflect.get(target, prop, recv);
    access.push({ at: label, key: String(prop), type: typeof value,
                  value: trim(value) });
    if (value && typeof value === "object" && !Array.isArray(value)) return wrap(value, label + "." + String(prop));
    if (Array.isArray(value)) {
      return new Proxy(value, {
        get(t, p, r) {
          const v = Reflect.get(t, p, r);
          if (String(p) === String(Number(p)) && v && typeof v === "object")
            return wrap(v, label + "." + String(prop) + "[" + p + "]");
          access.push({ at: label + "." + String(prop), key: String(p), type: typeof v, value: trim(v) });
          return v;
        },
      });
    }
    return value;
  },
  has(target, prop) { access.push({ at: label, key: "has:" + String(prop), type: "bool", value: "" }); return Reflect.has(target, prop); },
  ownKeys(target) { access.push({ at: label, key: "keys()", type: "keys", value: trim(Reflect.ownKeys(target)) }); return Reflect.ownKeys(target); },
});

const workType = setup.work_type === 0 ? "LEADING_ZERO" : "TARGET_HASH";
const start = {
  type: "start",
  data: {
    type: workType,
    itimeout: setup.timeout,
    seed: cfg.seed,
    startingNonce: cfg.starting_nonce,
    targetHashData: (cfg.splits || []).map((s) => ({ targetHashData: s.target_hash })),
    difficulty: cfg.count,
  },
};
start.data = wrap(start.data, "data");

const started = Date.now();
handler({ data: start });

const budget = 30000;
const timer = setInterval(() => {
  const done = sent.some((m) => m && m.type === "done");
  if (!done && Date.now() - started < budget) return;
  clearInterval(timer);
  console.log(JSON.stringify({
    elapsed_ms: Date.now() - started,
    access,
    messages: sent,
  }, null, 1));
  process.exit(0);
}, 50);
