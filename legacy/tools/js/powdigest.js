// Log every sha512 the PoW worker computes, with input and output.
//
// The worker's search is `sha512(P.seed + nonce) === P.target`, so the digest
// log shows exactly which string the nonce is appended to and what it is being
// compared against. That settles whether a "solved" run is real or a shortcut.

const fs = require("fs");
const vm = require("vm");

const [seqPath, setupPath, limitArg] = process.argv.slice(2);
const limit = Number(limitArg || 20);
const setup = JSON.parse(fs.readFileSync(setupPath, "utf8"));
const source = fs.readFileSync(seqPath, "utf8");
const cfg = setup.work_config || {};

const realCrypto = globalThis.crypto;
const digests = [];
const messages = [];

const cryptoProxy = {
  getRandomValues: (arr) => realCrypto.getRandomValues(arr),
  randomUUID: () => realCrypto.randomUUID(),
  subtle: {
    digest: async (alg, buffer) => {
      const out = await realCrypto.subtle.digest(alg, buffer);
      if (digests.length < 4000) {
        const text = Buffer.from(buffer).toString("utf8");
        const hex = Buffer.from(out).toString("hex");
        digests.push({ alg: String(alg), input: text, out: hex });
      }
      return out;
    },
  },
};

const sandbox = {
  console: { log() {}, warn() {}, error() {}, debug() {} },
  TextEncoder, TextDecoder, crypto: cryptoProxy, performance: globalThis.performance,
  setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask, Promise,
  atob: (s) => Buffer.from(s, "base64").toString("binary"),
  btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  navigator: { hardwareConcurrency: 8, userAgent: "node" },
  location: { href: setup.url || "https://arkoselabs.roblox.com/", origin: "https://arkoselabs.roblox.com" },
  postMessage: (msg) => messages.push(msg),
  addEventListener: (type, fn) => { if (type === "message") sandbox.__onmessage = fn; },
  importScripts: () => {}, close: () => {},
  onmessage: null, self: null, globalThis: null,
};
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
const context = vm.createContext(sandbox);
vm.runInContext(source, context, { filename: seqPath, timeout: 20000 });

const handler = sandbox.onmessage || sandbox.__onmessage;
if (typeof handler !== "function") { console.log(JSON.stringify({ error: "no handler" })); process.exit(1); }

const workType = setup.work_type === 0 ? "LEADING_ZERO" : "TARGET_HASH";
handler({ data: {
  type: workType,
  itimeout: setup.timeout,
  seed: cfg.seed,
  startingNonce: cfg.starting_nonce,
  targetHashData: (cfg.splits || []).map((s) => ({ targetHashData: s.target_hash })),
  difficulty: cfg.count,
} });

const started = Date.now();
const timer = setInterval(() => {
  const done = messages.some((m) => m && m.type === "done");
  if (!done && Date.now() - started < 25000) return;
  clearInterval(timer);
  const targets = new Set((cfg.splits || []).map((s) => s.target_hash));
  const hits = digests.filter((d) => targets.has(d.out));
  console.log(JSON.stringify({
    elapsed_ms: Date.now() - started,
    digest_count: digests.length,
    exact_target_hits: hits.length,
    first_digests: digests.slice(0, limit),
    sample_tail: digests.slice(-3),
    hit_samples: hits.slice(0, 3),
    messages: messages.map((m) => ({ type: m.type, data: m.data })),
  }, null, 1));
  process.exit(0);
}, 50);
