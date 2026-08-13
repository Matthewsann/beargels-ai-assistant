@echo off
chcp 65001 >nul
title Beargels 메뉴 수집 리포트
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem 집 PC 전용 — 채널 메뉴를 수집하고 결과 요약을 reports\ 에 남긴 뒤
rem GitHub 로 올린다. 클라우드 세션은 git pull 로 그 리포트를 읽고 코드를 고친다.
rem Claude 가 필요 없다. 파이썬만 있으면 된다.
rem
rem 수동 실행: worker\menu_report.bat
rem 한 채널만: worker\menu_report.bat naver   (배민·쿠팡 로그인 없이 네이버만)
rem 자동 실행: 작업 스케줄러에 이 파일을 등록(예: 매일 04:00)

rem 그냥 python 은 스토어 스텁이라 실행되지 않는다 — 실제 경로를 먼저 쓴다.
set PY=C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

if not exist logs mkdir logs
set LOG=logs\menu-report-%date:~0,4%%date:~5,2%%date:~8,2%.log

echo [1/4] 최신 코드 받기...
git pull --rebase --autostash >> "%LOG%" 2>&1

echo [2/4] 메뉴 수집 + 리포트 작성...
"%PY%" scripts\menu_report.py %* >> "%LOG%" 2>&1
set RESULT=%ERRORLEVEL%

echo [3/4] 리포트 커밋...
git add -A reports >> "%LOG%" 2>&1
git diff --cached --quiet
if %ERRORLEVEL%==0 (
  echo     변경된 리포트 없음 - 건너뜀
  goto DONE
)
git commit -m "메뉴 수집 리포트 %date:~0,10% %time:~0,5%" >> "%LOG%" 2>&1

echo [4/4] GitHub 로 올리기...
git push >> "%LOG%" 2>&1
if not %ERRORLEVEL%==0 (
  echo     [!] push 실패 - %LOG% 확인
)

:DONE
if not %RESULT%==0 (
  echo.
  echo [!] 수집에 문제가 있습니다. reports\latest-menu.md 를 확인하세요.
)
echo.
echo 완료. 리포트: reports\latest-menu.md
exit /b %RESULT%
