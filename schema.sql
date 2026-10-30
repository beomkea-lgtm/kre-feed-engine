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
   or (l.type in ('broker','tax','legal') and coalesce(l.contact->>'email','') <> '');
create view basket_counts as
select tab, count(*) as total,
       count(*) filter (where coalesce(email,'') <> '') as mail,
       count(*) filter (where fit) as fit,
       count(*) filter (where active_sequence is not null) as on_
from basket_rows group by tab;

-- 부동산·투자경제 양쪽에 걸린 블로거: 세그먼트별 점수를 따로 두고 높은 쪽 하나로 배정
alter table influencer_stats add column if not exists score_direct numeric;
alter table influencer_stats add column if not exists score_tech numeric;
