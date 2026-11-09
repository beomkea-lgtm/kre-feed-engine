-- KRE 이슈 피드 백엔드 스키마 v1
-- Supabase SQL Editor에 그대로 붙여넣고 실행. 여러 번 실행해도 안전 (if not exists).

create extension if not exists pgcrypto;

-- 0. 앱 상태 (첫 실행 시각 등)
create table if not exists app_state (
  key         text primary key,
  value       text,
  updated_at  timestamptz not null default now()
);

-- 1. 실거래 원본 (국토부 실거래가 API 4종)
create table if not exists rt_trades (
  id              bigserial primary key,
  trade_key       text not null unique,         -- 식별 필드 해시
  prop_type       text not null,                -- apt | rh | sh | offi
  sgg_cd          text not null,                -- 시군구 5자리
  umd_cd          text,                         -- 읍면동 5자리 (아파트 상세 API만 제공)
  umd_nm          text,                         -- 법정동명
  region_key      text not null,                -- sgg_cd|umd_nm  (조인 키)
  jibun           text,
  name            text,                         -- 단지·건물명 (단독은 null)
  dong            text,                         -- 아파트 동
  exclu_use_ar    numeric,                      -- 전용면적
  land_ar         numeric,                      -- 대지(권)면적
  total_floor_ar  numeric,                      -- 연면적 (단독)
  floor           int,
  build_year      int,
  deal_date       date not null,
  deal_amount     bigint not null,              -- 만원
  dealing_gbn     text,                         -- 중개거래 | 직거래
  agent_sgg_nm    text,                         -- 중개사 소재지
  seller_gbn      text,                         -- 매도자 구분 (개인/법인/공공기관/기타)
  buyer_gbn       text,                         -- 매수자 구분
  rgst_date       date,                         -- 등기일자 (아파트)
  cancel_flag     boolean not null default false,
  cancel_date     date,
  apt_seq         text,
  raw             jsonb,
  first_seen_at   timestamptz not null default now(),   -- 우리가 처음 본 시각 = "오늘 뜬 계약" 신호
  last_seen_at    timestamptz not null default now()
);
create index if not exists rt_trades_region_date_idx on rt_trades (region_key, deal_date);
create index if not exists rt_trades_first_seen_idx  on rt_trades (first_seen_at);
create index if not exists rt_trades_name_idx        on rt_trades (region_key, name);

-- 2. 리드: 파트너·테이블·조합 전부 한 테이블
create table if not exists leads (
  id            uuid primary key default gen_random_uuid(),
  lane          text,                 -- influencer | b2b2c | bank
  type          text,                 -- broker | tax | blogger | youtube | bank_branch | coop | platform
  name          text,
  region_key    text,                 -- sgg_cd|umd_nm (rt_trades.region_key와 조인)
  region_code   text,                 -- 법정동 10자리 (있으면)
  lat numeric, lng numeric,
  contact       jsonb,                -- {phone, email, url}
  score         numeric,
  partner_code  text,                 -- QR/링크 코드
  status        text not null default 'new',  -- new | contacted | active | dormant | rejected
  source        text,
  external_id   text,
  updated_at    timestamptz not null default now()
);
create index if not exists leads_region_idx on leads (region_key);
create index if not exists leads_lat_lng_idx on leads (lat, lng);   -- 지도 화면(bbox) 리드
create unique index if not exists leads_source_ext_idx on leads (source, external_id) where external_id is not null;

-- 2-1. 리드 노출 신호 (enrich 잡이 채움). 기존 DB에도 안전하게 추가.
alter table leads add column if not exists short_name     text;          -- 상호에서 접미사 뗀 핵심어 (블로그·카카오 매칭용)
alter table leads add column if not exists blog_total     int;           -- 네이버 블로그 검색 결과 수
alter table leads add column if not exists blog_checked_at timestamptz;
alter table leads add column if not exists blog_src       text;          -- naver | daum_web
alter table leads add column if not exists listing_count  int;           -- 네이버부동산·직방 매물 수 (외부 마이닝 CSV로 채움)
alter table leads add column if not exists listing_checked_at timestamptz;
alter table leads add column if not exists email_checked_at timestamptz;   -- 공개정보 메일 수집 마지막 시도
alter table leads add column if not exists email_src        text;          -- 메일 출처: manual | auto:<url> | accepted:<url> | claude:<url> | landing
alter table leads add column if not exists email_consent_at timestamptz;
alter table leads add column if not exists email_depth int;                 -- 메일 찾기 깊이: 1 가벼운(검색 3회), 2 깊은(전 경로)   -- '자료 받기' 랜딩에서 수신 동의한 시각
create unique index if not exists leads_partner_code_idx on leads (partner_code) where partner_code is not null;

-- 2-1b. 공개정보에서 찾은 메일 후보 (emails 잡). 점수 ≥ EMAIL_MIN_AUTO 면 자동 채택(auto), 그 아래는 후보(candidate) → 프론트에서 채택/거절
create table if not exists email_candidates (
  id          bigserial primary key,
  lead_id     uuid not null references leads(id) on delete cascade,
  email       text not null,
  score       int not null,
  signals     text[],              -- phone | title | body | region | own | context | stale
  source_url  text,
  source_kind text,                -- naver_local_link | naver_blog | naver_web
  status      text not null default 'candidate',   -- candidate | auto | accepted | rejected
  found_at    timestamptz not null default now(),
  unique (lead_id, email)
);
create index if not exists email_candidates_lead_idx on email_candidates (lead_id, status);

-- 2-2. 단지: 실거래에 등장한 단지의 좌표 (카카오 지오코딩)
create table if not exists complexes (
  id            bigserial primary key,
  region_key    text not null,
  name          text not null,
  prop_type     text,
  jibun         text,
  lat numeric, lng numeric,
  geocode_src   text,                 -- kakao_keyword | kakao_address | none
  geocoded_at   timestamptz,
  kakao_rank_at timestamptz,          -- "{단지명} 부동산" 검색 순위 마지막 수집
  unique (region_key, name)
);

-- 2-2a. 네이버 지역검색 순위 (상위 5). query_key: complex:<region_key>|<단지명> | dong:<region_key> | tax:<region_key> | legal:<region_key>
create table if not exists naver_ranks (
  id            bigserial primary key,
  query_key     text not null,
  lead_id       uuid references leads(id),
  rank          int not null,
  place_name    text,
  phone         text,
  collected_at  timestamptz not null default now(),
  unique (query_key, rank)
);
create index if not exists naver_ranks_lead_idx on naver_ranks (lead_id);
alter table naver_ranks add column if not exists link text;   -- 지역검색이 준 홈페이지/블로그 링크 (메일 수집 출처)

-- 2-2b. 법정동 중심 좌표 + 제휴·금융 장소 수집 시각 (places 잡)
create table if not exists regions (
  region_key    text primary key,     -- sgg_cd|umd_nm
  sgg_cd        text,
  umd_nm        text,
  lat numeric, lng numeric,
  geocode_src   text,                 -- leads_avg | kakao_address
  geocoded_at   timestamptz,
  places_at     timestamptz           -- 세무사·법무사·은행 마지막 수집
);

-- 2-3. 카카오 "{단지명|동} 부동산" 검색 순위 (리드 매칭 결과)
create table if not exists kakao_ranks (
  id            bigserial primary key,
  query_key     text not null,        -- complex:<region_key>|<name>  또는 dong:<region_key>
  lead_id       uuid references leads(id),
  rank          int not null,
  place_name    text,
  phone         text,
  collected_at  timestamptz not null default now(),
  unique (query_key, rank)
);
create index if not exists kakao_ranks_lead_idx on kakao_ranks (lead_id);


-- 3. 원천 지표: 자동 집계
create table if not exists metrics (
  id            bigserial primary key,
  kind          text not null,        -- tx_count | tx_new7d | complex_new14d | coop_auction | keyword_rank
  key           text not null,        -- region_key | region_key|단지명 | coop_id | keyword
  period        date not null,        -- 월 시작일 또는 집계일
  value         numeric,
  prev_value    numeric,
  pct_change    numeric,
  src_url       text,
  meta          jsonb,
  collected_at  timestamptz not null default now(),
  unique (kind, key, period)
);

-- 4. 이벤트 로그. 두 종류:
--    반응(남이 나한테): scan | visit | report | subscribe | pay | email_open | reply | comment_reply
--    접촉(내가 남한테): contact  — meta.channel = email | call | visit | comment | dm, meta.note
create table if not exists events (
  id            bigserial primary key,
  lead_id       uuid references leads(id),
  type          text not null,
  occurred_at   timestamptz not null default now(),
  meta          jsonb
);
create index if not exists events_lead_time_idx on events (lead_id, occurred_at);
create index if not exists events_time_idx on events (occurred_at);

-- 5. 외부 사건
create table if not exists news (
  id            bigserial primary key,
  title         text,
  url           text unique,
  published_at  timestamptz,
  tags          text[],               -- forgery | regulation | rate
  region_key    text,
  created_at    timestamptz not null default now()
);

-- 6. 이슈 = 피드 카드. 백엔드가 만들고, 프론트는 status/taken_action만 바꾼다
create table if not exists issues (
  id                uuid primary key default gen_random_uuid(),
  kind              text not null,    -- tx_spike | complex_cluster | lead_reaction | coop_spike | news | due
  headline          text not null,
  evidence          jsonb,
  region_key        text,
  related_lead_ids  uuid[],
  actions           text[],           -- convert | content | dm | later | ignore
  score             numeric,
  dedupe_key        text not null unique,
  created_at        timestamptz not null default now(),
  expires_at        timestamptz,
  status            text not null default 'open',  -- open | taken | ignored | expired  ('나중에'는 open + snoozed_until)
  taken_action      text,
  taken_at          timestamptz
);
create index if not exists issues_open_idx on issues (status, created_at desc);
-- '나중에': 이 시각까지 피드에서 숨김 (status는 open 유지 → 만료·순위 로직 그대로)
alter table issues add column if not exists snoozed_until timestamptz;

-- 6-1. 이슈별 리드 순위·이유 (rank 잡이 채움). 프론트는 이 순서로 보여준다.
create table if not exists issue_leads (
  issue_id      uuid not null references issues(id) on delete cascade,
  lead_id       uuid not null references leads(id),
  rank          int not null,
  score         numeric,
  reasons       text[],               -- ['단지 내 사무소','80m','1층','카카오 2위','블로그 7건']
  distance_m    int,
  primary key (issue_id, lead_id)
);
create index if not exists issue_leads_issue_idx on issue_leads (issue_id, rank);

-- 7. 파이프라인: 이슈에서 액션을 누르면 프론트가 생성
create table if not exists pipeline (
  id        uuid primary key default gen_random_uuid(),
  issue_id  uuid references issues(id),
  lead_id   uuid references leads(id),
  action    text,                     -- convert | content | dm
  stage     text not null default 'todo',  -- todo | doing | done
  due_at    timestamptz,
  done_at   timestamptz,
  note      text,
  created_at timestamptz not null default now()
);

-- 8. 시퀀스 등록: 리드 × 시퀀스. 카드에서 [시퀀스 시작]을 누르면 프론트가 한 줄 insert.
--    발송 잡이 next_send_at <= now 인 active 건을 보내고 step을 올린다. 템플릿은 templates/<sequence>/<step>.md
create table if not exists enrollments (
  id            uuid primary key default gen_random_uuid(),
  lead_id       uuid not null references leads(id),
  sequence      text not null,                -- templates/ 아래 폴더명 (예: broker_intro, tax_intro)
  step          int  not null default 0,      -- 다음에 보낼 단계 (0부터)
  status        text not null default 'active',  -- active | replied | stopped | done | bounced
  next_send_at  timestamptz not null default now(),
  last_sent_at  timestamptz,
  created_at    timestamptz not null default now(),
  unique (lead_id, sequence)
);
create index if not exists enrollments_due_idx on enrollments (status, next_send_at);
create index if not exists enrollments_lead_idx on enrollments (lead_id, status);   -- lead_cards 조인

-- 8b. 시퀀스 단계 (앱에서 미리보기·발송, 06시/09시 후속 발송이 같이 읽는다). templates/ 파일은 처음 채울 때만 쓰고, 이후 수정은 여기서.
--     channel: email(자동 발송) | call | sms | visit (사람이 할 일 → 파이프라인 할 일로 생성)
--     gap_days: 이전 단계 뒤 며칠 후 (0단계는 0)
create table if not exists sequence_steps (
  sequence   text not null,
  step       int  not null,
  channel    text not null default 'email',
  gap_days   int  not null default 0,
  subject    text,
  body       text,
  updated_at timestamptz not null default now(),
  primary key (sequence, step)
);
-- 워크플로우 조건: 이전 단계 뒤 gap_days 일, send_hour 시(KST)에
--   after(무조건) | no_reply(회신 없으면) | opened(열람하면) | not_opened(미열람이면)
alter table sequence_steps add column if not exists wait_type text not null default 'no_reply';
alter table sequence_steps add column if not exists send_hour int not null default 9;
-- 리드별 워크플로우 (앱 작성 카드에서 고친 그대로): [{channel, subject, body, wait_type, gap_days, send_hour}, ...]
alter table enrollments add column if not exists plan jsonb;
alter table sequence_steps add column if not exists attachments jsonb not null default '[]'::jsonb;   -- mail_files.id
-- 메일 첨부파일 보관함 (Supabase Storage 'mail-assets' 버킷). 서명·로고는 app_state(mail_signature, mail_logo)
create table if not exists mail_files (
  id uuid primary key default gen_random_uuid(),
  name text not null, path text not null, url text not null, size int, mime text,
  created_at timestamptz not null default now()
);


-- 온라인 제휴(인플루언서) 근거: 리드(type=influencer)마다 검색 노출·주제·지역·배지·이웃 수
create table if not exists influencer_stats (
  lead_id        uuid primary key references leads(id) on delete cascade,
  blog_id        text not null,
  hits           int,            -- 검색어 결과에 몇 번 떴나 (노출력)
  best_rank      int,
  topics         text[],
  regions        text[],
  queries        text[],
  last_post      date,
  influencer     boolean,        -- 네이버 인플루언서
  influencer_url text,
  buddies        int,            -- 이웃 수
  intro          text,
  reasons        text[],
  posts          jsonb,          -- 대표 글 5개 [{title,url,date,q,rank}]
  updated_at     timestamptz not null default now()
);
alter table influencer_stats add column if not exists topic_ratio numeric;
alter table influencer_stats add column if not exists segment text not null default 'direct';   -- direct(직접수요) | tech(테크·투자)   -- RSS 최근 글 중 부동산·매수 글 비율

-- 뷰는 컬럼이 바뀌면 create or replace가 실패하므로 매번 지우고 다시 만든다 (의존 순서: feed_open → lead_cards)
drop view if exists feed_open;
drop view if exists sheet_rows;
drop view if exists listing_rows;
drop view if exists map_leads;
drop view if exists lead_cards;
drop view if exists kpi_weekly;

-- 9. KPI는 테이블이 아니라 뷰
create view kpi_weekly as
select date_trunc('week', occurred_at)::date as week,
       count(*) filter (where type = 'contact')   as contacts,
       count(*) filter (where type = 'reply')     as replies,
       count(*) filter (where type = 'scan')      as scans,
       count(*) filter (where type = 'visit')     as visits,
       count(*) filter (where type = 'report')    as reports,
       count(*) filter (where type = 'subscribe') as subscriptions,
       count(*) filter (where type = 'pay')       as payments
from events
group by 1
order by 1 desc;

-- 10. 카드용 리드: 연락처 + 접촉 이력 요약. 프론트는 이것만 읽는다.
create view lead_cards as
select l.id, l.lane, l.type, l.name, l.region_key, l.lat, l.lng, l.score, l.status,
       l.contact->>'phone'   as phone,
       l.contact->>'email'   as email,
       l.contact->>'address' as address,
       l.contact->>'url'     as url,          -- 카카오 장소 링크 등
       l.contact->>'note'     as note,        -- 플랫폼·스타트업: 무엇을 하는 곳 + 제안 각도
       l.contact->>'category' as category,
       l.contact->>'email_kind' as email_kind, -- partnership | general | form_only | none
       l.email_src, l.email_consent_at, l.partner_code,
       ec.email              as email_candidate,
       ec.score              as email_candidate_score,
       ec.source_url         as email_candidate_url,
       l.short_name, l.blog_total, l.blog_src, l.listing_count,
       c.touch_count,
       c.last_contact_at,
       c.last_channel,
       r.last_reply_at,
       e.sequence            as active_sequence,
       e.step                as sequence_step,
       e.next_send_at
from leads l
left join lateral (
  select email, score, source_url from email_candidates c
  where c.lead_id = l.id and c.status = 'candidate' order by score desc, found_at desc limit 1
) ec on true
left join lateral (
  select count(*) as touch_count, max(occurred_at) as last_contact_at,
         (array_agg(meta->>'channel' order by occurred_at desc))[1] as last_channel
  from events where lead_id = l.id and type = 'contact'
) c on true
left join lateral (
  select max(occurred_at) as last_reply_at from events where lead_id = l.id and type = 'reply'
) r on true
left join lateral (
  select sequence, step, next_send_at from enrollments
  where lead_id = l.id and status = 'active' order by created_at desc limit 1
) e on true;

-- 지도 고정 핀: 최근 30일 이슈에서 분야별 상위 10위 안에 든 업체 (카드가 만료돼도 핀은 남게), 법무사·숨김·거절 제외
create view map_leads as
select lc.id, lc.type, lc.name, lc.region_key, lc.lat, lc.lng, lc.status, lc.phone, lc.email, lc.url,
       lc.touch_count, lc.last_contact_at, lc.last_channel, lc.last_reply_at,
       x.rank, x.reasons, x.issue_id, x.issue_headline, x.issue_score, x.issue_at
from (
  select distinct on (r.lead_id) r.lead_id, r.rank, r.reasons, r.issue_id, r.issue_headline, r.issue_score, r.issue_at
  from (
    select il.lead_id, il.rank, il.reasons, i.id issue_id, i.headline issue_headline, i.score issue_score, i.created_at issue_at,
           row_number() over (partition by il.issue_id, case when l.type = 'broker' then 'b' when l.type in ('tax','legal','platform') then 'p' else 'f' end
                              order by il.rank) as crank   -- 이슈 안 분야별(중개·제휴·금융) 순위
    from issue_leads il join issues i on i.id = il.issue_id join leads l on l.id = il.lead_id
    where i.created_at > now() - interval '30 days' and i.kind in ('tx_14d','complex_14d','tx_spike','complex_cluster')
  ) r
  where r.crank <= 10   -- 분야별 상위 10곳만 (2026-11-03)
  order by r.lead_id, r.rank, r.issue_score desc nulls last, r.issue_at desc
) x join lead_cards lc on lc.id = x.lead_id
where lc.lat is not null and coalesce(lc.type,'') <> 'legal' and lc.status not in ('rejected','hidden');
grant select on map_leads to anon, authenticated, service_role;

-- 빨랑튀어 '시트' 탭: 전화·메일 컨택 목록 (대출모집법인 · 중개사 · 세무사). 지역(시·도 / 시·군·구)으로 걸러 우선순위대로 전화.
create or replace function region_of(addr text, out sido text, out sigungu text) language sql immutable as $fn$
  select case split_part(coalesce(addr,''),' ',1) when '경기도' then '경기' when '강원도' then '강원' when '충청북도' then '충북' when '충청남도' then '충남'
              when '전라남도' then '전남' when '경상북도' then '경북' when '경상남도' then '경남'
              else regexp_replace(split_part(coalesce(addr,''),' ',1), '(특별자치시|특별자치도|특별시|광역시)$', '') end,
         split_part(coalesce(addr,''),' ',2) || case when split_part(coalesce(addr,''),' ',2) ~ '시$' and split_part(coalesce(addr,''),' ',3) ~ '구$'
                                                     then ' ' || split_part(coalesce(addr,''),' ',3) else '' end
$fn$;
create view sheet_rows as
with n as (
  select il.lead_id, count(distinct il.issue_id) n_issues, max(i.score) mx
  from issue_leads il join issues i on i.id = il.issue_id
  where i.created_at > now() - interval '30 days' and i.kind in ('tx_14d','complex_14d','tx_spike','complex_cluster') group by 1
),
mp as (
  select m.type as kind, m.id, m.name, m.phone, m.email, l.contact->>'address' addr, m.url, null::text homepage,
         m.rank best_rank, coalesce(n.n_issues, 0) n_issues, null::int score, m.issue_headline headline, array_to_string(m.reasons, ' · ') why,
         null::text final, null::text seg, null::text contracts, m.touch_count, m.last_contact_at, m.last_reply_at,
         row_number() over (partition by m.type order by m.rank, n.n_issues desc nulls last, n.mx desc nulls last, m.name) prio
  from map_leads m join leads l on l.id = m.id left join n on n.lead_id = m.id
  where m.type in ('broker','tax')
),
ln0 as (
  select l.id, l.name, l.contact->>'phone' phone, coalesce(l.contact->>'email', c.emails[1]) email, l.contact->>'address' addr, l.contact->>'url' url,
         nullif(coalesce(l.contact->>'homepage', c.homepage), '') homepage, lv.final, l.contact->>'seg' seg,
         array_to_string(array(select jsonb_array_elements_text(l.contact->'contracts')), ', ') contracts,
         lv.maemae, lv.copy_risk, lv.stance, lv.recent, lc.touch_count, lc.last_contact_at, lc.last_reply_at
  from leads l left join loan_verify lv on lv.lc_num = l.external_id
  left join lateral (select * from loan_contacts c where c.lc_num = l.external_id or c.core = l.name order by (c.lc_num = l.external_id) desc limit 1) c on true
  left join lead_cards lc on lc.id = l.id
  where l.type = 'loan_broker' and l.status <> 'hidden'
),
ln as (
  select ln0.*, (case when final = '통과(검증)' then 3 else 0 end + case maemae when '높음' then 2 when '보통' then 1 else 0 end
          + case copy_risk when '낮음' then 1 when '높음' then -1 else 0 end + case when stance = '비전형' then 1 else 0 end
          + case when coalesce(recent,'') >= '2026-01-01' then 1 else 0 end + case when seg in ('1금융','보험') then 1 else 0 end
          + case when (region_of(addr)).sido in ('서울','경기') then 1 else 0 end) score
  from ln0
),
lnx as (
  select 'loan'::text kind, id, name, phone, email, addr, url,
         case when homepage ~ '(kbthink|opensalary|rocketpunch|youtube|xn--|recruit|jobkorea|saramin|jobplanet)' then null else homepage end homepage,
         null::int best_rank, 0::bigint n_issues, score, nullif(concat_ws(' · ', case when final = '통과(검증)' then '검증 통과' end,
           case when maemae = '높음' then '매매 비중 높음' end, case when copy_risk = '높음' then '모방 위험' end,
           case when stance = '비전형' then '비전형' end, case when seg in ('1금융','보험') then seg || ' 위탁' end), '') headline,
         concat_ws(' · ', '매매 ' || coalesce(maemae,'?'), '모방 ' || coalesce(copy_risk,'?'), '최근 ' || coalesce(recent,'?')) why,
         final, seg, contracts, touch_count, last_contact_at, last_reply_at,
         row_number() over (order by score desc, (final = '통과(검증)') desc, name) prio
  from ln
),
u as (select * from mp union all select * from lnx)
select u.*, (region_of(u.addr)).sido, (region_of(u.addr)).sigungu, ev.meta->>'result' last_result, ev.meta->>'note' last_note, ev.occurred_at last_call_at
from u left join lateral (
  select meta, occurred_at from events e where e.lead_id = u.id and e.type = 'contact' and e.meta->>'channel' in ('call','visit','sheet')
  order by occurred_at desc limit 1) ev on true;
grant select on sheet_rows to anon, authenticated, service_role;

-- 네이버페이 부동산에서 손으로 모은 중개사 매물 수 (시트 탭 '매물순'). 붙여넣기로 들어오고, 통화 기록을 위해 리드와 연결한다.
create table if not exists broker_listings (
  reg_key text primary key, reg_no text, name text, phone text, email text, listings int, addr text,
  lead_id uuid, imported_at timestamptz not null default now()
);
alter table broker_listings enable row level security;
create or replace function name_core(n text) returns text language sql immutable as $fn$
  select regexp_replace(coalesce(n,''), '(공인중개사사무소|공인중개사무소|공인중개사|중개사사무소|중개사무소|부동산중개|중개법인|부동산|사무소|주식회사|\(주\)|㈜|\([^)]*\)|\s)', '', 'g')
$fn$;
create index if not exists leads_broker_core_idx on leads (name_core(name)) where type = 'broker';
create index if not exists leads_broker_phone_idx on leads ((regexp_replace(coalesce(contact->>'phone',''), '\D', '', 'g'))) where type = 'broker';
-- 여러 업체에 똑같이 붙은 메일(홈페이지 제작사·플랫폼 고객센터 등)은 그 업체 메일이 아니다 → 떼어 내고 진행 중 시퀀스 멈춤.
-- 기준: 서로 다른 상호 5곳 이상, 또는 3곳 이상인데 업종이 2가지 이상. 떼어 낸 주소는 contact.email_shared 에 남긴다. 매일 cron.
create or replace function clean_shared_emails() returns json language plpgsql as $fn$
declare n_leads int; n_enr int;
begin
  create temp table _bad on commit drop as
    select lower(contact->>'email') em from leads where coalesce(contact->>'email','') <> ''
    group by 1 having count(distinct name_core(name)) >= 5 or (count(distinct name_core(name)) >= 3 and count(distinct type) >= 2);
  update enrollments e set status = 'stopped' from leads l
   where l.id = e.lead_id and e.status = 'active' and lower(l.contact->>'email') in (select em from _bad);
  get diagnostics n_enr = row_count;
  update leads set contact = (contact - 'email') || jsonb_build_object('email_shared', contact->>'email'), updated_at = now()
   where lower(contact->>'email') in (select em from _bad);
  get diagnostics n_leads = row_count;
  return json_build_object('emails', (select count(*) from _bad), 'leads', n_leads, 'stopped', n_enr);
end $fn$;

create or replace function import_listings(rows jsonb) returns json language plpgsql as $fn$
declare r record; lid uuid; n_total int := 0; n_known int := 0; n_new int := 0;
begin
  for r in select * from jsonb_to_recordset(rows) as x(reg_key text, reg_no text, name text, phone text, email text, listings int, addr text) loop
    n_total := n_total + 1;
    lid := (select lead_id from broker_listings where reg_key = r.reg_key);
    if lid is null then lid := (select id from leads where source = 'naverland' and external_id = r.reg_key limit 1); end if;
    if lid is null and length(regexp_replace(coalesce(r.phone,''), '\D', '', 'g')) >= 9 then
      lid := (select id from leads where type = 'broker' and regexp_replace(coalesce(contact->>'phone',''), '\D', '', 'g') = regexp_replace(r.phone, '\D', '', 'g') limit 1);
    end if;
    if lid is null and name_core(r.name) <> '' then
      lid := (select l.id from leads l where l.type = 'broker' and name_core(l.name) = name_core(r.name)
                and (region_of(l.contact->>'address')).sigungu = (region_of(r.addr)).sigungu limit 1);
    end if;
    if lid is null then
      insert into leads (lane, type, name, contact, status, source, external_id, updated_at)
      values ('b2b2c', 'broker', r.name, jsonb_strip_nulls(jsonb_build_object('phone', nullif(r.phone,''), 'address', nullif(r.addr,''), 'email', nullif(r.email,''), 'reg_no', nullif(r.reg_no,''))),
              'new', 'naverland', r.reg_key, now())
      returning id into lid;
      n_new := n_new + 1;
    else
      n_known := n_known + 1;
    end if;
    insert into broker_listings (reg_key, reg_no, name, phone, email, listings, addr, lead_id, imported_at)
    values (r.reg_key, r.reg_no, r.name, r.phone, nullif(r.email,''), r.listings, r.addr, lid, now())
    on conflict (reg_key) do update set reg_no = excluded.reg_no, name = excluded.name, phone = excluded.phone, email = excluded.email,
      listings = excluded.listings, addr = excluded.addr, lead_id = excluded.lead_id, imported_at = now();
  end loop;
  return json_build_object('total', n_total, 'known', n_known, 'new', n_new);
end $fn$;

-- 동네 거래량 (최근 30일 계약일 기준 매매). 시트 탭 '매물순'에서 거래 상위 동네 → 그 안에서 매물 많은 순으로 묶을 때 쓴다. 잡이 schema.sql을 적용할 때마다 새로 계산.
create or replace function addr_dong(addr text) returns text language sql immutable as $fn$
  select nullif(regexp_replace(coalesce(
    substring(coalesce(addr,'') from '\(([가-힣]+[0-9]*동)[,.\s)]'),
    substring(coalesce(addr,'') from '(?:^|\s|,)([가-힣]+[0-9]*(?:동|읍|면))[0-9-]*(?=\s|,|$)')), '^.*(상가동|단지동|아파트동|관리동)$', ''), '')
$fn$;
drop materialized view if exists dong_heat;
create materialized view dong_heat as
with names(sgg_cd, sgg_name) as (values
  ('11110','종로구'),
  ('11140','중구'),
  ('11170','용산구'),
  ('11200','성동구'),
  ('11215','광진구'),
  ('11230','동대문구'),
  ('11260','중랑구'),
  ('11290','성북구'),
  ('11305','강북구'),
  ('11320','도봉구'),
  ('11350','노원구'),
  ('11380','은평구'),
  ('11410','서대문구'),
  ('11440','마포구'),
  ('11470','양천구'),
  ('11500','강서구'),
  ('11530','구로구'),
  ('11545','금천구'),
  ('11560','영등포구'),
  ('11590','동작구'),
  ('11620','관악구'),
  ('11650','서초구'),
  ('11680','강남구'),
  ('11710','송파구'),
  ('11740','강동구'),
  ('41111','수원시 장안구'),
  ('41113','수원시 권선구'),
  ('41115','수원시 팔달구'),
  ('41117','수원시 영통구'),
  ('41131','성남시 수정구'),
  ('41133','성남시 중원구'),
  ('41135','성남시 분당구'),
  ('41150','의정부시'),
  ('41171','안양시 만안구'),
  ('41173','안양시 동안구'),
  ('41192','부천시 원미구'),
  ('41194','부천시 소사구'),
  ('41196','부천시 오정구'),
  ('41210','광명시'),
  ('41220','평택시'),
  ('41250','동두천시'),
  ('41271','안산시 상록구'),
  ('41273','안산시 단원구'),
  ('41281','고양시 덕양구'),
  ('41285','고양시 일산동구'),
  ('41287','고양시 일산서구'),
  ('41290','과천시'),
  ('41310','구리시'),
  ('41360','남양주시'),
  ('41370','오산시'),
  ('41390','시흥시'),
  ('41410','군포시'),
  ('41430','의왕시'),
  ('41450','하남시'),
  ('41461','용인시 처인구'),
  ('41463','용인시 기흥구'),
  ('41465','용인시 수지구'),
  ('41480','파주시'),
  ('41500','이천시'),
  ('41550','안성시'),
  ('41570','김포시'),
  ('41591','화성시 만세구'),
  ('41593','화성시 효행구'),
  ('41595','화성시 병점구'),
  ('41597','화성시 동탄구'),
  ('41610','광주시'),
  ('41630','양주시'),
  ('41650','포천시'),
  ('41670','여주시'),
  ('41800','연천군'),
  ('41820','가평군'),
  ('41830','양평군')
)
select case left(t.sgg_cd, 2) when '11' then '서울' when '41' then '경기' else '' end as sido,
       split_part(nm.sgg_name, ' ', 1) as city, nm.sgg_name, split_part(t.umd_nm, ' ', 1) as dong,
       count(*) as n30, count(*) filter (where t.prop_type = 'apt') as apt30
from rt_trades t join names nm on nm.sgg_cd = t.sgg_cd
where not t.cancel_flag and t.deal_date >= current_date - 30
group by 1, 2, 3, 4;
create index if not exists dong_heat_idx on dong_heat (sido, city, dong);
create view listing_rows as
select b.reg_key, b.reg_no, b.name, b.phone, b.email, b.listings, b.addr, b.lead_id as id, b.imported_at,
       (region_of(b.addr)).sido, (region_of(b.addr)).sigungu, coalesce(addr_dong(b.addr), nullif(split_part(ld.region_key, '|', 2), '')) as dong,
       h.n30 as dong_n30, h.sgg_name as dong_sgg,
       row_number() over (order by b.listings desc nulls last, b.name) as prio,
       exists (select 1 from map_leads m where m.id = b.lead_id) as on_map,
       (select count(*) from events e where e.lead_id = b.lead_id and e.type = 'contact') as touch_count,
       ev.meta->>'result' last_result, ev.meta->>'note' last_note, ev.occurred_at last_call_at
from broker_listings b
left join leads ld on ld.id = b.lead_id
left join lateral (select meta, occurred_at from events e where e.lead_id = b.lead_id and e.type = 'contact' and e.meta->>'channel' in ('call','visit','sheet')
                   order by occurred_at desc limit 1) ev on true
left join lateral (select n30, sgg_name from dong_heat h where h.sido = (region_of(b.addr)).sido and h.city = split_part((region_of(b.addr)).sigungu, ' ', 1)
                   and h.dong = coalesce(addr_dong(b.addr), nullif(split_part(ld.region_key, '|', 2), '')) order by n30 desc limit 1) h on true;
grant select on listing_rows to anon, authenticated, service_role;

-- 11. 프론트용: 열린 이슈 + 관련 리드 카드(json). 카드가 안 쓰는 필드·null은 빼서 응답을 가볍게.
--     issue_leads가 있으면 그 순위·이유로, 없으면(아직 rank 전) related_lead_ids 순서로.
create view feed_open as
select i.*,
       (select array_agg(l.name) from leads l where l.id = any(i.related_lead_ids)) as related_lead_names,
       coalesce(
         (select jsonb_agg(jsonb_strip_nulls(to_jsonb(lc) - array['lane','address','short_name','blog_total','blog_src','listing_count','score','partner_code']
                                             || jsonb_build_object('rank', il.rank, 'reasons', il.reasons))
                           order by il.rank)
            from issue_leads il join lead_cards lc on lc.id = il.lead_id where il.issue_id = i.id),
         (select jsonb_agg(jsonb_strip_nulls(to_jsonb(lc) - array['lane','address','short_name','blog_total','blog_src','listing_count','score','partner_code'])
                           order by array_position(i.related_lead_ids, lc.id))
            from lead_cards lc where lc.id = any(i.related_lead_ids))
       ) as related_leads,
       exists (select 1 from issue_leads il where il.issue_id = i.id) as ranked
from issues i
where i.status = 'open'
  and (i.expires_at is null or i.expires_at > now())
  and (i.snoozed_until is null or i.snoozed_until <= now())
order by i.score desc nulls last, i.created_at desc;

-- 제휴 신청(앱 /apply 양식) → 승인 시 파트너 링크 배정·정적 QR·승인 메일 (앱이 처리)
create table if not exists partner_links (
  id uuid primary key default gen_random_uuid(),
  seq int, url text not null unique, login_id text, login_pw text,
  qr_path text, qr_url text,
  lead_id uuid references leads(id) on delete set null,
  application_id uuid, assigned_at timestamptz,
  created_at timestamptz not null default now()
);
create table if not exists applications (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid references leads(id) on delete set null,
  org text not null, person text, phone text, email text not null, channel_url text, kind text, message text,
  consent_at timestamptz, status text not null default 'pending',   -- pending | approved | rejected
  account text,   -- 정산 계좌 (은행 포함)
  link_id uuid references partner_links(id), decided_at timestamptz, mail_id text,
  created_at timestamptz not null default now()
);
create index if not exists applications_status_idx on applications (status, created_at);
alter table partner_links enable row level security;   -- 서비스 키만 접근 (ID/PW 보관)
alter table applications enable row level security;
alter table applications add column if not exists account text;

-- 파이프라인 제휴처 바구니: 탭별 리드 한 줄 (가볍게 — lead_cards 의 무거운 집계 없이)
drop view if exists basket_counts;
drop view if exists basket_rows;
create view basket_rows as
select case when l.type in ('broker','tax','legal','platform') then l.type
            when l.type = 'loan_broker' then 'loan'
            when s.segment = 'tech' and s.blog_id like '%.%' then 'chan'
            when s.segment = 'tech' then 'tblog'
            else 'blog' end as tab,
       l.id, l.name, l.type, l.region_key, l.status, l.score,
       l.contact->>'phone' as phone, l.contact->>'email' as email, l.contact->>'url' as url,
       l.contact->>'note' as note, l.contact->>'category' as category, l.contact->>'email_kind' as email_kind,
       e.sequence as active_sequence, e.step as sequence_step, e.next_send_at,
       r.last_reply_at,
       s.blog_id, s.influencer, s.buddies, s.topic_ratio, s.reasons,
       (coalesce(l.contact->>'email','') <> '' and l.status <> 'contacted' and coalesce(s.topic_ratio, 1) >= 0.25) as fit
from leads l
left join influencer_stats s on s.lead_id = l.id
left join lateral (select sequence, step, next_send_at from enrollments en where en.lead_id = l.id and en.status = 'active' order by en.created_at desc limit 1) e on true
left join lateral (select max(occurred_at) as last_reply_at from events ev where ev.lead_id = l.id and ev.type = 'reply') r on true
where l.type in ('influencer','influencer_tech','platform')
   or (l.type in ('broker','tax','legal') and coalesce(l.contact->>'email','') <> '')
   or (l.type = 'loan_broker' and l.status <> 'hidden');   -- 대출중개 탭 (검증 통과분만 status<>hidden)
create view basket_counts as
select tab, count(*) as total,
       count(*) filter (where coalesce(email,'') <> '') as mail,
       count(*) filter (where fit) as fit,
       count(*) filter (where active_sequence is not null) as on_
from basket_rows group by tab;

-- 부동산·투자경제 양쪽에 걸린 블로거: 세그먼트별 점수를 따로 두고 높은 쪽 하나로 배정
alter table influencer_stats add column if not exists score_direct numeric;
alter table influencer_stats add column if not exists score_tech numeric;
