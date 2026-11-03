import os, psycopg
q = """with k as (select lead_id, min(occurred_at) t from events where type='contact' and meta->>'sequence'='partner_kickoff' group by 1)
select coalesce(string_agg(distinct l.name||' ('||to_char(r.occurred_at at time zone 'Asia/Seoul','MM-DD HH24:MI')||')', ' | '),'없음')
from k join events r on r.lead_id=k.lead_id and r.type='reply' and r.occurred_at > k.t join leads l on l.id=k.lead_id"""
q2 = "select count(*) from events where type='reply' and occurred_at > now() - interval '3 hours'"
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    print("::warning title=off::" + str(c.execute(q).fetchone()[0]))
    print("::warning title=replies_3h::" + str(c.execute(q2).fetchone()[0]))
