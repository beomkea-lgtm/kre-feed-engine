# kre-feed-backend

이슈 피드 백엔드. 실거래 수집 → 집계 → 이슈 생성까지를 매일 자동으로 돌리고 Supabase에 쌓는다.
프론트(피드 웹앱)는 `feed_open` 뷰와 `issues` 테이블만 보면 된다.

```
국토부 실거래 API ──collect──▶ rt_trades ──aggregate──▶ metrics ──┐
events / news / pipeline ─────────────────────────────────────────┼──issues──▶ issues (피드 카드)
                                                                   │
enrollments ──send──▶ Gmail ──▶ events(type=contact)    first_seen_at = "오늘 뜬 계약"
```

카드 한 장의 동작: **연락처 보고 → 접촉 이력 있나 보고 → 없으면 [시퀀스 시작] → 매일 06시 자동 발송 → 접촉 이력이 쌓인다.**

## 1. 준비 (한 번, 15분)

### 공공데이터포털 키
1. https://www.data.go.kr 로그인 → 아래 4개 API 활용신청 (즉시 승인)
   - 국토교통부_아파트 매매 실거래가 상세 자료 (`RTMSDataSvcAptTradeDev`)
   - 국토교통부_연립다세대 매매 실거래가 자료 (`RTMSDataSvcRHTrade`)
   - 국토교통부_단독/다가구 매매 실거래가 자료 (`RTMSDataSvcSHTrade`)
   - 국토교통부_오피스텔 매매 실거래가 자료 (`RTMSDataSvcOffiTrade`)
2. 마이페이지의 **일반 인증키(Decoding)** 를 `DATA_GO_KR_KEY`로. (Encoding 키를 넣어도 알아서 디코딩함)
3. 개발계정은 **일 1,000콜**. 서울 25구 × 4유형 × 2개월 = 200콜/일이라 서울은 바로 된다.
   전국은 약 2,000콜/일 → 활용신청 화면에서 **운영계정 전환 신청** 걸어두기.
4. '아파트 상세' 승인이 안 나면 기본 API로: `MOLIT_APT_ENDPOINT=basic` (국토교통부_아파트 매매 실거래가 자료).

### Supabase
1. SQL Editor에 `schema.sql` 전체 붙여넣고 실행. (여러 번 실행해도 안전)
2. Project Settings → Database → Connection string **URI** (Session pooler, 5432) 를 `DATABASE_URL`로.

### Gmail (콜드메일 발송)
1. Google 계정 → 보안 → 2단계 인증 켜기 → **앱 비밀번호** → 이름 `kre-feed` → 16자리 복사.
2. GitHub Secrets에 `GMAIL_USER`(보내는 주소), `GMAIL_APP_PASSWORD`(16자리).
3. 둘 중 하나라도 없으면 발송 잡은 자동으로 **dry-run**(로그만, DB에는 dry_run=true로 기록).
   Workspace 관리자가 앱 비밀번호를 막아둔 조직은 메뉴 자체가 안 보인다 — 그러면 Gmail API(OAuth)로 바꿔야 한다.

### GitHub (스케줄 실행)
1. 이 폴더를 새 private repo로 푸시.
2. Settings → Secrets and variables → Actions
   - Secrets: `DATA_GO_KR_KEY`, `DATABASE_URL`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `KAKAO_REST_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`
   - 수집 범위를 바꾸려면 `.github/workflows/main.yml`의 `env:`에 `PROP_TYPES`, `REGIONS` 등을 한 줄 추가
3. Actions 탭 → `kre-feed daily` → **Run workflow** 로 첫 실행. 이후 매일 06:00 KST 자동.

## 2. 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # 키 채우기
python -m kre_feed init-db
python -m kre_feed daily        # collect → aggregate → issues
python -m kre_feed status
```

개별 단계: `collect` / `aggregate` / `issues` / `send`. `-v`로 상세 로그. `send --dry-run`은 발송 없이 로그만.
테스트 등록: `python -m kre_feed enroll <lead_id> broker_intro`

## 3. 동작 규칙

### 수집 (collect)
- 매일 `MONTHS_BACK`(기본 2)개월을 **재수집**한다. 신고가 계약 후 30일 안에 들어오고 해제도 생기기 때문.
- `trade_key`(식별 필드 해시)로 upsert. **`first_seen_at`은 처음 본 시각 그대로 유지** → 이게 "오늘 새로 공개된 계약" 신호.
- 호출 예산(`MAX_CALLS`, 기본 900)을 넘기면 멈추고 `app_state.collect_resume`에 위치를 적어둔다. 다음 날 그 조합부터 이어서 돈다.
- 쿼터 초과(22)·인증 오류(30번대)는 즉시 멈추고 로그에 남긴다.

### 지역 범위 — 서울 + 경기 타겟
- 실거래는 **서울 25구 + 경기 44개 시군구** 전부 매일 수집한다 (`REGIONS` 기본값, 하루 약 600호출). 경기 전체는 근거용.
- **활성 지역**(카드·리드 수집·순위·지도 핀) = 서울 전부 + 경기 타겟. 타겟은 `TARGET_SGG`(코드 콤마)로 직접 지정하거나, 비어 있으면 최근 60일 아파트·오피스텔 **거래대금 합계** 상위 `GYEONGGI_TARGET_N`(8)개를 매일 자동 선정 → `app_state.target_sgg`, 실행 로그 "경기 타겟 […]", `status`에 표시.
- 경기 타겟 동네의 중개사는 등록부 대신 카카오 로컬 중개업소(AG2) 검색으로 수집(type `broker`, source `kakao_local`). 서울은 등록부가 있어 이 검색을 건너뛴다.
- 리더보드 카드는 서울(동네 8·단지 5)과 경기 타겟(동네 `RECENT_TOP_DONGS_GG` 6·단지 `RECENT_TOP_COMPLEXES_GG` 4)을 따로 뽑아 섞는다. 비타겟 경기는 카드·리드 없음.
- 시군구 코드표: `kre_feed/regions.py` (`SEOUL_SGG`, `GYEONGGI_SGG`; 부천은 2024년 재설치된 원미·소사·오정구, 화성은 2026-02 신설 만세·효행·병점·동탄구 코드).

### 집계 (aggregate) → `metrics`
| kind | key | period | 의미 |
|---|---|---|---|
| `tx_count` | `시군구코드\|법정동` | 계약월 | 월 매매 건수(해제 제외), 전월 대비 |
| `tx_new7d` | `시군구코드\|법정동` | 집계일 | 최근 7일 **새로 공개된** 계약 수, 직전 7일 대비 ← 피드 신호 |
| `complex_new14d` | `시군구코드\|법정동\|단지명` | 집계일 | 단지별 최근 14일 새로 공개된 계약 수 (아파트·오피스텔) |

### 이슈 (issues) → `issues`
| kind | 조건 | 중복 방지 키 |
|---|---|---|
| `tx_14d` | 계약일 기준 최근 2주 계약 ≥ `TX_SPIKE_MIN`(5)인 동네 상위 `RECENT_TOP_DONGS`(8). 직전 2주와 비교 | 동네·7일 |
| `complex_14d` | 계약일 기준 최근 2주 계약 ≥ `RECENT_COMPLEX_MIN`(3)인 아파트·오피스텔 상위 `RECENT_TOP_COMPLEXES`(5) | 단지·7일 |
| `tx_spike` | `tx_new7d` ≥ `TX_SPIKE_MIN`(5) 이고 직전 주 대비 ≥ `TX_SPIKE_PCT`(30%) 또는 직전 주 0 | 지역·일 |
| `complex_cluster` | `complex_new14d` ≥ `COMPLEX_MIN`(5) | 단지·일 |
| `lead_reaction` | 최근 24시간 `events`(scan/visit/reply/email_open/…) | 리드·일 |
| `news` | 최근 24시간 등록된 태그 있는 `news` | 기사 |
| `due` | `pipeline.due_at` 도래 & 미완료 | 항목·일 |
| `coop_spike` | `metrics.kind='coop_auction'` 들어오면 자동 (경매 데이터 연결 후) | 조합·일 |

- 카드 = `headline` / `evidence`(근거) / `region_key` / `related_lead_ids`(같은 법정동 리드, 점수순 최대 20) / `actions`.
- `expires_at` = 생성 + `ISSUE_TTL_HOURS`(24). 지나면 `expired`. `tx_14d`/`complex_14d`는 `RECENT_TTL_DAYS`(7).
- **첫 수집 후 7일은 tx_spike, 14일은 complex_cluster를 만들지 않는다** (`BOOTSTRAP_QUIET_DAYS`). 첫날엔 모든 계약이 '오늘 뜬 계약'이라 신호가 가짜이기 때문. 그동안 피드를 채우는 건 계약일 기준 `tx_14d`/`complex_14d` — 이건 첫날부터 유효한 신호라 계속 돈다.
- `tx_14d`/`complex_14d`는 **같은 동네·단지 카드가 `RECENT_REPEAT_DAYS`(7) 안에는 다시 안 뜬다** — 처리함·무시·만료 상관없이. 그래서 매일 새 카드 몇 장이 아니라 "이번 주 거래 많은 곳" 한 세트가 유지된다.
- 프론트의 '나중에'는 `snoozed_until`(그 시각까지 `feed_open`에서 숨김). status는 open 그대로라 만료·순위 로직은 영향 없음. '처리함'·'무시'는 status 변경 → 프론트 '숨긴 이슈'에서 되돌릴 수 있다.

### 메일 수집 (emails) — `EMAIL_MODE=proposal`(기본)
제안·문의 맥락(협업·제휴·제안·문의·상담·연락…)이나 우리 전화번호 근처에 있는 메일만 집는다. '이메일 무단수집 거부' 고지가 있는 페이지는 건너뛴다. 리드별 경로(지역검색 링크+홈페이지 문의·제휴 페이지, 블로그 상세글·프로필, 웹문서)와 동네 역방향("{동} {업종} 문의 메일" → 전화로 매칭)을 스레드 `EMAIL_WORKERS`(8)로 동시에 돈다. 추가 경로: 카페·지식iN 스니펫, 카카오 장소 상세의 홈페이지, 2금융은 본점(금고·조합) 홈페이지를 지점끼리 한 번만, 못 찾고 자체 도메인 홈페이지가 있으면 `info@도메인`을 MX 확인 후 후보로만. 개인정보 보호책임자 메일은 제외. 하루: 06시 정기 실행 600곳 + **00시 초기 스윕(`sweep.yml`) 4조각×1,300곳**(리드당 네이버 검색 3회, `EMAIL_LIGHT`). 네이버 하루 한도(25,000)에 닿으면 남은 리드는 확인 표시 없이 다음 실행으로 넘긴다. 전 리드 한 바퀴 약 6일, 이후 대상이 없어 몇 분 만에 끝난다. 실행 로그는 runlog 브랜치 `logs/sweep-N.log`. 리드당 재시도 1년(`EMAIL_REFRESH_DAYS`). 커맨드센터 직접 검색: 정기 실행이 `exports/top_leads.csv`(runlog 브랜치)를 내보내고, Claude가 찾은 메일을 `imports/*.csv`로 push 하면 `import.yml`이 반영. `EMAIL_MODE=off`면 안 돌고, `quarantine`이면 자동으로 붙은 메일을 격리한다. 점수 규칙은 `kre_feed/emails.py` 맨 위.

#### (예전) extract 모드
- 매일 `enrich` 안에서(네이버 키 필요) 활성 지역 리드 중 메일 없는 것 `EMAIL_DAILY_LEADS`(150)건: ① 네이버 지역검색이 준 홈페이지/블로그 링크 ② 네이버 블로그 검색 "{상호} {동}" ③ 네이버 웹문서 검색 → 페이지 본문에서 메일 추출.
- 근거 점수: 전화 일치 +60 · 제목에 상호 +25(본문만 +15) · 지역 +10 · 본인 채널 +20(지역검색 링크, 블로그 작성자에 상호) · 연락 맥락 +15 · 2년 넘은 글 −20. **≥ `EMAIL_MIN_AUTO`(60) 이고 근거 2종 이상이면 자동 채택**(`leads.contact.email`, `email_src=auto:<url>`), `EMAIL_MIN_CANDIDATE`(40)~59는 `email_candidates`에 후보로만 → 프론트 리드 줄에서 채택/거절. 출처 URL·점수·근거 저장.
- noreply/webmaster/플랫폼 주소·이미지 파일명은 버린다. 상호 핵심어 2글자 미만은 안 찾는다. 리드당 `EMAIL_REFRESH_DAYS`(45)일마다 재시도, 페이지 fetch 하루 `EMAIL_PAGE_BUDGET`(450).
- 템플릿은 제목 "(광고)" + 본문 끝 수신거부 안내가 들어 있다(정보통신망법). 회신에 '수신거부'가 오면 프론트 [회신옴]으로 시퀀스가 멈춘다.

### 시퀀스 (send) — 콜드메일
- `enrollments`에 (lead_id, sequence) 한 줄 넣으면 등록. 프론트의 [시퀀스 시작] 버튼이 하는 일이 이것뿐.
- 매일 06시 잡이 `next_send_at <= now`인 active 건을 보낸다 → `events(type='contact', meta.channel='email')` 기록 → `leads.status`가 `new`면 `contacted`로 → step+1, 다음 발송일 = +3일, +4일, +7일(`MAIL_GAPS_DAYS`).
- 마지막 단계를 보내면 `done`. 메일 주소가 없거나 틀리면 `stopped`(접촉 이벤트에 `result=no_email`).
- 리드에 `events(type='reply')`가 생기면 다음 발송 전에 `replied`로 멈춘다. 회신 감지는 지금은 프론트의 [회신 옴] 버튼(= reply 이벤트 insert). Gmail API로 올리면 자동.
- 하루 발송 한도 `MAIL_DAILY_LIMIT`(기본 100). 템플릿은 `templates/<시퀀스>/<단계>.md`, 고치고 커밋하면 다음 발송부터 반영 — `templates/README.md` 참고.
- 접촉 이력은 메일만이 아니다. 전화·방문·댓글도 프론트에서 `events(type='contact', meta.channel='call'|'visit'|'comment', meta.note)`로 넣으면 같은 이력이 된다.

### 리드 순위 (enrich → rank) — "그 단지 앞에서 손님 받는 사무소"
이슈마다 같은 동네 리드를 점수로 줄 세운다. 이유가 같이 붙는다. 카테고리별 상한 — 중개 `RANK_LEADS_MAX`(20), 제휴(세무사·조합·법무사·플랫폼·인플루언서) `RANK_PARTNER_MAX`(10), 금융(은행 지점) `RANK_FINANCE_MAX`(10). 프론트는 이 셋을 탭으로 나눠 5개씩 보여준다.

| 신호 | 점수 | 출처 |
|---|---|---|
| 주소에 단지명 (단지 상가 안) | +100 | 우리 데이터 (주소 괄호 안 건물명) |
| 상호에 단지명 | +80 | 우리 데이터 |
| 단지까지 거리 ≤100m / 200 / 300 / 500 | +60 / 45 / 30 / 15 | 카카오 지오코딩 (단지 좌표) |
| 네이버 지역검색 상위 5 — 중개 "{단지명} 부동산"(없으면 "{동} 부동산"), 세무사 "{동} 세무사", 법무사 "{동} 법무사" | 1위 +40, 2위 +32, 3위 +24, 4위 +16, 5위 +8 | 네이버 검색 API(지역) → `naver_ranks`. **키 필요** (개발자센터 구키 `NAVER_HUB=false` 또는 API HUB) |
| 네이버 블로그 노출 | 5건당 +5, 최대 +20 | 네이버 검색 API(블로그). 키 없으면 다음 웹검색으로 대체(`blog_src=daum_web`) |
| 카카오 검색 순위 1위 → 10위 | +12 → +1 (약한 신호) | 카카오 로컬 API. 이 시장에선 카카오맵 리뷰·순위가 잘 안 돌아서 비중 낮춤 |
| 매물 수 | ×2, 최대 +30 | `listings` CSV (네이버부동산·직방 마이닝, 수동 공급) |
| 1층 | +15 | 우리 데이터 |

- `enrich`: 매일, **열린 이슈 동네 + 최근 2주 거래 상위 15개 동네만**. 단지 좌표·카카오/네이버 순위·블로그 수를 쌓는다(90일마다 갱신, `ENRICH_REFRESH_DAYS`). 장소 수집은 1년마다(`PLACES_REFRESH_DAYS`). 매일 꼭 도는 건 실거래·이슈·순위뿐이고 나머지는 기한 지난 것만 처리한다. 키 없으면 그 신호만 빠지고 나머지로 순위.
- `rank`: 열린 이슈 중 순위 없는 것에 `issue_leads`(rank, score, reasons, distance_m) 생성. `rank --all`로 전부 재계산. 점수 바꾸려면 `kre_feed/rank.py` 윗부분.
- `listings <csv>`: 헤더 `name,phone,listing_count` (+선택 `region_key`). 전화번호 → 상호 핵심어 순으로 매칭해 `leads.listing_count` 갱신. 매칭 안 된 줄은 로그.
- 동 단위 이슈(tx_spike)는 그 동네 최근 2주 계약 상위 5개 단지를 기준으로 같은 규칙.

### 프론트가 쓰는 것
- 읽기: `feed_open` 뷰 — 열린 이슈 + `related_leads`(json 배열: 각 리드의 전화·메일·주소·접촉 횟수·마지막 접촉·활성 시퀀스), `lead_cards` 뷰(리드 단건), `kpi_weekly`, `pipeline`.
- 쓰기: `issues.status / taken_action / taken_at`, `pipeline` 생성·수정, `leads.status / contact`, `enrollments` insert(시퀀스 시작), `events` insert(접촉·회신·스캔).
- 리드 반응은 `events`에 insert만 하면 다음 시간에 카드가 뜬다 (QR 랜딩에서 `type='scan'`, `lead_id` 또는 `partner_code`로 조회).

### 리드 적재
`leads.region_key`가 `시군구코드|법정동` 형식이면 자동 조인된다. 기존 중개사 DB를 넣을 때 이 컬럼만 맞추면 된다.

- **중개(broker)**: 서울시 중개업소 등록 CSV → `leads` (2026-10-23 25,293건, source `seoul_broker_registry`).
- **제휴·금융 (places, 자동)**: 매일 `enrich` 안에서 동네(`regions`)마다 카카오 로컬로 **세무사 · 법무사**, 그리고 **2금융 점포 — 새마을금고(`mg`) · 신협(`cu`) · 지역농협/축협(`nh_coop`) · 지역수협(`sh_coop`) · 저축은행(`savings_bank`) · 산림조합(`forest_coop`)** 을 수집한다 → `leads` (source `kakao_local`, lane `b2b2c`/`bank`). **1금융(국민·신한·우리·하나·NH농협은행·기업·SC·씨티·인터넷은행·지방은행·우체국·외국계)은 받지 않는다** (`places.FIRST_TIER`). 상호·대표번호·주소·좌표·카카오 링크, 그리고 그 동네 검색 순위(`kakao_ranks` `tax:{region_key}` 등 → 순위의 "카카오 N위"). 동네 좌표는 그 동네 중개사 좌표 평균(호출 없음), 없으면 카카오 주소 검색. 동네당 `PLACES_REFRESH_DAYS`(30)마다 갱신, 하루 `PLACES_DAILY_BUDGET`(4,500) 호출, 반경 `PLACES_RADIUS_M`(1,200). 이미 있는 리드는 상호·전화·주소·좌표만 갱신하고 status·email은 보존. 단독 실행: `python -m kre_feed places`.
- 세무서·법원 같은 기관, ATM·365코너·출장소, 하나로마트 같은 비금융 농협 시설은 거른다. 서울 밖 주소는 버린다.
- 검색어·분류 규칙이 바뀌면(`places.SPEC_VERSION`) 다음 실행에서 전 동네를 다시 돌고, 새 규칙으로 안 받는 옛 리드는 접촉 이력 없는 것만 지운다.

## 4. 테스트

```bash
pip install pytest
pytest tests                                              # 파서·페이지네이션·쿼터
TEST_DATABASE_URL=postgresql://... pytest tests          # + Postgres 통합(스키마·upsert·집계·이슈·만료)
```

## 5. 다음 단계 (설계만, 미구현)
- 세무사·협동조합 디렉토리 → `leads` CSV 적재 (중개사와 같은 변환).
- 네이버부동산·직방 매물 수 자동 수집 (지금은 수동 CSV).
- `news` 수집기: 네이버 뉴스·블로그 검색 API로 키워드(지역명·등기 위조·명의도용) 일 1회 → 기사·글 작성자가 곧 인플루언서 리드.
- Gmail API(OAuth)로 전환 → 회신·열람 자동 감지.
- `coop_auction` 메트릭 공급: 경매 CRM 데이터 접근 확정 후 어댑터 추가.

## 변경 이력
- v0.5 (2026-10-25): 공개정보 메일 수집(`emails`, 근거 점수·후보/자동 채택), 템플릿 (광고)·수신거부 문구. 경기 확장 — 실거래 서울+경기 전부 수집, 활성 지역(서울+경기 타겟 자동/지정) 개념, 경기 타겟 동네 카카오 중개업소 수집, 주소·라벨·지오코딩 시도 인식.
- v0.4 (2026-10-25): 금융 = 2금융권만(1금융 제외). 네이버 지역검색 상위 5 신호(`naver_ranks`, 키 들어오면 켜짐), 카카오 순위 비중 축소. 제휴·금융 리드 자동 수집(`places`: 카카오 로컬 세무사·법무사·은행 지점, `regions` 테이블). 순위에 중개 외 리드 포함, 카테고리(중개·제휴·금융)별 상한. 계약일 기준 2주 리더보드 `tx_14d`/`complex_14d` (조용한 기간에도 피드가 비지 않게; 동네·단지당 7일에 1장). `issues.snoozed_until`('나중에'), `feed_open`이 snoozed 제외.
- v0.3 (2026-10-23): 리드 순위 — 단지명 매칭·거리·카카오 검색순위·블로그·매물수·1층 → `issue_leads` + 이유. `complexes`, `kakao_ranks`, `listings` CSV 임포트. `feed_open`이 순위·이유를 싣는다.
- v0.2 (2026-10-23): 시퀀스 콜드메일(Gmail SMTP), `enrollments`, `lead_cards` 뷰, `feed_open`에 리드 카드 json, 접촉 이력(`events.type='contact'`), 템플릿 2종.
- v0.1 (2026-10-23): 실거래 수집·집계·이슈 생성.
