import os, psycopg
T0 = "(date_trunc('day', now() at time zone 'Asia/Seoul') at time zone 'Asia/Seoul')"
Q = {
 "today_by_seq": f"""select coalesce(string_agg(s||' '||st||'차: '||n||'통 (첫 '||f||' 마지막 '||l||')', ' | ' order by s, st),'없음') from (
   select coalesce(e.meta->>'sequence','?') s, coalesce(e.meta->>'step','?') st, count(*) n,
     to_char(min(occurred_at) at time zone 'Asia/Seoul','HH24:MI') f, to_char(max(occurred_at) at time zone 'Asia/Seoul','HH24:MI') l
   from events e where e.type='contact' and e.meta->>'channel'='email' and e.occurred_at >= {T0} group by 1,2) x""",
 "today_by_type": f"""select coalesce(string_agg(s||' → '||t||':'||n, ' | ' order by s, n desc),'없음') from (
   select coalesce(e.meta->>'sequence','?') s, coalesce(l.type,'?') t, count(*) n from events e join leads l on l.id=e.lead_id
   where e.type='contact' and e.meta->>'channel'='email' and e.occurred_at >= {T0} group by 1,2) x""",
 "seq_types": """select string_agg(sequence||':'||coalesce(lead_types,'?'),' | ') from (select e.sequence, string_agg(distinct l.type, ',') lead_types from enrollments e join leads l on l.id=e.lead_id where e.status='active' group by 1) x""",
 "week_sends": """select string_agg(d||':'||n, ' ' order by d) from (select to_char(occurred_at at time zone 'Asia/Seoul','MM-DD') d, count(*) n from events where type='contact' and meta->>'channel'='email' and occurred_at > now() - interval '8 days' group by 1) x""",
 "week_opens_clicks_apps": """select string_agg(d||' 열람'||o||' 클릭'||c||' 신청'||a, ' | ' order by d) from (
   select d, sum(o) o, sum(c) c, sum(a) a from (
     select to_char(occurred_at at time zone 'Asia/Seoul','MM-DD') d, (type='email_open')::int o, (type='apply_view')::int c, 0 a from events where occurred_at > now() - interval '8 days' and type in ('email_open','apply_view')
     union all select to_char(created_at at time zone 'Asia/Seoul','MM-DD'), 0, 0, 1 from applications where created_at > now() - interval '8 days') y group by 1) x""",
 "bounce_reply_today": f"""select string_agg(type||':'||n, ' ') from (select type, count(*) n from events where occurred_at >= {T0} and type in ('reply','bounce','email_open','apply_view') group by 1) x""",
 "enroll_status": """select string_agg(sequence||' '||status||':'||n, ' | ' order by sequence) from (select sequence, status, count(*) n from enrollments group by 1,2) x""",
 "caps": "select string_agg(key||'='||left(value,80), ' | ') from app_state where key in ('daily_send_cap','hourly_send_cap','paused_sequences','send_lock','mail_lock')",
 "last_runlog": "select string_agg(x, ' | ') from (select to_char(created_at at time zone 'Asia/Seoul','MM-DD HH24:MI')||' '||left(coalesce(job,'')||' '||coalesce(level,'')||' '||coalesce(msg,''),120) x from run_logs order by created_at desc limit 5) y",
 "sample_today": f"""select string_agg(l.name||'('||l.type||') '||coalesce(e.meta->>'to',l.contact->>'email',''), ' | ') from (select * from events where type='contact' and meta->>'channel'='email' and occurred_at >= {T0} order by random() limit 12) e join leads l on l.id=e.lead_id""",
}
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for k, q in Q.items():
        try: v = c.execute(q).fetchone()[0]
        except Exception as e: c.rollback(); v = "ERR " + str(e)[:200]
        print(f"::warning title={k}::{v}")
