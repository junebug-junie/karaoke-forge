from __future__ import annotations

import html
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import DEFAULT_INSTRUMENTAL_SELECTION, PUBLIC_BASE_PATH
from .review_contract import review_payload_summary, segments_preview_texts, segments_text_digest
from .review_gate import mark_review_complete

router = APIRouter()

REVIEW_UPSTREAM = "http://127.0.0.1:8000"
REVIEW_PATH = "/app/jobs/local/review"
REVIEW_API_READY_PATH = "/api/correction-data"
SEGMENT_TEXT_KEYS = ("text", "corrected_text", "lyrics", "line")
SEGMENT_START_KEYS = ("start", "start_time")
SEGMENT_END_KEYS = ("end", "end_time")
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
STRIP_RESPONSE_HEADERS = HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}
ABSOLUTE_REVIEW_PREFIXES = (
    "/_next/",
    "/api/",
    "/app/",
    "/assets/",
    "/audio/",
    "/files/",
    "/images/",
    "/lyrics/",
    "/media/",
    "/static/",
)
LOCALE_PREFIXES = ("/en/", "/es/", "/de/", "/fr/", "/it/", "/ja/", "/ko/", "/pt/", "/zh/")


def public_url(path: str = "/") -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{PUBLIC_BASE_PATH}{path}" if PUBLIC_BASE_PATH else path


def proxy_url(path: str = "/") -> str:
    if not path.startswith("/"):
        path = "/" + path
    return public_url("/review-proxy" + path)


def rewrite_body(content: bytes, content_type: str) -> bytes:
    if not (
        "text/html" in content_type
        or "text/css" in content_type
        or "javascript" in content_type
        or "application/json" in content_type
    ):
        return content

    text = content.decode("utf-8", errors="replace")
    prefix = public_url("/review-proxy")
    replacements = {
        'href="/': f'href="{prefix}/',
        "href='/": f"href='{prefix}/",
        'src="/': f'src="{prefix}/',
        "src='/": f"src='{prefix}/",
        'action="/': f'action="{prefix}/',
        "action='/": f"action='{prefix}/",
        'url("/': f'url("{prefix}/',
        "url('/": f"url('{prefix}/",
        "url(/": f"url({prefix}/",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    for absolute_prefix in ABSOLUTE_REVIEW_PREFIXES + LOCALE_PREFIXES:
        proxied_prefix = prefix + absolute_prefix
        for quote in ('"', "'", "`"):
            text = text.replace(f"{quote}{absolute_prefix}", f"{quote}{proxied_prefix}")
    return text.encode("utf-8")


def _looks_like_segment_list(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(item, dict) for item in value)


def _find_named_segment_list(data: Any, keys: tuple[str, ...], *, path: str = "$") -> tuple[str, list[Any], str] | None:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if _looks_like_segment_list(value):
                return key, value, f"{path}.{key}"
        for key, value in data.items():
            found = _find_named_segment_list(value, keys, path=f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(data, list):
        for idx, value in enumerate(data):
            found = _find_named_segment_list(value, keys, path=f"{path}[{idx}]")
            if found is not None:
                return found
    return None


def _find_corrected_segments(data: Any) -> tuple[list[Any], str] | None:
    found = _find_named_segment_list(data, ("corrected_segments",))
    if found is None:
        return None
    _key, segment_list, path = found
    return segment_list, path


def _find_display_segments(data: Any) -> tuple[list[Any], str, str] | None:
    corrected = _find_named_segment_list(data, ("corrected_segments",))
    if corrected is not None:
        key, segment_list, path = corrected
        return segment_list, key, path
    fallback = _find_named_segment_list(data, ("segments", "lyrics_segments"))
    if fallback is not None:
        key, segment_list, path = fallback
        return segment_list, key, path
    return None


def _extract_segment_dicts(segment_list: list[Any] | None) -> list[dict[str, Any]]:
    if segment_list is None:
        return []
    return [item for item in segment_list if isinstance(item, dict)]


def _segment_text(segment: dict[str, Any]) -> str:
    for key in SEGMENT_TEXT_KEYS:
        value = segment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    words = segment.get("words")
    if isinstance(words, list):
        text = " ".join(
            str(word.get("text") or word.get("word") or "").strip()
            for word in words
            if isinstance(word, dict)
        ).strip()
        if text:
            return text
    return ""


def _segment_time(segment: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = segment.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _parse_timestamp_seconds(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if ":" not in text:
        try:
            return round(float(text), 3)
        except ValueError:
            return None

    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        return round(minutes * 60 + seconds, 3)
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return round(hours * 3600 + minutes * 60 + seconds, 3)
    return None


def _set_segment_text(segment: dict[str, Any], text: str) -> bool:
    updated = False
    for key in SEGMENT_TEXT_KEYS:
        if key in segment:
            segment[key] = text
            updated = True
    if not updated:
        segment["text"] = text
        updated = True
    return updated


def _set_segment_time(segment: dict[str, Any], keys: tuple[str, ...], value: float) -> bool:
    updated = False
    for key in keys:
        if key in segment:
            segment[key] = value
            updated = True
    if not updated:
        segment[keys[0]] = value
        updated = True
    return updated


def _apply_text_and_timing(segment: dict[str, Any], edit: dict[str, Any]) -> tuple[int, int]:
    text_edits = 0
    timing_edits = 0

    text = str(edit.get("text") or "").strip()
    if text and text != _segment_text(segment):
        if _set_segment_text(segment, text):
            text_edits += 1

    start = _parse_timestamp_seconds(edit.get("start"))
    if start is not None and start != _segment_time(segment, SEGMENT_START_KEYS):
        if _set_segment_time(segment, SEGMENT_START_KEYS, start):
            timing_edits += 1

    end = _parse_timestamp_seconds(edit.get("end"))
    if end is not None and end != _segment_time(segment, SEGMENT_END_KEYS):
        if _set_segment_time(segment, SEGMENT_END_KEYS, end):
            timing_edits += 1

    return text_edits, timing_edits


def _find_parallel_corrections(data: Any, expected_len: int) -> tuple[list[Any], str] | None:
    found = _find_named_segment_list(data, ("corrections",))
    if found is None:
        return None
    _key, corrections, path = found
    if len(corrections) != expected_len:
        return None
    return corrections, path


def _apply_segment_edits_to_corrected_segments(data: Any, edits: Any) -> dict[str, Any]:
    corrected = _find_corrected_segments(data)
    if corrected is None:
        return {
            "corrected_segments_found": False,
            "corrected_segments_path": None,
            "before_corrected_segments_count": 0,
            "after_corrected_segments_count": 0,
            "removed_indexes": [],
            "skipped_indexes": [],
            "text_edit_count": 0,
            "timing_edit_count": 0,
            "corrections_updated": 0,
            "corrections_path": None,
        }

    corrected_segments, corrected_path = corrected
    before_count = len(corrected_segments)
    corrections = _find_parallel_corrections(data, before_count)
    corrections_list = corrections[0] if corrections else None
    corrections_path = corrections[1] if corrections else None

    result: dict[str, Any] = {
        "corrected_segments_found": True,
        "corrected_segments_path": corrected_path,
        "before_corrected_segments_count": before_count,
        "after_corrected_segments_count": before_count,
        "removed_indexes": [],
        "skipped_indexes": [],
        "text_edit_count": 0,
        "timing_edit_count": 0,
        "corrections_updated": 0,
        "corrections_path": corrections_path,
    }
    if not isinstance(edits, list):
        return result

    delete_indexes: set[int] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        try:
            idx = int(edit.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= before_count:
            result["skipped_indexes"].append(idx)
            continue
        if bool(edit.get("delete")):
            delete_indexes.add(idx)
            continue

        segment = corrected_segments[idx]
        if not isinstance(segment, dict):
            result["skipped_indexes"].append(idx)
            continue
        text_count, timing_count = _apply_text_and_timing(segment, edit)
        result["text_edit_count"] += text_count
        result["timing_edit_count"] += timing_count

        if corrections_list is not None and isinstance(corrections_list[idx], dict):
            correction_text, correction_timing = _apply_text_and_timing(corrections_list[idx], edit)
            if correction_text or correction_timing:
                result["corrections_updated"] += 1

    for idx in sorted(delete_indexes, reverse=True):
        if idx < 0 or idx >= len(corrected_segments):
            result["skipped_indexes"].append(idx)
            continue
        del corrected_segments[idx]
        if corrections_list is not None and idx < len(corrections_list):
            del corrections_list[idx]
            result["corrections_updated"] += 1
        result["removed_indexes"].append(idx)

    result["removed_indexes"] = sorted(result["removed_indexes"])
    result["skipped_indexes"] = sorted(set(result["skipped_indexes"]))
    result["after_corrected_segments_count"] = len(corrected_segments)
    return result


def _review_contract_debug(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "raw_correction_payload_keys": [],
            "corrected_segments_count": 0,
            "corrected_segments_path": None,
            "display_segments_count": 0,
            "display_segments_source_key": None,
            "display_segments_path": None,
            "display_segments_relation": "payload_not_object",
        }

    corrected = _find_corrected_segments(payload)
    display = _find_display_segments(payload)
    corrected_list = corrected[0] if corrected else None
    corrected_path = corrected[1] if corrected else None
    display_list = display[0] if display else None
    display_key = display[1] if display else None
    display_path = display[2] if display else None

    if corrected_path and display_path == corrected_path:
        relation = "display_segments_is_corrected_segments"
    elif display_path:
        relation = "display_segments_is_derived_or_fallback"
    else:
        relation = "no_display_segments_found"

    return {
        "raw_correction_payload_keys": list(payload.keys()),
        "corrected_segments_count": len(corrected_list) if corrected_list is not None else 0,
        "corrected_segments_path": corrected_path,
        "display_segments_count": len(display_list) if display_list is not None else 0,
        "display_segments_source_key": display_key,
        "display_segments_path": display_path,
        "display_segments_relation": relation,
    }


async def _upstream_json(path: str, *, method: str = "GET", json_body: Any | None = None) -> tuple[int, Any]:
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        response = await client.request(
            method,
            REVIEW_UPSTREAM + path,
            headers={"accept-encoding": "identity"},
            json=json_body,
        )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"text": response.text[:4000]}
    return response.status_code, payload


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Review · Karaoke Forge</title>
  <link rel="stylesheet" href="__CSS_URL__" />
</head>
<body>
  <main class="review-main">
    <header>
      <a class="brand" href="__HOME_URL__">Karaoke Forge</a>
      <nav class="tabs">
        <a href="__HOME_URL__">Jobs</a>
        <a href="__REVIEW_URL__">Review</a>
      </nav>
    </header>
    <section class="panel review-panel">
      <h1>Native Review</h1>
      <p class="muted">Rows shown here are the upstream <code>corrected_segments</code> contract. Finish mutates that same array before karaoke-gen finalizes.</p>
      <div class="button-row">
        <span id="review-status" class="status queued">checking review API...</span>
        <button type="button" id="native-refresh">Refresh native data</button>
        <button type="button" id="apply-canonical">Apply canonical lyrics by line order</button>
        <button type="button" id="delete-after-canonical">Remove rows after final canonical lyric</button>
        <button type="button" id="native-complete">Finish with selected data</button>
        <button type="button" id="review-reload">Reload iframe debug</button>
      </div>
      <p class="muted">Default instrumental selection: <code id="default-instrumental">__DEFAULT_INSTRUMENTAL__</code></p>
      <section class="canonical-lyrics-panel">
        <h2>Canonical lyrics for this review</h2>
        <p class="muted">Paste the finite lyric script here for this active review only. Nothing is pulled from older jobs.</p>
        <textarea id="canonical-lyrics-text" class="canonical-lyrics-text" rows="10" placeholder="Paste one lyric line per row..."></textarea>
      </section>
      <div id="native-review" class="native-review">Loading review data...</div>
      <details class="debug-details" open>
        <summary>Payload contract debug</summary>
        <pre id="contract-json" class="live-log">No data yet.</pre>
      </details>
      <details class="debug-details">
        <summary>Raw correction data</summary>
        <pre id="native-json" class="live-log">No data yet.</pre>
      </details>
      <details class="debug-details">
        <summary>Iframe debug fallback</summary>
        <p><a class="button-link" href="__DIRECT_URL__" target="_blank" rel="noreferrer">Open proxied review directly</a></p>
        <iframe id="review-frame" class="review-frame" data-src="__IFRAME_SRC__" src="__IFRAME_SRC__"></iframe>
      </details>
    </section>
  </main>
  <script>
    const statusUrl = "__STATUS_URL__";
    const dataUrl = "__DATA_URL__";
    const completeUrl = "__COMPLETE_URL__";
    const canonicalStorageKey = "karaokeForge.nativeReview.canonicalLyrics";
    const frame = document.getElementById("review-frame");
    const statusEl = document.getElementById("review-status");
    const nativeEl = document.getElementById("native-review");
    const nativeJsonEl = document.getElementById("native-json");
    const contractJsonEl = document.getElementById("contract-json");
    const canonicalLyricsEl = document.getElementById("canonical-lyrics-text");
    const reloadButton = document.getElementById("review-reload");
    const nativeRefreshButton = document.getElementById("native-refresh");
    const nativeCompleteButton = document.getElementById("native-complete");
    const applyCanonicalButton = document.getElementById("apply-canonical");
    const deleteAfterCanonicalButton = document.getElementById("delete-after-canonical");
    let lastReady = null;
    let completionRequested = false;
    let removedSegmentIndexes = [];

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[ch]));
    }
    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }
    function parseCanonicalLines() {
      return canonicalLyricsEl.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    }
    function persistCanonicalLyrics() {
      try { localStorage.setItem(canonicalStorageKey, canonicalLyricsEl.value); } catch (err) {}
    }
    function restoreCanonicalLyrics() {
      try { canonicalLyricsEl.value = localStorage.getItem(canonicalStorageKey) || ""; } catch (err) { canonicalLyricsEl.value = ""; }
    }
    function segmentText(seg) {
      return seg.text || seg.corrected_text || seg.lyrics || seg.line || "";
    }
    function secondsValue(value) {
      const n = Number(value);
      return Number.isFinite(n) ? n : null;
    }
    function splitTimestamp(value) {
      const total = secondsValue(value);
      if (total === null) return { min: "", sec: "", ms: "" };
      const minutes = Math.floor(total / 60);
      const seconds = total - minutes * 60;
      const wholeSec = Math.floor(seconds);
      const ms = Math.round((seconds - wholeSec) * 1000);
      return { min: String(minutes), sec: String(wholeSec), ms: String(ms).padStart(3, "0") };
    }
    function timePartsToSeconds(min, sec, ms) {
      const minutes = Number(min) || 0;
      const seconds = Number(sec) || 0;
      const millis = Number(ms) || 0;
      return roundSeconds(minutes * 60 + seconds + millis / 1000);
    }
    function roundSeconds(value) {
      return Math.round(value * 1000) / 1000;
    }
    function formatDuration(start, end) {
      const delta = roundSeconds(end - start);
      if (!Number.isFinite(delta)) return "—";
      return `${delta.toFixed(3)}s`;
    }
    function readRowTimes(row) {
      const start = timePartsToSeconds(
        row.querySelector("[data-field='start_min']")?.value,
        row.querySelector("[data-field='start_sec']")?.value,
        row.querySelector("[data-field='start_ms']")?.value,
      );
      const end = timePartsToSeconds(
        row.querySelector("[data-field='end_min']")?.value,
        row.querySelector("[data-field='end_sec']")?.value,
        row.querySelector("[data-field='end_ms']")?.value,
      );
      return { start, end };
    }
    function renderTimeFields(side, seconds) {
      const parts = splitTimestamp(seconds);
      return `<span class="time-group"><input class="time-part" data-field="${side}_min" inputmode="numeric" value="${escapeHtml(parts.min)}" aria-label="${side} minutes" /><span>:</span><input class="time-part" data-field="${side}_sec" inputmode="numeric" value="${escapeHtml(parts.sec)}" aria-label="${side} seconds" /><span>.</span><input class="time-part" data-field="${side}_ms" inputmode="numeric" value="${escapeHtml(parts.ms)}" aria-label="${side} milliseconds" /></span>`;
    }
    function rowIsDirty(row) {
      const textEl = row.querySelector("textarea[data-field='text']");
      const { start, end } = readRowTimes(row);
      const originalStart = Number(textEl?.dataset.originalStart || 0);
      const originalEnd = Number(textEl?.dataset.originalEnd || 0);
      const textChanged = (textEl?.value.trim() || "") !== (textEl?.dataset.originalValue || "");
      const startChanged = roundSeconds(start) !== roundSeconds(originalStart);
      const endChanged = roundSeconds(end) !== roundSeconds(originalEnd);
      const deleteEl = row.querySelector("input[data-field='delete']");
      return Boolean(deleteEl?.checked || textChanged || startChanged || endChanged || row.classList.contains("lyrics-applied"));
    }
    function refreshRowState(row) {
      const dirty = rowIsDirty(row);
      row.classList.toggle("row-dirty", dirty);
      const badge = row.querySelector(".dirty-badge");
      if (badge) badge.textContent = dirty ? "edited" : "clean";
      const durationEl = row.querySelector(".duration-cell");
      if (durationEl) {
        const { start, end } = readRowTimes(row);
        durationEl.textContent = formatDuration(start, end);
      }
    }
    function attachRowListeners(row) {
      row.querySelectorAll("input, textarea").forEach(el => el.addEventListener("input", () => refreshRowState(row)));
      refreshRowState(row);
    }
    function collectSegmentEdits() {
      const edits = Array.from(document.querySelectorAll("tr[data-segment-index]")).map(row => {
        const textEl = row.querySelector("textarea[data-field='text']");
        const deleteEl = row.querySelector("input[data-field='delete']");
        const { start, end } = readRowTimes(row);
        return {
          index: Number(row.dataset.segmentIndex),
          delete: Boolean(deleteEl && deleteEl.checked),
          original_text: textEl?.dataset.originalValue || "",
          text: textEl?.value.trim() || "",
          original_start: textEl?.dataset.originalStart || "",
          original_end: textEl?.dataset.originalEnd || "",
          start: String(start),
          end: String(end),
        };
      });
      const coveredDeletes = new Set(edits.filter(edit => edit.delete).map(edit => edit.index));
      for (const idx of removedSegmentIndexes) {
        if (!coveredDeletes.has(idx)) {
          edits.push({ index: idx, delete: true, original_text: "", text: "", original_start: "", original_end: "", start: "", end: "" });
        }
      }
      return edits.filter(edit => edit.delete || edit.text !== edit.original_text || String(edit.start) !== String(edit.original_start) || String(edit.end) !== String(edit.original_end));
    }
    function setRowTextFromCanonical(row, canonicalLines) {
      const idx = Number(row.dataset.segmentIndex);
      const canonicalLine = canonicalLines[idx];
      if (!canonicalLine) return false;
      const textEl = row.querySelector("textarea[data-field='text']");
      if (!textEl) return false;
      textEl.value = canonicalLine;
      row.classList.add("lyrics-applied");
      refreshRowState(row);
      return true;
    }
    function applyCanonicalLyricsByLineOrder() {
      persistCanonicalLyrics();
      const canonicalLines = parseCanonicalLines();
      let applied = 0;
      document.querySelectorAll("tr[data-segment-index]").forEach(row => {
        const idx = Number(row.dataset.segmentIndex);
        if (idx >= canonicalLines.length) return;
        if (setRowTextFromCanonical(row, canonicalLines)) applied += 1;
      });
      setStatus(`applied ${applied} canonical lyric line(s); timings preserved`, "done");
    }
    function removeRowsAfterFinalCanonicalLyric() {
      persistCanonicalLyrics();
      const canonicalLines = parseCanonicalLines();
      const removedIndexes = [];
      document.querySelectorAll("tr[data-segment-index]").forEach(row => {
        const idx = Number(row.dataset.segmentIndex);
        if (idx >= canonicalLines.length) {
          removedIndexes.push(idx);
          row.remove();
        }
      });
      removedSegmentIndexes = Array.from(new Set([...removedSegmentIndexes, ...removedIndexes])).sort((a, b) => a - b);
      setStatus(`removed ${removedIndexes.length} tail row(s) after canonical line ${canonicalLines.length}; indexes ${removedIndexes.join(", ") || "none"}`, "done");
    }
    function reloadFrame(reason) {
      const base = frame.dataset.src;
      const sep = base.includes("?") ? "&" : "?";
      frame.src = base + sep + "kf_reload=" + Date.now();
      setStatus("review API ready — iframe reloaded (" + reason + ")", "running");
    }
    function renderNative(payload) {
      nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
      contractJsonEl.textContent = JSON.stringify(payload.review_debug || {}, null, 2);
      const segments = payload.segments || [];
      const instrumentals = payload.instrumental_options || [];
      const meta = payload.metadata || {};
      const debug = payload.review_debug || {};
      const originalTexts = payload.original_segment_texts || [];
      const title = [meta.artist, meta.title].filter(Boolean).join(" — ") || "Current review session";
      const canonicalLines = parseCanonicalLines();
      const rows = segments.length
        ? segments.map((seg, idx) => {
            const text = segmentText(seg);
            const originalText = originalTexts[idx] || segmentText(payload.original_segments?.[idx] || {});
            const canonicalLine = canonicalLines[idx] || "";
            const mismatch = canonicalLine && canonicalLine.trim() !== text.trim();
            const startSeconds = secondsValue(seg.start ?? seg.start_time ?? 0) ?? 0;
            const endSeconds = secondsValue(seg.end ?? seg.end_time ?? 0) ?? 0;
            return `<tr data-segment-index="${idx}" class="${mismatch ? "lyric-mismatch" : ""}"><td class="row-index">${idx + 1}</td><td>${renderTimeFields("start", startSeconds)}</td><td>${renderTimeFields("end", endSeconds)}</td><td class="duration-cell">${escapeHtml(formatDuration(startSeconds, endSeconds))}</td><td class="original-text">${escapeHtml(originalText || "—")}</td><td><textarea class="segment-edit" data-field="text" data-original-value="${escapeHtml(text)}" data-original-start="${startSeconds}" data-original-end="${endSeconds}" rows="2">${escapeHtml(text)}</textarea></td><td>${canonicalLine ? `<div class="pasted-line">${escapeHtml(canonicalLine)}</div><button type="button" data-use-canonical="${idx}">Use canonical</button>` : `<span class="muted">no canonical line</span>`}</td><td><span class="dirty-badge">clean</span></td><td><button type="button" class="row-delete" data-delete-row="${idx}">Remove</button></td></tr>`;
          }).join("")
        : `<tr><td colspan="9">No corrected_segments list found in correction payload. Use the contract debug drawer.</td></tr>`;
      const defaultSelection = document.getElementById("default-instrumental").textContent || "clean";
      const inst = instrumentals.length
        ? `<fieldset><legend>Instrumental selection</legend>${instrumentals.map((opt, idx) => {
            const id = opt.id || opt.value || (idx === 0 ? "clean" : "with_backing");
            const checked = id === defaultSelection || (!instrumentals.some(o => (o.id || o.value) === defaultSelection) && idx === 0);
            return `<label class="radio-row"><input type="radio" name="instrumental_selection" value="${escapeHtml(id)}" ${checked ? "checked" : ""} /> <strong>${escapeHtml(opt.label || id)}</strong> <code>${escapeHtml(opt.audio_path || opt.audio_url || "")}</code></label>`;
          }).join("")}</fieldset>`
        : `<p class="muted">No instrumental options found in payload; defaulting to <code>${escapeHtml(defaultSelection)}</code>.</p>`;
      nativeEl.innerHTML = `
        <h2>${escapeHtml(title)}</h2>
        <div class="debug-summary"><strong>Refresh debug:</strong> corrected_segments=<code>${escapeHtml(debug.corrected_segments_count ?? 0)}</code>, top-level segments=<code>${escapeHtml(debug.display_segments_count ?? 0)}</code>, payload keys=<code>${escapeHtml((debug.payload_keys || debug.raw_correction_payload_keys || []).join(", ") || "none")}</code>, review API=<code>${escapeHtml(debug.review_api_upstream || "unknown")}</code></div>
        <div class="debug-summary"><strong>Contract:</strong> display source <code>${escapeHtml(debug.display_segments_source_key || "none")}</code>, relation <code>${escapeHtml(debug.display_segments_relation || "unknown")}</code></div>
        <h3>Instrumental options</h3>
        ${inst}
        <h3>Lyric segments</h3>
        <p class="muted">Edit timings with minutes, seconds, and milliseconds. Finish mutates the upstream <code>corrected_segments</code> array and mirrors it into top-level <code>segments</code>.</p>
        <table class="review-table"><thead><tr><th>#</th><th>Start (m:s.ms)</th><th>End (m:s.ms)</th><th>Duration</th><th>Original / Whisper</th><th>Corrected lyric</th><th>Canonical preview</th><th>Status</th><th>Remove</th></tr></thead><tbody>${rows}</tbody></table>
      `;
      document.querySelectorAll("tr[data-segment-index]").forEach(attachRowListeners);
    }
    async function loadNativeData() {
      persistCanonicalLyrics();
      removedSegmentIndexes = [];
      try {
        const response = await fetch(dataUrl, {cache: "no-store"});
        const payload = await response.json();
        if (!response.ok || payload.ready === false) {
          if (!completionRequested) nativeEl.innerHTML = `<p class="error">${escapeHtml(payload.error || "Review data not ready")}</p>`;
          nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
          contractJsonEl.textContent = JSON.stringify(payload.review_debug || {}, null, 2);
          return;
        }
        renderNative(payload);
      } catch (err) {
        if (!completionRequested) nativeEl.innerHTML = `<p class="error">Failed to load native review data: ${escapeHtml(err)}</p>`;
      }
    }
    async function completeNativeReview() {
      persistCanonicalLyrics();
      nativeCompleteButton.disabled = true;
      nativeCompleteButton.textContent = "Finishing...";
      try {
        const selected = document.querySelector('input[name="instrumental_selection"]:checked');
        const selection = selected ? selected.value : (document.getElementById("default-instrumental").textContent || "clean");
        const segmentEdits = collectSegmentEdits();
        const url = completeUrl + "?instrumental_selection=" + encodeURIComponent(selection);
        const response = await fetch(url, {
          method: "POST",
          cache: "no-store",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({segment_edits: segmentEdits}),
        });
        const payload = await response.json();
        nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
        contractJsonEl.textContent = JSON.stringify(payload.review_debug || payload, null, 2);
        if (!response.ok || payload.ok === false) throw new Error(JSON.stringify(payload.error || payload));
        completionRequested = true;
        setStatus("review submitted — waiting for karaoke-gen to finalise", "done");
        nativeEl.insertAdjacentHTML("afterbegin", `<p class="status done">Review accepted with instrumental <code>${escapeHtml(selection)}</code>; corrected_segments ${payload.before_corrected_segments_count} → ${payload.after_corrected_segments_count}; text edits: ${payload.text_edit_count || 0}, timing edits: ${payload.timing_edit_count || 0}, removed indexes: ${(payload.removed_indexes || []).join(", ") || "none"}; outgoing keys: ${(payload.outgoing_payload_top_level_keys || []).join(", ")}. Watch the job log for final render.</p>`);
      } catch (err) {
        nativeEl.insertAdjacentHTML("afterbegin", `<p class="error">Finish failed: ${escapeHtml(err)}</p>`);
      } finally {
        nativeCompleteButton.disabled = false;
        nativeCompleteButton.textContent = "Finish with selected data";
      }
    }
    async function checkReviewServer() {
      try {
        const response = await fetch(statusUrl, {cache: "no-store"});
        const data = await response.json();
        if (data.ready) {
          if (completionRequested) setStatus("review submitted — waiting for review API to close", "done");
          else if (lastReady !== true) { reloadFrame("API became ready"); loadNativeData(); }
          else setStatus("review API ready", "running");
          lastReady = true;
        } else {
          setStatus(completionRequested ? "review API closed — karaoke-gen should be finalising" : "waiting for karaoke-gen review API...", completionRequested ? "done" : "queued");
          lastReady = false;
        }
      } catch (err) {
        setStatus(completionRequested ? "review API closed — karaoke-gen should be finalising" : "review status check failed: " + err, completionRequested ? "done" : "failed");
        lastReady = false;
      }
    }
    nativeEl.addEventListener("click", event => {
      const useCanonical = event.target.closest("button[data-use-canonical]");
      if (useCanonical) {
        const row = useCanonical.closest("tr[data-segment-index]");
        if (row && setRowTextFromCanonical(row, parseCanonicalLines())) setStatus("canonical lyric copied into row " + (Number(row.dataset.segmentIndex) + 1), "done");
        return;
      }
      const deleteButton = event.target.closest("button[data-delete-row]");
      if (deleteButton) {
        const row = deleteButton.closest("tr[data-segment-index]");
        if (!row) return;
        const idx = Number(row.dataset.segmentIndex);
        removedSegmentIndexes = Array.from(new Set([...removedSegmentIndexes, idx])).sort((a, b) => a - b);
        row.remove();
        setStatus("row " + (idx + 1) + " removed; index " + idx + " will be deleted on finish", "done");
      }
    });
    canonicalLyricsEl.addEventListener("input", persistCanonicalLyrics);
    reloadButton.addEventListener("click", () => reloadFrame("manual"));
    nativeRefreshButton.addEventListener("click", loadNativeData);
    nativeCompleteButton.addEventListener("click", completeNativeReview);
    applyCanonicalButton.addEventListener("click", applyCanonicalLyricsByLineOrder);
    deleteAfterCanonicalButton.addEventListener("click", removeRowsAfterFinalCanonicalLyric);
    restoreCanonicalLyrics();
    checkReviewServer();
    loadNativeData();
    setInterval(checkReviewServer, 2000);
  </script>
</body>
</html>"""


@router.get("/review", response_class=HTMLResponse)
def review_tab() -> HTMLResponse:
    values = {
        "__IFRAME_SRC__": html.escape(proxy_url(REVIEW_PATH)),
        "__STATUS_URL__": html.escape(public_url("/review/status")),
        "__DATA_URL__": html.escape(public_url("/review/native/data")),
        "__COMPLETE_URL__": html.escape(public_url("/review/native/complete")),
        "__DIRECT_URL__": html.escape(proxy_url(REVIEW_PATH)),
        "__HOME_URL__": html.escape(public_url("/")),
        "__REVIEW_URL__": html.escape(public_url("/review")),
        "__CSS_URL__": html.escape(public_url("/static/style.css")),
        "__DEFAULT_INSTRUMENTAL__": html.escape(DEFAULT_INSTRUMENTAL_SELECTION),
    }
    html_text = HTML_TEMPLATE
    for placeholder, value in values.items():
        html_text = html_text.replace(placeholder, value)
    return HTMLResponse(html_text)


@router.get("/review/status")
async def review_status() -> JSONResponse:
    try:
        status, payload = await _upstream_json(REVIEW_API_READY_PATH)
    except httpx.HTTPError as exc:
        return JSONResponse({"ready": False, "phase": "review_api_unavailable", "error": str(exc), "review_url": proxy_url(REVIEW_PATH)})
    return JSONResponse({"ready": status < 500, "phase": "review_api_ready" if status < 500 else "review_api_error", "status_code": status, "has_payload": isinstance(payload, dict), "review_url": proxy_url(REVIEW_PATH)})


@router.get("/review/native/data")
async def native_review_data() -> JSONResponse:
    try:
        status, payload = await _upstream_json("/api/correction-data")
    except httpx.HTTPError as exc:
        return JSONResponse({"ready": False, "error": str(exc)}, status_code=502)
    if status >= 400:
        return JSONResponse({"ready": False, "status_code": status, "error": payload}, status_code=502)
    if not isinstance(payload, dict):
        return JSONResponse({"ready": False, "error": "correction payload was not an object", "payload_type": type(payload).__name__}, status_code=502)

    summary = review_payload_summary(payload)
    debug = _review_contract_debug(payload)
    debug["review_api_upstream"] = REVIEW_UPSTREAM
    debug["payload_keys"] = summary.get("payload_keys", [])

    if not summary["ready"]:
        return JSONResponse(
            {
                "ready": False,
                "error": "No corrected_segments rows in upstream correction payload",
                "review_debug": debug,
                **summary,
            },
            status_code=503,
        )

    display = _find_display_segments(payload)
    display_segments = _extract_segment_dicts(display[0] if display else None)
    original_segments = _extract_segment_dicts(payload.get("original_segments") if isinstance(payload.get("original_segments"), list) else None)

    response_payload = dict(payload)
    response_payload["segments"] = display_segments
    response_payload["original_segment_texts"] = [_segment_text(seg) for seg in original_segments]
    response_payload["ready"] = True
    response_payload["review_debug"] = debug
    return JSONResponse(response_payload)


@router.post("/review/native/complete")
async def native_complete_review(request: Request, instrumental_selection: str | None = None) -> JSONResponse:
    try:
        status, correction_data = await _upstream_json("/api/correction-data")
        if status >= 400:
            return JSONResponse({"ok": False, "stage": "load", "status_code": status, "error": correction_data}, status_code=502)
        if not isinstance(correction_data, dict):
            return JSONResponse({"ok": False, "stage": "load", "error": "correction payload was not an object", "payload_type": type(correction_data).__name__}, status_code=502)

        try:
            body = await request.json()
        except Exception:
            body = {}

        selection = (instrumental_selection or DEFAULT_INSTRUMENTAL_SELECTION or "clean").strip()
        payload: dict[str, Any] = correction_data
        edit_debug = _apply_segment_edits_to_corrected_segments(payload, body.get("segment_edits"))

        if not edit_debug["corrected_segments_found"]:
            return JSONResponse(
                {
                    "ok": False,
                    "stage": "edit",
                    "error": "No corrected_segments list found; refusing to submit display-only edits.",
                    "review_debug": _review_contract_debug(payload),
                    **edit_debug,
                },
                status_code=422,
            )

        corrected = _find_corrected_segments(payload)
        if corrected is not None:
            payload["segments"] = corrected[0]
        payload["instrumental_selection"] = selection
        payload.setdefault("is_duet", False)
        outgoing_payload_top_level_keys = list(payload.keys())

        complete_status, complete_payload = await _upstream_json("/api/complete", method="POST", json_body=payload)
        if complete_status >= 400:
            return JSONResponse(
                {
                    "ok": False,
                    "stage": "complete",
                    "status_code": complete_status,
                    "error": complete_payload,
                    "review_debug": _review_contract_debug(payload),
                    "outgoing_payload_top_level_keys": outgoing_payload_top_level_keys,
                    **edit_debug,
                },
                status_code=502,
            )

        corrected_list = corrected[0] if corrected is not None else []
        review_completed_debug = {
            "before_corrected_segments_count": edit_debug["before_corrected_segments_count"],
            "after_corrected_segments_count": edit_debug["after_corrected_segments_count"],
            "text_edit_count": edit_debug["text_edit_count"],
            "timing_edit_count": edit_debug["timing_edit_count"],
            "removed_indexes": edit_debug["removed_indexes"],
            "outgoing_payload_top_level_keys": outgoing_payload_top_level_keys,
        }
        mark_review_complete(
            {
                "review_completed_debug": review_completed_debug,
                "resolved_corrected_segments_count": len(corrected_list),
                "resolved_segments_digest": segments_text_digest(corrected_list),
                "resolved_segments_preview": segments_preview_texts(corrected_list),
            }
        )

        return JSONResponse(
            {
                "ok": True,
                "status_code": complete_status,
                "instrumental_selection": selection,
                "review_debug": _review_contract_debug(payload),
                "outgoing_payload_top_level_keys": outgoing_payload_top_level_keys,
                "review_completed_debug": review_completed_debug,
                "applied_segment_edits": {
                    "text": edit_debug["text_edit_count"],
                    "timing": edit_debug["timing_edit_count"],
                    "deleted": len(edit_debug["removed_indexes"]),
                },
                "response": complete_payload,
                **edit_debug,
            }
        )
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@router.api_route("/review-proxy/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def review_proxy(path: str, request: Request) -> Response:
    upstream_url = f"{REVIEW_UPSTREAM}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"
    headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"}
    headers["accept-encoding"] = "identity"
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=120.0) as client:
            upstream = await client.request(request.method, upstream_url, headers=headers, content=await request.body())
    except httpx.ConnectError:
        home_url = html.escape(public_url("/"))
        return HTMLResponse(f"""<!doctype html><html><body><h1>karaoke-gen review server is not running</h1><p>The current job has not reached review yet, or the review server exited.</p><p><a href=\"{home_url}\">Back to Karaoke Forge</a></p></body></html>""", status_code=502)
    response_headers = {key: value for key, value in upstream.headers.items() if key.lower() not in STRIP_RESPONSE_HEADERS}
    location = response_headers.get("location")
    if location:
        if location.startswith(REVIEW_UPSTREAM):
            location = location.removeprefix(REVIEW_UPSTREAM)
        if location.startswith("/"):
            response_headers["location"] = proxy_url(location)
    content_type = upstream.headers.get("content-type", "")
    content = rewrite_body(upstream.content, content_type)
    return Response(content=content, status_code=upstream.status_code, headers=response_headers, media_type=content_type.split(";")[0] if content_type else None)
