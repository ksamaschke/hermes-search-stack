"use strict";

// Firecrawl's admission guard calls systeminformation.currentLoad(). Some
// sandbox runtimes (notably gVisor/runsc) expose enough /proc data for the
// library to load but return NaN for currentLoad. Firecrawl then rejects every
// queue attempt because NaN is below no configured threshold. Keep Kubernetes'
// hard CPU limit and Firecrawl's memory gate, but fail this advisory sample
// open instead of stalling the whole queue.
const systeminformation = require("systeminformation");
const currentLoad = systeminformation.currentLoad.bind(systeminformation);
let warned = false;

function fallback(reason) {
  if (!warned) {
    console.warn(
      `[firecrawl-systeminformation-compat] ${reason}; using 0% advisory CPU load`,
    );
    warned = true;
  }
  return { currentLoad: 0 };
}

systeminformation.currentLoad = async (...args) => {
  try {
    const sample = await currentLoad(...args);
    if (!sample || !Number.isFinite(sample.currentLoad)) {
      return { ...(sample || {}), ...fallback("non-finite CPU sample") };
    }
    return sample;
  } catch (_error) {
    return fallback("CPU sample unavailable");
  }
};
