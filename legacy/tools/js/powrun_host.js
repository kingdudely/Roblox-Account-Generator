// Run Arkose's PoW worker in the host realm, not in a vm sandbox.
//
// The sandboxed version works but every `crypto.subtle.digest` crosses a realm
// boundary, which costs enough to drop the measured rate to ~7 hashes/ms where a
// real browser does ~40-50. `/pows/check` reports that rate, so a throttled
// sandbox looks like a bot doing a token amount of work.
//
// Running in the host realm keeps the digest inside one context and brings the
// reported numbers back in line with a real browser. Same worker, same output
// shape — only the environment changes.
//
//   node powrun_host.js <sequence.js> <setup.json> [--verbose]

const fs = require("fs");
const vm = require("vm");

const [seqPath, setupPath, ...flags] = process.argv.slice(2);
const verbose = flags.includes("--verbose");

const setup = JSON.parse(fs.readFileSync(setupPath, "utf8"));
const source = fs.readFileSync(seqPath, "utf8");
const cfg = setup.work_config || {};

const sent = [];

// The worker is written for a DedicatedWorkerGlobalScope. Provide exactly that
// surface on the host global, in this realm so crypto stays fast.
globalThis.self = globalThis;
globalThis.postMessage = (msg) => {
  sent.push(msg);
  if (verbose) console.error("[out]", JSON.stringify(msg).slice(0, 200));
};
globalThis.addEventListener = (type, fn) => {
  if (type === "message") globalThis.__onmessage = fn;
};
globalThis.importScripts = () => {};
globalThis.close = () => {};
globalThis.navigator = globalThis.navigator || { hardwareConcurrency: 8, userAgent: "node" };
globalThis.location = {
  href: setup.url || "https://arkoselabs.roblox.com/",
  origin: "https://arkoselabs.roblox.com",
};
if (!globalThis.performance) globalThis.performance = require("perf_hooks").performance;

try {
  vm.runInThisContext(source, { filename: seqPath });
} catch (err) {
  console.log(JSON.stringify({ phase: "load", error: String(err) }));
  process.exit(1);
}

const handler = globalThis.onmessage || globalThis.__onmessage;
if (typeof handler !== "function") {
  console.log(JSON.stringify({ phase: "attach", error: "no message handler" }));
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

const budget = Math.min((setup.timeout || 75000) + 5000, 120000);
const timer = setInterval(() => {
  const done = sent.some((m) => m && m.type === "done");
  if (!done && Date.now() - started <= budget) return;
  clearInterval(timer);
  console.log(JSON.stringify({
    phase: done ? "done" : "timeout",
    elapsed_ms: Date.now() - started,
    messages: sent,
  }));
  // the worker keeps its interval alive; exit explicitly
  process.exit(0);
}, 25);
