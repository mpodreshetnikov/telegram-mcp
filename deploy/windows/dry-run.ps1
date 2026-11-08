#Requires -Version 5.1
<#
.SYNOPSIS
  Dry-run only: print intended Windows RO MCP layout. Makes NO changes.

.DESCRIPTION
  Must not create users, ACLs, directories, installs, or network rules.
  Parameterized paths/year/timezone for portability.
#>
param(
    [string]$InstallRoot = 'C:\ProgramData\telegram-mcp-ro',
    [string]$ServiceUser = 'telegram-mcp-ro',
    [string]$PinnedSha = '1451ae72a7a8fe92f0af018b3a1b000ca4d5b5fe',
    [string]$ForkUrl = 'https://github.com/mpodreshetnikov/telegram-mcp.git',
    [int]$RoYear = 0,
    [string]$RoTimezone = 'Asia/Krasnoyarsk'
)

$ErrorActionPreference = 'Stop'
if ($RoYear -le 0) {
    # Derived at apply time in Asia/Krasnoyarsk; shown as placeholder in dry-run.
    $RoYear = (Get-Date).Year
}

Write-Host '=== telegram-mcp RO dry-run (NO CHANGES) ==='
Write-Host "Would use service user: $ServiceUser (not created in dry-run)"
Write-Host "Would protect data root: $InstallRoot (ACL not modified in dry-run)"
Write-Host "Would clone: $ForkUrl"
Write-Host "Would checkout SHA: $PinnedSha"
Write-Host "Would set TELEGRAM_RO_YEAR=$RoYear TELEGRAM_RO_TIMEZONE=$RoTimezone"
Write-Host 'Would prefer MCP_TRANSPORT=stdio; HTTP only with TELEGRAM_MCP_AUTH_TOKEN on loopback'
Write-Host 'Would keep session/credentials outside agent local-exec paths'
Write-Host 'Login/network/tunnels/firewall: NOT CHOSEN — skipped'
Write-Host 'Dry-run complete: zero changes made.'
exit 0
