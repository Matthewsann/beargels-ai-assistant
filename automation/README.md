# 🤖 베어글스 블로그 자동화 (크롬 직접 구동)

켜둔 PC의 **크롬으로 네이버 스마트에디터에 직접 입력 → '임시저장'** 까지 자동으로 해두는
로컬 자동화입니다. 네이버는 개인 블로그 글쓰기 공식 API가 없어, 이 방식이 가장 현실적입니다.

> **자동:** 소재 선정 · 글 작성 · 에디터 입력 · 임시저장
> **사장님 몫:** 임시저장된 글에 **실물 사진 넣고 → 발행 클릭** (검토 겸)
>
> 발행까지 자동화하지 않는 이유: ① 네이버 자동 게시 감지로 인한 **계정 제재 예방**,
> ② 사진·오탈자 **최종 확인**. 수동 업무는 "사진 + 발행 클릭"으로 최소화됩니다.

---

## 1. 설치 (처음 한 번 · PC에서)

PC에 **크롬**과 **파이썬 3.10+** 이 설치돼 있어야 합니다.

```bash
cd automation
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env            # ANTHROPIC_API_KEY 입력
cp config.example.yaml config.yaml   # 블로그ID·지역·주소 등 입력
```

- `.env` → Claude API 키 (글 생성용, https://console.anthropic.com)
- `config.yaml` → 블로그 아이디, 지역/주소/영업시간 등 브랜드 정보

## 2. 네이버 로그인 (처음 한 번)

전용 크롬 프로필에 로그인 세션만 저장합니다. (로그인 자동화 아님 — 사장님이 직접)

```bash
python src/naver_autodraft.py --login
```

크롬 창이 뜨면 네이버에 로그인(2차인증 포함) → 터미널에서 Enter.
이후로는 다시 로그인할 필요가 없습니다.

## 3. 이번 주 글 자동 생성 + 임시저장

먼저 `content-plan.yaml` 에서 이번 주 변수(신메뉴·이벤트·주제)만 바꾼 뒤:

```bash
python src/run_weekly.py
```

- `posts/` 에 초안(제목·본문·태그)이 생성되고,
- 크롬이 열려 네이버 에디터에 입력 후 **임시저장**까지 합니다.
- 네이버 블로그 → 글쓰기 → **임시저장 목록**에서 사진 넣고 발행하면 끝!

### 나눠서 실행하고 싶다면
```bash
python src/generate_post.py                 # 글만 생성 (posts/ 에 저장)
python src/naver_autodraft.py --all         # posts/ 전체를 임시저장
python src/naver_autodraft.py --post posts/01-신메뉴.json   # 하나만
```

---

## 4. 완전 반자동 — 예약 실행 (PC를 24시간 켜두는 경우)

정해진 시간에 자동으로 돌게 걸어두면, 사장님은 손도 안 대고 임시저장이 쌓입니다.

### Windows (작업 스케줄러)
1. `작업 스케줄러` → `기본 작업 만들기`
2. 트리거: 매주 원하는 요일·시간 (예: 매주 월 06:00)
3. 동작: `프로그램 시작`
   - 프로그램: `python`
   - 인수: `src\run_weekly.py`
   - 시작 위치: `C:\...\beargels-ai-assistant\automation`

### Mac / 리눅스 (cron)
```bash
crontab -e
# 매주 월요일 오전 6시 실행 (경로는 실제 위치로)
0 6 * * 1 cd /경로/beargels-ai-assistant/automation && /usr/bin/python3 src/run_weekly.py >> cron.log 2>&1
```

> 예약 실행 시엔 `config.yaml` 의 `headful: false` 로 두면 창 없이 조용히 돕니다.
> 단, 처음 몇 번은 `true` 로 두고 정상 동작을 눈으로 확인하세요.

---

## 5. 화면이 안 맞을 때 (선택자 튜닝)

네이버 스마트에디터는 화면 구조가 가끔 바뀝니다. 입력/저장이 실패하면:

1. 실패 화면이 `posts/_debug/` 에 스크린샷·HTML 로 남습니다.
2. 아래로 에디터를 열어 직접 요소를 확인:
   ```bash
   python src/naver_autodraft.py --inspect
   ```
3. `config.yaml` 에 `naver.selectors:` 를 추가해 제목/본문/저장 선택자를 덮어쓰면 됩니다. 예:
   ```yaml
   naver:
     selectors:
       title: [".se-section-documentTitle .se-text-paragraph"]
       body:  [".se-component.se-text .se-text-paragraph"]
       save:  ["button:has-text('저장')"]
   ```

이 부분은 처음 한 번만 맞춰두면 계속 잘 돕니다. 막히면 스크린샷과 함께 알려주세요 — 같이 맞추면 됩니다.

---

## ⚠️ 안전 수칙

- 로그인·발행은 **사람이 직접** (자동화 최소 노출 = 계정 안전).
- 하루에 너무 많은 글을 몰아 임시저장하지 마세요(자연스러운 빈도 권장, 주 3회 정도).
- `config.yaml`·`.env`·`chrome_profile/` 에는 개인정보/세션이 있어 **깃에 올라가지 않습니다**.
