#Requires -Version 5.1
param(
    [switch]$ConfirmRemove,
    [string]$InstallRoot = 'C:\ProgramData\telegram-mcp-ro'
)

if (-not $ConfirmRemove) {
    Write-Error 'Refusing remove without -ConfirmRemove. Checklist only when confirmed.'
    exit 2
}

Write-Host 'Manual remove/rollback checklist:'
Write-Host '1. Stop MCP process running as service user'
Write-Host '2. Revoke session in Telegram → Devices'
Write-Host "3. Wipe session/.env/transcripts under $InstallRoot"
Write-Host '4. Remove Connections / MCP client entry'
Write-Host '5. Remove service user only if dedicated and unused'
Write-Host 'No automated destructive actions executed by this stub.'
exit 0
