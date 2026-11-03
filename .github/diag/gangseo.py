import os, psycopg
sql = open("schema.sql", encoding="utf-8").read()
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    try:
        c.execute("set lock_timeout = '20s'"); c.execute(sql); c.commit(); print("::warning title=schema::ok")
    except Exception as e:
        c.rollback(); print("::warning title=schema::ERR " + str(e)[:300])
    print("::warning title=basket::" + str(c.execute("select string_agg(tab||':'||total,' ') from basket_counts").fetchone()[0]))
    print("::warning title=map_leads::" + str(c.execute("select count(*) from map_leads").fetchone()[0]))
