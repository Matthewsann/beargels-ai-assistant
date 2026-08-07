@echo off
REM [미디어 자동화 원클릭] 사진 보정 → 짧은 영상 제작
REM 사용법: media\inbox\ 폴더에 폰 사진을 복사해 넣고 이 파일을 더블클릭
cd /d %~dp0
echo [1/2] 사진 자동 보정...
py src\enhance_photos.py
echo.
echo [2/2] 짧은 영상 제작...
py src\make_video.py
echo.
echo 끝! 보정사진: media\enhanced\  /  영상: media\video\
pause
