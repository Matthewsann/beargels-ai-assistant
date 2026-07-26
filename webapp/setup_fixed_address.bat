@echo off
chcp 65001 >nul
title Beargels AI - fixed address setup
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set PY=C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

"%PY%" -m pip install -q -r requirements.txt
"%PY%" setup_fixed_address.py

pause
