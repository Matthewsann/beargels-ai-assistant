@echo off
chcp 65001 >nul
title 베어글스 순위 확인
cd /d "%~dp0"
set PY=C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python
echo 네이버 순위 확인 중...
"%PY%" rank_checker.py
echo 완료. (webapp\rank_history.json 갱신)
