# 베어글스 네이버 검색광고 데이터 파이프라인 — 구현 스펙

> 이 문서는 Claude Code에 그대로 전달하는 작업 지시서입니다.
> 리포지토리 루트에 `SPEC.md` 또는 `CLAUDE.md`로 두고 시작하세요.

---

## 0. 목표

베어글스 송도점의 **플레이스 광고 성과를 자동 수집**하고, 기존 매장 매출 데이터(TOS/IMU 엑셀)와 조인해
"광고비 대비 실제 매출"을 주 단위로 확인한다.

광고 **자동 집행(입찰가 자동 변경)은 1단계 범위에서 제외**한다.
먼저 데이터가 쌓이고 판단 근거가 생긴 뒤에 붙인다.

---

## 1. 인증 (여기가 제일 많이 틀리는 구간)

네이버 검색광고 API는 OAuth가 아니라 **HMAC-SHA256 서명 방식**이다.

### 필요한 값 (.env)

```
NAVER_AD_API_KEY=       # 액세스 라이선스
NAVER_AD_SECRET_KEY=    # 비밀키
NAVER_AD_CUSTOMER_ID=   # 고객 ID (숫자)
```

- `.env`는 **반드시 .gitignore에 등록**한다.
- 키 값을 코드에 하드코딩하거나 로그에 출력하지 않는다.

### 서명 생성 규칙

- Base URL: `https://api.searchad.naver.com`
- 서명 원문: `{timestamp}.{METHOD}.{path}`
  - `timestamp` = 밀리초 단위 epoch (문자열)
  - `METHOD` = 대문자 (`GET`, `POST`, `PUT`, `DELETE`)
  - `path` = **쿼리스트링을 제외한 경로만** (예: `/ncc/campaigns`)
- 서명 = HMAC-SHA256(원문, SECRET_KEY) → **Base64 인코딩**

### 요청 헤더

| 헤더 | 값 |
|---|---|
| `X-Timestamp` | 위에서 쓴 timestamp (서명과 동일한 값이어야 함) |
| `X-API-KEY` | 액세스 라이선스 |
| `X-Customer` | CUSTOMER_ID |
| `X-Signature` | Base64 서명 |
| `Content-Type` | `application/json; charset=UTF-8` |

### 공식 문서 대조 결과 (2026-09-20)

공식 `python-sample/examples/signaturehelper.py` 와 `ad_management_sample.py` 를 대조했다 — **위 규칙과 완전히 일치**(원문 `timestamp.METHOD.uri`, HMAC-SHA256 → Base64, 헤더 5개 이름 동일, 쿼리는 `params=` 로 따로). 가이드 페이지(`#/guides`) 자체에는 인증 규칙이 없고 예제 코드가 스펙이다. 구현: `naver_ads/client.py`, 확인 스크립트: `python -m naver_ads.check_campaigns`.

### 구현 요구사항

- 공통 클라이언트 모듈 하나(`naver_ads/client.py`)로 감싼다. 호출부마다 서명 로직을 복붙하지 않는다.
- 401/403이 나오면 **timestamp 불일치(로컬 시계 오차)** 를 제일 먼저 의심하도록 에러 메시지를 남긴다.
- 429(rate limit) 시 지수 백오프 재시도, 최대 3회.

---

## 2. 엔드포인트

> **주의: 아래 경로/필드명은 검증 대상이다.**
> 구현 시작 전 공식 문서 <https://naver.github.io/searchad-apidoc/#/guides> 를 열어
> 실제 스펙과 대조하고, 다르면 **문서를 따르고 이 스펙 파일을 수정**할 것.

### 2-1. 구조 조회 (마스터 데이터)

| 용도 | 메서드 | 경로 |
|---|---|---|
| 캠페인 목록 | GET | `/ncc/campaigns` |
| 광고그룹 목록 | GET | `/ncc/adgroups?nccCampaignId={id}` |
| 소재 목록 | GET | `/ncc/ads?nccAdgroupId={id}` |

- 대상 캠페인 ID: `cmp-a001-06-000000010065791`
- ID의 `-06-` 구간이 캠페인 유형 코드로 보인다(플레이스 추정). **실제 응답의 `campaignTp` 값을 로그로 찍어 확인**하고 이 문서에 기록할 것.
  - ✅ **확인(2026-09-20):** `GET /ncc/campaigns` 200, 해당 캠페인 `campaignTp = "PLACE"`. 계정 4192626 에 캠페인은 이 하나뿐.
  - ⚠️ 이 캠페인은 고객 ID **4192626** 소유다. 다른 계정 키로 부르면 200 이지만 빈 배열이 오고, ID 직접 조회는 404 `code 1018 No permission` 이 난다 — 인증 실패가 아니라 계정이 다른 것.

### 2-2. 성과 조회

| 용도 | 메서드 | 경로 |
|---|---|---|
| 단건/소량 통계 | GET | `/stats?id={id}&fields={json}&timeRange={json}` |
| 대량 리포트 | POST | `/stat-reports` (생성 → 폴링 → 다운로드 URL) |
| 마스터 리포트 | POST | `/master-reports` |

- `fields` 예: `["impCnt","clkCnt","salesAmt","ctr","cpc","avgRnk"]`
- `timeRange` 예: `{"since":"2026-09-01","until":"2026-09-19"}`
- 일별 데이터가 필요하면 `datePreset`/`timeIncrement` 계열 파라미터 지원 여부를 문서에서 확인할 것.

### 2-3. 키워드 리서치 (이게 사실상 핵심)

| 용도 | 메서드 | 경로 |
|---|---|---|
| 연관 키워드 + 월간 검색수 | GET | `/keywordstool?hintKeywords={kw}&showDetail=1` |

**왜 중요한가:** 플레이스 광고는 키워드를 직접 입찰하지 않고 스마트플레이스 업체정보로 자동 매칭된다.
따라서 이 API의 역할은 "어떤 키워드를 살까"가 아니라
**"송도 상권에서 검색량이 실제로 있는 키워드가 무엇인지 알아내서, 스마트플레이스 업체정보·메뉴명·상세설명에 그 표현을 심는 것"** 이다.

초기 시드 키워드:
```
송도 베이글, 송도 카페, 송도 브런치, 송도동 카페, 센트럴파크 카페,
타임스페이스 카페, 인천 베이글, 크림치즈 베이글, 송도 샌드위치, 송도 커피
```

산출물: `data/keyword_research_{YYYYMMDD}.csv`
컬럼: `keyword, monthlyPcQcCnt, monthlyMobileQcCnt, compIdx, monthlyAveragePcClkCnt, monthlyAveragePcCtr`

---

## 3. 알려진 함정

1. **성과 데이터에 이름이 없다.** stats 응답에는 ID만 들어오고 캠페인명/광고그룹명이 포함되지 않는다.
   → 구조 조회(2-1) 결과를 별도 테이블로 저장해두고 ID로 조인해야 한다. 2단계 구조를 전제로 설계할 것.
2. **`ror` 필드는 퍼센트 단위다** (350 = 350%). 다른 지표와 합칠 때 100으로 나눠 배수로 통일한다.
3. **플레이스 캠페인은 `/ncc/adkeywords`가 비거나 404일 수 있다.** 키워드를 직접 등록하는 유형이 아니므로 정상 동작일 가능성이 높다.
   빈 배열을 에러로 처리하지 말고 `campaign_type == PLACE`이면 키워드 수집 단계를 건너뛰도록 분기한다.
4. **비즈머니 잔액이 0이면 광고가 아예 안 나간다.** 잔액 조회 API(`/billing/bizmoney` 계열)가 있는지 확인하고,
   있으면 일일 수집에 포함해 잔액이 임계치 이하일 때 경고를 남긴다.
5. 플레이스 광고 하루 예산 상한 관련 정책이 있으므로, 소진액이 예산 대비 어떻게 붙는지 로그로 관찰한다.

---

## 4. 저장 구조

```
data/
  raw/       # API 원본 JSON (날짜별, 재파싱 대비 보존)
  ads.db     # SQLite
```

### 테이블

**`campaigns`** — `id, name, campaign_type, status, daily_budget, fetched_at`
**`adgroups`** — `id, campaign_id, name, bid_amt, status, fetched_at`
**`daily_stats`** — `stat_date, entity_type, entity_id, imp_cnt, clk_cnt, cost, ctr, cpc, avg_rnk`
  - PK: `(stat_date, entity_type, entity_id)` — 재실행 시 UPSERT (중복 적재 금지)

---

## 5. 실행

```bash
python -m naver_ads.collect --since 2026-09-01 --until 2026-09-19
python -m naver_ads.report --weeks 4
```

- 수집은 **멱등**해야 한다. 같은 기간을 두 번 돌려도 결과가 같아야 한다.
- 네이버 통계는 당일 데이터가 확정되지 않으므로, 기본 수집 범위는 **어제까지**로 한다.
- 과거 데이터는 광고 시작 시점부터 한 번 백필한다.

---

## 6. 리포트 출력 (1단계 최종 산출물)

주간 요약을 마크다운으로 출력:

```
## 2026-09-14 ~ 2026-09-20
노출 12,430  클릭 187  CTR 1.50%  CPC 62원  광고비 11,594원
전주 대비: 클릭 +12%, CPC -4원
```

### 매출 조인 (2단계)

기존 `/mnt/project`의 TOS/IMU 월별 엑셀과 날짜 기준으로 붙여
`일자 | 광고비 | 클릭 | 매출 | 광고비/매출 비중`을 산출한다.
단, **플레이스 광고 클릭과 실제 방문은 직접 연결되지 않는다.**
인과로 단정하지 말고 상관·추세로만 제시하고, 리포트에 그 한계를 한 줄 명시할 것.

---

## 7. 작업 순서

1. `.env` 로드 + HMAC 클라이언트 구현 → `/ncc/campaigns` 한 방 호출해서 **200 뜨는 것만 먼저 확인**
2. 캠페인/광고그룹 구조 저장
3. `/stats` 일별 수집 + SQLite UPSERT
4. 백필
5. 주간 리포트 출력
6. `/keywordstool` 키워드 리서치 스크립트
7. (이후) 매출 조인

**1번이 통과하기 전에 2번 이후를 작성하지 말 것.** 인증이 스펙대로 안 되는 경우가 잦다.
