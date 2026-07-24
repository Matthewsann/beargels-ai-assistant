@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Beargels SNS Bot - Setup
echo ============================================
echo.
echo Installing required packages...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env file from template.
  echo Open .env with Notepad and fill in your keys.
) else (
  echo .env already exists - leaving it as is.
)
echo.
echo Setup finished. You can close this window.
pause
