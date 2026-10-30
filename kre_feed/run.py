"""CLI.

  python -m kre_feed init-db     # schema.sql 적용 (여러 번 실행 안전)
  python -m kre_feed collect     # 실거래 수집 → rt_trades
  python -m kre_feed aggregate   # rt_trades → metrics
  python -m kre_feed issues      # metrics/events/news/pipeline → issues, 만료 처리
  python -m kre_feed send        # enrollments 중 오늘 발송분 → Gmail (--dry-run 가능)
  python -m kre_feed enrich      # 활성 이슈 동네·단지: 카카오 좌표·검색순위, 네이버 블로그 노출 (+ places)
  python -m kre_feed places      # 제휴·금융 리드: 동네마다 세무사·법무사·2금융 (카카오 로컬) → leads
  python -m kre_feed emails      # 공개정보(네이버 검색 링크·블로그·웹문서)에서 리드 메일 찾기 → leads.contact.email / email_candidates
  python -m kre_feed rank        # 열린 이슈마다 리드 순위·이유 → issue_leads (--all 이면 전부 다시)
  python -m kre_feed listings <csv>   # 매물 수 CSV(name|phone|listing_count) → leads.listing_count
  python -m kre_feed daily       # collect → aggregate → issues → enrich → rank → send
  python -m kre_feed status      # 테이블 건수·마지막 실행
  python -m kre_feed enroll <lead_id> <sequence>   # 수동 등록 (테스트용)
  python -m kre_feed lookup lookups/<x>.json       # 특정 지번 실거래(매매·전월세) 즉석 조회 → 마크다운 (DB 불필요)
  python -m kre_feed export-top <csv>              # 열린 이슈 탭별 상위 5 중 메일 없는 리드 → CSV (Claude 직접 검색용)
  python -m kre_feed import-emails <csv>           # Claude가 찾은 메일 CSV(lead_id,email,source_url,note) 반영
"""
from __future__ import annotations

import argparse
import logging
import sys

from .aggregate import run_aggregate
from .collect import run_collect
from .config import settings
from .db import connect, get_state, init_schema
from .issues import run_issues
from .enrich import run_enrich
from .listings import import_listings
from .mailer import list_sequences
from .places import run_places
from .rank import run_rank
from .sequences import run_sequences


def cmd_status(conn) -> None:
    for t in ("rt_trades", "leads", "metrics", "events", "news", "issues", "issue_leads", "pipeline", "enrollments", "complexes", "kakao_ranks", "regions"):
        n = conn.execute(f"select count(*) from {t}").fetchone()[0]
        print(f"{t:10s} {n:>8d}")
    for k in ("first_run_at", "collect_last_run", "collect_resume", "target_sgg"):
        print(f"{k:17s} {get_state(conn, k) or '-'}")
    for ltype, n in conn.execute("select type, count(*) from leads group by type order by 2 desc").fetchall():
        print(f"  leads.{ltype:12s} {n:>8d}")
    em = conn.execute("select count(*) filter (where coalesce(contact->>'email','') <> ''), count(*) from leads").fetchone()
    cand = conn.execute("select count(*) from email_candidates where status = 'candidate'").fetchone()[0]
    print(f"emails            {em[0]}/{em[1]} ({100.0 * em[0] / max(em[1], 1):.1f}%) · 후보 대기 {cand}")
    open_n = conn.execute("select count(*) from issues where status = 'open'").fetchone()[0]
    print(f"open issues       {open_n}")
    due_n = conn.execute("select count(*) from enrollments where status='active' and next_send_at <= now()").fetchone()[0]
    print(f"mail due now      {due_n}")
    print(f"sequences         {list_sequences()}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kre_feed")
    p.add_argument("command", choices=["init-db", "collect", "aggregate", "issues", "send", "enrich", "places", "emails", "rank", "listings", "daily", "status", "enroll", "lookup", "export-top", "import-emails", "import-platforms", "influencers", "creators", "sweep"])
    p.add_argument("args", nargs="*")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="send: 실제 발송 없이 로그만")
    p.add_argument("--all", action="store_true", help="rank: 열린 이슈 전부 다시 순위")
    p.add_argument("--skip-collect", action="store_true", help="daily: 수집·집계 건너뛰고 issues→enrich→rank→send 만 (빠른 모드)")
    a = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.command == "lookup":
        from .lookup import main as lookup_main
        if len(a.args) != 1:
            raise SystemExit("usage: lookup <json>")
        return lookup_main(a.args[0])
    settings.require("database_url")

    with connect(settings.database_url) as conn:
        if a.command == "init-db":
            init_schema(conn)
            print("schema applied")
        elif a.command == "collect":
            print(run_collect(conn, settings))
        elif a.command == "aggregate":
            print(run_aggregate(conn, settings))
        elif a.command == "issues":
            print(run_issues(conn, settings))
        elif a.command == "send":
            print(run_sequences(conn, settings, dry_run=True if a.dry_run else None))
        elif a.command == "enrich":
            print(run_enrich(conn, settings))
        elif a.command == "places":
            print(run_places(conn, settings))
        elif a.command == "emails":
            from .emails import run_emails
            from .enrich import Naver
            from .targets import active_sgg
            from .enrich import make_naver
            nv = make_naver(settings)
            print(run_emails(conn, settings, nv, active_sgg(conn, settings, persist=False)))
        elif a.command == "influencers":
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            from .db import get_state
            from .emails import DaumSearch
            from .enrich import Naver
            from .influencers import run_influencers
            from .targets import active_sgg
            kst_today = (_dt.now(_tz.utc) + _td(hours=9)).date().isoformat()
            searchers = []
            from .enrich import make_naver
            nvp = make_naver(settings)
            if nvp is not None and get_state(conn, "naver_quota_day") != kst_today:
                searchers.append(nvp)
            if settings.kakao_rest_key:
                searchers.append(DaumSearch(settings.kakao_rest_key))
            seg = "tech" if (a.args and a.args[0] == "tech") else "direct"
            print(run_influencers(conn, settings, searchers, active_sgg(conn, settings, persist=False), segment=seg))
        elif a.command == "creators":
            from .creators import run_creators
            from .emails import DaumSearch
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            from .db import get_state
            from .enrich import make_naver
            kst_today = (_dt.now(_tz.utc) + _td(hours=9)).date().isoformat()
            srch = []
            nvp = make_naver(settings)
            if nvp is not None and get_state(conn, "naver_quota_day") != kst_today:
                srch.append(nvp)   # 네이버 웹문서 먼저
            if settings.kakao_rest_key:
                srch.append(DaumSearch(settings.kakao_rest_key))
            print(run_creators(conn, srch))
        elif a.command == "sweep":
            # 경기권 전수 수집: python -m kre_feed sweep tax,broker [호출예산]
            from .sweep_places import run_sweep
            kinds = (a.args[0] if a.args else "tax,broker").split(",")
            budget = int(a.args[1]) if len(a.args) > 1 else 20000
            if not settings.kakao_rest_key:
                raise SystemExit("KAKAO_REST_KEY 없음")
            print(run_sweep(conn, settings.kakao_rest_key, kinds, budget))
        elif a.command == "rank":
            print(run_rank(conn, settings, rerank_all=a.all))
        elif a.command == "listings":
            if len(a.args) != 1:
                raise SystemExit("usage: listings <csv>")
            print(import_listings(conn, a.args[0]))
        elif a.command == "enroll":
            if len(a.args) != 2:
                raise SystemExit("usage: enroll <lead_id> <sequence>")
            conn.execute("insert into enrollments (lead_id, sequence) values (%s, %s) on conflict do nothing", tuple(a.args))
            conn.commit()
            print("enrolled")
        elif a.command == "daily":
            out: dict = {}
            failed = []
            stages = [("collect", run_collect), ("aggregate", run_aggregate), ("issues", run_issues),
                      ("enrich", run_enrich), ("rank", lambda c, s: run_rank(c, s, rerank_all=True))]   # 메일 발송은 앱(빨랑튀어)이 한다
            if a.skip_collect:
                stages = stages[2:]
            for name, fn in stages:
                try:
                    out[name] = fn(conn, settings)
                except Exception as e:  # 한 단계가 죽어도 나머지는 돈다. 트레이스백은 로그.
                    conn.rollback()
                    logging.getLogger("kre_feed.daily").exception("%s 단계 실패: %s", name, e)
                    out[name] = {"error": f"{type(e).__name__}: {e}"}
                    failed.append(name)
            print(out)
            if failed:
                raise SystemExit(f"실패한 단계: {failed}")
        elif a.command == "status":
            cmd_status(conn)
        elif a.command == "import-platforms":
            from .handoff import import_platforms
            for f in a.args:
                print(import_platforms(conn, f))
        elif a.command in ("export-top", "import-emails"):
            from .handoff import export_top, import_emails
            if len(a.args) != 1:
                raise SystemExit(f"usage: {a.command} <csv>")
            print(export_top(conn, a.args[0]) if a.command == "export-top" else import_emails(conn, a.args[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
