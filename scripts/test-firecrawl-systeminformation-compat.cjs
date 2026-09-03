#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const shim = path.resolve(
  __dirname,
  "../deploy/kubernetes/overlays/sandboxed/firecrawl-systeminformation-compat.cjs",
);
let sample = { currentLoad: Number.NaN, marker: "preserved" };
let shouldThrow = false;
const warnings = [];
const systeminformation = {
  async currentLoad() {
    if (shouldThrow) {
      throw new Error("unsupported sandbox sample");
    }
    return sample;
  },
};

vm.runInNewContext(fs.readFileSync(shim, "utf8"), {
  console: { warn: (message) => warnings.push(message) },
  module: { exports: {} },
  exports: {},
  require(name) {
    assert.equal(name, "systeminformation");
    return systeminformation;
  },
});

(async () => {
  const nonFinite = await systeminformation.currentLoad();
  assert.equal(nonFinite.currentLoad, 0);
  assert.equal(nonFinite.marker, "preserved");

  sample = { currentLoad: 37.5, marker: "unchanged" };
  const finite = await systeminformation.currentLoad();
  assert.equal(finite.currentLoad, 37.5);
  assert.equal(finite.marker, "unchanged");

  shouldThrow = true;
  const unavailable = await systeminformation.currentLoad();
  assert.equal(unavailable.currentLoad, 0);

  assert.equal(warnings.length, 1, "fallback warning must be rate-limited per process");
  assert.match(warnings[0], /using 0% advisory CPU load/);
  console.log("Firecrawl sandbox CPU compatibility shim: PASS");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
