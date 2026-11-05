import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    q = {
     "free": "select count(*)||' / '||coalesce(string_agg(login_id, ',' order by seq),'') from partner_links where application_id is null and reserved is null",
     "reserved": "select count(*)||' (배치됨 '||count(*) filter (where lead_id is not null)||')' from partner_links where reserved is not null",
     "total": "select count(*)||' 전체, 배정 '||count(*) filter (where application_id is not null) from partner_links",
     "pending_apps": "select count(*) from applications where status in ('new','pending')",
    }
    for k, s in q.items():
        try: v = c.execute(s).fetchone()[0]
        except Exception as e: c.rollback(); v = "ERR " + str(e)[:200]
        print(f"::warning title={k}::{v}")
