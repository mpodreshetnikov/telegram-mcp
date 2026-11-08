"""Application entrypoints for the Telegram MCP server."""

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

import sys
from telethon.errors import AuthKeyDuplicatedError, BotMethodInvalidError

# Ensure sys.stderr is reconfigured for UTF-8 on Windows where possible
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

from telegram_mcp import runtime as _runtime
from telegram_mcp import transcription as _transcription
from telegram_mcp.runtime import *
from telegram_mcp.singleton import (
    DEFAULT_GRACE_SECONDS,
    SessionLock,
    SessionLockError,
    session_identity,
)
# Fail-closed allowlist on untouched upstream runtime (no exec / no runtime rewrite).
from telegram_mcp.ro_allowlist_patch import apply_to_runtime as _apply_ro_allowlist

_apply_ro_allowlist(_runtime)

import telegram_mcp.tools  # noqa: F401 - registers MCP tools via decorators

# Populated as each account's session lock is acquired; released in _main's
# finally block so a lock is never held past this process's lifetime.
_session_locks: dict[str, SessionLock] = {}


def _lock_grace_seconds() -> float:
    raw = os.getenv("TELEGRAM_LOCK_GRACE_SECONDS")
    if not raw:
        return DEFAULT_GRACE_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_GRACE_SECONDS


_SESSION_LOCK_MODES = ("exclusive", "shared")


def _session_lock_shared() -> bool:
    """``TELEGRAM_SESSION_LOCK``: exclusive (default) | shared.

    ``exclusive`` refuses to start while another instance on this host holds
    the same session. ``shared`` takes the lock in shared mode instead, so any
    number of instances on this host can use one session -- safe only when
    they all reach Telegram from the same IP, since Telegram invalidates a
    session it sees from two IPs at once. Shared and exclusive instances never
    overlap, so the default keeps its guarantee.
    """
    raw = (os.getenv("TELEGRAM_SESSION_LOCK") or "exclusive").strip().lower()
    if raw not in _SESSION_LOCK_MODES:
        accepted = ", ".join(_SESSION_LOCK_MODES)
        raise SystemExit(f"Invalid TELEGRAM_SESSION_LOCK '{raw}'. Expected one of: {accepted}.")
    return raw == "shared"


def _normalize_username(value) -> str:
    return (value or "").strip().lstrip("@").casefold()


def _expected_username(label: str) -> str:
    """TELEGRAM_EXPECTED_USERNAME_<LABEL>, else TELEGRAM_EXPECTED_USERNAME; "" if unset."""
    raw = os.getenv(f"TELEGRAM_EXPECTED_USERNAME_{label.upper()}") or os.getenv(
        "TELEGRAM_EXPECTED_USERNAME"
    )
    return _normalize_username(raw)


async def _verify_expected_username(label: str, client) -> None:
    """Refuse to serve a session logged in to a different account."""
    expected = _expected_username(label)
    if not expected:
        return

    me = await client.get_me()
    usernames = {_normalize_username(getattr(me, "username", None))}
    for entry in getattr(me, "usernames", None) or []:
        if getattr(entry, "active", False):
            usernames.add(_normalize_username(getattr(entry, "username", None)))
    usernames.discard("")

    if expected in usernames:
        return

    try:
        await client.disconnect()
    except Exception:
        pass
    raise RuntimeError(
        f"Telegram client '{label}' is logged in to a different account than "
        f"TELEGRAM_EXPECTED_USERNAME_{label.upper()} / TELEGRAM_EXPECTED_USERNAME expects."
    )


class _NotAuthorized(RuntimeError):
    """The session connects but was never logged in."""


async def _connect_authorized_client(label, client) -> None:
    # First, prevent our own duplicate-spawn case outright: a per-session lock
    # means a second instance of this server never even attempts to connect
    # while another instance already holds the same session (see
    # telegram_mcp/singleton.py for why and how). The lock only sees processes
    # on this host; TELEGRAM_SESSION_LOCK=shared lets those share one session
    # where they all reach Telegram from a single IP.
    lock = SessionLock(label, session_identity(client))
    await asyncio.to_thread(
        lock.acquire, grace_seconds=_lock_grace_seconds(), shared=_session_lock_shared()
    )
    _session_locks[label] = lock

    # Once we hold the lock, still tolerate a transient AuthKeyDuplicatedError
    # from Telegram itself (e.g. the same session briefly seen from two IPs
    # during a VPN reconnect) with a bounded retry, since that's not caused by
    # a second instance of this server and a blip shouldn't take the server
    # down. Give each concurrent client its own session (TELEGRAM_SESSION_STRINGS
    # pool or TELEGRAM_SESSION_STRING_<LABEL>) to avoid the collision entirely.
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            await client.connect()
            break
        except AuthKeyDuplicatedError:
            if attempt >= max_attempts:
                raise
            delay = min(2**attempt, 15)
            print(
                f"AuthKeyDuplicatedError connecting '{label}' (attempt "
                f"{attempt}/{max_attempts}): session in use from another IP. "
                f"Retrying in {delay}s. If this persists, give each concurrent "
                "client its own session via TELEGRAM_SESSION_STRINGS or "
                "TELEGRAM_SESSION_STRING_<LABEL>.",
                file=sys.stderr,
            )
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(delay)

    if await client.is_user_authorized():
        await _verify_expected_username(label, client)
        return

    raise _NotAuthorized(
        f"Telegram client '{label}' is not authorized. Interactive phone login "
        "is disabled for the MCP server because it runs over stdio. Generate a "
        "session string with `uv run session_string_generator.py`, then set "
        "TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env. "
        "For existing file sessions, run the login outside the MCP server first."
    )


async def _connect_or_skip(label, client) -> None:
    """Connect one account; drop an optional one that was never logged in."""
    try:
        await _connect_authorized_client(label, client)
    except _NotAuthorized:
        if label not in _runtime.OPTIONAL_ACCOUNTS:
            raise
        lock = _session_locks.pop(label, None)
        if lock is not None:
            lock.release()
        try:
            await client.disconnect()
        except Exception:
            pass
        clients.pop(label, None)
        print(
            f"Skipping account '{label}': TELEGRAM_SESSION_NAME names a session that is not "
            "logged in, and labelled accounts are configured. Unset TELEGRAM_SESSION_NAME "
            "or log that session in to use it as the default account.",
            file=sys.stderr,
        )


# FastMCP (mcp 1.30) auto-enables DNS-rebinding protection for loopback binds
# with exactly these Host patterns. Keep them when extending via MCP_ALLOWED_HOSTS
# so local probes (Host: 127.0.0.1:8765) still pass behind Tailscale Serve.
_LOOPBACK_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def _configure_transport_security() -> None:
    """Extend FastMCP Host allowlist for reverse-proxy / Tailscale Serve names.

    When MCP_ALLOWED_HOSTS is unset, leave FastMCP constructor defaults alone
    (loopback-only Hosts when bound to 127.0.0.1/localhost/::1). When set,
    enable protection with those Hosts **merged** with loopback patterns — never
    disable DNS-rebinding, never accept ``*`` wildcards beyond per-host ``:*``.
    """
    raw_hosts = os.getenv("MCP_ALLOWED_HOSTS", "")
    extra_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
    if not extra_hosts:
        return

    from mcp.server.transport_security import TransportSecuritySettings

    raw_origins = os.getenv("MCP_ALLOWED_ORIGINS", "")
    extra_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

    merged_hosts = list(dict.fromkeys([*_LOOPBACK_ALLOWED_HOSTS, *extra_hosts]))

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=merged_hosts,
        allowed_origins=extra_origins,
    )


def _enforce_readonly_fork_invariants() -> None:
    """Abort startup if write/leak tools slipped into the registry or media cloud is on."""
    from telegram_mcp.ro_policy import BOUNDARY_NOTICE, RO_ALLOWED_TOOLS

    os.environ["TELEGRAM_TRANSCRIBE"] = "off"
    if (os.getenv("TELEGRAM_MEDIA_BASE_URL") or "").strip():
        raise SystemExit(
            "Refusing to start: TELEGRAM_MEDIA_BASE_URL is forbidden in the "
            "read-only fork (no unauthenticated media URL surface)."
        )

    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    unexpected = sorted(registered - RO_ALLOWED_TOOLS)
    missing = sorted(RO_ALLOWED_TOOLS - registered)
    if unexpected:
        raise SystemExit(
            "Refusing to start: non-read-only tools registered: "
            + ", ".join(unexpected)
        )
    if missing:
        raise SystemExit(
            "Refusing to start: required read-only tools missing: " + ", ".join(missing)
        )
    print(BOUNDARY_NOTICE, file=sys.stderr)


async def _serve(transport: str) -> None:
    """Run the MCP server on the selected transport.

    Hardened fork: stdio is default. HTTP requires TELEGRAM_MCP_AUTH_TOKEN
    and binds to 127.0.0.1 by default — no public unauthenticated endpoint.
    Legacy SSE is rejected (no auth wrapper).
    """
    if transport == "sse":
        raise SystemExit(
            "SSE transport is disabled in the read-only fork. Use MCP_TRANSPORT=stdio "
            "or MCP_TRANSPORT=http with TELEGRAM_MCP_AUTH_TOKEN."
        )
    if transport == "http":
        from telegram_mcp.ro_policy import require_http_auth_token
        from telegram_mcp.transport.auth import wrap_http_app_with_auth
        from telegram_mcp.transport.http import create_streamable_http_app
        import uvicorn

        require_http_auth_token()
        mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8765"))
        host = mcp.settings.host
        if host not in ("127.0.0.1", "localhost", "::1") and not (
            os.getenv("TELEGRAM_RO_ALLOW_NON_LOOPBACK") or ""
        ).strip():
            raise SystemExit(
                f"Refusing HTTP bind to '{host}': set MCP_HOST=127.0.0.1 or "
                "explicitly set TELEGRAM_RO_ALLOW_NON_LOOPBACK=1 after reviewing exposure."
            )
        _configure_transport_security()
        app = wrap_http_app_with_auth(create_streamable_http_app(mcp))
        config = uvicorn.Config(
            app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=str(mcp.settings.log_level).lower(),
        )
        print(
            "HTTP MCP auth enabled (Bearer TELEGRAM_MCP_AUTH_TOKEN).",
            file=sys.stderr,
        )
        await uvicorn.Server(config).serve()
        return

    await mcp.run_stdio_async()


async def _main() -> None:
    try:
        labels = ", ".join(clients.keys())
        print(f"Starting {len(clients)} Telegram client(s) ({labels})...", file=sys.stderr)
        await asyncio.gather(*(_connect_or_skip(label, cl) for label, cl in list(clients.items())))
        labels = ", ".join(clients.keys())

        # Warm entity caches — StringSession has no persistent cache,
        # so fetch all dialogs once per client to populate them.
        # Runs in background: blocking startup on this (e.g. under a
        # GetDialogsRequest flood wait) makes MCP clients time out, and
        # resolve_entity() re-warms the cache on miss anyway.
        print("Warming entity caches (background)...", file=sys.stderr)

        async def _warm_caches() -> None:
            async def _warm_client(label: str, cl: TelegramClient) -> None:
                try:
                    await cl.get_dialogs()
                except BotMethodInvalidError:
                    print(
                        f"Skipping entity cache pre-warm for bot client "
                        f"'{label}' (dialogs restricted for bots).",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    print(f"Entity cache warm failed for '{label}': {exc}", file=sys.stderr)

            try:
                await asyncio.gather(*(_warm_client(label, cl) for label, cl in clients.items()))
                print("Entity caches warmed.", file=sys.stderr)
            except Exception as warm_exc:
                print(f"Entity cache warm failed: {warm_exc}", file=sys.stderr)

        asyncio.create_task(_warm_caches())

        transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

        # Validate transport mode
        VALID_TRANSPORTS = ("stdio", "http", "sse")
        if transport not in VALID_TRANSPORTS:
            accepted = ", ".join(VALID_TRANSPORTS)
            print(
                f"Invalid MCP_TRANSPORT '{transport}'. Expected one of: {accepted}.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Startup log with host:port for HTTP/SSE transports
        if transport in ("http", "sse"):
            host = os.getenv("MCP_HOST", "127.0.0.1")
            port = os.getenv("MCP_PORT", "8765")
            print(
                f"Telegram client(s) started ({labels}). Running MCP server "
                f"({transport}) on {host}:{port}...",
                file=sys.stderr,
            )
        else:
            print(
                f"Telegram client(s) started ({labels}). Running MCP server ({transport})...",
                file=sys.stderr,
            )

        await _serve(transport)
    except Exception as e:
        print(f"Error starting client: {e}", file=sys.stderr)
        if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
            print(
                "Database lock detected. Please ensure no other instances are running.",
                file=sys.stderr,
            )
        elif isinstance(e, SessionLockError):
            print(
                "Another instance of this MCP server already holds this Telegram "
                "session (e.g. the client restarted the connector without the old "
                "process exiting yet). This instance is exiting instead of "
                "connecting a second time, which would risk Telegram invalidating "
                "the session for both. Retry once the other instance is gone, or "
                "set TELEGRAM_SESSION_LOCK=shared if several instances on this "
                "host are meant to share one session (safe when they all reach "
                "Telegram from the same IP).",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        try:
            await asyncio.gather(
                *(cl.disconnect() for cl in clients.values()), return_exceptions=True
            )
        except Exception:
            pass
        for lock in _session_locks.values():
            lock.release()
        _session_locks.clear()


def main() -> None:
    _configure_allowed_roots_from_cli(sys.argv[1:])
    # Apply CLI transport/host/port overrides to environment (runtime sets globals)
    transport = _runtime._CLI_TRANSPORT or "stdio"
    host = _runtime._CLI_HOST
    port = _runtime._CLI_PORT

    if transport != "stdio":
        os.environ["MCP_TRANSPORT"] = transport
    if host:
        os.environ["MCP_HOST"] = host
    if port is not None:
        os.environ["MCP_PORT"] = str(port)
    # Hardened fork: tool surface is fixed by telegram_mcp.tools.ro_surface;
    # ignore upstream TELEGRAM_EXPOSED_TOOLS expansion (+write) entirely.
    os.environ["TELEGRAM_EXPOSED_TOOLS"] = "read-only"
    os.environ.setdefault("TELEGRAM_TRANSCRIBE", "off")
    _enforce_readonly_fork_invariants()
    _transcription.validate_transcription_config()
    _session_lock_shared()  # fail loudly at startup on a bad toggle
    asyncio.run(_main())


if __name__ == "__main__":
    main()
