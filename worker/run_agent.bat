@echo off
chcp 65001 >nul
title Beargels home-PC worker
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set PY=C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

"%PY%" worker\agent.py

pause
