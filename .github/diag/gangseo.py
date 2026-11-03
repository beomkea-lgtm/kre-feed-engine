import os, psycopg
Q = {
 "today_by_step": """select coalesce(string_agg(s||'일차 단계'||st||': '||n||'통', ' / ' order by st),'없음') from (
    select meta->>'sequence' s, meta->>'step' st, count(*) n from events
    where type='contact' and meta->>'sequence' like 'partner_%%' and occurred_at >= date_trunc('day', now() at time zone 'Asia/Seoul') at time zone 'Asia/Seoul'
    group by 1,2) x""",
 "today_list": """select coalesce(string_agg(l.name||'['||(e.meta->>'step')||'] '||to_char(e.occurred_at at time zone 'Asia/Seoul','HH24:MI'), ' | ' order by e.occurred_at),'없음') from events e join leads l on l.id=e.lead_id
    where e.type='contact' and e.meta->>'sequence'='partner_nudge' and e.occurred_at >= date_trunc('day', now() at time zone 'Asia/Seoul') at time zone 'Asia/Seoul'""",
 "dup_same_step_alltime": """select coalesce(string_agg(name||' step'||st||' x'||n||' ('||ts||')', ' | '),'없음') from (
    select l.name, e.meta->>'step' st, count(*) n, string_agg(to_char(e.occurred_at at time zone 'Asia/Seoul','MM-DD HH24:MI'), ',' order by e.occurred_at) ts
    from events e join leads l on l.id=e.lead_id where e.type='contact' and e.meta->>'sequence'='partner_nudge'
    group by l.name, e.lead_id, 2 having count(*)>1) x""",
 "dup_by_email_today": """select coalesce(string_agg(em||' x'||n, ' | '),'없음') from (
    select coalesce(e.meta->>'to', l.contact->>'email') em, e.meta->>'step' st, count(*) n from events e join leads l on l.id=e.lead_id
    where e.type='contact' and e.meta->>'sequence'='partner_nudge' and e.occurred_at > now() - interval '7 days' group by 1,2 having count(*)>1) x""",
 "kickoff_dup": """select coalesce(string_agg(name||' x'||n, ' | '),'없음') from (select l.name, count(*) n from events e join leads l on l.id=e.lead_id
    where e.type='contact' and e.meta->>'sequence'='partner_kickoff' group by l.name, e.lead_id having count(*)>1) x""",
 "seqs": "select string_agg(distinct meta->>'sequence', ',') from events where type='contact' and meta->>'sequence' like 'partner%%'",
}
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for k, q in Q.items():
        try: v = c.execute(q).fetchone()[0]
        except Exception as e: c.rollback(); v = "ERR " + str(e)[:200]
        print(f"::warning title={k}::{v}")
