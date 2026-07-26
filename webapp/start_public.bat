@echo off
chcp 65001 >nul
title 베어글스 AI 비서 — 외부 접속용
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set ENVFILE=%~dp0..\.env

set PY=C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

echo ====================================================
echo  베어글스 AI 비서 — 밖에서 쓰기 (인터넷 공개)
echo ====================================================
echo.

rem [1] 비밀번호 확인 — 없으면 만들어서 .env 에 저장한다.
findstr /b /c:"BEARGELS_WEB_PASSWORD=" "%ENVFILE%" >nul 2>&1
if errorlevel 1 call :setpw
if errorlevel 1 goto :nopw

rem [2] cloudflared(터널 프로그램) 확인
where cloudflared >nul 2>&1
if errorlevel 1 goto :nocf

rem [3] 웹앱 켜기 (별도 창)
echo [1/2] 웹앱 시작...
"%PY%" -m pip install -q -r requirements.txt
start "베어글스 웹앱" cmd /k ""%PY%" app.py"
timeout /t 5 >nul

rem [4] 터널 열기 — 나오는 https://....trycloudflare.com 주소로 밖에서 접속
echo.
echo [2/2] 외부 접속 주소 만드는 중...
echo.
echo   조금 뒤 아래에 https://... .trycloudflare.com 주소가 나와요.
echo   그 주소를 휴대폰/다른 컴퓨터 브라우저에 입력하면 됩니다.
echo   ^(이 창을 닫으면 외부 접속이 끊겨요 - 웹앱 창은 따로 있어요^)
echo.
cloudflared tunnel --url http://localhost:5050
pause
exit /b 0


:setpw
echo 인터넷에 공개하려면 비밀번호가 반드시 있어야 해요.
echo 다른 사람은 못 맞출 만한 걸로, 12자 이상 추천.
echo.
set "PW="
set /p "PW=새 비밀번호 입력: "
if not defined PW exit /b 1
>>"%ENVFILE%" echo BEARGELS_WEB_PASSWORD=%PW%
echo.
echo 저장했어요(.env). 다음부터는 안 물어봐요.
echo.
exit /b 0

:nocf
echo [!] 터널 프로그램(cloudflared)이 없어요. 아래를 한 번만 실행해 설치하세요:
echo.
echo      winget install --id Cloudflare.cloudflared
echo.
echo 설치가 끝나면 이 창을 닫고 이 파일을 다시 실행해주세요.
pause
exit /b 1

:nopw
echo 비밀번호 없이는 인터넷에 열 수 없어요. 다시 실행해주세요.
pause
exit /b 1
