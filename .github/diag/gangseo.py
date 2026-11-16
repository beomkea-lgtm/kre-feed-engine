import os, psycopg, json
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    q=lambda s,*a: c.execute(s,a).fetchall()
    rows=q("select prop_type, count(*), sum((dealing_gbn='직거래')::int) from rt_trades where deal_date between '2026-11-09' and '2026-11-15' and not cancel_flag group by 1")
    print("::warning title=type::"+json.dumps(rows,default=str))
    print("::warning title=sgg::"+str(q("select count(distinct sgg_cd) from rt_trades where deal_date between '2026-11-09' and '2026-11-15' and not cancel_flag")))
    print("::warning title=top::"+json.dumps(q("select region_key,count(*) from rt_trades where deal_date between '2026-11-09' and '2026-11-15' and not cancel_flag group by 1 order by 2 desc limit 3"),ensure_ascii=False))
    print("::warning title=max::"+json.dumps(q("select region_key,name,prop_type,deal_amount from rt_trades where deal_date between '2026-11-09' and '2026-11-15' and not cancel_flag order by deal_amount::numeric desc limit 1"),ensure_ascii=False,default=str))
    p=q("""select a.org, p.title, p.url, p.checks, coalesce(p.posted_at,p.found_at) from partner_posts p left join applications a on a.id=p.application_id
      where coalesce(p.posted_at,p.found_at) >= '2026-11-08T15:00:00Z' and coalesce(p.posted_at,p.found_at) < '2026-11-15T15:00:00Z' and p.found_at >= '2026-11-01T15:00:00Z' order by coalesce(p.posted_at,p.found_at) desc""")
    print("::warning title=posts::"+json.dumps(p,ensure_ascii=False,default=str)[:950])
