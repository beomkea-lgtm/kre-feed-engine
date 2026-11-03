import os, psycopg
base = """with x as (
  select il.lead_id, l.type,
         case when l.type='broker' then 'broker' when l.type in ('tax','legal','platform') then 'partner' else 'finance' end cat,
         row_number() over (partition by il.issue_id, case when l.type='broker' then 'broker' when l.type in ('tax','legal','platform') then 'partner' else 'finance' end order by il.rank) crank
  from issue_leads il join issues i on i.id=il.issue_id join leads l on l.id=il.lead_id
  where i.created_at > now() - interval '30 days' and i.kind in ('tx_14d','complex_14d','tx_spike','complex_cluster')
    and l.lat is not null and coalesce(l.type,'')<>'legal' and l.status not in ('rejected','hidden'))
select string_agg(cat||':'||n, ' ') from (select cat, count(distinct lead_id) n from x where crank <= %s group by cat order by cat) y"""
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for k in (5, 10, 15, 999):
        print(f"::warning title=top{k}::" + str(c.execute(base % k).fetchone()[0]))
