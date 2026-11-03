import os, time, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    t=time.time()
    try:
        c.execute("set statement_timeout='60s'")
        v=c.execute("select length(send_board()::text)").fetchone()[0]
        print(f"::warning title=send_board::ok len={v} {time.time()-t:.1f}s")
    except Exception as e:
        c.rollback(); print(f"::warning title=send_board::ERR {time.time()-t:.1f}s "+str(e)[:300])
    d=c.execute("select pg_get_functiondef('send_board'::regproc)").fetchone()[0]
    print("::warning title=def::"+d[:3000].replace("\n"," ⏎ "))
    print("::warning title=role_timeout::"+str(c.execute("select string_agg(rolname||'='||coalesce(array_to_string(rolconfig,';'),''),' | ') from pg_roles where rolname in ('anon','authenticated','service_role','authenticator')").fetchone()[0]))
