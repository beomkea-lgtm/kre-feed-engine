import os, sys, json, psycopg
sys.path.insert(0, ".")
from kre_feed.regions import sgg_name
D = """select sgg_cd, umd_nm, count(*) n, count(*) filter (where prop_type='apt') apt, count(*) filter (where prop_type='rh') rh,
  round(avg(deal_amount)/10000.0,1) avg_eok
  from rt_trades where not cancel_flag and deal_date >= current_date - 30 group by 1,2 order by n desc limit 40"""
C = """select sgg_cd, umd_nm, name, count(*) n, round(avg(deal_amount)/10000.0,1) avg_eok, max(prop_type) t
  from rt_trades where not cancel_flag and deal_date >= current_date - 30 and prop_type in ('apt','offi') and name is not null
  group by 1,2,3 order by n desc limit 60"""
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    d = [[sgg_name(r[0]) or r[0]] + list(r[1:]) for r in c.execute(D).fetchall()]
    cx = [[sgg_name(r[0]) or r[0]] + list(r[1:]) for r in c.execute(C).fetchall()]
    rng = c.execute("select min(deal_date), max(deal_date) from rt_trades where not cancel_flag and deal_date >= current_date - 30").fetchone()
s = json.dumps({"range": [str(rng[0]), str(rng[1])], "d": d, "c": cx}, ensure_ascii=False, default=float)
for i in range(0, len(s), 3000):
    print(f"::warning title=p{i//3000:02d}::" + s[i:i+3000])
