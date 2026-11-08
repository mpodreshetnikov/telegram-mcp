"""HTTP bearer auth middleware tests (no network server)."""

from __future__ import annotations

import json

import pytest
from starlette.responses import PlainTextResponse

from telegram_mcp.transport.auth import BearerAuthMiddleware


async def _ok_app(scope, receive, send):
    await PlainTextResponse("secret-data")(scope, receive, send)


@pytest.mark.asyncio
async def test_auth_denial_before_data():
    app = BearerAuthMiddleware(_ok_app, token="correct-token")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [],
    }
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert status == 401
    assert b"secret-data" not in body
    payload = json.loads(body.decode())
    assert payload["error"]["code"] == -32001


@pytest.mark.asyncio
async def test_auth_allows_bearer():
    app = BearerAuthMiddleware(_ok_app, token="correct-token")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer correct-token")],
    }
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert status == 200
    assert body == b"secret-data"
