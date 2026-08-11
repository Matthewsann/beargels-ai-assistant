@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  답글 '자동 등록 대기'만 쌓이고 등록이 안 될 때 실행하세요.
echo  (설정을 확인해 실게시 모드로 바꿔줍니다)
echo.
python scripts\fix_auto_post.py
echo.
pause
