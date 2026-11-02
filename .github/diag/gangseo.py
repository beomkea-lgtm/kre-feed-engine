import os, psycopg
Q = {
 "gu_rank": """with s as (select sgg_cd, count(*) n from rt_trades where not cancel_flag and deal_date >= current_date - 90 and sgg_cd like '11%' group by 1)
   select string_agg(sgg_cd||':'||n, ' ' order by n desc) from (select * from s order by n desc limit 8) x""",
 "gangseo_rank": """with s as (select sgg_cd, count(*) n from rt_trades where not cancel_flag and deal_date >= current_date - 90 and sgg_cd like '11%' group by 1)
   select (select n from s where sgg_cd='11500')||' / rank '||(select count(*)+1 from s where n > (select n from s where sgg_cd='11500'))||' of '||(select count(*) from s)""",
 "gangseo_month": """select string_agg(m||':'||n, ' ' order by m) from (select to_char(date_trunc('month', deal_date),'YYYY-MM') m, count(*) n from rt_trades where not cancel_flag and sgg_cd='11500' and deal_date >= '2026-04-01' group by 1) x""",
 "seoul_month": """select string_agg(m||':'||n, ' ' order by m) from (select to_char(date_trunc('month', deal_date),'YYYY-MM') m, count(*) n from rt_trades where not cancel_flag and sgg_cd like '11%' and deal_date >= '2026-04-01' group by 1) x""",
 "gangseo_dongs": """select string_agg(d||':'||n, ' ' order by n desc) from (select split_part(region_key,'|',2) d, count(*) n from rt_trades where not cancel_flag and deal_date >= current_date - 90 and sgg_cd='11500' group by 1 order by 2 desc limit 10) x""",
 "songjeong_complexes": """select string_agg(name||':'||n, ' | ' order by n desc) from (select split_part(region_key,'|',2)||' '||name name, count(*) n from rt_trades where not cancel_flag and deal_date >= current_date - 90 and region_key in ('11500|공항동','11500|방화동','11500|송정동','11500|마곡동') and name is not null group by 1 order by 2 desc limit 10) x""",
 "types": """select string_agg(prop_type||':'||n, ' ') from (select prop_type, count(*) n from rt_trades where not cancel_flag and deal_date >= current_date - 90 and sgg_cd='11500' group by 1) x""",
}
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for k, q in Q.items():
        v = c.execute(q).fetchone()[0]
        print(f"::warning title={k}::{v}")
