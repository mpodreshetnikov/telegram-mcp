"""Fail-closed TELEGRAM_ALLOWED_CHAT_IDS patch applied after upstream runtime load."""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional, Union


def apply(ns: dict) -> None:
    """Replace allowlist helpers in the runtime module namespace (fail-closed)."""

    get_marked_id = ns["get_marked_id"]

    def _parse_allowed_chat_ids(
        raw: Optional[Union[str, Iterable[Union[int, str]]]],
    ) -> Optional[set[Union[int, str]]]:
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return set()
            tokens = [t.strip() for t in raw.split(",") if t.strip()]
        elif isinstance(raw, (list, tuple, set)):
            tokens = [str(t).strip() for t in raw if str(t).strip()]
        else:
            return set()
        if not tokens:
            return set()
        allowed: set[Union[int, str]] = set()
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
                clean_handle = token.lstrip("@").strip().lower()
                if clean_handle and clean_handle.replace("_", "").isalnum():
                    allowed.add(clean_handle)
        return allowed

    def _load_allowed_chat_ids() -> Optional[set[Union[int, str]]]:
        if "TELEGRAM_ALLOWED_CHAT_IDS" not in os.environ:
            return None
        return _parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS"))

    def get_effective_allowed_chat_ids() -> Optional[set[Union[int, str]]]:
        if "TELEGRAM_ALLOWED_CHAT_IDS" not in os.environ:
            return ns.get("ALLOWED_CHAT_IDS")
        return _parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS"))

    def is_chat_allowlist_enabled() -> bool:
        return "TELEGRAM_ALLOWED_CHAT_IDS" in os.environ or ns.get("ALLOWED_CHAT_IDS") is not None

    def is_chat_allowed(chat_identifier: Any, entity: Any = None) -> bool:
        if "TELEGRAM_ALLOWED_CHAT_IDS" not in os.environ and ns.get("ALLOWED_CHAT_IDS") is None:
            return True
        allowed = get_effective_allowed_chat_ids()
        if allowed is None:
            return True
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
            try:
                marked_id = get_marked_id(entity)
                if marked_id in allowed:
                    return True
            except Exception:
                pass
            bare_id = getattr(entity, "id", None)
            if isinstance(bare_id, int) and bare_id in allowed:
                return True
            username = getattr(entity, "username", None)
            if username and str(username).lower() in allowed:
                return True
        return False

    def check_chat_access(chat_identifier: Any, entity: Any = None) -> Optional[str]:
        if not is_chat_allowed(chat_identifier, entity):
            return (
                "Access denied by fail-closed chat allowlist "
                "(TELEGRAM_ALLOWED_CHAT_IDS)."
            )
        return None

    ns["_parse_allowed_chat_ids"] = _parse_allowed_chat_ids
    ns["_load_allowed_chat_ids"] = _load_allowed_chat_ids
    ns["ALLOWED_CHAT_IDS"] = _load_allowed_chat_ids()
    ns["get_effective_allowed_chat_ids"] = get_effective_allowed_chat_ids
    ns["is_chat_allowlist_enabled"] = is_chat_allowlist_enabled
    ns["is_chat_allowed"] = is_chat_allowed
    ns["check_chat_access"] = check_chat_access
