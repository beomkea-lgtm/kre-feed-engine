import os, psycopg, json
Q = """
with a as (select id, lead_id, org, decided_at, first_post_url from applications where status='approved' and merged_into is null and lead_id is not null),
k as (select lead_id, min(occurred_at) t from events where type='contact' and meta->>'sequence'='partner_kickoff' group by 1),
n as (select lead_id, array_agg(distinct (meta->>'step') order by (meta->>'step')) steps from events where type='contact' and meta->>'sequence'='partner_nudge' group by 1),
r as (select distinct e.lead_id from events e join k on k.lead_id=e.lead_id where e.type='reply' and e.occurred_at > k.t),
h as (select coalesce((select value::jsonb from app_state where key='nudge_hold'),'[]'::jsonb) v)
select a.org, to_char(a.decided_at at time zone 'Asia/Seoul','MM-DD') dec, (current_date - (a.decided_at at time zone 'Asia/Seoul')::date) age,
  k.t is not null kicked, coalesce(array_to_string(n.steps,','),'') steps, a.first_post_url is not null posted, r.lead_id is not null replied,
  (select v ? a.id::text from h) held
from a left join k on k.lead_id=a.lead_id left join n on n.lead_id=a.lead_id left join r on r.lead_id=a.lead_id
order by a.decided_at"""
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    rows = c.execute(Q).fetchall()
from collections import Counter
cat = Counter(); det = {}
for org, dec, age, kicked, steps, posted, replied, held in rows:
    s = set(steps.split(",")) - {""}
    if s >= {"0","1","2"}: k = "3회 완료"
    elif posted: k = "첫 글 올려서 중단"
    elif replied: k = "회신 와서 끔"
    elif held: k = "보류(nudge_hold)"
    elif not kicked: k = "안내 메일 전"
    elif age <= 8: k = "진행 중"
    else: k = "기간 지남(누락)"
    cat[k] += 1
    det.setdefault(k, []).append(f"{org[:14]}({dec},D+{age},보낸회차[{steps}])")
print("::warning title=total::" + str(len(rows)) + " " + json.dumps(cat, ensure_ascii=False))
for k, v in det.items():
    if k != "3회 완료":
        print(f"::warning title={k}::" + " | ".join(v)[:1800])
