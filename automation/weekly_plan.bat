@echo off
REM [주 1회 · 세션 A] 시장조사 → 기획 주제안 → 번호 선택 → 본문작성+AI검수까지
REM 더블클릭으로 실행. 선택 후 y 를 누르면 글 생성까지 이어집니다.
cd /d %~dp0
echo [0/2] 트렌드 수집...
py src\trend_search.py
echo.
echo [1/2] 주제안 생성 + 선택...
py src\propose_topics.py
pause
