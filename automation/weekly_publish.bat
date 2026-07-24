@echo off
REM [2단계] 주 1회 — 창고에서 골라 예약 발행. 더블클릭해서 실행하세요.
REM 목록이 뜨면 발행할 번호와 시작일을 입력하면 됩니다.
cd /d %~dp0
py src\schedule_publish.py
pause
