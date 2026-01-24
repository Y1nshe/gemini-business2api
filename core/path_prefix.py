"""
Path prefix support for deployments behind a reverse proxy.

Goal:
- Allow the whole app to be served under a single prefix like "/<PATH_PREFIX>/*".
- Keep the backend routes unchanged (still defined as "/login", "/admin/*", "/v1/*", ...).
- Make it compatible with proxies that either preserve the prefix or not.

How it works:
- If PATH_PREFIX is set, this middleware will:
  - Redirect "/<prefix>" -> "/<prefix>/" (to make relative URLs resolve correctly)
  - Strip "/<prefix>" from incoming request paths so routing works
  - Populate scope["root_path"] so URL generation can include the prefix when needed
"""

from __future__ import annotations

from typing import Callable, Awaitable, Any


class PathPrefixMiddleware:
    """ASGI middleware that strips a fixed leading path prefix."""

    def __init__(self, app: Callable[..., Awaitable[Any]], prefix: str):
        self.app = app
        prefix = (prefix or "").strip()
        prefix = prefix.strip("/")
        self._prefix = f"/{prefix}" if prefix else ""

    async def __call__(self, scope, receive, send):
        if not self._prefix:
            return await self.app(scope, receive, send)

        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        path = scope.get("path") or ""

        # Ensure trailing slash for the mount root so relative URLs (assets/api) resolve properly.
        # Without this, visiting "/prefix" would treat "prefix" as a file path.
        if path == self._prefix and scope["type"] == "http":
            async def _send_redirect():
                await send({
                    "type": "http.response.start",
                    "status": 308,
                    "headers": [
                        (b"location", (self._prefix + "/").encode("utf-8")),
                    ],
                })
                await send({"type": "http.response.body", "body": b""})

            return await _send_redirect()

        if path.startswith(self._prefix + "/"):
            # Make a shallow copy to avoid mutating upstream scope in-place.
            scope = dict(scope)

            # Preserve nested root_path for cases where upstream already sets one.
            scope["root_path"] = (scope.get("root_path") or "") + self._prefix
            scope["path"] = path[len(self._prefix):] or "/"

        return await self.app(scope, receive, send)

