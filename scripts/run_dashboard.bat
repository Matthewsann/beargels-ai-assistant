@echo off
REM 베어글스 운영 대시보드(웹) 실행 — 브라우저에서 http://127.0.0.1:5000
setlocal
set "ROOT=%~dp0.."
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONUTF8=1"
title 베어글스 대시보드
cd /d "%ROOT%"
echo 대시보드: http://127.0.0.1:5000  (종료: Ctrl+C)
"%PY%" -m web.app
pause
