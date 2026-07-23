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

### 1. Supabase 테이블 생성

기존 베어글스 비서 프로젝트의 Supabase SQL Editor에서
`supabase/migrations/001_create_sns_posts.sql`을 실행합니다.

### 2. Google Drive 서비스 계정

1. Google Cloud Console에서 프로젝트 생성 → **Drive API 활성화**
2. 서비스 계정 생성 → JSON 키 다운로드 → `service_account.json`으로 저장
3. 모니터링할 드라이브 폴더를 서비스 계정 이메일(`...@...iam.gserviceaccount.com`)에 **편집자로 공유**
   (승인 시 파일을 링크 공개로 전환해 Buffer가 이미지를 가져가야 하므로 편집자 권한 필요)
4. 폴더 URL의 ID를 `GOOGLE_DRIVE_FOLDER_ID`에 입력

### 3. Buffer

1. https://publish.buffer.com 에서 인스타그램 계정 연결
2. https://buffer.com/developers/apps 에서 앱 생성 → Access Token 발급 → `BUFFER_ACCESS_TOKEN`
3. `GET https://api.bufferapp.com/1/profiles.json?access_token=...` 으로 인스타그램 프로필 ID 확인 → `INSTAGRAM_PROFILE_ID`

승인된 게시물은 Buffer **큐**에 추가되어, Buffer에 설정된 발행 스케줄에 따라 자동 발행됩니다.

### 4. 텔레그램

기존 베어글스 비서 봇과 동일한 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 사용합니다.

## 실행

```bash
python main.py
```

시작하면 즉시 한 번 폴링하고, 이후 `POLL_INTERVAL_MINUTES`(기본 5분)마다 폴더를 확인합니다.

## 주의사항

- **텔레그램 봇 토큰 공유**: 이 파이프라인은 자체적으로 텔레그램 폴링을 합니다. 기존 베어글스 비서 프로세스가 같은 토큰으로 **동시에 폴링 중이면 Conflict 에러**가 납니다. 두 프로세스를 같이 돌리려면 (a) 이 파이프라인용 봇을 따로 만들거나, (b) 기존 비서 프로세스에 이 코드를 통합하세요. 수정 피드백이 아닌 일반 텍스트 메시지는 이 봇이 무시하므로 통합 시 충돌하지 않습니다.
- **영상 파일**: Claude 분석은 드라이브가 생성한 썸네일 이미지로 수행합니다. Buffer의 인스타그램 영상(릴스) 직접 업로드는 API 제약이 있어, 영상은 링크 첨부 방식으로 등록됩니다 — 영상이 주력이면 Buffer 대시보드에서 확인 후 발행하는 것을 권장합니다.
- **이미지 공개 URL**: 승인 시 해당 드라이브 파일이 "링크가 있는 모든 사용자 보기 가능"으로 전환됩니다 (Buffer가 이미지를 가져가기 위함).

## 프로젝트 구조

```
main.py                          # 진입점 (스케줄러 + 텔레그램 봇)
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
