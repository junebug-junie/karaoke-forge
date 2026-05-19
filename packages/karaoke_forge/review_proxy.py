from __future__ import annotations

import html
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import DEFAULT_INSTRUMENTAL_SELECTION, PUBLIC_BASE_PATH

router = APIRouter()

REVIEW_UPSTREAM = "http://127.0.0.1:8000"
REVIEW_PATH = "/app/jobs/local/review"
REVIEW_API_READY_PATH = "/api/correction-data"
SEGMENT_TEXT_KEYS = ("text", "corrected_text", "lyrics", "line")
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


def _extract_segments(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        for key in ("segments", "lyrics_segments", "corrected_segments"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in data.values():
            found = _extract_segments(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _extract_segments(value)
            if found:
                return found
    return []


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


def _set_segment_text(segment: dict[str, Any], text: str) -> None:
    updated = False
    for key in SEGMENT_TEXT_KEYS:
        if key in segment:
            segment[key] = text
            updated = True
    if not updated:
        segment["text"] = text


def _apply_segment_edits(data: Any, edits: Any) -> int:
    if not isinstance(edits, list):
        return 0
    segments = _extract_segments(data)
    applied = 0
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        try:
            idx = int(edit.get("index"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(segments):
            continue
        text = str(edit.get("text") or "").strip()
        if text and text != _segment_text(segments[idx]):
            _set_segment_text(segments[idx], text)
            applied += 1
    return applied


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


@router.get("/review", response_class=HTMLResponse)
def review_tab() -> HTMLResponse:
    iframe_src = html.escape(proxy_url(REVIEW_PATH))
    status_url = html.escape(public_url("/review/status"))
    data_url = html.escape(public_url("/review/native/data"))
    complete_url = html.escape(public_url("/review/native/complete"))
    direct_url = html.escape(proxy_url(REVIEW_PATH))
    home_url = html.escape(public_url("/"))
    review_url = html.escape(public_url("/review"))
    css_url = html.escape(public_url("/static/style.css"))
    default_instrumental = html.escape(DEFAULT_INSTRUMENTAL_SELECTION)

    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Review · Karaoke Forge</title>
  <link rel="stylesheet" href="{css_url}" />
</head>
<body>
  <main class="review-main">
    <header>
      <a class="brand" href="{home_url}">Karaoke Forge</a>
      <nav class="tabs">
        <a href="{home_url}">Jobs</a>
        <a href="{review_url}">Review</a>
      </nav>
    </header>
    <section class="panel review-panel">
      <h1>Native Review</h1>
      <p class="muted">Edit lyric segment text here, then finish. Edits are merged into karaoke-gen's correction payload.</p>
      <div class="button-row">
        <span id="review-status" class="status queued">checking review API...</span>
        <button type="button" id="native-refresh">Refresh native data</button>
        <button type="button" id="native-complete">Finish with selected data</button>
        <button type="button" id="review-reload">Reload iframe debug</button>
      </div>
      <p class="muted">Default instrumental selection: <code id="default-instrumental">{default_instrumental}</code></p>
      <div id="native-review" class="native-review">Loading review data...</div>
      <details class="debug-details">
        <summary>Raw correction data</summary>
        <pre id="native-json" class="live-log">No data yet.</pre>
      </details>
      <details class="debug-details">
        <summary>Iframe debug fallback</summary>
        <p><a class="button-link" href="{direct_url}" target="_blank" rel="noreferrer">Open proxied review directly</a></p>
        <iframe id="review-frame" class="review-frame" data-src="{iframe_src}" src="{iframe_src}"></iframe>
      </details>
    </section>
  </main>
  <script>
    const statusUrl = "{status_url}";
    const dataUrl = "{data_url}";
    const completeUrl = "{complete_url}";
    const frame = document.getElementById("review-frame");
    const statusEl = document.getElementById("review-status");
    const nativeEl = document.getElementById("native-review");
    const nativeJsonEl = document.getElementById("native-json");
    const reloadButton = document.getElementById("review-reload");
    const nativeRefreshButton = document.getElementById("native-refresh");
    const nativeCompleteButton = document.getElementById("native-complete");
    let lastReady = null;
    let completionRequested = false;

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>\"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'\"':"&quot;","'":"&#039;"}}[ch]));
    }}
    function setStatus(text, cls) {{
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }}
    function segmentText(seg) {{
      return seg.text || seg.corrected_text || seg.lyrics || seg.line || "";
    }}
    function collectSegmentEdits() {{
      return Array.from(document.querySelectorAll("textarea[data-segment-index]")).map(el => {{
        return {{index: Number(el.dataset.segmentIndex), original_text: el.dataset.originalText || "", text: el.value.trim()}};
      }}).filter(edit => edit.text && edit.text !== edit.original_text);
    }}
    function reloadFrame(reason) {{
      const base = frame.dataset.src;
      const sep = base.includes("?") ? "&" : "?";
      frame.src = base + sep + "kf_reload=" + Date.now();
      setStatus("review API ready — iframe reloaded (" + reason + ")", "running");
    }}
    function renderNative(payload) {{
      nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
      const segments = payload.segments || [];
      const instrumentals = payload.instrumental_options || [];
      const meta = payload.metadata || {{}};
      const title = [meta.artist, meta.title].filter(Boolean).join(" — ") || "Current review session";
      const rows = segments.length
        ? segments.map((seg, idx) => {{
            const text = segmentText(seg);
            return `<tr><td>${{idx + 1}}</td><td>${{escapeHtml(seg.start ?? seg.start_time ?? "")}}</td><td>${{escapeHtml(seg.end ?? seg.end_time ?? "")}}</td><td><textarea class="segment-edit" data-segment-index="${{idx}}" data-original-text="${{escapeHtml(text)}}" rows="2">${{escapeHtml(text)}}</textarea></td></tr>`;
          }}).join("")
        : `<tr><td colspan="4">No segment list found in correction payload. Use the raw JSON drawer.</td></tr>`;
      const defaultSelection = document.getElementById("default-instrumental").textContent || "clean";
      const inst = instrumentals.length
        ? `<fieldset><legend>Instrumental selection</legend>${{instrumentals.map((opt, idx) => {{
            const id = opt.id || opt.value || (idx === 0 ? "clean" : "with_backing");
            const checked = id === defaultSelection || (!instrumentals.some(o => (o.id || o.value) === defaultSelection) && idx === 0);
            return `<label class="radio-row"><input type="radio" name="instrumental_selection" value="${{escapeHtml(id)}}" ${{checked ? "checked" : ""}} /> <strong>${{escapeHtml(opt.label || id)}}</strong> <code>${{escapeHtml(opt.audio_path || opt.audio_url || "")}}</code></label>`;
          }}).join("")}}</fieldset>`
        : `<p class="muted">No instrumental options found in payload; defaulting to <code>${{escapeHtml(defaultSelection)}}</code>.</p>`;
      nativeEl.innerHTML = `
        <h2>${{escapeHtml(title)}}</h2>
        <h3>Instrumental options</h3>
        ${{inst}}
        <h3>Lyric segments</h3>
        <p class="muted">Fix transcription errors directly in the text boxes before clicking Finish.</p>
        <table class="review-table"><thead><tr><th>#</th><th>Start</th><th>End</th><th>Editable text</th></tr></thead><tbody>${{rows}}</tbody></table>
      `;
    }}
    async function loadNativeData() {{
      try {{
        const response = await fetch(dataUrl, {{cache: "no-store"}});
        const payload = await response.json();
        if (!response.ok || payload.ready === false) {{
          if (!completionRequested) nativeEl.innerHTML = `<p class="error">${{escapeHtml(payload.error || "Review data not ready")}}</p>`;
          nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
          return;
        }}
        renderNative(payload);
      }} catch (err) {{
        if (!completionRequested) nativeEl.innerHTML = `<p class="error">Failed to load native review data: ${{escapeHtml(err)}}</p>`;
      }}
    }}
    async function completeNativeReview() {{
      nativeCompleteButton.disabled = true;
      nativeCompleteButton.textContent = "Finishing...";
      try {{
        const selected = document.querySelector('input[name="instrumental_selection"]:checked');
        const selection = selected ? selected.value : (document.getElementById("default-instrumental").textContent || "clean");
        const segmentEdits = collectSegmentEdits();
        const url = completeUrl + "?instrumental_selection=" + encodeURIComponent(selection);
        const response = await fetch(url, {{
          method: "POST",
          cache: "no-store",
          headers: {{"content-type": "application/json"}},
          body: JSON.stringify({{segment_edits: segmentEdits}}),
        }});
        const payload = await response.json();
        nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
        if (!response.ok || payload.ok === false) throw new Error(JSON.stringify(payload.error || payload));
        completionRequested = true;
        setStatus("review submitted — waiting for karaoke-gen to finalise", "done");
        nativeEl.insertAdjacentHTML("afterbegin", `<p class="status done">Review accepted with instrumental <code>${{escapeHtml(selection)}}</code> and ${{segmentEdits.length}} lyric edit(s). Watch the job log for final render.</p>`);
      }} catch (err) {{
        nativeEl.insertAdjacentHTML("afterbegin", `<p class="error">Finish failed: ${{escapeHtml(err)}}</p>`);
      }} finally {{
        nativeCompleteButton.disabled = false;
        nativeCompleteButton.textContent = "Finish with selected data";
      }}
    }}
    async function checkReviewServer() {{
      try {{
        const response = await fetch(statusUrl, {{cache: "no-store"}});
        const data = await response.json();
        if (data.ready) {{
          if (completionRequested) setStatus("review submitted — waiting for review API to close", "done");
          else if (lastReady !== true) {{ reloadFrame("API became ready"); loadNativeData(); }}
          else setStatus("review API ready", "running");
          lastReady = true;
        }} else {{
          setStatus(completionRequested ? "review API closed — karaoke-gen should be finalising" : "waiting for karaoke-gen review API...", completionRequested ? "done" : "queued");
          lastReady = false;
        }}
      }} catch (err) {{
        setStatus(completionRequested ? "review API closed — karaoke-gen should be finalising" : "review status check failed: " + err, completionRequested ? "done" : "failed");
        lastReady = false;
      }}
    }}
    reloadButton.addEventListener("click", () => reloadFrame("manual"));
    nativeRefreshButton.addEventListener("click", loadNativeData);
    nativeCompleteButton.addEventListener("click", completeNativeReview);
    checkReviewServer();
    loadNativeData();
    setInterval(checkReviewServer, 2000);
  </script>
</body>
</html>"""
    )


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
