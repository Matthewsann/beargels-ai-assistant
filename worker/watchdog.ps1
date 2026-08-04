# 일꾼 감시견 — 5분마다 작업 스케줄러(BeargelsWatchdog)가 실행한다.
#
# 일꾼(worker/agent.py)이 죽어 있으면 다시 켠다. PC 전원은 켜져 있는데
# 프로그램만 꺼진 상황(창을 실수로 닫음, 오류로 종료 등)을 웹에서 기다리면
# 최대 5분 안에 저절로 복구되게 하기 위함.
#
# 등록:  schtasks BeargelsWatchdog (5분 간격)  — 직접 실행할 일은 없다.

$root = Split-Path -Parent $PSScriptRoot   # 리포 루트
$agent = Join-Path $PSScriptRoot 'run_agent.bat'

$alive = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
         Where-Object { $_.CommandLine -match 'worker[\\/]agent\.py' }

if ($alive) { exit 0 }

Start-Process -FilePath $agent -WorkingDirectory $PSScriptRoot
"$(Get-Date -Format 'yyyy-MM-dd HH:mm') 일꾼이 꺼져 있어 다시 켬" |
    Out-File -Append -Encoding utf8 (Join-Path $root 'logs\watchdog.log')
