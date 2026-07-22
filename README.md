# 베어글스 카페 AI 비서 시스템 🐻

배달의민족·쿠팡이츠 주문/리뷰 데이터를 크롤링하고, Supabase에 저장한 뒤,
AI 비서가 이를 분석해 텔레그램으로 리포트를 보내주는 자동화 시스템입니다.

## 아키텍처

```
[크롤러] --> [Supabase DB] --> [AI 비서] --> [텔레그램 봇]
   ▲                                              
   └────────────── [스케줄러] ─────────────────────┘
```

- **crawler/** : 배민·쿠팡이츠 데이터 수집
- **database/** : Supabase 클라이언트 (저장/조회)
- **assistant/** : 수집 데이터를 분석하는 AI 비서 로직
- **bot/** : 텔레그램 알림/명령 처리
- **scheduler/** : 정기 실행 잡(job) 관리

## 폴더 구조

```
beargels-ai-assistant/
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
├── crawler/
│   ├── baemin.py          # 배달의민족 크롤러
│   └── coupang.py         # 쿠팡이츠 크롤러
├── database/
│   └── supabase_client.py # Supabase 연동
├── assistant/
│   └── beargels.py        # AI 비서 핵심 로직
├── bot/
│   └── telegram_bot.py    # 텔레그램 봇
└── scheduler/
    └── jobs.py            # 정기 실행 잡
```

## 설치

```bash
# 1. 의존성 설치
pip install -r requirements.txt

# 2. 환경 변수 설정
cp .env.example .env
# .env 파일을 열어 실제 값을 채워 넣으세요
```

## 실행

```bash
# 스케줄러 실행 (정기 크롤링 + 리포트)
python -m scheduler.jobs

# 텔레그램 봇 실행
python -m bot.telegram_bot
```

## 개발 상태

🚧 현재 프로젝트 뼈대(scaffold) 단계입니다. 각 파일의 `TODO` 주석을 참고하세요.
