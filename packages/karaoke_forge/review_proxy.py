from __future__ import annotations

import html

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

    # Next.js client bundles often call fetch('/api/...') or refer to absolute
    # '/_next/...' paths from JavaScript. Those do not appear as href/src attrs,
    # so rewrite common quoted absolute route prefixes too.
    for absolute_prefix in ABSOLUTE_REVIEW_PREFIXES:
        proxied_prefix = prefix + absolute_prefix
        for quote in ('"', "'", "`"):
            text = text.replace(f"{quote}{absolute_prefix}", f"{quote}{proxied_prefix}")

    return text.encode("utf-8")


@router.get("/review", response_class=HTMLResponse)
def review_tab() -> HTMLResponse:
    iframe_src = html.escape(proxy_url(REVIEW_PATH))
    status_url = html.escape(public_url("/review/status"))
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
      <h1>Review</h1>
      <p class="muted">Embedded karaoke-gen review session from Atlas localhost.</p>
      <div class="button-row">
        <span id="review-status" class="status queued">checking review server...</span>
        <button type="button" id="review-reload">Reload review frame</button>
      </div>
      <iframe id="review-frame" class="review-frame" data-src="{iframe_src}" src="{iframe_src}"></iframe>
    </section>
  </main>
  <script>
    const statusUrl = "{status_url}";
    const frame = document.getElementById("review-frame");
    const statusEl = document.getElementById("review-status");
    const reloadButton = document.getElementById("review-reload");
    let lastReady = null;
    let reloadCount = 0;

    function setStatus(text, cls) {{
      statusEl.textContent = text;
      statusEl.className = "status " + cls;
    }}

    function reloadFrame(reason) {{
      const base = frame.dataset.src;
      const sep = base.includes("?") ? "&" : "?";
      frame.src = base + sep + "kf_reload=" + Date.now();
      reloadCount += 1;
      setStatus("review server ready — frame reloaded (" + reason + ")", "running");
    }}

    async function checkReviewServer() {{
      try {{
        const response = await fetch(statusUrl, {{cache: "no-store"}});
        const data = await response.json();
        if (data.ready) {{
          if (lastReady !== true) {{
            reloadFrame("server became ready");
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
    checkReviewServer();
    setInterval(checkReviewServer, 2000);
  </script>
</body>
</html>"""
    )


@router.get("/review/status")
async def review_status() -> JSONResponse:
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=1.5) as client:
            response = await client.get(REVIEW_UPSTREAM + REVIEW_PATH)
    except httpx.HTTPError as exc:
        return JSONResponse({"ready": False, "error": str(exc), "review_url": proxy_url(REVIEW_PATH)})

    return JSONResponse(
        {
            "ready": response.status_code < 500,
            "status_code": response.status_code,
            "review_url": proxy_url(REVIEW_PATH),
        }
    )


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
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "content-length"
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
