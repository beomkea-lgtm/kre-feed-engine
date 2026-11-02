"""콜드비짓 전단용 선발급 링크(reserved='flyer-NN') → CSV. 암호화해서 올린다 (ID·PW 포함)."""
import csv, os, sys
import psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    adm = (c.execute("select value from app_state where key='partner_admin_url'").fetchone() or [""])[0]
    rows = c.execute("select reserved, url, login_id, login_pw from partner_links where reserved like 'flyer-%' order by reserved").fetchall()
with open(sys.argv[1], "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["no", "url", "login_id", "login_pw", "admin_url"])
    for r in rows: w.writerow([r[0], r[1], r[2], r[3], adm])
print(len(rows))
