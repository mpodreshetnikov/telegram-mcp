"""Host-header allowlist for HTTP RO (Tailscale Serve) — no live network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import Request

from telegram_mcp import runner


SERVE_HOST = "mp.tailfc8bcd.ts.net"


def _request(host: str | None, *, origin: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8765),
    }
    return Request(scope)


@pytest.fixture
def configure_hosts(monkeypatch):
    fake = SimpleNamespace(
        settings=SimpleNamespace(transport_security=None),
    )
    monkeypatch.setattr(runner, "mcp", fake)
    return fake


def test_configure_merges_serve_host_with_loopback(configure_hosts, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", SERVE_HOST)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)

    runner._configure_transport_security()

    security = configure_hosts.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert SERVE_HOST in security.allowed_hosts
    for loopback in runner._LOOPBACK_ALLOWED_HOSTS:
        assert loopback in security.allowed_hosts
    # No broad wildcards
    assert "*" not in security.allowed_hosts
    assert "*.ts.net" not in security.allowed_hosts


def test_configure_noop_when_unset_leaves_existing(configure_hosts, monkeypatch):
    sentinel = object()
    configure_hosts.settings.transport_security = sentinel
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)

    runner._configure_transport_security()

    assert configure_hosts.settings.transport_security is sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host,expect_ok",
    [
        (SERVE_HOST, True),
        ("127.0.0.1:8765", True),
        ("localhost:8765", True),
        ("evil.example", False),
        ("mp.tailfc8bcd.ts.net.evil", False),
        ("other.tailfc8bcd.ts.net", False),
        (None, False),
    ],
)
async def test_middleware_accepts_serve_and_loopback_rejects_arbitrary(
    configure_hosts, monkeypatch, host, expect_ok
):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", SERVE_HOST)
    runner._configure_transport_security()
    mw = TransportSecurityMiddleware(configure_hosts.settings.transport_security)

    result = await mw.validate_request(_request(host), is_post=False)
    if expect_ok:
        assert result is None
    else:
        assert result is not None
        assert result.status_code == 421
        body = result.body if isinstance(result.body, bytes) else b""
        # Starlette Response may lazy-encode; check status + typical message via render
        assert result.status_code == 421


@pytest.mark.asyncio
async def test_invalid_host_message_matches_symptom(configure_hosts, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", SERVE_HOST)
    runner._configure_transport_security()
    mw = TransportSecurityMiddleware(configure_hosts.settings.transport_security)
    result = await mw.validate_request(_request("attacker.example"), is_post=False)
    assert result is not None
    assert result.status_code == 421
    assert result.body == b"Invalid Host header"
