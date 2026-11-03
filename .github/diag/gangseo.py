import os, time, psycopg
t=time.time()
with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=20) as c:
    for k,q in {"ping":"select 1","feed_open":"select count(*) from feed_open","map_leads":"select count(*) from map_leads",
                "visit_warm":"select count(*) from visit_warm","conns":"select count(*) from pg_stat_activity",
                "long":"select count(*) from pg_stat_activity where state='active' and now()-query_start > interval '30 seconds'"}.items():
        try: v=c.execute(q).fetchone()[0]
        except Exception as e: c.rollback(); v="ERR "+str(e)[:150]
        print(f"::warning title={k}::{v} ({time.time()-t:.1f}s)")
