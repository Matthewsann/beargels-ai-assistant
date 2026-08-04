@echo off
chcp 65001 >nul
title Beargels nightly check
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem 매일 03:30 작업 스케줄러(BeargelsNightly)가 실행한다.
rem worker\nightly_check.md 의 지시문대로 어제 오류를 점검·수정·커밋한다.
rem 결과는 worker\점검기록.md 와 logs\nightly-YYYYMMDD.log 에 남는다.

if not exist logs mkdir logs
set LOG=logs\nightly-%date:~0,4%%date:~5,2%%date:~8,2%.log

rem 모델: Sonnet 5 — 에러로그 분류·소규모 수정·규칙 정리 같은 정기 작업에
rem 충분한 성능이면서, 구독 사용량을 Fable/Opus 급으로 태우지 않는다.
call claude -p "worker/nightly_check.md 파일을 읽고 그 지시문을 그대로 수행하라." ^
  --model claude-sonnet-5 ^
  --permission-mode acceptEdits ^
  --allowedTools "Bash(git *) Bash(python *) Read Edit Write Glob Grep" ^
  > "%LOG%" 2>&1

exit /b 0
