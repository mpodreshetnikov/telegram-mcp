"""Minimal read-only MCP tool surface for the hardened fork.

Only tools in ro_policy.RO_ALLOWED_TOOLS are registered. Every chat path shares
require_chat_eligible. mark-read / writes / contacts / resolve / global search
are intentionally absent.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, List, Optional, Union
from uuid import uuid4

from mcp.types import ToolAnnotations
from telethon.tl import types

from sanitize import sanitize_name, sanitize_user_content
from telegram_mcp import ro_policy
from telegram_mcp.runtime import (
    Context,
    ErrorCategory,
    ChatAccessDeniedError,
    MAX_FILE_BYTES,
    ensure_connected,
    get_client,
    get_entity_type,
    get_marked_id,
    json_serializer,
    log_and_format_error,
    mcp,
    person_name,
    resolve_entity,
    validate_id,
    with_account,
    _resolve_writable_file_path,
)

# Transcription / cloud media must stay off in this fork.
os.environ.setdefault("TELEGRAM_TRANSCRIBE", "off")
os.environ.pop("TELEGRAM_MEDIA_BASE_URL", None)


class _DownloadLimitExceeded(Exception):
    pass


def _deny(tool: str, reason: str, **ctx: Any) -> str:
    return log_and_format_error(
        tool,
        ChatAccessDeniedError(reason),
        prefix=ErrorCategory.PRIVACY,
        user_message=reason,
        **ctx,
    )


def _document_meta_from_message(msg: Any) -> dict[str, Any]:
    media = getattr(msg, "media", None)
    if isinstance(media, types.MessageMediaPhoto):
        return {"is_photo_media": True}
    if isinstance(media, types.MessageMediaDocument):
        doc = media.document
        if isinstance(doc, types.Document):
            mime = getattr(doc, "mime_type", None)
            file_name = None
            for attr in doc.attributes or []:
                if isinstance(attr, types.DocumentAttributeFilename):
                    file_name = attr.file_name
                    break
            return {
                "is_photo_media": False,
                "mime_type": mime,
                "file_name": file_name,
                "attributes": list(doc.attributes or []),
            }
    file_obj = getattr(msg, "file", None)
    if file_obj is not None:
        return {
            "is_photo_media": bool(getattr(msg, "photo", None)),
            "mime_type": getattr(file_obj, "mime_type", None),
            "file_name": getattr(file_obj, "name", None),
            "attributes": [],
        }
    return {"is_photo_media": bool(getattr(msg, "photo", None))}


def _format_message_record(msg: Any, chat_id: int) -> dict[str, Any]:
    """Minimal sanitized message record (no photo bytes / no raw media dump)."""
    text = getattr(msg, "message", None) or ""
    sender = getattr(msg, "sender_id", None)
    date = getattr(msg, "date", None)
    record: dict[str, Any] = {
        "id": getattr(msg, "id", None),
        "chat_id": chat_id,
        "date": date.isoformat() if date is not None else None,
        "sender_id": int(sender) if sender is not None else None,
        "text": sanitize_user_content(text, max_length=8000),
    }
    media = getattr(msg, "media", None)
    if media is not None:
        meta = _document_meta_from_message(msg)
        kind, _ = ro_policy.classify_attachment(**meta)
        record["attachment_kind"] = kind
        if meta.get("file_name"):
            record["file_name"] = sanitize_name(str(meta["file_name"]))
        if meta.get("mime_type"):
            record["mime_type"] = str(meta["mime_type"])
    return record


async def _gate(cl, chat_id, entity) -> Optional[str]:
    return await ro_policy.require_chat_eligible(cl, entity, chat_id)


@mcp.tool(
    annotations=ToolAnnotations(
        title="List Eligible Chats", openWorldHint=True, readOnlyHint=True
    )
)
@with_account(readonly=True)
async def list_eligible_chats(
    limit: int = 50,
    account: Optional[str] = None,
) -> str:
    """List private chats / groups / supergroups eligible under year-activity policy.

    Broadcast channels are omitted unless TELEGRAM_RO_INCLUDE_BROADCAST_CHANNELS is set.
    Ineligible dialogs are not described to the client.
    """
    try:
        cfg = ro_policy.resolve_policy_config()
    except ro_policy.PolicyError as exc:
        return _deny("list_eligible_chats", str(exc))

    try:
        cl = get_client(account)
        await ensure_connected(cl)
        dialogs = await cl.get_dialogs(limit=max(1, min(int(limit), 200)))
        records: List[dict] = []
        for dialog in dialogs:
            entity = dialog.entity
            ok, _reason = await ro_policy.evaluate_chat_eligibility(
                cl,
                entity,
                get_marked_id(entity),
                config=cfg,
                dialog=dialog,
            )
            if not ok:
                continue
            record: dict[str, Any] = {
                "chat_id": get_marked_id(entity),
                "type": get_entity_type(entity),
            }
            if hasattr(entity, "title"):
                record["title"] = sanitize_name(entity.title)
            elif hasattr(entity, "first_name"):
                record["name"] = sanitize_name(person_name(entity))
            records.append(record)
        return json.dumps(
            {
                "year": cfg.year,
                "timezone": cfg.timezone_name,
                "chats": records,
                "boundary": ro_policy.BOUNDARY_NOTICE,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        return log_and_format_error("list_eligible_chats", e, limit=limit)


@mcp.tool(
    annotations=ToolAnnotations(title="Get Messages", openWorldHint=True, readOnlyHint=True)
)
@with_account(readonly=True)
@validate_id("chat_id")
async def get_messages(
    chat_id: Union[int, str],
    page: int = 1,
    page_size: int = 20,
    account: Optional[str] = None,
) -> str:
    """Paginated messages from an eligible chat. Full history once eligible.

    Does not mark messages as read.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        denied = await _gate(cl, chat_id, entity)
        if denied:
            return _deny("get_messages", denied, chat_id=chat_id)

        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 100))
        offset = (page - 1) * page_size
        messages = await cl.get_messages(entity, limit=page_size, add_offset=offset)
        if not messages:
            return "No messages found for this page."
        numeric_chat_id = get_marked_id(entity)
        records = [_format_message_record(msg, numeric_chat_id) for msg in messages]
        return json.dumps(records, indent=2, default=json_serializer, ensure_ascii=False)
    except Exception as e:
        return log_and_format_error(
            "get_messages", e, chat_id=chat_id, page=page, page_size=page_size
        )


@mcp.tool(
    annotations=ToolAnnotations(title="Get History", openWorldHint=True, readOnlyHint=True)
)
@with_account(readonly=True)
@validate_id("chat_id")
async def get_history(
    chat_id: Union[int, str],
    limit: int = 100,
    account: Optional[str] = None,
) -> str:
    """Fetch up to `limit` recent messages from an eligible chat (full history allowed).

    Does not mark messages as read. Does not call send_read_acknowledge.
    """
    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        denied = await _gate(cl, chat_id, entity)
        if denied:
            return _deny("get_history", denied, chat_id=chat_id)

        limit = max(1, min(int(limit), 500))
        messages = await cl.get_messages(entity, limit=limit)
        if not messages:
            return "No messages found."
        numeric_chat_id = get_marked_id(entity)
        records = [_format_message_record(msg, numeric_chat_id) for msg in messages]
        return json.dumps(records, indent=2, default=json_serializer, ensure_ascii=False)
    except Exception as e:
        return log_and_format_error("get_history", e, chat_id=chat_id, limit=limit)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Message Read By", openWorldHint=True, readOnlyHint=True
    )
)
@with_account(readonly=True)
@validate_id("chat_id")
async def get_message_read_by(
    chat_id: Union[int, str], message_id: int, account: Optional[str] = None
) -> str:
    """View read receipts for a message in an eligible chat (read-only).

    Does not mark the message as read.
    """
    try:
        from telethon import functions
        from telethon.errors.rpcerrorlist import (
            ChatAdminRequiredError,
            MsgTooOldError,
            PeerIdInvalidError,
            UserNotParticipantError,
        )

        cl = get_client(account)
        await ensure_connected(cl)
        entity = await resolve_entity(chat_id, cl)
        denied = await _gate(cl, chat_id, entity)
        if denied:
            return _deny("get_message_read_by", denied, chat_id=chat_id)

        try:
            result = await cl(
                functions.messages.GetMessageReadParticipantsRequest(
                    peer=entity, msg_id=message_id
                )
            )
        except MsgTooOldError:
            return "Read receipts unavailable: message too old or receipts disabled."
        except ChatAdminRequiredError:
            return "Read receipts unavailable: admin rights required."
        except UserNotParticipantError:
            return "Read receipts unavailable: not a participant."
        except PeerIdInvalidError:
            return "Invalid chat."

        if not result:
            return "No read receipts available."

        readers = []
        for item in result:
            if hasattr(item, "user_id"):
                readers.append(
                    {
                        "user_id": item.user_id,
                        "read_at": item.date.isoformat() if getattr(item, "date", None) else None,
                    }
                )
            else:
                readers.append({"user_id": item, "read_at": None})

        return json.dumps(
            {
                "chat_id": str(get_marked_id(entity)),
                "message_id": message_id,
                "read_by": readers,
                "count": len(readers),
            },
            indent=2,
            default=json_serializer,
            ensure_ascii=False,
        )
    except Exception as e:
        return log_and_format_error(
            "get_message_read_by", e, chat_id=chat_id, message_id=message_id
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Download Document", openWorldHint=True, readOnlyHint=True
    )
)
@with_account(readonly=True)
@validate_id("chat_id")
async def download_document(
    chat_id: Union[int, str],
    message_id: int,
    file_path: Optional[str] = None,
    ctx: Optional[Context] = None,
    account: Optional[str] = None,
) -> str:
    """Download a non-image document from an eligible chat message only.

    Photos, image MIME, image-as-document, video/audio, thumbnails, and arbitrary
    URL/path fetches are rejected. No cloud media transfer URLs.
    """
    # Hard-block remote media URL feature in this tool path
    if os.getenv("TELEGRAM_MEDIA_BASE_URL"):
        return _deny(
            "download_document",
            "TELEGRAM_MEDIA_BASE_URL is disabled in the read-only fork.",
            chat_id=chat_id,
        )

    try:
        cl = get_client(account)
        entity = await resolve_entity(chat_id, cl)
        denied = await _gate(cl, chat_id, entity)
        if denied:
            return _deny("download_document", denied, chat_id=chat_id)

        msg = await cl.get_messages(entity, ids=message_id)
        if not msg:
            return _deny(
                "download_document",
                "Message not found in eligible chat.",
                chat_id=chat_id,
                message_id=message_id,
            )

        # Reject URL / UNC inputs before any roots resolution (no SSRF / path bypass).
        if file_path:
            lowered = file_path.strip().lower()
            if "://" in lowered or lowered.startswith("\\\\"):
                return _deny(
                    "download_document",
                    "Arbitrary URL/UNC download paths are forbidden.",
                    chat_id=chat_id,
                )

        meta = _document_meta_from_message(msg)
        allowed, reason = ro_policy.attachment_is_downloadable_document(**meta)
        if not allowed:
            return _deny("download_document", reason, chat_id=chat_id, message_id=message_id)

        if not msg.media:
            return _deny(
                "download_document",
                "No document attachment on this message.",
                chat_id=chat_id,
                message_id=message_id,
            )

        limit = MAX_FILE_BYTES.get("download_media", 50 * 1024 * 1024)
        default_name = f"telegram_doc_{get_marked_id(entity)}_{message_id}_{uuid4().hex}"
        out_path, path_error = await _resolve_writable_file_path(
            raw_path=file_path,
            default_filename=default_name,
            ctx=ctx,
            tool_name="download_document",
        )
        if path_error:
            return path_error

        temp_dir = Path(
            tempfile.mkdtemp(prefix=".telegram-mcp-ro-doc-", dir=str(out_path.parent))
        )
        try:
            staged = temp_dir / (out_path.name or "document")

            def enforce_limit(received: int, total: int) -> None:
                if received > limit or (total and total > limit):
                    raise _DownloadLimitExceeded

            try:
                downloaded = await cl.download_media(
                    msg,
                    file=str(staged),
                    progress_callback=enforce_limit,
                )
            except _DownloadLimitExceeded:
                return f"Document exceeds download limit ({limit} bytes)."

            if not downloaded:
                return _deny("download_document", "Download failed.", chat_id=chat_id)

            temp_final = Path(downloaded).resolve(strict=True)
            # Re-check magic bytes after download (spoofed extension/MIME)
            header = temp_final.read_bytes()[:64]
            allowed2, reason2 = ro_policy.attachment_is_downloadable_document(
                is_photo_media=False,
                mime_type=meta.get("mime_type"),
                file_name=meta.get("file_name") or temp_final.name,
                attributes=meta.get("attributes"),
                header_bytes=header,
            )
            if not allowed2:
                temp_final.unlink(missing_ok=True)
                return _deny("download_document", reason2, chat_id=chat_id)

            final_path = out_path
            if temp_final.suffix:
                final_path, path_error = await _resolve_writable_file_path(
                    raw_path=str(out_path.with_suffix(temp_final.suffix)),
                    default_filename=default_name,
                    ctx=ctx,
                    tool_name="download_document",
                )
                if path_error:
                    return path_error
            os.replace(temp_final, final_path)
            # Never emit media transfer URLs
            return json.dumps(
                {
                    "status": "ok",
                    "path": str(final_path),
                    "chat_id": get_marked_id(entity),
                    "message_id": message_id,
                    "note": sanitize_user_content(
                        "Document saved locally under allowed roots. No preview/URL emitted.",
                        max_length=200,
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as e:
        return log_and_format_error(
            "download_document", e, chat_id=chat_id, message_id=message_id
        )


__all__ = [
    "list_eligible_chats",
    "get_messages",
    "get_history",
    "get_message_read_by",
    "download_document",
]
