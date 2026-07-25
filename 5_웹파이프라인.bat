@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   베어글스 인스타 파이프라인 웹페이지
echo   잠시 후 브라우저에서 http://localhost:8000 을 여세요.
echo   (이 창을 닫으면 서버가 꺼집니다)
echo.
"C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe" run_web.py
pause
