# Copy latest.enc into the laptop vault. Never touches inventory.db.
#
# Preferred: webpanel Settings → Download latest.enc (owner), then:
#   .\scripts\pull-vault.ps1
#   .\scripts\pull-vault.ps1 C:\Users\Remy\Downloads\latest.enc
# Or fetch with a fresh /webpanel token:
#   .\scripts\pull-vault.ps1 -Url "https://unicornfartzz-bot.onrender.com/panel/api/backup.enc" -Token "<t>"
#
# Restore is a separate, explicit step: scripts\restore_backup.py
# (that copies the live DB aside first; this script never restores).

param(
    [Parameter(Position = 0)]
    [string]$Source,
    [string]$Url,
    [string]$Token
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Vault = Join-Path $Repo "backups"
$LiveDb = Join-Path $Repo "inventory.db"

if (-not (Test-Path -LiteralPath $Vault)) {
    New-Item -ItemType Directory -Path $Vault | Out-Null
}

function Copy-Enc([string]$From) {
    if (-not (Test-Path -LiteralPath $From)) {
        throw "Not found: $From"
    }
    $name = Split-Path -Leaf $From
    if ($name -notlike "*.enc") {
        throw "Refusing non-.enc file: $name"
    }
    $dest = Join-Path $Vault "latest.enc"
    Copy-Item -LiteralPath $From -Destination $dest -Force
    $stamp = Get-Date -Format "yyyyMMddTHHmmssZ"
    $dated = Join-Path $Vault ("laptop-" + $stamp + ".enc")
    Copy-Item -LiteralPath $dest -Destination $dated -Force
    Write-Output "Vault updated: $dest"
    Write-Output "Dated copy:    $dated"
    Write-Output "Live DB left alone: $LiveDb"
}

if ($Url) {
    if (-not $Token) { throw "Pass -Token from a fresh /webpanel owner link." }
    $sep = if ($Url -match "\?") { "&" } else { "?" }
    $fetch = "$Url$sep" + "t=" + [uri]::EscapeDataString($Token)
    $tmp = Join-Path $env:TEMP "unicorn-latest.enc"
    Write-Output "Downloading encrypted snapshot…"
    Invoke-WebRequest -Uri $fetch -OutFile $tmp -UseBasicParsing
    Copy-Enc $tmp
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    exit 0
}

$candidates = @()
if ($Source) { $candidates += $Source }
$candidates += (Join-Path $env:USERPROFILE "Downloads\latest.enc")
$candidates += (Join-Path $env:USERPROFILE "Downloads\latest (1).enc")

$found = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path -LiteralPath $c)) { $found = $c; break }
}
if (-not $found) {
    Write-Output "No latest.enc found."
    Write-Output "1. Open the shop panel (owner) → Settings → Download latest.enc"
    Write-Output "2. Re-run: .\scripts\pull-vault.ps1"
    Write-Output "Laptop vault: $Vault"
    exit 1
}
Copy-Enc $found
