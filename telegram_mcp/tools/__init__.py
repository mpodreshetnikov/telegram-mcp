"""Register only the hardened read-only MCP tool surface.

Upstream write/contact/search modules are intentionally not imported so their
``@mcp.tool`` decorators never run.
"""

from telegram_mcp.tools.ro_surface import *  # noqa: F401,F403

__all__ = [name for name in globals() if not name.startswith("_")]
