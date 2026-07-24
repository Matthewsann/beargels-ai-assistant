# 🤖 베어글스 블로그 자동화 (2단계 구조)

**생성/적재**와 **발행/예약**을 완전히 분리했습니다.
켜둔 윈도우 PC가 알아서 콘텐츠를 **창고에 계속 쌓고**, 사장님은 **주 1회** 창고에서
"이거, 이거" 골라 예약 발행만 걸면 됩니다.

```
[1단계 · 자동으로 계속]  stockpile.py
   네이버 SEO 규칙대로 본문 생성 + 이미지/촬영목록 → 창고(library/)에 status=ready 로 쌓임
                                   │
                                   ▼   창고에 콘텐츠가 계속 쌓임
[2단계 · 주 1회 · 사장님]  schedule_publish.py
   창고 목록에서 번호 선택 → 네이버 에디터 입력 → '예약 발행' 설정
```

- **1단계는 발행하지 않습니다.** 오직 생성·적재만. → 마음껏 자동으로 돌려도 안전.
- **2단계에서만** 사장님이 고른 글이 네이버에 예약 발행됩니다.
- 상위 노출 규칙은 레포 루트의 **`네이버-SEO-지식.md`** 를 AI가 학습해 반영합니다(C-Rank·D.I.A.).

---

## 1. 설치 (처음 한 번 · 윈도우)

윈도우에 **크롬**과 **파이썬 3.10+** 설치돼 있어야 합니다. (설치 시 "Add python to PATH" 체크)

```bat
cd automation
py -m pip install -r requirements.txt
py -m playwright install chromium

copy .env.example .env
copy config.example.yaml config.yaml
```

- `.env` → `ANTHROPIC_API_KEY`(글 생성). AI 이미지까지 쓰려면 `OPENAI_API_KEY`도.
- `config.yaml` → 블로그 아이디, 지역/주소/영업시간, 이미지/발행 옵션.

## 2. 네이버 로그인 (처음 한 번)

```bat
py src\naver_autodraft.py --login
```
크롬 창에서 직접 로그인 → 터미널 Enter. 세션이 저장돼 이후 재로그인 불필요.

---

## 3. 1단계 — 콘텐츠 자동 적재

```bat
py src\stockpile.py          REM config 의 per_run 개수만큼 쌓기
py src\stockpile.py 5        REM 5개 쌓기
py src\stockpile.py --plan   REM content-plan.yaml 의 신메뉴/이벤트도 함께
```

→ `library/0001, 0002 …` 폴더에 `post.md`(본문), `images/`(촬영목록·AI프롬프트) 가 쌓입니다.

### 완전 자동 — 작업 스케줄러 등록 (핵심)
`stockpile.bat` 을 등록하면 PC가 알아서 창고를 채웁니다.
1. **작업 스케줄러 → 기본 작업 만들기**
2. 트리거: 매일(또는 격일) 원하는 시간
3. 동작: **프로그램 시작** → 프로그램/스크립트에 `automation\stockpile.bat` 전체 경로
4. `config.yaml` 의 `headful: false` 로 두면 창 없이 조용히 실행

## 4. 2단계 — 주 1회, 골라서 예약 발행

`weekly_publish.bat` 을 더블클릭하거나:
```bat
py src\schedule_publish.py
```
1. 발행 대기(ready) 목록이 번호와 함께 뜹니다.
2. `발행할 번호: 1,3,5` 입력
3. `예약 시작일: 2026-07-28`, `매일 시간: 08:00` 입력 → 하루 간격 오전 8시로 배치
4. 크롬이 열려 글을 입력하고 **발행창에서 '예약'을 선택**합니다.
5. (기본 assist 모드) 시간만 확인하고 **'예약 발행' 클릭** → 끝. 사진은 미리/그때 넣으세요.

명령으로 한 번에:
```bat
py src\schedule_publish.py --pick 1,3,5 --daily 08:00 --start 2026-07-28
```

> **assist vs full**: 네이버 발행창의 시간 선택 UI는 특히 자주 바뀌어, 기본은 안정적인
> `assist`(마지막 클릭만 사장님)입니다. 화면에 맞게 selectors 를 튜닝하면 `config.yaml`
> 의 `publish.mode: full` 로 최종 클릭까지 자동화할 수 있습니다.

---

## 5. 사진 & 영상

- 각 글 폴더 `images/shotlist.md` 에 **직접 찍을 사진/영상 목록**이 자동 생성됩니다.
  네이버 상위 노출은 **직접 촬영 실물 사진**을 가장 우대하므로, 음식 사진은 실사진 권장.
- **AI 보조 이미지**(썸네일/배경/일러스트)는 `images.provider: openai` + `OPENAI_API_KEY`
  설정 시 `images/ai_1.png` 로 생성됩니다. 미설정이면 `ai_prompts.txt`(프롬프트)만 저장.
- **영상**은 자동 생성하지 않습니다(비용·품질·정책). 대신 shotlist 의 "5~10초 영상" 아이디어를
  폰으로 찍어 넣으면 체류시간 가점에 유리합니다.

---

## 6. 화면이 안 맞을 때 (선택자 튜닝)

입력/발행이 실패하면 `library/**/... ` 대신 `posts/_debug/` 에 스크린샷·HTML 이 남습니다.
```bat
py src\naver_autodraft.py --inspect
```
로 에디터를 열어 요소를 확인하고, `config.yaml` 에 `naver.selectors:` 로 덮어쓰면 됩니다:
```yaml
naver:
  selectors:
    title: [".se-section-documentTitle .se-text-paragraph"]
    body:  [".se-component.se-text .se-text-paragraph"]
    save:  ["button:has-text('저장')"]
    publish_open: ["button:has-text('발행')"]
    reserve_radio: ["label:has-text('예약')"]
```

---

## ⚠️ 안전 수칙

- 로그인은 사람이 직접(자동화 안 함) → 계정 안전.
- 1단계(적재)는 발행이 아니므로 자유롭게 자동 실행 OK.
- 2단계 예약 발행은 **너무 몰아서 X**, 주 3회 정도 자연스러운 빈도 권장.
- `config.yaml`·`.env`·`chrome_profile/`·`library/` 는 개인정보/세션이라 **깃에 안 올라갑니다**.
