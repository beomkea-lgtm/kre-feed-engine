import os, psycopg
sql = open("schema.sql", encoding="utf-8").read()
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    try:
        c.execute("set lock_timeout = '20s'"); c.execute("set statement_timeout = '300s'"); c.execute(sql); c.commit(); print("::warning title=schema::ok")
    except Exception as e:
        c.rollback(); print("::warning title=schema::ERR " + str(e)[:400])
    try:
        rows = c.execute("select name, addr, dong, dong_n30, listings from listing_rows order by dong_n30 desc nulls last, listings desc").fetchall()
        print("::warning title=rows::" + " | ".join(f"{r[0][:8]}:{r[2]}:{r[3]}:{r[4]}" for r in rows)[:1500])
        miss = [r[1] for r in rows if r[3] is None]
        print("::warning title=miss::" + " || ".join(m for m in miss)[:1500])
    except Exception as e:
        print("::warning title=q::ERR "+str(e)[:300])
