@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo [엠즈푸드 발주품목 수집] 크롤링용 크롬이 켜져 있어야 합니다.
python -m worker.msfs_items
pause
