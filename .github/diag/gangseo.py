import os, psycopg
DDL = """
create or replace view map_leads as
select lc.id, lc.type, lc.name, lc.region_key, lc.lat, lc.lng, lc.status, lc.phone, lc.email, lc.url,
       lc.touch_count, lc.last_contact_at, lc.last_channel, lc.last_reply_at,
       x.rank, x.reasons, x.issue_id, x.issue_headline, x.issue_score, x.issue_at
from (
  select distinct on (il.lead_id) il.lead_id, il.rank, il.reasons, i.id issue_id, i.headline issue_headline, i.score issue_score, i.created_at issue_at
  from issue_leads il join issues i on i.id = il.issue_id
  where i.created_at > now() - interval '30 days' and i.kind in ('tx_14d','complex_14d','tx_spike','complex_cluster')
  order by il.lead_id, il.rank, i.score desc nulls last, i.created_at desc
) x join lead_cards lc on lc.id = x.lead_id
where lc.lat is not null and coalesce(lc.type,'') <> 'legal' and lc.status not in ('rejected','hidden');
grant select on map_leads to anon, authenticated, service_role;
"""
Q = {
 "map_leads_n": "select count(*)||' total, by type '||(select string_agg(t||':'||n,' ') from (select coalesce(type,'?') t, count(*) n from map_leads group by 1 order by 2 desc) a) from map_leads",
 "gangseo_n": "select count(*) from map_leads where region_key like '11500%'",
 "songjeong_near": """select count(*)||' / '||string_agg(name||'('||rank||'위)', ', ') from map_leads
   where lat between 37.553 and 37.570 and lng between 126.800 and 126.823""",
 "hillstate_near": """select count(*)||' / '||string_agg(name||'('||rank||'위)', ', ') from map_leads
   where lat between 37.545 and 37.556 and lng between 126.835 and 126.850""",
}
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    try:
        c.execute(DDL); print("::warning title=ddl::ok")
    except Exception as e:
        print("::warning title=ddl::ERR " + str(e)[:200])
    for k, q in Q.items():
        try:
            v = c.execute(q).fetchone()[0]
        except Exception as e:
            v = "ERR " + str(e)[:150]
        print(f"::warning title={k}::{v}")
