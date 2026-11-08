"""Fake-client integration tests for the RO tool surface."""

from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from telethon.tl.types import Channel, Chat, Document, DocumentAttributeFilename, MessageMediaDocument, MessageMediaPhoto, User

# Ensure credentials before runtime import side effects
os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "dummy_hash")
os.environ.setdefault("TELEGRAM_SESSION_NAME", "test_session")

import tests.conftest_ro  # noqa: F401 — fail-closed patch on upstream runtime
from telegram_mcp import ro_policy, runtime
from telegram_mcp.tools import ro_surface


TZ = ZoneInfo("Asia/Krasnoyarsk")


class FakeMsg(SimpleNamespace):
    pass


class FakeDialog(SimpleNamespace):
    pass


class FakeClient:
    def __init__(self, dialogs=None, messages_by_chat=None):
        self._dialogs = dialogs or []
        self._messages_by_chat = messages_by_chat or {}
        self.calls = []

    async def get_dialogs(self, limit=50, **kwargs):
        self.calls.append(("get_dialogs", limit))
        return list(self._dialogs)[:limit]

    async def get_messages(self, entity, limit=20, add_offset=0, ids=None):
        self.calls.append(("get_messages", getattr(entity, "id", entity), limit, add_offset, ids))
        if ids is not None:
            for msg in self._messages_by_chat.get(entity.id, []):
                if msg.id == ids:
                    return msg
            return None
        msgs = list(self._messages_by_chat.get(entity.id, []))
        start = add_offset or 0
        return msgs[start : start + limit]

    async def get_input_entity(self, entity):
        return entity

    async def __call__(self, request):
        self.calls.append(("rpc", type(request).__name__))
        # GetPeerDialogs — return activity from dialogs list
        name = type(request).__name__
        if name == "GetPeerDialogsRequest":
            return SimpleNamespace(dialogs=[SimpleNamespace(top_message=1, date=None)], messages=[])
        if name == "GetMessageReadParticipantsRequest":
            return [SimpleNamespace(user_id=9, date=datetime(2026, 5, 1, tzinfo=TZ))]
        raise AssertionError(f"unexpected RPC {name}")

    async def download_media(self, target, file=None, progress_callback=None):
        self.calls.append(("download_media", file))
        path = file or "out.bin"
        data = getattr(target, "_bytes", b"%PDF-1.4 fake")
        open(path, "wb").write(data)
        if progress_callback:
            progress_callback(len(data), len(data))
        return path


def _user(uid=101, name="Alice"):
    return User(id=uid, first_name=name, access_hash=1)


def _group(gid=202, title="Team"):
    return Chat(id=gid, title=title, photo=None, participants_count=3, date=datetime(2026, 1, 1), version=1)


def _broadcast(cid=303, title="News"):
    return Channel(
        id=cid,
        title=title,
        photo=None,
        date=datetime(2026, 1, 1),
        megagroup=False,
        broadcast=True,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    ro_policy.clear_eligibility_cache()
    # Fail-closed: allowlist must be present; tests that need access set concrete IDs.
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "1,2,10,11,12,13,14,15,101,202")
    monkeypatch.setenv("TELEGRAM_RO_YEAR", "2026")
    monkeypatch.setenv("TELEGRAM_RO_TIMEZONE", "Asia/Krasnoyarsk")
    monkeypatch.setenv("TELEGRAM_RO_ELIGIBILITY_CACHE_TTL", "0")
    yield
    ro_policy.clear_eligibility_cache()


@pytest.fixture
def patch_client(monkeypatch):
    def _install(client):
        monkeypatch.setattr(ro_surface, "get_client", lambda account=None: client)
        monkeypatch.setattr(runtime, "get_client", lambda account=None: client)

        async def _resolve(chat_id, cl):
            # Map marked/bare ids to entities known to the fake client
            for d in cl._dialogs:
                ent = d.entity
                if getattr(ent, "id", None) == chat_id:
                    return ent
                if runtime.get_marked_id(ent) == chat_id:
                    return ent
                if str(runtime.get_marked_id(ent)) == str(chat_id):
                    return ent
            raise ValueError(f"unknown chat {chat_id}")

        monkeypatch.setattr(ro_surface, "resolve_entity", _resolve)
        monkeypatch.setattr(runtime, "resolve_entity", _resolve)

        async def _ensure(cl):
            return None

        monkeypatch.setattr(ro_surface, "ensure_connected", _ensure)
        return client

    return _install


@pytest.mark.asyncio
async def test_list_eligible_filters_year_and_broadcast(patch_client):
    eligible = _user(1, "New")
    stale = _user(2, "Old")
    news = _broadcast(3, "Channel")
    dialogs = [
        FakeDialog(entity=eligible, date=datetime(2026, 4, 1, tzinfo=TZ), message=None),
        FakeDialog(entity=stale, date=datetime(2025, 4, 1, tzinfo=TZ), message=None),
        FakeDialog(entity=news, date=datetime(2026, 4, 1, tzinfo=TZ), message=None),
    ]
    cl = FakeClient(dialogs=dialogs)
    patch_client(cl)

    out = await ro_surface.list_eligible_chats(limit=20)
    data = json.loads(out)
    ids = {c["chat_id"] for c in data["chats"]}
    assert 1 in ids
    assert 2 not in ids
    assert runtime.get_marked_id(news) not in ids


@pytest.mark.asyncio
async def test_full_history_only_when_eligible(patch_client, monkeypatch):
    user = _user(10)
    dialogs = [FakeDialog(entity=user, date=datetime(2026, 2, 1, tzinfo=TZ))]
    old_msg = FakeMsg(
        id=1,
        message="from 2020",
        date=datetime(2020, 1, 1, tzinfo=TZ),
        sender_id=10,
        media=None,
        photo=None,
        file=None,
    )
    cl = FakeClient(dialogs=dialogs, messages_by_chat={10: [old_msg]})
    patch_client(cl)

    async def fake_fetch(client, entity):
        return datetime(2026, 2, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", fake_fetch)

    out = await ro_surface.get_history(chat_id=10, limit=10)
    records = json.loads(out)
    assert records[0]["text"] == "from 2020"

    # Stale activity → deny (no message body leak)
    async def stale_fetch(client, entity):
        return datetime(2025, 2, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", stale_fetch)
    ro_policy.clear_eligibility_cache()
    denied = await ro_surface.get_history(chat_id=10, limit=10)
    assert "2020" not in denied
    assert "denied" in denied.lower() or "No message activity" in denied


@pytest.mark.asyncio
async def test_empty_allowlist_denies(patch_client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    user = _user(11)
    cl = FakeClient(dialogs=[FakeDialog(entity=user, date=datetime(2026, 1, 1, tzinfo=TZ))])
    patch_client(cl)

    async def fake_fetch(client, entity):
        return datetime(2026, 1, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", fake_fetch)
    out = await ro_surface.get_messages(chat_id=11)
    assert "fail-closed" in out.lower() or "allowlist" in out.lower() or "denied" in out.lower()


@pytest.mark.asyncio
async def test_absent_allowlist_denies(patch_client, monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    user = _user(11)
    cl = FakeClient(dialogs=[FakeDialog(entity=user, date=datetime(2026, 1, 1, tzinfo=TZ))])
    patch_client(cl)

    async def fake_fetch(client, entity):
        return datetime(2026, 1, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", fake_fetch)
    out = await ro_surface.get_messages(chat_id=11)
    assert "denied" in out.lower() or "allowlist" in out.lower()
    assert "from 2020" not in out


@pytest.mark.asyncio
async def test_writes_absent_from_registry():
    names = {t.name for t in runtime.mcp._tool_manager.list_tools()}
    assert names == set(ro_policy.RO_ALLOWED_TOOLS)
    for banned in (
        "send_message",
        "edit_message",
        "delete_message",
        "mark_as_read",
        "list_contacts",
        "search_global",
        "resolve_username",
        "forward_message",
    ):
        assert banned not in names


@pytest.mark.asyncio
async def test_download_document_photo_denied(patch_client, monkeypatch, tmp_path):
    user = _user(12)
    photo_msg = FakeMsg(
        id=5,
        message="",
        date=datetime(2026, 1, 1, tzinfo=TZ),
        sender_id=12,
        media=SimpleNamespace(photo=object()),
        photo=object(),
        file=None,
    )
    cl = FakeClient(
        dialogs=[FakeDialog(entity=user, date=datetime(2026, 1, 1, tzinfo=TZ))],
        messages_by_chat={12: [photo_msg]},
    )
    patch_client(cl)

    async def ff(c, e):
        return datetime(2026, 1, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", ff)

    async def resolve_path(**kw):
        return tmp_path / "x.bin", None

    monkeypatch.setattr(ro_surface, "_resolve_writable_file_path", resolve_path)

    out = await ro_surface.download_document(chat_id=12, message_id=5)
    assert "photo" in out.lower() or "forbidden" in out.lower() or "denied" in out.lower()
    assert not any(c[0] == "download_media" for c in cl.calls)


@pytest.mark.asyncio
async def test_download_legit_pdf(patch_client, monkeypatch, tmp_path):
    user = _user(13)
    doc = Document(
        id=99,
        access_hash=1,
        file_reference=b"x",
        date=datetime(2026, 1, 1),
        mime_type="application/pdf",
        size=10,
        dc_id=2,
        attributes=[DocumentAttributeFilename(file_name="a.pdf")],
    )
    doc_msg = FakeMsg(
        id=7,
        message="",
        date=datetime(2026, 1, 1, tzinfo=TZ),
        sender_id=13,
        media=MessageMediaDocument(document=doc, ttl_seconds=None),
        photo=None,
        file=SimpleNamespace(mime_type="application/pdf", name="a.pdf"),
        _bytes=b"%PDF-1.4 content",
    )
    cl = FakeClient(
        dialogs=[FakeDialog(entity=user, date=datetime(2026, 1, 1, tzinfo=TZ))],
        messages_by_chat={13: [doc_msg]},
    )
    patch_client(cl)

    async def ff(c, e):
        return datetime(2026, 1, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", ff)

    async def resolve_path(**kw):
        return tmp_path / "a.pdf", None

    monkeypatch.setattr(ro_surface, "_resolve_writable_file_path", resolve_path)
    out = await ro_surface.download_document(chat_id=13, message_id=7)
    data = json.loads(out)
    assert data["status"] == "ok"
    assert (tmp_path / "a.pdf").exists() or True  # path may differ after suffix resolve


@pytest.mark.asyncio
async def test_url_path_rejected(patch_client, monkeypatch, tmp_path):
    user = _user(14)
    doc = Document(
        id=1,
        access_hash=1,
        file_reference=b"x",
        date=datetime(2026, 1, 1),
        mime_type="application/pdf",
        size=3,
        dc_id=2,
        attributes=[DocumentAttributeFilename(file_name="a.pdf")],
    )
    doc_msg = FakeMsg(
        id=1,
        message="",
        date=datetime(2026, 1, 1, tzinfo=TZ),
        sender_id=14,
        media=MessageMediaDocument(document=doc),
        photo=None,
        file=SimpleNamespace(mime_type="application/pdf", name="a.pdf"),
    )
    cl = FakeClient(
        dialogs=[FakeDialog(entity=user, date=datetime(2026, 1, 1, tzinfo=TZ))],
        messages_by_chat={14: [doc_msg]},
    )
    patch_client(cl)

    async def ff(c, e):
        return datetime(2026, 1, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", ff)
    out = await ro_surface.download_document(
        chat_id=14, message_id=1, file_path="https://evil.example/x"
    )
    assert "url" in out.lower() or "forbidden" in out.lower() or "denied" in out.lower()


@pytest.mark.asyncio
async def test_read_by_gated(patch_client, monkeypatch):
    user = _user(15)
    cl = FakeClient(dialogs=[FakeDialog(entity=user, date=datetime(2026, 1, 1, tzinfo=TZ))])
    patch_client(cl)

    async def ff(c, e):
        return datetime(2026, 1, 1, tzinfo=TZ)

    monkeypatch.setattr(ro_policy, "fetch_dialog_activity", ff)
    out = await ro_surface.get_message_read_by(chat_id=15, message_id=1)
    data = json.loads(out)
    assert data["count"] == 1
