create or replace view map_leads as
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
