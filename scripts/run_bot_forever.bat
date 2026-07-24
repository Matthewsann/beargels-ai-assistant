@echo off
REM ==========================================================
REM  베어글스 봇 24h 자동 재시작 감시자 실행
REM ==========================================================
REM  이 창을 켜두면 텔레그램 봇이 죽어도 자동으로 다시 뜹니다.
REM  (붙을 Chrome 이 죽어 있으면 감시자가 먼저 되살립니다)
REM
REM  사용:  run_bot_forever.bat            (봇 감시)
REM         run_bot_forever.bat scheduler.jobs   (스케줄러 감시)
REM  종료:  이 창에서 Ctrl+C
REM ==========================================================
setlocal
set "ROOT=%~dp0.."
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONUTF8=1"
title 베어글스 봇 감시자 (닫지 마세요)
cd /d "%ROOT%"
"%PY%" -m scripts.supervisor %*
echo.
echo [감시자가 종료되었습니다] 창을 닫으려면 아무 키나 누르세요.
pause >nul
