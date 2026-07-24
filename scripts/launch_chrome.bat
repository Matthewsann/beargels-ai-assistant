@echo off
REM ==========================================================
REM  베어글스 AI 비서 - 자동화용 Chrome 실행 스크립트
REM ==========================================================
REM  이 스크립트로 띄운 Chrome 에만 크롤러가 붙을(attach) 수 있습니다.
REM  (평소 쓰는 Chrome 창에는 원격 디버깅 포트가 없어 붙을 수 없습니다.)
REM
REM  사용법:
REM   1) 이 파일을 더블클릭하거나 터미널에서 실행
REM   2) 열린 배민/쿠팡 탭에서 각각 로그인 (최초 1회, 세션 저장됨)
REM   3) 이 창은 켜둔 채로 크롤러/스케줄러를 실행
REM ==========================================================

setlocal
set "SCRIPT_DIR=%~dp0"
set "PROFILE_DIR=%SCRIPT_DIR%..\.browser_profile"
set "PORT=9222"

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" set "CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME%" (
  echo [오류] Chrome 을 찾을 수 없습니다. 경로를 확인하세요.
  pause
  exit /b 1
)

echo ==========================================================
echo  Chrome 원격 디버깅 모드 실행
echo   - 포트    : %PORT%
echo   - 프로필  : %PROFILE_DIR%
echo  (이 프로필은 평소 Chrome 과 분리된 전용 프로필입니다)
echo ==========================================================
echo  열린 탭에서 배민/쿠팡에 로그인한 뒤, 이 창을 켜둔 채로
echo  크롤러를 실행하세요.
echo ==========================================================

"%CHROME%" --remote-debugging-port=%PORT% --user-data-dir="%PROFILE_DIR%" ^
  "https://self.baemin.com" "https://store.coupangeats.com"

endlocal
