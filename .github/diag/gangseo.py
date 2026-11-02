import os, psycopg
Q = {
 "open_total": "select count(*) from feed_open",
 "gs_open": """select string_agg(rk||'위 '||kind||' '||left(headline,30)||' exp:'||to_char(expires_at,'MM-DD HH24'), ' | ' order by rk) from (
   select row_number() over (order by score desc nulls last, created_at desc) rk, * from feed_open) x where region_key like '11500%'""",
 "gs_recent_all": """select string_agg(status||' '||kind||' '||left(headline,25)||' cr:'||to_char(created_at,'MM-DD')||' exp:'||coalesce(to_char(expires_at,'MM-DD HH24'),'-'), ' | ' order by created_at desc) from (
   select * from issues where region_key like '11500%' order by created_at desc limit 15) x""",
 "cut100_score": "select min(score) from (select score from feed_open order by score desc nulls last, created_at desc limit 100) x",
 "created_today": "select count(*)||' new, '||count(*) filter (where status='open') ||' open' from issues where created_at > now() - interval '12 hours'",
 "expired_today": "select count(*) from issues where expires_at between now() - interval '24 hours' and now()",
 "kinds_top100": "select string_agg(kind||':'||n,' ') from (select kind, count(*) n from (select kind from feed_open order by score desc nulls last, created_at desc limit 100) a group by 1) b",
}
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for k, q in Q.items():
        try:
            v = c.execute(q).fetchone()[0]
        except Exception as e:
            c.rollback(); v = "ERR " + str(e)[:150]
        print(f"::warning title={k}::{v}")
