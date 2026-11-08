"""Upstream runtime restored via split bodies (MCP push size limit workaround).

Fail-closed allowlist is applied after load by telegram_mcp.ro_allowlist_patch.
"""
from __future__ import annotations

from pathlib import Path

_dir = Path(__file__).resolve().parent
_src = (_dir / "runtime_body_a.py").read_text(encoding="utf-8") + (
    _dir / "runtime_body_b.py"
).read_text(encoding="utf-8")
exec(compile(_src, str(_dir / "runtime.py"), "exec"), globals())

from telegram_mcp import ro_allowlist_patch as _ro_allowlist_patch

_ro_allowlist_patch.apply(globals())
