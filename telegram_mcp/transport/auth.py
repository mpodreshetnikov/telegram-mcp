"""Bearer auth gate for HTTP/SSE MCP transports in the read-only fork."""

from __future__ import annotations

import logging
from typing import Any, Optional

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from telegram_mcp.ro_policy import http_auth_header_ok, require_http_auth_token

logger = logging.getLogger(__name__)


class BearerAuthMiddleware:
    """Reject unauthenticated HTTP before any MCP/data handler runs."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method == "OPTIONS":
            # Preflight still requires that a token is configured server-side;
            # actual data requests must present Authorization.
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization")
        if not http_auth_header_ok(auth, self.token):
            response = JSONResponse(
                status_code=401,
                content={
                    "jsonrpc": "2.0",
                    "id": "auth-error",
                    "error": {
                        "code": -32001,
                        "message": "Unauthorized: valid Authorization Bearer token required.",
                    },
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def wrap_http_app_with_auth(app: Any) -> Any:
    token = require_http_auth_token()
    return BearerAuthMiddleware(app, token)
