// Run Arkose's signed PoW worker outside a browser.
//
// The worker is plain JS: it takes a `start` message, grinds a nonce, posts
// `split_done` / `done`. It touches no DOM, so a Node VM context with a handful
// of shims is enough — no Chromium, no headless, ~40 MB instead of ~600 MB.
//
//   node powrun.js <sequence.js> <setup.json> [--verbose]
//
// Prints one JSON object per line on stdout: whatever the worker posts back.

const fs = require("fs");
const vm = require("vm");

const [seqPath, setupPath, ...flags] = process.argv.slice(2);
const verbose = flags.includes("--verbose");

const setup = JSON.parse(fs.readFileSync(setupPath, "utf8"));
const source = fs.readFileSync(seqPath, "utf8");
const cfg = setup.work_config || {};

const sent = [];
const errors = [];

const sandbox = {
  console: { log: (...a) => verbose && console.error("[worker]", ...a),
             warn: (...a) => verbose && console.error("[worker:warn]", ...a),
             error: (...a) => verbose && console.error("[worker:error]", ...a),
             debug: () => {} },
  TextEncoder,
  TextDecoder,
  crypto: globalThis.crypto,
  performance: globalThis.performance,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  queueMicrotask,
  atob: (s) => Buffer.from(s, "base64").toString("binary"),
  btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  navigator: { hardwareConcurrency: 8, userAgent: "node" },
  location: { href: setup.url || "https://arkoselabs.roblox.com/", origin: "https://arkoselabs.roblox.com" },
  postMessage: (msg) => { sent.push(msg); if (verbose) console.error("[out]", JSON.stringify(msg).slice(0, 200)); },
  addEventListener: (type, fn) => { if (type === "message") sandbox.__onmessage = fn; },
  importScripts: () => {},
  close: () => {},
  onmessage: null,
  self: null,
  globalThis: null,
};
sandbox.self = sandbox;
sandbox.globalThis = sandbox;

const context = vm.createContext(sandbox);
try {
  vm.runInContext(source, context, { filename: seqPath, timeout: 20000 });
} catch (err) {
  console.log(JSON.stringify({ phase: "load", error: String(err) }));
  process.exit(1);
}

const handler = sandbox.onmessage || sandbox.__onmessage;
if (typeof handler !== "function") {
  console.log(JSON.stringify({ phase: "attach", error: "no message handler",
                               keys: Object.keys(sandbox).filter((k) => typeof sandbox[k] === "function") }));
  process.exit(1);
}

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

const started = Date.now();
try {
  handler({ data: start });
} catch (err) {
  console.log(JSON.stringify({ phase: "start", error: String(err) }));
  process.exit(1);
}

// the worker is async; poll until it reports done or the timeout passes
const budget = Math.min((setup.timeout || 75000) + 5000, 120000);
const timer = setInterval(() => {
  const done = sent.some((m) => m && m.type === "done");
  if (done || Date.now() - started > budget) {
    clearInterval(timer);
    console.log(JSON.stringify({
      phase: "done",
      elapsed_ms: Date.now() - started,
      messages: sent,
      errors,
    }));
    process.exit(0);
  }
}, 50);
