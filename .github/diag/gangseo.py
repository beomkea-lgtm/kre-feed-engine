import os, psycopg, json
sql = open("schema.sql", encoding="utf-8").read()
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    try:
        c.execute("set lock_timeout = '20s'"); c.execute(sql); c.commit(); print("::warning title=schema::ok")
    except Exception as e:
        c.rollback(); print("::warning title=schema::ERR " + str(e)[:400])
    try:
        print("::warning title=test::" + str(c.execute("select name_core('한라공인중개사무소'), name_core('센텀스마일(단지내)공인중개사무소'), (region_of('경기도 군포시 산본동 1156-8')).sigungu, count(*) from listing_rows").fetchone()))
    except Exception as e:
        print("::warning title=test::ERR "+str(e)[:300])
