import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for v in ("basket_rows","basket_counts"):
        d = c.execute("select pg_get_viewdef(%s::regclass, true)", (v,)).fetchone()[0]
        print(f"::warning title={v}::" + d.replace("\n"," ⏎ "))
    for r in c.execute("select dependent_view.relname from pg_depend join pg_rewrite on pg_depend.objid=pg_rewrite.oid join pg_class dependent_view on pg_rewrite.ev_class=dependent_view.oid join pg_class source on pg_depend.refobjid=source.oid where source.relname in ('lead_cards','feed_open','kpi_weekly','basket_rows') and dependent_view.relname not in ('lead_cards','feed_open','kpi_weekly','basket_rows') group by 1").fetchall():
        print(f"::warning title=dep::{r[0]}")
