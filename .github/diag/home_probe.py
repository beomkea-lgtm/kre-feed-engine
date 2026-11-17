"""이사·인테리어 시트 반영 확인 (집계만 출력 — 연락처 값은 찍지 않음)."""
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    print("== sheet_rows home", c.execute("""select count(*), count(*) filter (where coalesce(email,'')<>''), count(*) filter (where coalesce(phone,'')<>''),
          count(*) filter (where headline like '플랫폼%%') from sheet_rows where kind='home'""").fetchone())
    print("== 상위 30 (이름·점수·근거)")
    for r in c.execute("select prio, name, score, headline, why, coalesce(phone,'')<>'', coalesce(email,'')<>'' from sheet_rows where kind='home' order by prio limit 30"):
        print("  ", r)
    print("== 시도", c.execute("select sido, count(*) from sheet_rows where kind='home' group by 1 order by 2 desc").fetchall())
