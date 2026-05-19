from __future__ import annotations

import html

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from .config import PUBLIC_BASE_PATH

router = APIRouter()

REVIEW_UPSTREAM = "http://127.0.0.1:8000"
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

    return text.encode("utf-8")


@router.get("/review", response_class=HTMLResponse)
def review_tab() -> HTMLResponse:
    iframe_src = html.escape(proxy_url("/app/jobs/local/review"))
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
      <iframe class="review-frame" src="{iframe_src}"></iframe>
    </section>
  </main>
</body>
</html>"""
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
