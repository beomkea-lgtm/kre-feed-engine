import os, psycopg
sql = open("schema.sql", encoding="utf-8").read()
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    try:
        c.execute("set lock_timeout = '20s'"); c.execute(sql); c.commit(); print("::warning title=schema::ok")
    except Exception as e:
        c.rollback(); print("::warning title=schema::ERR " + str(e)[:400])
    try:
        print("::warning title=kinds::" + str(c.execute("select string_agg(kind||':'||n,' ') from (select kind, count(*) n from sheet_rows group by 1) x").fetchone()[0]))
        print("::warning title=regions::" + str(c.execute("select string_agg(sido||'/'||sigungu||':'||n,' , ') from (select sido, sigungu, count(*) n from sheet_rows where kind='broker' group by 1,2 order by 3 desc limit 25) x").fetchone()[0]))
        print("::warning title=sido::" + str(c.execute("select string_agg(kind||'-'||sido||':'||n,' ') from (select kind, sido, count(*) n from sheet_rows group by 1,2 order by 1,3 desc) x").fetchone()[0]))
        print("::warning title=loantop::" + str(c.execute("select string_agg(prio||' '||name||' '||score||' '||sido||' '||sigungu, ' | ' order by prio) from sheet_rows where kind='loan' and prio<=8").fetchone()[0]))
    except Exception as e:
        print("::warning title=q::ERR "+str(e)[:300])
