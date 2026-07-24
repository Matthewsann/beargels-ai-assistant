@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting Beargels SNS bot... (keep this window open)
echo Press Ctrl+C to stop.
echo.
python main.py
pause
