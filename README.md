# 베어글스 AI 비서 🐻

베어글스 카페의 운영을 AI로 자동화하는 통합 시스템입니다. 세 개의 축으로 구성됩니다:

| 축 | 하는 일 | 폴더 | 문서 |
|----|---------|------|------|
| 📊 **매출·리뷰 봇** | 배민·쿠팡 주문/리뷰 수집 → AI 분석 → 리포트 파일 + 리뷰 답글(웹) | `assistant/ crawler/ database/ service/ worker/ scripts/` | 아래 "매출·리뷰 봇" |
| ✍️ **블로그 자동화** | 네이버 SEO 규칙대로 글·이미지 자동 적재 → 골라서 예약 발행 | `automation/` | [`automation/README.md`](automation/README.md) |
| 📷 **인스타 파이프라인** | 촬영 원본 → 릴스 자동편집 → 브라우저에서 확인·발행 | `sns_automation/ run_web.py web/` | 아래 "인스타 파이프라인" |

> 공통 원칙: **발행·게시 등 대외 작업은 사장님 승인 후에만.** 로그인은 사람이 직접(계정 안전).

---

## 📊 매출·리뷰 봇

배달의민족·쿠팡이츠 주문/리뷰를 수집하고 Supabase에 저장한 뒤, AI 비서가 분석해 `reports/` 에 리포트를 남기고, 리뷰 답글은 직원 웹앱에서 승인 게이트로 관리합니다.

> 알림은 텔레그램을 쓰지 않습니다(2026-08-13 제거). 사장님이 알아야 할 일은 **웹 화면의 오류기록**과 **로그·`reports/` 파일**로 남습니다.

```
[크롤러] --> [Supabase DB] --> [AI 비서(Claude)] --> [텔레그램 봇]
   ▲                                                      
   └────────────────── [스케줄러] ─────────────────────────┘
```

- **crawler/** : 배민·쿠팡이츠 수집 (`browser.py` = 공용 브라우저 세션 계층)
- **database/** : Supabase 클라이언트 + `schema.sql`
- **assistant/** : Claude 로 리포트/리뷰요약 생성
- **bot/** : 텔레그램 봇(대화형 명령) + 알림
- **scheduler/** : 정기 크롤링·리포트 잡
- **scripts/** : 자동화용 Chrome 실행/로그인 셋업

### 브라우저 세션 전략 (중요)
배민·쿠팡은 로그인 플로우 봇 탐지가 강해, 매번 로그인하지 않고 **로그인된 세션을 재사용**한다 (`.env` `BROWSER_MODE`).
- **attach**(권장): `scripts/launch_chrome.bat` 로 원격 디버깅 포트를 연 전용 Chrome 에 로그인 → 크롤러가 CDP 로 붙어 작업. 사람이 켠 실제 창이라 탐지 위험 최저.
- **profile**: `.browser_profile/` 로 직접 실행(무인 서버용).
> ⚠️ 그냥 켜둔 Chrome 에는 attach 불가 — 반드시 `launch_chrome.bat` 경유.

### 설치 & 최초 셋업
```bash
pip install -r requirements.txt
cp .env.example .env      # Supabase/배민/쿠팡/텔레그램/Anthropic 값 채우기
#  Supabase 대시보드 → SQL Editor → database/schema.sql 실행 (최초 1회)
scripts\launch_chrome.bat          # 창에서 배민·쿠팡 로그인 후 켜둠
python -m scripts.setup_login      # 로그인 상태 검증
```

### 실행
```bash
python -m scheduler.jobs      # 정기 크롤링(2h) + 매일 09시 리포트 + 심각리뷰 14/22시
python -m bot.telegram_bot    # 텔레그램 봇 (/report /reviews /drafts /sales /start …)
python -m assistant.beargels  # 단독: 지금 수집→리포트 1회 출력
```

### 24시간 무중단 (자동 재시작)
봇이 죽어도 되살리고, 붙을 Chrome(포트 9222)이 꺼져 있으면 먼저 되살린다. 이력은 `logs/supervisor.log`.
```bash
scripts\run_bot_forever.bat                   # 봇 감시(자동 재시작)
scripts\run_bot_forever.bat scheduler.jobs    # 스케줄러도 같은 방식
scripts\install_autostart.bat                 # PC 로그인 시 자동 실행 등록
```

### 리뷰 답글 승인 게이트 (`/drafts`)
`/drafts` → 미답변 리뷰의 답글 초안을 버튼(✅게시/⏭️넘기기)과 함께 제시.
- ⚠️ 기본은 dry-run(`.env` `WRITE_DRY_RUN=true`) — 버튼 눌러도 미리보기만. 실게시는 `WRITE_DRY_RUN=false` + 봇 재시작 후에만.
- 🚨 이물질·환불·법적 등 에스컬레이션 리뷰는 자동 게시 차단(사장님 직접 대응).

### 개발 상태 (2026-07)
- ✅ 배민/쿠팡이츠 크롤러: 세션 재사용(attach), 주문·리뷰·매출 파싱 — 실계정 검증
- ✅ AI 비서: Claude 리포트/리뷰요약 (LLM 장애 시 숫자 리포트로 graceful degrade)
- ✅ Supabase 저장 계층 + 스키마 / 텔레그램 봇·알림 / 스케줄러 / 심각리뷰 자동보고
- ✅ 리뷰 답글 생성기 + 승인 게이트(`/drafts`) + dry-run 게시 / 봇 24h 자동 재시작

---

## ✍️ 블로그 자동화

네이버 블로그 운영을 AI로 반자동화. "무슨 글을 쓸지 → 글쓰기 → 발행"의 앞 두 단계를 AI가 대신하고, 사장님은 **최종 확인·발행만** 합니다. 상세 사용법은 [`automation/README.md`](automation/README.md).

- **1단계(자동):** 켜둔 PC가 네이버 SEO 규칙대로 글·이미지를 만들어 창고(`automation/library/`)에 쌓음
- **2단계(주 1회):** 창고에서 골라 예약 발행 설정
- 상위 노출 로직(C-Rank·D.I.A.)은 [`네이버-SEO-지식.md`](네이버-SEO-지식.md)에 정리, AI가 학습해 반영
- 로그인은 사장님이 직접 1회(세션 저장), 발행은 사장님이 고른 글만

---

## 📷 인스타 파이프라인

구글 드라이브 "콘텐츠 생성/[주제]" 폴더의 사진·영상을 텔레그램에서 주제명으로 부르면, Claude가 캡션·해시태그를 만들고 승인 후 Buffer로 인스타에 발행합니다 (`main.py`, `sns_automation/`).

```
드라이브 폴더 → Claude 캡션/해시태그 → 텔레그램 승인(✅/✏️/❌) → Buffer 큐 → 인스타 발행
```

- 영상 있으면 릴스, 사진만 있으면 게시물. 브랜드 톤은 `sns_automation` 지식 파일 기반.
- 구글 드라이브는 OAuth 1회 로그인(`python authorize_drive.py` → `token.json`).
- 텔레그램·Supabase·Anthropic 값은 매출봇과 **동일**하게 공유.

---

*각 시스템은 계속 발전 중입니다. 세 축을 하나의 "베어글스 AI 비서"로 통합해 운영합니다.*
