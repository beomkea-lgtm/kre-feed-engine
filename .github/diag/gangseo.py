import os, psycopg
Q = {
 "funnel_by_seq_7d": """with s as (select e.lead_id, e.meta->>'sequence' seq from events e where e.type='contact' and e.meta->>'channel'='email' and e.occurred_at > now() - interval '7 days' and e.meta->>'sequence' like '%_intro'),
   sl as (select distinct lead_id, seq from s)
   select string_agg(seq||': 발송 '||sent||' / 열람 '||op||' / 클릭 '||cl||' / 신청 '||ap, ' | ') from (
     select sl.seq, (select count(*) from s where s.seq=sl.seq) sent,
       count(distinct sl.lead_id) filter (where exists (select 1 from events o where o.lead_id=sl.lead_id and o.type='email_open' and o.occurred_at > now()-interval '7 days')) op,
       count(distinct sl.lead_id) filter (where exists (select 1 from events o where o.lead_id=sl.lead_id and o.type='apply_view' and o.occurred_at > now()-interval '7 days')) cl,
       count(distinct sl.lead_id) filter (where exists (select 1 from applications a where a.lead_id=sl.lead_id and a.created_at > now()-interval '7 days')) ap
     from sl group by sl.seq) x""",
 "apps_7d_by_type": """select string_agg(t||':'||n, ' ') from (select coalesce(l.type,'(리드없음)') t, count(*) n from applications a left join leads l on l.id=a.lead_id where a.created_at > now()-interval '7 days' group by 1) x""",
 "tax_email_domains_today": """select string_agg(d||':'||n, ' ' order by n desc) from (select split_part(coalesce(l.contact->>'email',''),'@',2) d, count(*) n from events e join leads l on l.id=e.lead_id where e.type='contact' and e.meta->>'sequence'='tax_intro' and e.occurred_at > now()-interval '1 day' group by 1 order by 2 desc limit 12) x""",
 "tax_step0_sample_subject": """select left(plan->0->>'subject',120) from enrollments where sequence='tax_intro' and plan is not null order by created_at desc limit 1""",
}
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for k, q in Q.items():
        try: v = c.execute(q).fetchone()[0]
        except Exception as e: c.rollback(); v = "ERR " + str(e)[:200]
        print(f"::warning title={k}::{v}")
