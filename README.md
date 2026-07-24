# 베어글스 카페 AI 비서 시스템 🐻

배달의민족·쿠팡이츠 주문/리뷰 데이터를 수집하고, Supabase에 저장한 뒤,
AI 비서(Claude)가 분석해 텔레그램으로 리포트를 보내주는 자동화 시스템입니다.
(향후 리뷰 답글·프로모션 관리 등 쓰기 작업까지 확장 예정)

## 아키텍처

```
[크롤러] --> [Supabase DB] --> [AI 비서(Claude)] --> [텔레그램 봇]
   ▲                                                      
   └────────────────── [스케줄러] ─────────────────────────┘
```

- **crawler/** : 배민·쿠팡이츠 데이터 수집 (`browser.py` = 공용 브라우저 세션 계층)
- **database/** : Supabase 클라이언트 (저장/조회) + `schema.sql`
- **assistant/** : Claude 로 리포트/리뷰요약 생성
- **bot/** : 텔레그램 봇(대화형 명령) + 알림
- **scheduler/** : 정기 크롤링·리포트 잡
- **scripts/** : 자동화용 Chrome 실행/로그인 셋업

## 브라우저 세션 전략 (중요)

배민·쿠팡은 **로그인 플로우에서 봇 탐지가 가장 강하다.** 그래서 매번
로그인하지 않고 **로그인된 세션을 재사용**한다. 두 모드(`.env` `BROWSER_MODE`):

- **attach** (권장): `scripts/launch_chrome.bat` 로 원격 디버깅 포트를 연
  전용 Chrome 을 띄우고 배민·쿠팡에 로그인 → 크롤러가 그 창에 CDP 로 붙어
  새 탭에서 작업. 사람이 켠 실제 창이라 탐지 위험이 가장 낮다.
- **profile**: 크롤러가 `.browser_profile/` 로 Chrome 을 직접 실행(무인 서버용).

세션 만료 시 `SessionExpiredError` + 텔레그램 재로그인 알림.
> ⚠️ 그냥 켜둔 Chrome 에는 attach 불가 — 반드시 `launch_chrome.bat` 경유.

## 설치 & 최초 셋업

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. 환경 변수
cp .env.example .env      # 값 채우기 (Supabase/배민/쿠팡/텔레그램/Anthropic)

# 3. Supabase 테이블 생성 (최초 1회)
#    Supabase 대시보드 → SQL Editor → database/schema.sql 전체 실행

# 4. 자동화용 Chrome 실행 + 로그인 (최초 1회)
scripts\launch_chrome.bat            # 창에서 배민·쿠팡 로그인 후 켜둠
python -m scripts.setup_login        # 로그인 상태 검증
```

## 실행

```bash
python -m scheduler.jobs      # 정기 크롤링(2h) + 매일 09시 리포트 + 심각리뷰 14/22시
python -m bot.telegram_bot    # 텔레그램 봇 (/report /reviews /drafts /start …)
python -m assistant.beargels  # 단독: 지금 수집→리포트 1회 출력
```

### 24시간 무중단 (자동 재시작)

봇이 죽어도 자동으로 되살리고, 붙을 Chrome(포트 9222)이 꺼져 있으면 먼저
되살린다. 재시작 이력은 `logs/supervisor.log` 에 남는다.

```bash
scripts\run_bot_forever.bat          # 봇 감시(자동 재시작). 창은 켜둔 채로
scripts\run_bot_forever.bat scheduler.jobs   # 스케줄러도 같은 방식으로 감시
scripts\install_autostart.bat        # PC 로그인 시 자동 실행 등록(관리자 불필요)
```

### 리뷰 답글 승인 게이트 (`/drafts`)

`/drafts` → 미답변 리뷰의 답글 초안을 하나씩 버튼(✅게시/⏭️넘기기)과 함께 제시.
- ⚠️ **기본은 dry-run**(`.env` `WRITE_DRY_RUN=true`) — 버튼을 눌러도 미리보기만.
  실제 게시는 `WRITE_DRY_RUN=false` 로 바꾸고 봇 재시작 후에만 나간다.
- 🚨 이물질·환불·법적 등 **에스컬레이션 리뷰는 자동 게시 차단**(사장님 직접 대응).
- 첫 실게시는 반드시 사장님 승인·감독 하에(잘못된 답글이 실고객에 노출되므로).

## 개발 상태 (2026-07)

- ✅ 배민 크롤러: 로그인 세션 재사용(attach), 주문·리뷰 파싱 — 실계정 검증 완료
- ✅ 쿠팡이츠 크롤러: 리뷰 + **주문(매출)** — 내부 JSON API 인터셉트(Akamai 우회), 실검증
- ✅ AI 비서: Claude 리포트/리뷰요약 (LLM 장애 시 숫자 리포트로 graceful degrade)
- ✅ Supabase 저장 계층 + 스키마 (`schema.sql` 실행 필요)
- ✅ 텔레그램 봇/알림, 스케줄러, 심각리뷰 자동보고(14/22시)
- ✅ 리뷰 답글: 생성기 + **승인 게이트(`/drafts`)** + dry-run 게시 코드
  (`crawler/review_reply.py`, `write_guard.py`). 실게시는 승인+비-dry-run 필요
- ✅ 봇 24h 자동 재시작 감시자(`scripts/supervisor.py`)
