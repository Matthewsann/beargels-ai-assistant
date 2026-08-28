@echo off
rem NOTE: keep this file ASCII-only. Korean comments in UTF-8 broke cmd
rem parsing (2026-08-06), same lesson as watchdog.ps1.
rem Output goes to logs\worker.log because the watchdog starts this
rem hidden (visible windows piled up on the owner's screen).
rem No "pause": dead runs must close their window, not stack up.
chcp 65001 >nul
title Beargels home-PC worker
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set PY=C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

if not exist logs mkdir logs

rem Rotate the log before starting. The file is appended to by this shell,
rem so it can only be rotated here (not while the worker runs). It reached
rem 76 MB by 2026-08-28 and nothing ever trimmed it. Keep one older copy.
rem 10 MB threshold = several weeks now that the HTTP noise is silenced.
for %%F in (logs\worker.log) do if %%~zF GTR 10485760 (
  if exist logs\worker.log.1 del logs\worker.log.1
  move /y logs\worker.log logs\worker.log.1 >nul
)

"%PY%" worker\agent.py >> logs\worker.log 2>&1
