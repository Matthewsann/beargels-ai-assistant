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
"%PY%" worker\agent.py >> logs\worker.log 2>&1
