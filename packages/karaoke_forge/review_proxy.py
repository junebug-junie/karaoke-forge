from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import DEFAULT_INSTRUMENTAL_SELECTION, PUBLIC_BASE_PATH
from .store import list_jobs

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


def _find_segments_list(data: Any) -> list[Any] | None:
    if isinstance(data, dict):
        for key in ("segments", "lyrics_segments", "corrected_segments"):
            value = data.get(key)
            if _looks_like_segment_list(value):
                return value
        for value in data.values():
            found = _find_segments_list(value)
            if found is not None:
                return found
    elif isinstance(data, list):
        if _looks_like_segment_list(data):
            return data
        for value in data:
            found = _find_segments_list(value)
            if found is not None:
                return found
    return None


def _extract_segments(data: Any) -> list[dict[str, Any]]:
    segments = _find_segments_list(data)
    if segments is None:
        return []
    return [item for item in segments if isinstance(item, dict)]


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


def _set_segment_text(segment: dict[str, Any], text: str) -> None:
    updated = False
    for key in SEGMENT_TEXT_KEYS:
        if key in segment:
            segment[key] = text
            updated = True
    if not updated:
        segment["text"] = text


def _set_segment_time(segment: dict[str, Any], keys: tuple[str, ...], value: float) -> None:
    updated = False
    for key in keys:
        if key in segment:
            segment[key] = value
            updated = True
    if not updated:
        segment[keys[0]] = value


def _apply_segment_edits(data: Any, edits: Any) -> dict[str, int]:
    result = {"text": 0, "timing": 0, "deleted": 0}
    if not isinstance(edits, list):
        return result

    segment_list = _find_segments_list(data)
    if segment_list is None:
        return result
    segments = [item for item in segment_list if isinstance(item, dict)]

    delete_indexes: list[int] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        try:
            idx = int(edit.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(segments):
            continue
        if bool(edit.get("delete")):
            delete_indexes.append(idx)
            continue

        segment = segments[idx]
        text = str(edit.get("text") or "").strip()
        if text and text != _segment_text(segment):
            _set_segment_text(segment, text)
            result["text"] += 1

        start = _parse_timestamp_seconds(edit.get("start"))
        if start is not None and start != _segment_time(segment, SEGMENT_START_KEYS):
            _set_segment_time(segment, SEGMENT_START_KEYS, start)
            result["timing"] += 1

        end = _parse_timestamp_seconds(edit.get("end"))
        if end is not None and end != _segment_time(segment, SEGMENT_END_KEYS):
            _set_segment_time(segment, SEGMENT_END_KEYS, end)
            result["timing"] += 1

    for idx in sorted(set(delete_indexes), reverse=True):
        target = segments[idx]
        try:
            segment_list.remove(target)
            result["deleted"] += 1
        except ValueError:
            pass

    return result


def _canonical_lyrics_payload() -> dict[str, Any]:
    for job in list_jobs(limit=50):
        if not job.lyrics_path:
            continue
        path = Path(job.lyrics_path)
        if not path.exists():
            continue
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        return {
            "canonical_lyrics_lines": lines,
            "canonical_lyrics_source": str(path),
            "canonical_lyrics_job_id": job.id,
            "canonical_lyrics_title": f"{job.artist} — {job.title}",
        }
    return {
        "canonical_lyrics_lines": [],
        "canonical_lyrics_source": None,
        "canonical_lyrics_job_id": None,
        "canonical_lyrics_title": None,
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
      <p class="muted">Treat pasted lyrics as canonical. Use them by line order, then adjust timing/deletions.</p>
      <div class="button-row">
        <span id="review-status" class="status queued">checking review API...</span>
        <button type="button" id="native-refresh">Refresh native data</button>
        <button type="button" id="apply-pasted">Apply pasted lyrics by line order</button>
        <button type="button" id="native-complete">Finish with selected data</button>
        <button type="button" id="review-reload">Reload iframe debug</button>
      </div>
      <p class="muted">Default instrumental selection: <code id="default-instrumental">__DEFAULT_INSTRUMENTAL__</code></p>
      <div id="native-review" class="native-review">Loading review data...</div>
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
    const frame = document.getElementById("review-frame");
    const statusEl = document.getElementById("review-status");
    const nativeEl = document.getElementById("native-review");
    const nativeJsonEl = document.getElementById("native-json");
    const reloadButton = document.getElementById("review-reload");
    const nativeRefreshButton = document.getElementById("native-refresh");
    const nativeCompleteButton = document.getElementById("native-complete");
    const applyPastedButton = document.getElementById("apply-pasted");
    let lastReady = null;
    let completionRequested = false;
    let canonicalLyrics = [];

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[ch]));
    }
    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }
    function segmentText(seg) {
      return seg.text || seg.corrected_text || seg.lyrics || seg.line || "";
    }
    function secondsValue(value) {
      const n = Number(value);
      return Number.isFinite(n) ? n : null;
    }
    function formatTimestamp(value) {
      const total = secondsValue(value);
      if (total === null) return value ?? "";
      const minutes = Math.floor(total / 60);
      const seconds = total - minutes * 60;
      return `${minutes}:${seconds.toFixed(3).padStart(6, "0")}`;
    }
    function collectSegmentEdits() {
      return Array.from(document.querySelectorAll("tr[data-segment-index]")).map(row => {
        const textEl = row.querySelector("textarea[data-field='text']");
        const startEl = row.querySelector("input[data-field='start']");
        const endEl = row.querySelector("input[data-field='end']");
        const deleteEl = row.querySelector("input[data-field='delete']");
        return {
          index: Number(row.dataset.segmentIndex),
          delete: Boolean(deleteEl && deleteEl.checked),
          original_text: textEl?.dataset.originalValue || "",
          text: textEl?.value.trim() || "",
          original_start: startEl?.dataset.originalValue || "",
          start: startEl?.value.trim() || "",
          original_end: endEl?.dataset.originalValue || "",
          end: endEl?.value.trim() || "",
        };
      }).filter(edit => edit.delete || edit.text !== edit.original_text || edit.start !== edit.original_start || edit.end !== edit.original_end);
    }
    function setRowTextFromPasted(row) {
      const idx = Number(row.dataset.segmentIndex);
      const pasted = canonicalLyrics[idx];
      if (!pasted) return false;
      const textEl = row.querySelector("textarea[data-field='text']");
      if (!textEl) return false;
      textEl.value = pasted;
      row.classList.add("lyrics-applied");
      return true;
    }
    function applyPastedLyricsByLineOrder() {
      let applied = 0;
      document.querySelectorAll("tr[data-segment-index]").forEach(row => {
        if (setRowTextFromPasted(row)) applied += 1;
      });
      setStatus(`applied ${applied} pasted lyric line(s) by row order`, "done");
    }
    function reloadFrame(reason) {
      const base = frame.dataset.src;
      const sep = base.includes("?") ? "&" : "?";
      frame.src = base + sep + "kf_reload=" + Date.now();
      setStatus("review API ready — iframe reloaded (" + reason + ")", "running");
    }
    function renderNative(payload) {
      nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
      const segments = payload.segments || [];
      canonicalLyrics = payload.canonical_lyrics_lines || [];
      const source = payload.canonical_lyrics_source || "none";
      const instrumentals = payload.instrumental_options || [];
      const meta = payload.metadata || {};
      const title = [meta.artist, meta.title].filter(Boolean).join(" — ") || payload.canonical_lyrics_title || "Current review session";
      const rows = segments.length
        ? segments.map((seg, idx) => {
            const text = segmentText(seg);
            const pasted = canonicalLyrics[idx] || "";
            const mismatch = pasted && pasted.trim() !== text.trim();
            const start = formatTimestamp(seg.start ?? seg.start_time ?? "");
            const end = formatTimestamp(seg.end ?? seg.end_time ?? "");
            return `<tr data-segment-index="${idx}" class="${mismatch ? "lyric-mismatch" : ""}"><td>${idx + 1}</td><td><input class="time-edit" data-field="start" data-original-value="${escapeHtml(start)}" value="${escapeHtml(start)}" /></td><td><input class="time-edit" data-field="end" data-original-value="${escapeHtml(end)}" value="${escapeHtml(end)}" /></td><td><textarea class="segment-edit" data-field="text" data-original-value="${escapeHtml(text)}" rows="2">${escapeHtml(text)}</textarea></td><td>${pasted ? `<div class="pasted-line">${escapeHtml(pasted)}</div><button type="button" data-use-pasted="${idx}">Use pasted</button>` : `<span class="muted">no pasted line</span>`}</td><td><label><input type="checkbox" data-field="delete" /> delete</label></td></tr>`;
          }).join("")
        : `<tr><td colspan="6">No segment list found in correction payload. Use the raw JSON drawer.</td></tr>`;
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
        <h3>Instrumental options</h3>
        ${inst}
        <h3>Lyric segments</h3>
        <p class="muted">Pasted lyric source: <code>${escapeHtml(source)}</code>. Time fields accept seconds, m:ss, or m:ss.mm.</p>
        <table class="review-table"><thead><tr><th>#</th><th>Start</th><th>End</th><th>Final editable text</th><th>Pasted lyric line</th><th>Delete</th></tr></thead><tbody>${rows}</tbody></table>
      `;
    }
    async function loadNativeData() {
      try {
        const response = await fetch(dataUrl, {cache: "no-store"});
        const payload = await response.json();
        if (!response.ok || payload.ready === false) {
          if (!completionRequested) nativeEl.innerHTML = `<p class="error">${escapeHtml(payload.error || "Review data not ready")}</p>`;
          nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
          return;
        }
        renderNative(payload);
      } catch (err) {
        if (!completionRequested) nativeEl.innerHTML = `<p class="error">Failed to load native review data: ${escapeHtml(err)}</p>`;
      }
    }
    async function completeNativeReview() {
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
        if (!response.ok || payload.ok === false) throw new Error(JSON.stringify(payload.error || payload));
        completionRequested = true;
        const counts = payload.applied_segment_edits || {};
        setStatus("review submitted — waiting for karaoke-gen to finalise", "done");
        nativeEl.insertAbjacentHTML("afterbegin", `<p class="status done">Review accepted with instrumental <code>${escapeHtml(selection)}</code>; text edits: ${counts.text || 0}, timing edits: ${counts.timing || 0}, deleted: ${counts.deleted || 0}. Watch the job log for final render.</p>`);
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
      const button = event.target.closest("button[data-use-pasted]");
      if (!button) return;
      const row = button.closest("tr[data-segment-index]");
      if (row && setRowTextFromPasted(row)) setStatus("pasted lyric copied into row " + (Number(row.dataset.segmentIndex) + 1), "done");
    });
    reloadButton.addEventListener("click", () => reloadFrame("manual"));
    nativeRefreshButton.addEventListener("click", loadNativeData);
    nativeCompleteButton.addEventListener("click", completeNativeReview);
    applyPastedButton.addEventListener("click", applyPastedLyricsByLineOrder);
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
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.setdefault("segments", _extract_segments(payload))
        payload.update(_canonical_lyrics_payload())
    return JSONResponse(payload)


@router.post("/review/native/complete")
async def native_complete_review(request: Request, instrumental_selection: str | None = None) -> JSONResponse:
    try:
        status, correction_data = await _upstream_json("/api/correction-data")
        if status >= 400:
            return JSONResponse({"ok": False, "stage": "load", "status_code": status, "error": correction_data}, status_code=502)
        try:
            body = await request.json()
        except Exception:
            body = {}
        selection = (instrumental_selection or DEFAULT_INSTRUMENTAL_SELECTION or "clean").strip()
        payload: dict[str, Any] = correction_data if isinstance(correction_data, dict) else {}
        applied_edits = _apply_segment_edits(payload, body.get("segment_edits"))
        payload.setdefault("segments", _extract_segments(payload))
        payload["instrumental_selection"] = selection
        payload.setdefault("is_duet", False)
        complete_status, complete_payload = await _upstream_json("/api/complete", method="POST", json_body=payload)
        if complete_status >= 400:
            return JSONResponse({"ok": False, "stage": "complete", "status_code": complete_status, "error": complete_payload}, status_code=502)
        return JSONResponse({"ok": True, "status_code": complete_status, "instrumental_selection": selection, "applied_segment_edits": applied_edits, "response": complete_payload})
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@router.api_route("/review-proxy/{path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
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
