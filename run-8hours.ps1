# Keep inventory bot running for up to 8 hours; restart on crash.
# Usage: powershell -File run-8hours.ps1

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $Root "venv\Scripts\python.exe"
$Log = Join-Path $Root "watchdog-8h.log"
$PidFile = Join-Path $Root ".bot.pid"
$Hours = 8
$Deadline = (Get-Date).AddHours($Hours)

function Write-Log($msg) {
  $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
  Add-Content -Path $Log -Value $line -Encoding utf8
  Write-Output $line
}

function Stop-BotProcesses {
  Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match 'peptide_inventory_bot') -and ($_.CommandLine -match 'bot\.py') } |
    ForEach-Object {
      try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Log "stopped stale bot pid $($_.ProcessId)"
      } catch {}
    }
}

if (-not (Test-Path $Py)) {
  Write-Log "ERROR: venv python not found at $Py"
  exit 1
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
  Write-Log "ERROR: missing .env — set TELEGRAM_BOT_TOKEN first"
  exit 1
}

Write-Log "watchdog start — run until $Deadline ($Hours h)"
Stop-BotProcesses
Start-Sleep -Seconds 2

while ((Get-Date) -lt $Deadline) {
  $remaining = ($Deadline - (Get-Date)).ToString("hh\:mm\:ss")
  Write-Log "starting bot.py (time left $remaining)"
  $p = Start-Process -FilePath $Py -ArgumentList "bot.py" `
    -WorkingDirectory $Root -PassThru -WindowStyle Hidden
  $p.Id | Set-Content $PidFile -Encoding ascii
  Write-Log "bot pid $($p.Id)"

  # Wait until process exits or deadline
  while (-not $p.HasExited) {
    if ((Get-Date) -ge $Deadline) {
      Write-Log "deadline reached — stopping bot pid $($p.Id)"
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
      break
    }
    Start-Sleep -Seconds 15
    try { $p.Refresh() } catch { break }
  }

  if ((Get-Date) -ge $Deadline) { break }

  $code = $p.ExitCode
  Write-Log "bot exited code=$code — restart in 5s"
  Start-Sleep -Seconds 5
}

Stop-BotProcesses
Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Write-Log "watchdog finished after $Hours h window"
