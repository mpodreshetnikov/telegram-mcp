"""Import-time fail-closed allowlist patch for untouched upstream runtime.py.

Does NOT replace or exec-load runtime. Monkeypatches allowlist helpers on the
already-imported ``telegram_mcp.runtime`` module.

Semantics (Maxim 2026-11-08):
  - TELEGRAM_ALLOWED_CHAT_IDS absent → DENY ALL
  - empty / whitespace / unparseable → DENY ALL
  - non-empty parseable IDs/handles → allow only those (year gate still applies in RO tools)
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional, Union


def _parse_allowed_chat_ids(
    raw: Optional[Union[str, Iterable[Union[int, str]]]],
) -> set[Union[int, str]]:
    """Always returns a set. Empty set means deny-all."""
    if raw is None:
        return set()
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return set()
        tokens = [t.strip() for t in text.split(",") if t.strip()]
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
            handle = token.lstrip("@").strip().lower()
            if handle and handle.replace("_", "").isalnum():
                allowed.add(handle)
    return allowed


def apply_to_runtime(runtime_module: Any = None) -> None:
    """Patch allowlist helpers on upstream runtime (import-time, no exec)."""
    if runtime_module is None:
        from telegram_mcp import runtime as runtime_module

    get_marked_id = runtime_module.get_marked_id

    def get_effective_allowed_chat_ids() -> set[Union[int, str]]:
        # Absent env → empty set → deny all (fail-closed).
        if "TELEGRAM_ALLOWED_CHAT_IDS" not in os.environ:
            return set()
        return _parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS"))

    def is_chat_allowlist_enabled() -> bool:
        # Always enforce in this hardened fork.
        return True

    def is_chat_allowed(chat_identifier: Any, entity: Any = None) -> bool:
        allowed = get_effective_allowed_chat_ids()
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
                marked = get_marked_id(entity)
                if marked in allowed:
                    return True
            except Exception:
                pass
            bare = getattr(entity, "id", None)
            if isinstance(bare, int) and bare in allowed:
                return True
            username = getattr(entity, "username", None)
            if username and str(username).lower() in allowed:
                return True
        return False

    def check_chat_access(chat_identifier: Any, entity: Any = None) -> Optional[str]:
        if not is_chat_allowed(chat_identifier, entity):
            return (
                "Access denied by fail-closed chat allowlist "
                "(TELEGRAM_ALLOWED_CHAT_IDS missing/empty/broken or chat not listed)."
            )
        return None

    runtime_module._parse_allowed_chat_ids = (  # type: ignore[attr-defined]
        lambda raw: (_parse_allowed_chat_ids(raw) if raw is not None else set())
    )
    runtime_module.get_effective_allowed_chat_ids = get_effective_allowed_chat_ids
    runtime_module.is_chat_allowlist_enabled = is_chat_allowlist_enabled
    runtime_module.is_chat_allowed = is_chat_allowed
    runtime_module.check_chat_access = check_chat_access
    runtime_module.ALLOWED_CHAT_IDS = get_effective_allowed_chat_ids()
