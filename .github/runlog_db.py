"""실행 로그를 Supabase run_logs 표에 남긴다 (공개 저장소라 로그를 git 에 올리지 않는다).
   python .github/runlog_db.py <이름> <상태> <로그파일>  — 이름마다 마지막 실행 로그 한 줄 (앞 300KB 꼬리)."""
import os
import sys

import psycopg

name, status, path = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    body = open(path, encoding="utf-8", errors="replace").read()[-300000:]
except OSError:
    body = ""
head = f"# run {os.environ.get('GITHUB_RUN_ID', '?')} · {os.environ.get('GITHUB_EVENT_NAME', '?')} · {status}\n"
try:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15) as c:
        c.execute("create table if not exists run_logs (name text primary key, run_id text, event text, status text, body text, "
                  "updated_at timestamptz not null default now())")
        c.execute("alter table run_logs enable row level security")
        c.execute("insert into run_logs (name, run_id, event, status, body, updated_at) values (%s,%s,%s,%s,%s,now()) "
                  "on conflict (name) do update set run_id=excluded.run_id, event=excluded.event, status=excluded.status, "
                  "body=excluded.body, updated_at=now()",
                  (name, os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_EVENT_NAME"), status, head + body))
except Exception as e:  # 로그 저장 실패는 작업을 멈추지 않는다
    print(f"run_logs save failed: {e}", file=sys.stderr)
