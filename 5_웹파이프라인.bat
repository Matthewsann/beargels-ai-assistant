@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  ============================================
echo    Beargels Insta Pipeline (Web)
echo  ============================================
echo.
echo  Opening http://localhost:8000 in your browser...
echo  (Keep this window open - closing it stops the server)
echo.
start "" http://localhost:8000

rem -- find python: PATH first, then py launcher, then default install path --
where python >nul 2>&1
if %errorlevel%==0 (
  python run_web.py
  goto done
)
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 run_web.py
  goto done
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" run_web.py
  goto done
)
echo  [ERROR] Python not found. Please install Python 3.12.
:done
pause
