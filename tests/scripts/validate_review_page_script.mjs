#!/usr/bin/env node
/**
 * Smoke-test the Native Review inline script: syntax + renderNative() with a mock payload.
 * Usage: node tests/scripts/validate_review_page_script.mjs <path-to-script.js>
 */
import fs from "node:fs";
import vm from "node:vm";

const scriptPath = process.argv[2];
if (!scriptPath) {
  console.error("usage: node validate_review_page_script.mjs <script.js>");
  process.exit(2);
}

const rawScript = fs.readFileSync(scriptPath, "utf8");
// Drop auto-polling so we can call renderNative deterministically.
const script = rawScript
  .replace(/\n\s*checkReviewServer\(\);\s*\n\s*loadNativeData\(\);\s*\n\s*setInterval\(checkReviewServer,\s*2000\);\s*$/, "\n");

const elements = new Map();

function makeElement(id) {
  const listeners = [];
  return {
    id,
    value: "",
    textContent: id === "default-instrumental" ? "clean" : "",
    className: "",
    innerHTML: "",
    open: false,
    disabled: false,
    dataset: {},
    classList: {
      toggle() {},
      add() {},
    },
    style: {},
    querySelector(sel) {
      if (sel === "textarea[data-field='text']") {
        return {
          value: "hello",
          dataset: { originalValue: "hello", originalStart: "1", originalEnd: "2" },
        };
      }
      if (sel?.includes("data-field='start_min'")) return { value: "0" };
      if (sel?.includes("data-field='start_sec'")) return { value: "1" };
      if (sel?.includes("data-field='start_ms'")) return { value: "0" };
      if (sel?.includes("data-field='end_min'")) return { value: "0" };
      if (sel?.includes("data-field='end_sec'")) return { value: "2" };
      if (sel?.includes("data-field='end_ms'")) return { value: "0" };
      if (sel === ".dirty-badge") return { textContent: "clean" };
      if (sel === ".duration-cell") return { textContent: "—" };
      return null;
    },
    querySelectorAll(sel) {
      if (sel === "input, textarea") return [];
      if (sel === "tr[data-segment-index]") return [];
      return [];
    },
    addEventListener(type, fn) {
      listeners.push({ type, fn });
    },
    remove() {},
    insertAdjacentHTML() {},
    closest() {
      return null;
    },
  };
}

global.document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeElement(id));
    return elements.get(id);
  },
  querySelector() {
    return null;
  },
  querySelectorAll() {
    return [];
  },
  addEventListener() {},
};

global.fetch = async () => ({
  ok: true,
  json: async () => ({ ready: false }),
});

global.localStorage = {
  getItem: () => null,
  setItem() {},
};

const mockPayload = {
  segments: [
    { text: "Behind the red door", start_time: 56.56, end_time: 58.64 },
    { text: "In American skin", start_time: 59.62, end_time: 61.98 },
  ],
  original_segment_texts: ["Behind the red door", "In American skin"],
  original_segments: [{ text: "Behind the red door" }, { text: "In American skin" }],
  canonical_lyrics_lines: ["Behind the red door in american skin", "Next line"],
  canonical_lines_aligned: ["Behind the red door in american skin", "Behind the red door in american skin"],
  tail_junk_indexes: [],
  canonical_lyrics_source: "/tmp/lyrics.txt",
  canonical_lyrics_title: "Artist — Title",
  review_debug: { corrected_segments_count: 2, display_segments_count: 2, payload_keys: ["corrected_segments"] },
  alignment_debug: { tail_junk_count: 0, aligned_segment_count: 2 },
  instrumental_options: [{ id: "clean", label: "Clean" }],
  metadata: { artist: "Artist", title: "Title" },
};

const sandbox = {
  document,
  fetch,
  localStorage,
  console,
  setInterval() {},
};

try {
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox, { filename: "review-inline.js", timeout: 5000 });
  for (const fn of ["renderNative", "applyResolvedLyricsToRows", "removeTailJunkRows", "collectSegmentEdits"]) {
    if (typeof sandbox[fn] !== "function") {
      throw new Error(`${fn} is not defined after script load`);
    }
  }
  sandbox.renderNative(mockPayload);
  const native = document.getElementById("native-review");
  if (!native.innerHTML.includes("Lyric segments")) {
    throw new Error("renderNative did not populate native-review table");
  }
  if (native.innerHTML.includes("undefined")) {
    throw new Error("renderNative output contains the string 'undefined'");
  }
  sandbox.applyResolvedLyricsToRows();
  sandbox.removeTailJunkRows();
  sandbox.collectSegmentEdits();
  console.log("OK");
} catch (err) {
  console.error(String(err?.stack || err));
  process.exit(1);
}
