@echo off
REM [1단계] 콘텐츠 자동 적재 — Windows 작업 스케줄러에 이 파일을 등록하세요.
REM 더블클릭으로 수동 실행도 가능합니다.
cd /d %~dp0
py src\stockpile.py
if %ERRORLEVEL% NEQ 0 pause
