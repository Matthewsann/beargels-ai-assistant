@echo off
REM ==========================================================
REM  베어글스 봇 감시자 - 부팅(로그인) 시 자동 실행 등록
REM ==========================================================
REM  Windows 시작프로그램 폴더에 실행 스텁을 만들어, PC 로그인 시
REM  봇 감시자(run_bot_forever.bat)가 자동으로 뜨게 합니다. (관리자 권한 불필요)
REM
REM  ⚠️ 자동 크롤링/attach 는 로그인된 Chrome 세션이 필요합니다. 세션이 만료되면
REM     감시자가 Chrome 을 되살려도 배민/쿠팡 재로그인이 필요할 수 있습니다(봇이
REM     텔레그램으로 알림).
REM ==========================================================
setlocal
set "SCRIPTS=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "STUB=%STARTUP%\beargels-bot.bat"

if not exist "%STARTUP%" (
  echo [오류] 시작프로그램 폴더를 찾지 못했습니다: %STARTUP%
  pause & exit /b 1
)

> "%STUB%" echo @echo off
>> "%STUB%" echo REM 베어글스 봇 감시자 자동 실행 (install_autostart.bat 이 생성)
>> "%STUB%" echo start "" /min "%SCRIPTS%run_bot_forever.bat"

echo ==========================================================
echo  자동 실행 등록 완료
echo   스텁 : %STUB%
echo   실행 : %SCRIPTS%run_bot_forever.bat (최소화 창)
echo ==========================================================
echo  지금 바로 한 번 실행하려면 이 창을 닫고 run_bot_forever.bat 를 더블클릭하세요.
echo  자동 실행을 해제하려면 위 스텁 파일을 삭제하세요:
echo    del "%STUB%"
echo ==========================================================
pause
