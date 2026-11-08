"""Apply fail-closed allowlist patch for RO tests (upstream runtime left untouched)."""

from telegram_mcp import runtime
from telegram_mcp.ro_allowlist_patch import apply_to_runtime

apply_to_runtime(runtime)
