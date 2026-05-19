from __future__ import annotations

import html
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import PUBLIC_BASE_PATH

router = APIRouter()

REVIEW_UPSTREAM = "http://127.0.0.1:8000"
REVIEW_PATH = "/app/jobs/local/review"
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
STRIP_RESPONSE_HEADERS = HOP_BY_HOP_HEADERS | {
    "content-length",
    "content-encoding",
}
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
    for key in ("text", "corrected_text", "lyrics", "line"):
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
      <p class="muted">This bypasses the brittle Next.js iframe and talks to karaoke-gen's local review API directly.</p>
      <div class="button-row">
        <span id="review-status" class="status queued">checking review server...</span>
        <button type="button" id="native-refresh">Refresh native data</button>
        <button type="button" id="native-complete">Finish with current/default data</button>
        <button type="button" id="review-reload">Reload iframe debug</button>
      </div>
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

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>\"']/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\\"":"&quot;","'":"&#039;"}}[ch]));
    }}

    function setStatus(text, cls) {{
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }}

    function reloadFrame(reason) {{
      const base = frame.dataset.src;
      const sep = base.includes("?") ? "&" : "?";
      frame.src = base + sep + "kf_reload=" + Date.now();
      setStatus("review server ready — iframe reloaded (" + reason + ")", "running");
    }}

    function renderNative(payload) {{
      nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
      const segments = payload.segments || [];
      const instrumentals = payload.instrumental_options || [];
      const meta = payload.metadata || {{}};
      const title = [meta.artist, meta.title].filter(Boolean).join(" — ") || "Current review session";
      const rows = segments.length
        ? segments.map((seg, idx) => `<tr><td>${{idx + 1}}</td><td>${{escapeHtml(seg.start ?? seg.start_time ?? "")}}</td><td>${{escapeHtml(seg.end ?? seg.end_time ?? "")}}</td><td>${{escapeHtml(seg.text || seg.corrected_text || seg.lyrics || seg.line || "")}}</td></tr>`).join("")
        : `<tr><td colspan="4">No segment list found in correction payload. Use the raw JSON drawer.</td></tr>`;
      const inst = instrumentals.length
        ? `<ul>${{instrumentals.map(opt => `<li><strong>${{escapeHtml(opt.label || opt.id || "option")}}</strong> <code>${{escapeHtml(opt.audio_path || opt.audio_url || "")}}</code></li>`).join("")}}</ul>`
        : `<p class="muted">No instrumental options found in payload.</p>`;
      nativeEl.innerHTML = `
        <h2>${{escapeHtml(title)}}</h2>
        <h3>Instrumental options</h3>
        ${{inst}}
        <h3>Lyric segments</h3>
        <table class="review-table"><thead><tr><th>#</th><th>Start</th><th>End</th><th>Text</th></tr></thead><tbody>${{rows}}</tbody></table>
      `;
    }}

    async function loadNativeData() {{
      try {{
        const response = await fetch(dataUrl, {{cache: "no-store"}});
        const payload = await response.json();
        if (!response.ok || payload.ready === false) {{
          nativeEl.innerHTML = `<p class="error">${{escapeHtml(payload.error || "Review data not ready")}}</p>`;
          nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
          return;
        }}
        renderNative(payload);
      }} catch (err) {{
        nativeEl.innerHTML = `<p class="error">Failed to load native review data: ${{escapeHtml(err)}}</p>`;
      }}
    }}

    async function completeNativeReview() {{
      nativeCompleteButton.disabled = true;
      nativeCompleteButton.textContent = "Finishing...";
      try {{
        const response = await fetch(completeUrl, {{method: "POST", cache: "no-store"}});
        const payload = await response.json();
        nativeJsonEl.textContent = JSON.stringify(payload, null, 2);
        if (!response.ok) throw new Error(payload.error || "complete failed");
        nativeEl.insertAdjacentHTML("afterbegin", `<p class="status done">Review completion request sent. Watch the job log for final render.</p>`);
      }} catch (err) {{
        nativeEl.insertAdjacentHTML("afterbegin", `<p class="error">Finish failed: ${{escapeHtml(err)}}</p>`);
      }} finally {{
        nativeCompleteButton.disabled = false;
        nativeCompleteButton.textContent = "Finish with current/default data";
      }}
    }}

    async function checkReviewServer() {{
      try {{
        const response = await fetch(statusUrl, {{cache: "no-store"}});
        const data = await response.json();
        if (data.ready) {{
          if (lastReady !== true) {{
            reloadFrame("server became ready");
            loadNativeData();
          }} else {{
            setStatus("review server ready", "running");
          }}
          lastReady = true;
        }} else {{
          setStatus("waiting for karaoke-gen review server...", "queued");
          lastReady = false;
        }}
      }} catch (err) {{
        setStatus("review status check failed: " + err, "failed");
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
        async with httpx.AsyncClient(follow_redirects=False, timeout=1.5) as client:
            response = await client.get(
                REVIEW_UPSTREAM + REVIEW_PATH,
                headers={"accept-encoding": "identity"},
            )
    except httpx.HTTPError as exc:
        return JSONResponse({"ready": False, "error": str(exc), "review_url": proxy_url(REVIEW_PATH)})

    return JSONResponse(
        {
            "ready": response.status_code < 500,
            "status_code": response.status_code,
            "review_url": proxy_url(REVIEW_PATH),
        }
    )


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
async def native_complete_review() -> JSONResponse:
    try:
        status, correction_data = await _upstream_json("/api/correction-data")
        if status >= 400:
            return JSONResponse({"ok": False, "stage": "load", "status_code": status, "error": correction_data}, status_code=502)

        payload: dict[str, Any] = correction_data if isinstance(correction_data, dict) else {}
        payload.setdefault("instrumental_selection", "with_backing")
        payload.setdefault("is_duet", False)

        complete_status, complete_payload = await _upstream_json("/api/complete", method="POST", json_body=payload)
        if complete_status >= 400:
            return JSONResponse(
                {"ok": False, "stage": "complete", "status_code": complete_status, "error": complete_payload},
                status_code=502,
            )
        return JSONResponse({"ok": True, "status_code": complete_status, "response": complete_payload})
    except httpx.HTTPError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@router.api_route("/review-proxy/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def review_proxy(path: str, request: Request) -> Response:
    upstream_url = f"{REVIEW_UPSTREAM}/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    headers["accept-encoding"] = "identity"

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=120.0) as client:
            upstream = await client.request(
                request.method,
                upstream_url,
                headers=headers,
                content=await request.body(),
            )
    except httpx.ConnectError:
        home_url = html.escape(public_url("/"))
        return HTMLResponse(
            f"""<!doctype html>
<html>
<body>
  <h1>karaoke-gen review server is not running</h1>
  <p>The current job has not reached the interactive review step yet, or the review server exited.</p>
  <p><a href="{home_url}">Back to Karaoke Forge</a></p>
</body>
</html>""",
            status_code=502,
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in STRIP_RESPONSE_HEADERS
    }

    location = response_headers.get("location")
    if location:
        if location.startswith(REVIEW_UPSTREAM):
            location = location.removeprefix(REVIEW_UPSTREAM)
        if location.startswith("/"):
            response_headers["location"] = proxy_url(location)

    content_type = upstream.headers.get("content-type", "")
    content = rewrite_body(upstream.content, content_type)

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=content_type.split(";")[0] if content_type else None,
    )
