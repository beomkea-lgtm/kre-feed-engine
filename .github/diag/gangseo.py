import os, psycopg, json
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    rows = c.execute("""select to_char(e.occurred_at at time zone 'Asia/Seoul','HH24:MI'), e.meta->>'sequence', e.meta->>'step', e.meta->>'subject',
      coalesce(a.org,l.name), a.link_id
      from events e left join leads l on l.id=e.lead_id
      left join applications a on a.lead_id=e.lead_id and a.status='approved' and a.merged_into is null
      where e.type='contact' and e.meta->>'by'='retention' and e.occurred_at >= (date_trunc('day', now() at time zone 'Asia/Seoul') at time zone 'Asia/Seoul')
      order by 1""").fetchall()
    out = "\n".join(" | ".join(str(x) for x in r) for r in rows)
    print(f"::warning title=n::{len(rows)}")
    for i in range(0, len(out), 900): print(f"::warning title=r{i//900}::" + out[i:i+900].replace("\n"," // "))
    f = c.execute("select value from app_state where key='weekly_featured'").fetchone()
    print("::warning title=feat::" + json.dumps(f[0] if f else None, ensure_ascii=False)[:900])
