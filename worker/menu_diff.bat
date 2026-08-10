@echo off
chcp 65001 >nul
title Beargels 메뉴 수정 지시서
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem 집 PC 전용 — 정본(Supabase)과 채널 스냅샷을 비교해 "어느 채널에서 무엇을
rem 고칠지" 지시서(reports\menu-diff.md)를 만들고 GitHub 로 올린다.
rem 크롬·로그인 불필요(수집이 아니라 비교만 한다). 파이썬만 있으면 된다.

set PY=C:\Users\명구\AppData\Local\Programs\Python\Python312\python.exe
if not exist "%PY%" set PY=python

if not exist logs mkdir logs
set LOG=logs\menu-diff-%date:~0,4%%date:~5,2%%date:~8,2%.log

echo [1/4] 최신 코드 받기...
git pull --rebase --autostash >> "%LOG%" 2>&1

echo [2/4] 지시서 작성...
"%PY%" scripts\menu_diff_report.py >> "%LOG%" 2>&1
set RESULT=%ERRORLEVEL%
if not %RESULT%==0 (
  echo [!] 지시서 작성 실패 - %LOG% 확인
  exit /b %RESULT%
)

echo [3/4] 커밋...
git add -A reports >> "%LOG%" 2>&1
git diff --cached --quiet
if %ERRORLEVEL%==0 (
  echo     변경 없음 - 건너뜀
  goto DONE
)
git commit -m "메뉴 수정 지시서 %date:~0,10% %time:~0,5%" >> "%LOG%" 2>&1

echo [4/4] GitHub 로 올리기...
git push >> "%LOG%" 2>&1

:DONE
echo.
echo 완료. 지시서: reports\menu-diff.md
exit /b 0
