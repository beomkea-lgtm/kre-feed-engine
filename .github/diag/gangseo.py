import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    print("::warning title=cnt::"+str(c.execute("""select count(*) from partner_posts where coalesce(posted_at,found_at) >= '2026-11-08T15:00:00Z' and coalesce(posted_at,found_at) < '2026-11-15T15:00:00Z' and found_at >= '2026-11-01T15:00:00Z'""").fetchone()))
