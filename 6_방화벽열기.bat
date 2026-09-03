@echo off
chcp 949 >nul
:: 파이프라인 화면(8000)을 폰에서 열 수 있게 방화벽을 허용한다 (집 와이파이 안에서만)
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 관리자 권한이 필요해요 - 잠시 후 뜨는 창에서 [예]를 눌러주세요
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
netsh advfirewall firewall delete rule name="Beargels Pipeline 8000" >nul 2>&1
netsh advfirewall firewall add rule name="Beargels Pipeline 8000" dir=in action=allow protocol=TCP localport=8000 profile=private
echo.
echo 완료! 이제 폰(집 와이파이)에서 파이프라인 화면이 열려요.
pause
