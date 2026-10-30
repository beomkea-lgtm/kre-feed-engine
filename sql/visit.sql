-- 콜드비짓 지도 (Supabase 안에서 매일 계산 — GitHub Actions 와 무관)
-- 1) 서울·경기 동(region_key) 최근 90일 매매 건수 상위 30% → visit_regions
-- 2) 그 동의 중개사·세무사 점수 (이슈 순위와 같은 신호): 단지명 +100/+80, 거래 중심 거리 +60~15 (중개사만),
--    네이버 지역검색 1~5위 +40~8, 카카오 1~10위 +12~1, 블로그 5건당 +5(최대 20), 1층 +15
-- 3) 동 안 유형별 상위 30% (점수 0 제외) → visit_leads.top
-- 반응(열람·신청링크·회신)은 visit_warm 뷰로 실시간. 이번 주 이슈 표시는 앱이 얹는다.

create table if not exists visit_regions (
  region_key  text primary key,
  trades_90d  int not null,
  rank_no     int not null,
  n_regions   int not null,
  lat numeric, lng numeric,
  brokers int, taxes int,
  updated_at  timestamptz not null default now()
);
create table if not exists visit_leads (
  lead_id     uuid primary key references leads(id) on delete cascade,
  region_key  text not null,
  type        text not null,
  lat numeric, lng numeric,
  score       numeric,
  reasons     text[],
  rank_in_dong int,
  n_in_dong   int,
  top         boolean not null default false,
  updated_at  timestamptz not null default now()
);
create index if not exists visit_leads_geo on visit_leads (lat, lng) where top;
create index if not exists visit_leads_region on visit_leads (region_key);
alter table visit_regions enable row level security;
alter table visit_leads enable row level security;

create or replace view visit_warm as
select l.id as lead_id, l.type, l.region_key, l.lat, l.lng,
       bool_or(e.type = 'reply') as replied, bool_or(e.type = 'apply_view') as clicked,
       count(*) filter (where e.type = 'email_open') as opens, max(e.occurred_at) as last_at
from events e join leads l on l.id = e.lead_id
where e.type in ('email_open', 'apply_view', 'reply') and l.type in ('broker', 'tax') and l.lat is not null
group by l.id, l.type, l.region_key, l.lat, l.lng;

create or replace function refresh_visit() returns json language plpgsql as $$
declare n_all int; n_top int; res json;
begin
  create temp table _dong on commit drop as
    select region_key, count(*)::int n from rt_trades
    where not cancel_flag and deal_date >= current_date - 90 and (sgg_cd like '11%' or sgg_cd like '41%') and region_key like '%|%'
    group by region_key;
  select count(*) into n_all from _dong;
  n_top := greatest(1, ceil(n_all * 0.3));
  create temp table _top on commit drop as
    select region_key, n, row_number() over (order by n desc, region_key)::int rank_no from _dong order by n desc, region_key limit n_top;

  -- 동마다 최근 90일 거래 상위 단지 5곳 (이름 + 좌표) → 거래 가중 중심점
  create temp table _cx on commit drop as
    select * from (
      select t.region_key, t.name, c.lat, c.lng, count(*) n,
             row_number() over (partition by t.region_key order by count(*) desc) rn
      from rt_trades t join _top using (region_key)
      left join complexes c on c.region_key = t.region_key and c.name = t.name
      where not t.cancel_flag and t.name is not null and t.prop_type <> 'rh' and t.deal_date >= current_date - 90
      group by t.region_key, t.name, c.lat, c.lng) x where rn <= 5;
  create temp table _anchor on commit drop as
    select region_key, sum(lat * n) / nullif(sum(n) filter (where lat is not null), 0) alat,
           sum(lng * n) / nullif(sum(n) filter (where lat is not null), 0) alng
    from (select region_key, lat, lng, case when lat is null then 0 else n end n from _cx) y group by region_key;

  create temp table _l on commit drop as
    select l.id, l.type, l.region_key, l.lat, l.lng, l.name, coalesce(l.contact->>'address', '') addr, l.blog_total, l.score base
    from leads l join _top using (region_key)
    where l.type in ('broker', 'tax') and l.status <> 'rejected';

  create temp table _s on commit drop as
  with
  cxm as (   -- 단지명: 주소에 있으면 +100, 상호에 있으면 +80 (중개사만)
    select l.id, max(case when length(replace(c.name, ' ', '')) >= 2 and position(replace(c.name, ' ', '') in replace(l.addr, ' ', '')) > 0 then 100
                          when length(replace(c.name, ' ', '')) >= 2 and position(replace(c.name, ' ', '') in replace(coalesce(l.name, ''), ' ', '')) > 0 then 80 end) pts,
           (array_agg(c.name order by c.n desc))[1] cname
    from _l l join _cx c on c.region_key = l.region_key where l.type = 'broker' group by l.id),
  dist as (
    select l.id, 2 * 6371000 * asin(sqrt(power(sin(radians(l.lat - a.alat) / 2), 2) +
           cos(radians(a.alat)) * cos(radians(l.lat)) * power(sin(radians(l.lng - a.alng) / 2), 2))) d
    from _l l join _anchor a using (region_key) where l.type = 'broker' and l.lat is not null and a.alat is not null),
  nv as (
    select r.lead_id id, min(r.rank) rk from naver_ranks r join _l l on l.id = r.lead_id
    where r.rank > 0 and (r.query_key in ('dong:' || l.region_key, 'tax:' || l.region_key)
          or r.query_key in (select 'complex:' || c.region_key || '|' || c.name from _cx c where c.region_key = l.region_key))
    group by r.lead_id),
  kk as (
    select r.lead_id id, min(r.rank) rk from kakao_ranks r join _l l on l.id = r.lead_id
    where r.query_key in ('dong:' || l.region_key, 'tax:' || l.region_key)
          or r.query_key in (select 'complex:' || c.region_key || '|' || c.name from _cx c where c.region_key = l.region_key)
    group by r.lead_id)
  select l.id, l.type, l.region_key, l.lat, l.lng,
    coalesce(cxm.pts, 0)
    + coalesce(case when dist.d <= 100 then 60 when dist.d <= 200 then 45 when dist.d <= 300 then 30 when dist.d <= 500 then 15 end, 0)
    + coalesce(greatest(0, 48 - nv.rk * 8), 0)
    + coalesce(greatest(0, 12 - (kk.rk - 1) * 1.2), 0)
    + least(coalesce(l.blog_total, 0) / 5 * 5, 20)
    + case when coalesce(l.base, 0) >= 1 then 15 else 0 end as score,
    array_remove(array[
      case when cxm.pts = 100 then cxm.cname || ' 단지 내 사무소' when cxm.pts = 80 then '상호에 ' || cxm.cname end,
      case when dist.d <= 500 then '거래 중심 ' || round(dist.d)::int || 'm' end,
      case when nv.rk between 1 and 5 then '네이버 ' || nv.rk || '위' end,
      case when kk.rk between 1 and 10 then '카카오 ' || kk.rk || '위' end,
      case when coalesce(l.blog_total, 0) >= 5 then '블로그 ' || l.blog_total || '건' end,
      case when coalesce(l.base, 0) >= 1 then '1층' end], null) reasons
  from _l l left join cxm using (id) left join dist using (id) left join nv using (id) left join kk using (id);

  delete from visit_leads;
  insert into visit_leads (lead_id, region_key, type, lat, lng, score, reasons, rank_in_dong, n_in_dong, top)
  select id, region_key, type, lat, lng, round(score::numeric, 1), reasons, rk, cnt, rk <= ceil(cnt * 0.3) and score > 0
  from (select s.*, row_number() over (partition by region_key, type order by score desc, id)::int rk,
               count(*) over (partition by region_key, type)::int cnt from _s s) z;

  delete from visit_regions;
  insert into visit_regions (region_key, trades_90d, rank_no, n_regions, lat, lng, brokers, taxes)
  select t.region_key, t.n, t.rank_no, n_all,
         coalesce(r.lat, a.alat, (select avg(lat) from _l where _l.region_key = t.region_key)),
         coalesce(r.lng, a.alng, (select avg(lng) from _l where _l.region_key = t.region_key)),
         (select count(*) from _l where _l.region_key = t.region_key and type = 'broker'),
         (select count(*) from _l where _l.region_key = t.region_key and type = 'tax')
  from _top t left join regions r using (region_key) left join _anchor a using (region_key);

  select json_build_object('regions', n_all, 'top_regions', n_top,
    'min_trades', (select min(n) from _top),
    'leads', (select count(*) from visit_leads),
    'top_broker', (select count(*) from visit_leads where top and type = 'broker'),
    'top_tax', (select count(*) from visit_leads where top and type = 'tax')) into res;
  return res;
end $$;

-- 매일 07:20 KST (22:20 UTC) — 아침 실거래 수집 뒤
select cron.schedule('visit-refresh', '20 22 * * *', $$select refresh_visit()$$);
