@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  답글 '자동 등록 대기'만 쌓이고 등록이 안 될 때 실행하세요.
echo  설정을 확인해 실게시 모드로 바꾸고, 답글 일꾼을 다시 켭니다.
echo  (인스타 봇 4_run_bot.bat 과는 무관합니다)
echo.
python scripts\fix_auto_post.py
echo.
pause
