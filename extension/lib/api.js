// language: JavaScript, file: lib/api.js

export const api = globalThis.browser ?? globalThis.chrome ?? {};
export const isFirefox = typeof api.contextualIdentities !== "undefined";

export const STATE_KEY = "rac.state";
export const LOG_KEY = "rac.log";
export const LOG_CAP = 2000;

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function uid(prefix = "id") {
  const rand = Math.random().toString(36).slice(2, 10);
  const stamp = Date.now().toString(36).slice(-4);
  return `${prefix}_${stamp}${rand}`;
}

export function withTimeout(promise, ms, label = "operation") {
  let timer = null;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`${label} timed out after ${Math.round(ms / 1000)}s`)),
      ms
    );
  });
  return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

export async function mapLimit(items, limit, worker) {
  const list = [...items];
  const results = new Array(list.length);
  let cursor = 0;
  let failed = null;

  async function pump() {
    while (cursor < list.length) {
      if (failed) return;
      const index = cursor++;
      try {
        results[index] = await worker(list[index], index);
      } catch (err) {
        failed = err;
        throw err;
      }
    }
  }

  const width = Math.max(1, Math.min(limit, list.length));
  await Promise.all(Array.from({ length: width }, pump));
  return results;
}

// A promise-based semaphore. Google's directory API answers 403 quotaExceeded
// long before a naive fan-out would notice.
export class Semaphore {
  constructor(width) {
    this.width = Math.max(1, width);
    this.active = 0;
    this.queue = [];
  }

  async acquire() {
    if (this.active < this.width) {
      this.active += 1;
      return;
    }
    await new Promise((resolve) => this.queue.push(resolve));
    this.active += 1;
  }

  release() {
    this.active -= 1;
    const next = this.queue.shift();
    if (next) next();
  }

  async run(fn) {
    await this.acquire();
    try {
      return await fn();
    } finally {
      this.release();
    }
  }
}
