# Hardened read-only fork

Pinned upstream base: `1451ae72a7a8fe92f0af018b3a1b000ca4d5b5fe` (tag v3.3.11).

## Tool surface

Only: `list_eligible_chats`, `get_messages`, `get_history`, `get_message_read_by`, `download_document`.

- Year-activity eligibility (`TELEGRAM_RO_YEAR`, `TELEGRAM_RO_TIMEZONE`, default Asia/Krasnoyarsk).
- Full history once eligible; ineligible dialogs return no metadata/history.
- Broadcast channels excluded unless `TELEGRAM_RO_INCLUDE_BROADCAST_CHANNELS=1`.
- Static `TELEGRAM_ALLOWED_CHAT_IDS`: unset = no static filter; empty/broken = deny all.
- Documents only; photos / image-as-doc / video / audio forbidden.
- `mark_as_read` / writes absent. HTTP requires `TELEGRAM_MCP_AUTH_TOKEN`; SSE disabled.
- Transcribe/cloud media URLs off.

## Year rollover & cache

Eligibility cache key includes year+timezone. Changing `TELEGRAM_RO_YEAR` or crossing the configured calendar year clears cache (`invalidate_cache_if_year_changed`). TTL: `TELEGRAM_RO_ELIGIBILITY_CACHE_TTL` (seconds, default 300; `0` disables).

## Boundary

Telegram sessions are not read-only. Service user + NTFS ACL reduce risk; they do not stop admin local-exec.

## Tests

```bash
pytest tests/test_ro_policy.py tests/test_ro_surface.py tests/test_ro_http_auth.py -q
```

Fake Telegram client only — no live network/session.
