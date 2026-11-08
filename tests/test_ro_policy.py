"""Unit tests for read-only policy (no live Telegram)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import tests.conftest_ro  # noqa: F401 — fail-closed patch on upstream runtime
from telegram_mcp import ro_policy


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    ro_policy.clear_eligibility_cache()
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_RO_YEAR", raising=False)
    monkeypatch.delenv("TELEGRAM_RO_TIMEZONE", raising=False)
    monkeypatch.delenv("TELEGRAM_RO_INCLUDE_BROADCAST_CHANNELS", raising=False)
    yield
    ro_policy.clear_eligibility_cache()


def test_year_window_krasnoyarsk():
    cfg = ro_policy.resolve_policy_config(year=2026, timezone_name="Asia/Krasnoyarsk")
    start, end = ro_policy.year_window(cfg)
    assert start == datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Krasnoyarsk"))
    assert end == datetime(2027, 1, 1, tzinfo=ZoneInfo("Asia/Krasnoyarsk"))


def test_activity_2026_allows_2025_denies():
    cfg = ro_policy.resolve_policy_config(year=2026, timezone_name="Asia/Krasnoyarsk")
    tz = cfg.tz
    now = datetime(2026, 6, 1, 12, 0, tzinfo=tz)
    ok = ro_policy.normalize_activity_timestamp(
        datetime(2026, 3, 1, tzinfo=tz), config=cfg, now=now
    )
    bad = ro_policy.normalize_activity_timestamp(
        datetime(2025, 12, 31, 23, 59, tzinfo=tz), config=cfg, now=now
    )
    assert ro_policy.activity_in_year(ok, cfg)
    assert not ro_policy.activity_in_year(bad, cfg)


def test_utc_plus7_year_boundary():
    cfg = ro_policy.resolve_policy_config(year=2026, timezone_name="Asia/Krasnoyarsk")
    tz = cfg.tz
    # 2025-12-31 23:30 Krasnoyarsk is still 2025
    late_2025 = datetime(2025, 12, 31, 23, 30, tzinfo=tz)
    # 2026-01-01 00:00 Krasnoyarsk is 2026
    early_2026 = datetime(2026, 1, 1, 0, 0, tzinfo=tz)
    now = datetime(2026, 1, 2, tzinfo=tz)
    assert not ro_policy.activity_in_year(
        ro_policy.normalize_activity_timestamp(late_2025, config=cfg, now=now), cfg
    )
    assert ro_policy.activity_in_year(
        ro_policy.normalize_activity_timestamp(early_2026, config=cfg, now=now), cfg
    )


def test_future_timestamp_denied():
    cfg = ro_policy.resolve_policy_config(year=2026, timezone_name="Asia/Krasnoyarsk")
    now = datetime(2026, 6, 1, tzinfo=cfg.tz)
    future = now + timedelta(days=30)
    assert ro_policy.normalize_activity_timestamp(future, config=cfg, now=now) is None


def test_invalid_timezone_deny():
    with pytest.raises(ro_policy.PolicyError):
        ro_policy.resolve_policy_config(timezone_name="Not/AZone")


def test_empty_static_allowlist_denies_all(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    assert ro_policy.parse_static_allowlist("") == set()
    assert ro_policy.static_allowlist_permits(123) is False


def test_absent_static_allowlist_denies_all(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    assert "TELEGRAM_ALLOWED_CHAT_IDS" not in __import__("os").environ
    assert ro_policy.static_allowlist_permits(123) is False
    assert ro_policy.parse_static_allowlist(None) == set()


def test_broken_allowlist_tokens_deny(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "@@@,###")
    assert ro_policy.parse_static_allowlist("@@@,###") == set()
    assert ro_policy.static_allowlist_permits(123) is False


def test_broadcast_channel_out_of_scope_by_default():
    entity = SimpleNamespace(id=1, megagroup=False, broadcast=True)
    # Channel type check uses isinstance — use telethon types
    from telethon.tl.types import Channel

    ch = Channel(
        id=1,
        title="News",
        photo=None,
        date=datetime(2026, 1, 1),
        megagroup=False,
        broadcast=True,
    )
    assert ro_policy.is_in_scope_dialog_type(ch) is False
    assert ro_policy.is_in_scope_dialog_type(ch, include_broadcast_channels=True) is True


def test_photo_and_image_as_doc_denied():
    ok, _ = ro_policy.attachment_is_downloadable_document(is_photo_media=True)
    assert not ok
    ok, _ = ro_policy.attachment_is_downloadable_document(
        mime_type="image/png", file_name="x.bin"
    )
    assert not ok
    ok, _ = ro_policy.attachment_is_downloadable_document(
        mime_type="application/octet-stream",
        file_name="photo.jpg",
    )
    assert not ok
    ok, _ = ro_policy.attachment_is_downloadable_document(
        mime_type="application/pdf",
        file_name="report.pdf",
        header_bytes=b"%PDF-1.4",
    )
    assert ok
    # Spoofed extension but PNG magic
    ok, reason = ro_policy.attachment_is_downloadable_document(
        mime_type="application/pdf",
        file_name="report.pdf",
        header_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 8,
    )
    assert not ok
    assert "signature" in reason.lower() or "image" in reason.lower()


def test_video_audio_rejected():
    ok, _ = ro_policy.attachment_is_downloadable_document(mime_type="video/mp4", file_name="a.mp4")
    assert not ok
    ok, _ = ro_policy.attachment_is_downloadable_document(mime_type="audio/mpeg", file_name="a.mp3")
    assert not ok


def test_year_rollover_clears_cache():
    cfg26 = ro_policy.resolve_policy_config(year=2026, timezone_name="UTC")
    ro_policy._eligibility_cache[("x",)] = (9999999999.0, True, "eligible")
    ro_policy._cache_year_marker = 2026
    cfg27 = ro_policy.resolve_policy_config(year=2027, timezone_name="UTC")
    ro_policy.invalidate_cache_if_year_changed(cfg27)
    assert ro_policy._eligibility_cache == {}


def test_http_auth_compare():
    assert ro_policy.http_auth_header_ok("Bearer secret", "secret")
    assert not ro_policy.http_auth_header_ok("Bearer wrong", "secret")
    assert not ro_policy.http_auth_header_ok(None, "secret")
    with pytest.raises(ro_policy.PolicyError):
        ro_policy.require_http_auth_token()


def test_runtime_allowlist_patch_absent_denies(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    from telegram_mcp import runtime
    from telegram_mcp.ro_allowlist_patch import apply_to_runtime

    apply_to_runtime(runtime)
    assert runtime.is_chat_allowed(123) is False
    assert runtime.check_chat_access(123) is not None
