@echo off
chcp 65001 >nul
cd /d "%~dp0"
python get_instagram_id.py
pause
