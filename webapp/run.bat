@echo off
chcp 65001 >nul
title 베어글스 AI 비서 웹앱 (이 PC용)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

set PY=C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo [1/3] 필요한 것 설치 확인...
"%PY%" -m pip install -q -r requirements.txt

echo [2/3] 웹앱 시작...
start "" http://localhost:5050

echo [3/3] 브라우저가 열려요. (이 창을 닫으면 웹앱이 꺼져요)
echo      * 밖에서 쓰려면 이 파일 대신 start_public.bat 를 실행하세요.
"%PY%" app.py

pause
