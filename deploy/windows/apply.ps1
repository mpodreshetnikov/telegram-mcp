#Requires -Version 5.1
<#
.SYNOPSIS
  Apply phase stub. Refuses to run unless -ConfirmApply is set.

.DESCRIPTION
  Does not invent login, network, tunnels, or firewall. Does not store secrets in repo.
  Account/ACL/install creation is APPLY — never call this "dry-run".
#>
param(
    [switch]$ConfirmApply,
    [string]$InstallRoot = 'C:\ProgramData\telegram-mcp-ro',
    [string]$ServiceUser = 'telegram-mcp-ro',
    [string]$PinnedSha = '1451ae72a7a8fe92f0af018b3a1b000ca4d5b5fe',
    [string]$ForkUrl = 'https://github.com/mpodreshetnikov/telegram-mcp.git'
)

$ErrorActionPreference = 'Stop'
if (-not $ConfirmApply) {
    Write-Error 'Refusing apply: pass -ConfirmApply after plan review. Use dry-run.ps1 for no-change preview.'
    exit 2
}

Write-Host 'APPLY is operator-attended on the target Windows host.'
Write-Host "Intended InstallRoot=$InstallRoot ServiceUser=$ServiceUser SHA=$PinnedSha"
Write-Host 'This cloud artifact does not create local users or copy secrets.'
Write-Host 'Next (manual on host): create unprivileged user, ACL $InstallRoot, git clone+checkout, venv, local .env, stdio MCP entry.'
Write-Host "Fork: $ForkUrl"
exit 0
