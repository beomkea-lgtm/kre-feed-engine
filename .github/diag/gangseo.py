import os, psycopg, json
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    rows=c.execute("""with p as (select a.org, a.link_id, pl.url as plink, pp.url, pp.title, coalesce(pp.posted_at,pp.found_at) t,
        row_number() over (partition by pp.application_id order by coalesce(pp.posted_at,pp.found_at)) rn,
        count(*) over (partition by pp.application_id) n
      from partner_posts pp join applications a on a.id=pp.application_id left join partner_links pl on pl.id=a.link_id)
      select org, n, to_char(t at time zone 'Asia/Seoul','MM/DD'), url, title from p where n>=2 and rn=2 order by n desc, t""").fetchall()
    print(f"::warning title=n::{len(rows)}")
    out=json.dumps(rows,ensure_ascii=False)
    for i in range(0,len(out),900): print(f"::warning title=r{i//900:02d}::"+out[i:i+900])
