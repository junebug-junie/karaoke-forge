from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send


class StripBasePathMiddleware:
    """Strip PUBLIC_BASE_PATH from incoming requests so mounted routes resolve."""

    def __init__(self, app: ASGIApp, base_path: str) -> None:
        self.app = app
        self.base_path = base_path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.base_path:
            path = scope.get("path", "")
            if path == self.base_path or path.startswith(self.base_path + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.base_path) :] or "/"
                scope["root_path"] = scope.get("root_path", "") + self.base_path
        await self.app(scope, receive, send)
