import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    try:
        c.execute(open("sql/send_board.sql", encoding="utf-8").read()); c.commit()
        v=c.execute("select length(send_board()::text)").fetchone()[0]
        print(f"::warning title=send_board::ok len={v}")
    except Exception as e:
        c.rollback(); print("::warning title=send_board::ERR "+str(e)[:300])
