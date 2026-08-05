# Beargels worker watchdog - runs every 5 min via Task Scheduler (BeargelsWatchdog).
# Restarts worker/agent.py if it is not running. Web 'wake' button relies on this.
# NOTE: keep this file ASCII-only. PowerShell 5.1 misreads UTF-8 without BOM,
#       and Korean text here broke parsing (LastTaskResult=1, 2026-08-04).

$root  = Split-Path -Parent $PSScriptRoot
$agent = Join-Path $PSScriptRoot 'run_agent.bat'

$alive = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -match 'worker[\\/]agent\.py' }

if ($alive) { exit 0 }

# Hidden: visible windows piled up on the owner's screen (2026-08-06).
# Worker status is shown in the staff web app; output goes to logs\worker.log.
Start-Process -FilePath $agent -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
"$(Get-Date -Format 'yyyy-MM-dd HH:mm') worker was down - restarted" |
    Out-File -Append -Encoding utf8 (Join-Path $root 'logs\watchdog.log')
exit 0
