import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    try:
        c.execute(open(".github/diag/map_leads.sql", encoding="utf-8").read()); c.commit(); print("::warning title=view::ok")
    except Exception as e:
        c.rollback(); print("::warning title=view::ERR "+str(e)[:300])
    print("::warning title=count::"+str(c.execute("select count(*)||' / '||string_agg(t||':'||n,' ') from (select case when type='broker' then 'broker' when type in ('tax','platform') then 'partner' else 'finance' end t, count(*) n from map_leads group by 1) x").fetchone()[0]))
    print("::warning title=songjeong::"+str(c.execute("select count(*) from map_leads where lat between 37.553 and 37.570 and lng between 126.800 and 126.823").fetchone()[0]))
