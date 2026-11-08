"""Read-only hardening policy for the constrained Telegram MCP fork.

Year-activity eligibility, fail-closed static allowlist, document-only media,
and honest OS-boundary notes. Telegram has no native read-only session scope:
service-user + NTFS ACL reduce risk but do not stop admin local-exec.
"""

from __future__ import annotations

import mimetypes
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon.tl.types import Channel, Chat, User

# ---------------------------------------------------------------------------
# Config (parameterized — no eternal calendar-year hardcode)
# ---------------------------------------------------------------------------

DEFAULT_TIMEZONE = "Asia/Krasnoyarsk"
DEFAULT_CACHE_TTL_SECONDS = 300.0

# Image MIME prefixes / exact types treated as photos (never downloadable).
_IMAGE_MIME_PREFIXES = ("image/",)
_IMAGE_MIME_EXACT = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
        "image/heic",
        "image/heif",
        "image/x-icon",
        "image/svg+xml",
    }
)
_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
        ".ico",
        ".svg",
        ".jfif",
        ".avif",
    }
)
# Magic-byte sniffers for image-as-document spoofing.
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


class PolicyError(Exception):
    """Policy misconfiguration or evaluation failure — always deny."""

    def __init__(self, message: str = "Access denied by read-only policy."):
        super().__init__(message)
        self.user_message = message


@dataclass(frozen=True)
class RoPolicyConfig:
    """Resolved RO policy parameters for one evaluation."""

    year: int
    timezone_name: str
    tz: ZoneInfo
    cache_ttl_seconds: float
    include_broadcast_channels: bool = False


_eligibility_cache: dict[tuple[Any, ...], tuple[float, bool, str]] = {}
_cache_year_marker: Optional[int] = None


def clear_eligibility_cache() -> None:
    """Drop all cached eligibility decisions (tests / year rollover)."""
    global _cache_year_marker
    _eligibility_cache.clear()
    _cache_year_marker = None


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise PolicyError(f"Invalid integer for {name}.") from exc


def resolve_policy_config(
    *,
    year: Optional[int] = None,
    timezone_name: Optional[str] = None,
    now: Optional[datetime] = None,
    cache_ttl_seconds: Optional[float] = None,
    include_broadcast_channels: Optional[bool] = None,
) -> RoPolicyConfig:
    """Build policy config from args or environment. Broken config → PolicyError."""
    tz_name = (timezone_name or os.getenv("TELEGRAM_RO_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    if not tz_name:
        raise PolicyError("TELEGRAM_RO_TIMEZONE is empty.")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise PolicyError(f"Unknown timezone '{tz_name}'.") from exc

    if year is None:
        year = _env_int("TELEGRAM_RO_YEAR")
    if year is None:
        anchor = now.astimezone(tz) if now is not None else datetime.now(tz)
        year = anchor.year
    if year < 1970 or year > 2100:
        raise PolicyError(f"TELEGRAM_RO_YEAR out of range: {year}.")

    if cache_ttl_seconds is None:
        raw_ttl = os.getenv("TELEGRAM_RO_ELIGIBILITY_CACHE_TTL", "").strip()
        if raw_ttl:
            try:
                cache_ttl_seconds = float(raw_ttl)
            except ValueError as exc:
                raise PolicyError("Invalid TELEGRAM_RO_ELIGIBILITY_CACHE_TTL.") from exc
        else:
            cache_ttl_seconds = DEFAULT_CACHE_TTL_SECONDS
    if cache_ttl_seconds < 0:
        raise PolicyError("Eligibility cache TTL must be >= 0.")

    if include_broadcast_channels is None:
        flag = (os.getenv("TELEGRAM_RO_INCLUDE_BROADCAST_CHANNELS") or "").strip().lower()
        include_broadcast_channels = flag in {"1", "true", "yes", "on"}

    return RoPolicyConfig(
        year=year,
        timezone_name=tz_name,
        tz=tz,
        cache_ttl_seconds=float(cache_ttl_seconds),
        include_broadcast_channels=bool(include_broadcast_channels),
    )


def year_window(config: RoPolicyConfig) -> tuple[datetime, datetime]:
    """Inclusive start / exclusive end of the configured calendar year in policy TZ."""
    start = datetime(config.year, 1, 1, 0, 0, 0, tzinfo=config.tz)
    end = datetime(config.year + 1, 1, 1, 0, 0, 0, tzinfo=config.tz)
    return start, end


def invalidate_cache_if_year_changed(config: RoPolicyConfig) -> None:
    """Year-rollover policy: any config year change clears the eligibility cache."""
    global _cache_year_marker
    if _cache_year_marker is not None and _cache_year_marker != config.year:
        clear_eligibility_cache()
    _cache_year_marker = config.year


# ---------------------------------------------------------------------------
# Fail-closed static allowlist (fixes upstream fail-open empty/unset misuse)
# ---------------------------------------------------------------------------

def parse_static_allowlist(raw: Optional[str]) -> set:
    """Parse TELEGRAM_ALLOWED_CHAT_IDS (fail-closed).

    Always returns a set. Empty set = DENY ALL (missing/empty/broken policy).
    """
    if raw is None:
        return set()
    text = raw.strip()
    if not text:
        return set()
    tokens = [t.strip() for t in text.split(",") if t.strip()]
    if not tokens:
        return set()
    allowed: set = set()
    for token in tokens:
        try:
            val = int(token)
            allowed.add(val)
            if str(val).startswith("-100") and len(str(val)) > 4:
                try:
                    allowed.add(int(str(val)[4:]))
                except ValueError:
                    pass
            elif val > 0:
                allowed.add(-1000000000000 - val)
                allowed.add(-val)
            elif val < 0:
                allowed.add(-val)
        except ValueError:
            handle = token.lstrip("@").strip().lower()
            if handle and handle.replace("_", "").isalnum():
                allowed.add(handle)
    return allowed


def static_allowlist_permits(chat_identifier: Any, entity: Any = None) -> bool:
    """Fail-closed static allowlist. Absent/empty/broken → False (deny)."""
    if "TELEGRAM_ALLOWED_CHAT_IDS" not in os.environ:
        return False
    allowed = parse_static_allowlist(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS"))
    if not allowed:
        return False

    if chat_identifier is not None:
        if isinstance(chat_identifier, int) and chat_identifier in allowed:
            return True
        if isinstance(chat_identifier, str):
            try:
                if int(chat_identifier) in allowed:
                    return True
            except ValueError:
                clean = chat_identifier.lstrip("@").strip().lower()
                if clean and clean in allowed:
                    return True

    if entity is not None:
        bare = getattr(entity, "id", None)
        if isinstance(bare, int) and bare in allowed:
            return True
        username = getattr(entity, "username", None)
        if username and str(username).lower() in allowed:
            return True
        marked = marked_id(entity)
        if marked is not None and marked in allowed:
            return True
    return False


def marked_id(entity: Any) -> Optional[int]:
    try:
        if isinstance(entity, Channel):
            return -1000000000000 - entity.id
        if isinstance(entity, Chat):
            return -entity.id
        if hasattr(entity, "id"):
            return int(entity.id)
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Dialog type + year-activity eligibility
# ---------------------------------------------------------------------------

def is_in_scope_dialog_type(entity: Any, *, include_broadcast_channels: bool = False) -> bool:
    """Private, basic group, supergroup only unless broadcast explicitly enabled."""
    if isinstance(entity, User):
        return True
    if isinstance(entity, Chat):
        return True
    if isinstance(entity, Channel):
        if getattr(entity, "megagroup", False):
            return True
        if getattr(entity, "broadcast", False):
            return bool(include_broadcast_channels)
        # Unknown channel flavour → deny
        return False
    return False


def normalize_activity_timestamp(
    value: Any, *, config: RoPolicyConfig, now: Optional[datetime] = None
) -> Optional[datetime]:
    """Convert Telethon/dialog dates to aware datetime. Invalid/future → None (deny)."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            # Reject absurd futures / non-positive
            if value <= 0:
                return None
            dt = datetime.fromtimestamp(float(value), tz=config.tz)
        elif isinstance(value, datetime):
            dt = value if value.tzinfo is not None else value.replace(tzinfo=config.tz)
            dt = dt.astimezone(config.tz)
        else:
            return None
    except (OverflowError, OSError, ValueError, TypeError):
        return None

    anchor = now.astimezone(config.tz) if now is not None else datetime.now(config.tz)
    # Future timestamps must NOT grant access (clock skew tolerance: 5 minutes).
    skew = (dt - anchor).total_seconds()
    if skew > 300:
        return None
    if skew > 0:
        # within 5 min skew — clamp to now for window test
        dt = anchor
    return dt


def activity_in_year(activity: Optional[datetime], config: RoPolicyConfig) -> bool:
    if activity is None:
        return False
    start, end = year_window(config)
    return start <= activity < end


def extract_dialog_activity(dialog: Any) -> Any:
    """Best-effort last-activity field without loading history."""
    if dialog is None:
        return None
    for attr in ("date", "message"):
        val = getattr(dialog, attr, None)
        if val is None:
            continue
        if attr == "message" and hasattr(val, "date"):
            return val.date
        if attr == "date":
            return val
    msg = getattr(dialog, "message", None)
    if msg is not None and hasattr(msg, "date"):
        return msg.date
    return None


async def fetch_dialog_activity(client: Any, entity: Any) -> Any:
    """Minimal peer dialog probe — no history walk. Errors → None (deny)."""
    try:
        from telethon import functions, utils

        peer = await client.get_input_entity(entity)
        result = await client(functions.messages.GetPeerDialogsRequest(peers=[peer]))
        dialogs = getattr(result, "dialogs", None) or []
        if not dialogs:
            return None
        d0 = dialogs[0]
        # Top message date if present in messages map
        top_message = getattr(d0, "top_message", None)
        messages = getattr(result, "messages", None) or []
        if top_message and messages:
            for msg in messages:
                if getattr(msg, "id", None) == top_message:
                    return getattr(msg, "date", None)
        return getattr(d0, "date", None)
    except Exception:
        return None


async def evaluate_chat_eligibility(
    client: Any,
    entity: Any,
    chat_identifier: Any = None,
    *,
    config: Optional[RoPolicyConfig] = None,
    now: Optional[datetime] = None,
    dialog: Any = None,
    use_cache: bool = True,
) -> tuple[bool, str]:
    """Shared gate: (ok, reason). Doubt/error → (False, reason)."""
    try:
        cfg = config or resolve_policy_config(now=now)
    except PolicyError as exc:
        return False, str(exc)

    invalidate_cache_if_year_changed(cfg)

    if not static_allowlist_permits(chat_identifier, entity):
        return False, "Denied by static chat allowlist (fail-closed)."

    if not is_in_scope_dialog_type(entity, include_broadcast_channels=cfg.include_broadcast_channels):
        return False, "Dialog type out of read-only scope (broadcast channels require explicit opt-in)."

    mid = marked_id(entity)
    cache_key = (id(client), mid, cfg.year, cfg.timezone_name, cfg.include_broadcast_channels)
    if use_cache and cfg.cache_ttl_seconds > 0 and cache_key in _eligibility_cache:
        expires, ok, reason = _eligibility_cache[cache_key]
        if time.monotonic() < expires:
            return ok, reason

    raw_activity = extract_dialog_activity(dialog) if dialog is not None else None
    if raw_activity is None:
        raw_activity = await fetch_dialog_activity(client, entity)

    activity = normalize_activity_timestamp(raw_activity, config=cfg, now=now)
    if not activity_in_year(activity, cfg):
        ok, reason = False, (
            f"No message activity in calendar year {cfg.year} ({cfg.timezone_name}); access denied."
        )
    else:
        ok, reason = True, "eligible"

    if use_cache and cfg.cache_ttl_seconds > 0:
        _eligibility_cache[cache_key] = (
            time.monotonic() + cfg.cache_ttl_seconds,
            ok,
            reason,
        )
    return ok, reason


async def require_chat_eligible(
    client: Any,
    entity: Any,
    chat_identifier: Any = None,
    **kwargs: Any,
) -> Optional[str]:
    """Return an error string if denied, else None."""
    ok, reason = await evaluate_chat_eligibility(
        client, entity, chat_identifier, **kwargs
    )
    if ok:
        return None
    return reason


# ---------------------------------------------------------------------------
# Document-only media policy (photos forbidden; image-as-doc blocked)
# ---------------------------------------------------------------------------

def _magic_is_image(header: bytes) -> bool:
    if not header:
        return False
    for magic, _ in _IMAGE_MAGIC:
        if header.startswith(magic):
            return True
    if header[:4] == b"RIFF" and len(header) >= 12 and header[8:12] == b"WEBP":
        return True
    return False


def classify_attachment(
    *,
    is_photo_media: bool = False,
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
    attributes: Optional[list] = None,
    header_bytes: Optional[bytes] = None,
) -> tuple[str, str]:
    """Return (kind, reason) where kind is 'document' | 'photo' | 'rejected'.

    Photos and image/* (including image sent as document) → photo/rejected.
    Video/audio → rejected (docs only). Real non-image documents → document.
    """
    if is_photo_media:
        return "photo", "Telegram photo media is forbidden."

    mime = (mime_type or "").strip().lower()
    name = (file_name or "").strip()
    ext = ""
    if "." in name:
        ext = "." + name.rsplit(".", 1)[-1].lower()

    # Attribute hints (Telethon DocumentAttribute*)
    attr_names = []
    for attr in attributes or []:
        attr_names.append(type(attr).__name__)

    if any("Animated" in n or "Video" in n or "Audio" in n or "Sticker" in n for n in attr_names):
        if any("Video" in n for n in attr_names):
            return "rejected", "Video attachments are not in read-only document scope."
        if any("Audio" in n or "Voice" in n for n in attr_names):
            return "rejected", "Audio attachments are not in read-only document scope."
        if any("Sticker" in n or "Animated" in n for n in attr_names):
            return "rejected", "Sticker/animated media is forbidden."

    if mime in _IMAGE_MIME_EXACT or any(mime.startswith(p) for p in _IMAGE_MIME_PREFIXES):
        return "photo", "Image MIME type is forbidden (including image-as-document)."

    if ext in _IMAGE_EXTENSIONS:
        return "photo", "Image file extension is forbidden (including spoofed document wrapper)."

    if header_bytes is not None and _magic_is_image(header_bytes[:32]):
        return "photo", "Image content signature detected; photo downloads forbidden."

    if mime.startswith("video/") or mime.startswith("audio/"):
        return "rejected", "Video/audio MIME is outside document-only scope."

    # Guess from name if mime missing
    if not mime and name:
        guessed, _ = mimetypes.guess_type(name)
        if guessed:
            if guessed.startswith("image/"):
                return "photo", "Guessed image MIME from filename; forbidden."
            if guessed.startswith("video/") or guessed.startswith("audio/"):
                return "rejected", "Guessed video/audio MIME; outside document scope."

    if not mime and not name and not attributes:
        return "rejected", "Insufficient attachment metadata; deny by default."

    return "document", "ok"


def attachment_is_downloadable_document(**kwargs: Any) -> tuple[bool, str]:
    kind, reason = classify_attachment(**kwargs)
    if kind == "document":
        return True, reason
    return False, reason


# ---------------------------------------------------------------------------
# Auth token for HTTP transport
# ---------------------------------------------------------------------------

def require_http_auth_token() -> str:
    """HTTP/SSE must have a non-empty shared secret. Missing → PolicyError."""
    token = (os.getenv("TELEGRAM_MCP_AUTH_TOKEN") or "").strip()
    if not token:
        raise PolicyError(
            "TELEGRAM_MCP_AUTH_TOKEN is required for HTTP/SSE transport. "
            "Use stdio or set a non-empty shared secret."
        )
    return token


def http_auth_header_ok(authorization: Optional[str], expected: str) -> bool:
    if not authorization or not expected:
        return False
    value = authorization.strip()
    if value.lower().startswith("bearer "):
        got = value[7:].strip()
    else:
        got = value
    if not got:
        return False
    # Constant-time compare
    if len(got) != len(expected):
        return False
    result = 0
    for a, b in zip(got.encode(), expected.encode()):
        result |= a ^ b
    return result == 0


# Explicit tool surface for this fork (single registry source of truth).
RO_ALLOWED_TOOLS = frozenset(
    {
        "list_eligible_chats",
        "get_messages",
        "get_history",
        "get_message_read_by",
        "download_document",
    }
)

BOUNDARY_NOTICE = (
    "Telegram sessions have no native read-only scope. Isolation relies on a "
    "dedicated unprivileged service user and NTFS ACLs for session/credential "
    "files. This does not protect against an administrator or other local-exec "
    "on the same machine."
)
