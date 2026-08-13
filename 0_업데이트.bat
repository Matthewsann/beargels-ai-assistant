@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================================
echo  베어글스 프로그램 업데이트
echo   1) 최신 코드 받기 (git pull)
echo   2) 답글 일꾼 다시 시작
echo ==========================================================
echo.
git pull origin main
if errorlevel 1 (
  echo.
  echo [!] 코드 받기에 실패했습니다.
  echo     - 인터넷 연결을 확인하세요.
  echo     - "Your local changes..." 가 보이면 이 창 내용을 사장님/개발자에게 보여주세요.
  echo.
  pause
  exit /b 1
)
echo.
echo [코드 받기 완료] 이제 일꾼을 새 코드로 다시 시작합니다.
echo.
python scripts\fix_auto_post.py
echo.
echo 끝났습니다. 이 창은 닫아도 됩니다.
pause
