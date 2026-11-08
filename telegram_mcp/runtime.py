"""Upstream runtime restored via split bodies (MCP push size limit workaround).

Fail-closed allowlist is applied after load by telegram_mcp.ro_allowlist_patch.
"""
from __future__ import annotations

from pathlib import Path

_dir = Path(__file__).resolve().parent
_parts = [
    'runtime_body_a0.py',
    'runtime_body_a1.py',
    'runtime_body_a2.py',
    'runtime_body_a3.py',
    'runtime_body_a4.py',
    'runtime_body_b0.py',
    'runtime_body_b1.py',
    'runtime_body_b2.py',
    'runtime_body_b3.py',
    'runtime_body_b4.py',
]
_src = "".join((_dir / p).read_text(encoding="utf-8") for p in _parts)
exec(compile(_src, str(_dir / "runtime.py"), "exec"), globals())

from telegram_mcp import ro_allowlist_patch as _ro_allowlist_patch

_ro_allowlist_patch.apply(globals())
