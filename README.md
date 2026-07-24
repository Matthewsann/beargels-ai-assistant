# 베어글스 인스타그램 SNS 자동화 파이프라인

Matthew가 사진/영상을 구글 드라이브에 올리면 → Claude가 캡션/해시태그를 만들고 → 텔레그램으로 승인 요청 → 승인하면 Buffer를 통해 인스타그램에 예약 발행됩니다.

## 파이프라인 흐름

```
구글 드라이브 폴더 (폴링, 기본 5분)
        │ 새 파일 감지
        ▼
Claude API — 이미지 분석 → 메뉴 파악 → 캡션 + 해시태그 생성
        ▼
텔레그램 승인 요청 (사진 미리보기 + 캡션 + [✅ 승인 / ✏️ 수정 / ❌ 취소])
        │
        ├─ ✅ 승인 → Buffer 큐 등록 → 인스타그램 예약 발행
        ├─ ✏️ 수정 → 수정 내용 답장 → 캡션 재생성 → 다시 승인 요청
        └─ ❌ 취소 → 발행 안 함
```

처리 상태는 Supabase `sns_posts` 테이블에 기록되어 같은 파일이 중복 처리되지 않습니다.

## 설치

```bash
pip install -r requirements.txt
cp .env.example .env   # 값 채우기
```

### 1. Supabase 테이블 + 미디어 버킷 생성

기존 베어글스 비서 프로젝트의 Supabase SQL Editor에서 아래 두 개를 실행합니다.

1. `supabase/migrations/001_create_sns_posts.sql` — 게시물 상태 추적 테이블
2. `supabase/migrations/002_media_bucket.sql` — 발행용 이미지 임시 호스팅 버킷(`sns-media`)

> 인스타그램/Buffer는 파일을 직접 받지 않고 **공개 URL**만 받습니다. 그래서 승인 시
> 사진을 이 공개 버킷에 잠깐 올려 URL을 만들어 Buffer에 전달합니다.

### 2. Google Drive (OAuth 로그인 방식)

서비스 계정 키 생성이 조직 정책으로 차단되는 경우가 많아, 본인 계정으로
한 번만 로그인하는 OAuth 방식을 사용합니다.

1. Google Cloud Console에서 프로젝트 생성 → **Drive API 활성화**
2. **OAuth 동의 화면** 구성
   - Workspace(회사 도메인) 계정이면 **내부(Internal)** 선택 권장 (토큰 만료 없음)
   - 개인 Gmail이면 **외부(External)** → 게시 상태를 **프로덕션(In production)**으로 게시
     (테스트 모드로 두면 7일마다 재로그인 필요)
3. **사용자 인증 정보 → OAuth 클라이언트 ID → 애플리케이션 유형: 데스크톱 앱** 생성
   → JSON 다운로드 → `client_secret.json`으로 프로젝트 폴더에 저장
4. 폴더 URL의 ID를 `.env`의 `GOOGLE_DRIVE_FOLDER_ID`에 입력
5. 최초 1회 로그인:
   ```bash
   python authorize_drive.py
   ```
   브라우저가 열리면 사진 폴더를 소유한 구글 계정으로 로그인 → 허용.
   `token.json`이 생성되며, 이후 봇은 이 파일로 자동 로그인/갱신합니다.

> 서비스 계정과 달리 폴더를 별도로 공유할 필요가 없습니다 — 로그인한 계정이
> 접근 가능한 폴더면 됩니다. 토큰이 만료돼 봇이 재인증을 요청하면
> `python authorize_drive.py`를 다시 실행하세요.

### 3. Buffer (새 Public API / GraphQL)

Buffer의 옛 REST API는 신규 발급이 중단되어, 새 Public API(GraphQL)를 사용합니다.

1. https://publish.buffer.com 에서 인스타그램 계정(채널) 연결
2. Buffer 로그인 → **Settings → API** → **Create API Key**로 토큰 발급 → `BUFFER_ACCESS_TOKEN`
3. 인스타그램 **채널 ID**는 토큰을 채운 뒤 `get_instagram_id.bat`(또는 `python get_instagram_id.py`)를
   실행하면 자동으로 찾아 `.env`의 `BUFFER_CHANNEL_ID`에 넣어줍니다.

승인된 게시물은 Buffer **큐(addToQueue)**에 추가되어, Buffer에 설정된 발행 스케줄에 따라 자동 발행됩니다.

### 4. 텔레그램

기존 베어글스 비서 봇과 동일한 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 사용합니다.

## 실행

```bash
python authorize_drive.py   # 최초 1회 구글 로그인 (token.json 생성)
python main.py              # 봇 실행
```

시작하면 즉시 한 번 폴링하고, 이후 `POLL_INTERVAL_MINUTES`(기본 5분)마다 폴더를 확인합니다.

## 주의사항

- **텔레그램 봇 토큰 공유**: 이 파이프라인은 자체적으로 텔레그램 폴링을 합니다. 기존 베어글스 비서 프로세스가 같은 토큰으로 **동시에 폴링 중이면 Conflict 에러**가 납니다. 두 프로세스를 같이 돌리려면 (a) 이 파이프라인용 봇을 따로 만들거나, (b) 기존 비서 프로세스에 이 코드를 통합하세요. 수정 피드백이 아닌 일반 텍스트 메시지는 이 봇이 무시하므로 통합 시 충돌하지 않습니다.
- **영상 파일**: Claude 분석은 드라이브가 생성한 썸네일 이미지로 수행합니다. Buffer의 인스타그램 영상(릴스) 직접 업로드는 API 제약이 있어, 영상은 링크 첨부 방식으로 등록됩니다 — 영상이 주력이면 Buffer 대시보드에서 확인 후 발행하는 것을 권장합니다.
- **이미지 공개 URL**: 승인 시 해당 드라이브 파일이 "링크가 있는 모든 사용자 보기 가능"으로 전환됩니다 (Buffer가 이미지를 가져가기 위함).

## 프로젝트 구조

```
main.py                          # 진입점 (스케줄러 + 텔레그램 봇)
authorize_drive.py               # 최초 1회 구글 로그인 스크립트
sns_automation/
├── config.py                    # 환경변수 로드
├── db.py                        # Supabase sns_posts 저장소
├── drive_monitor.py             # 구글 드라이브 폴더 모니터링 (1단계)
├── caption_generator.py         # Claude 이미지 분석 + 캡션 생성 (2단계)
├── telegram_bot.py              # 텔레그램 승인 인터페이스 (3단계)
├── buffer_client.py             # Buffer API 연동 (4단계)
└── pipeline.py                  # 전체 흐름 오케스트레이션
supabase/migrations/
└── 001_create_sns_posts.sql     # 상태 추적 테이블
```
