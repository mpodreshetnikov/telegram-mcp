# Windows deploy artifacts (parameterized, later apply)

**Not verified on Maxim's PC.** Runtime versions/permissions unknown. These files are plans + dry-run scripts only.

## Honest boundary

Telegram has **no** native read-only session. Use a dedicated unprivileged Windows service account and NTFS ACLs on session/credential/policy storage. This does **not** stop an administrator or other local-exec on the same machine. The MCP agent must not control the allow/eligibility filter files.

## Phases (do not blur)

| Phase | Meaning |
| --- | --- |
| plan | Decide chats/login/transport offline |
| dry-run | Print intended actions; **no filesystem/account/ACL/install changes** |
| apply | Create service user, ACL dirs, venv, pin clone — only after explicit approval |
| verify | Process user, tool list, loopback/auth checks |
| remove / rollback | Stop process, wipe session/env, revoke Telegram device, remove ACL dir |

Login method, network, tunnels, and firewall are **not chosen** here — do not invent them at apply time.

## Pin

- Upstream fork: `mpodreshetnikov/telegram-mcp`
- Base upstream SHA: `1451ae72a7a8fe92f0af018b3a1b000ca4d5b5fe` (v3.3.11)
- Prefer git clone + checkout SHA (not unpinned `uvx` / PyPI `telegram-mcp`)

## Required env (values local only — never cloud/repo/logs)

- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`
- Session via stock mechanism under service user (file or string) — path outside agent-writable dirs
- `TELEGRAM_RO_YEAR` / `TELEGRAM_RO_TIMEZONE=Asia/Krasnoyarsk` (parameterized)
- Optional fail-closed `TELEGRAM_ALLOWED_CHAT_IDS`
- `MCP_TRANSPORT=stdio` preferred; HTTP needs `TELEGRAM_MCP_AUTH_TOKEN` + loopback
- `TELEGRAM_TRANSCRIBE=off`

## Scripts

- `dry-run.ps1` — prints plan; exits 0 without changes
- `apply.ps1` — **blocked until** `-ConfirmApply:$true`; still does not invent login/network
- `remove.ps1` — teardown checklist

License remains Apache-2.0 from upstream.
