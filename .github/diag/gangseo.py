import os, psycopg, time
sql = open("schema.sql", encoding="utf-8").read()
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    try:
        c.execute("set lock_timeout = '20s'"); c.execute("set statement_timeout = '300s'"); c.execute(sql); c.commit(); print("::warning title=schema::ok")
    except Exception as e:
        c.rollback(); print("::warning title=schema::ERR " + str(e)[:400])
    t=time.time()
    r = c.execute("""explain analyze select id from leads where type='broker' and name_core(name)='한라' """).fetchall()
    print("::warning title=plan::" + " | ".join(x[0] for x in r)[:600])
    t=time.time()
    v = c.execute("""select import_listings('[{"reg_key":"TEST-X","reg_no":"","name":"한라공인중개사무소","phone":"010-2264-7143","email":"","listings":1,"addr":"경기도 군포시 산본동 1156-8"}]'::jsonb)""").fetchone()[0]
    c.rollback()
    print(f"::warning title=import::{v} {time.time()-t:.2f}s (rolled back)")
