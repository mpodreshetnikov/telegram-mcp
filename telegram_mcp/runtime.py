"""Upstream runtime restored via zlib-split bodies (MCP push size limit workaround).

Fail-closed allowlist is applied after load by telegram_mcp.ro_allowlist_patch.
"""
from __future__ import annotations

import base64
import zlib
from pathlib import Path

_dir = Path(__file__).resolve().parent
_parts = [
    'runtime_body_z0.b64',
    'runtime_body_z1.b64',
    'runtime_body_z2.b64',
    'runtime_body_z3.b64',
    'runtime_body_z4.b64',
    'runtime_body_z5.b64'
]
_b64 = "".join((_dir / p).read_text(encoding="utf-8").strip() for p in _parts)
_src = zlib.decompress(base64.b64decode(_b64)).decode("utf-8")
exec(compile(_src, str(_dir / "runtime.py"), "exec"), globals())

from telegram_mcp import ro_allowlist_patch as _ro_allowlist_patch

_ro_allowlist_patch.apply(globals())
