#!/usr/bin/env node
import { main } from "../cli/main.js";
import { error } from "../cli/util.js";

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code || 0;
  })
  .catch((err) => {
    error(err?.message || String(err));
    if (process.env.RAC_DEBUG) console.error(err?.stack || err);
    process.exitCode = 1;
  });
