#!/usr/bin/env bash
# 실행 로그를 Supabase run_logs 표(name=${LOG_NAME:-latest})에 올린다.
#   push_log.sh once <logfile> <status> | push_log.sh loop <logfile>   (loop: 3분마다)
set -u
mode=$1; log=$2; status=${3:-running}
name=${LOG_NAME:-latest}
here=$(cd "$(dirname "$0")" && pwd)
push_once() { python "$here/runlog_db.py" "$name" "$1" "$log" || true; }
if [ "$mode" = "loop" ]; then
  while sleep 180; do push_once running; done
else
  push_once "$status"
fi
