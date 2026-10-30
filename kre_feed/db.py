"""Postgres(Supabase) 접근. psycopg3, 연결 하나, 트랜잭션은 호출자가 관리."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)


def _json_default(o: Any):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"not serializable: {type(o).__name__}")


def _dumps(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, default=_json_default)


def jsonb(o: Any) -> Jsonb:
    return Jsonb(o, dumps=_dumps)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def connect(database_url: str) -> psycopg.Connection:
    if not database_url:
        raise SystemExit("DATABASE_URL 비어 있음")
    return psycopg.connect(database_url, autocommit=False)


def init_schema(conn: psycopg.Connection, path: Path = SCHEMA_PATH, attempts: int = 8) -> None:
    """schema.sql 적용. 다른 잡(스윕 등)이 테이블을 쓰고 있으면 ALTER/뷰 재생성이 락을 못 잡을 수 있어,
    짧은 lock_timeout 으로 시도하고 실패하면 기다렸다 다시 한다."""
    import logging
    import time as _t
    sql = path.read_text(encoding="utf-8")
    log = logging.getLogger(__name__)
    for i in range(1, attempts + 1):
        try:
            with conn.cursor() as cur:
                cur.execute("set lock_timeout = '15s'")
                cur.execute(sql)
            from .mailer import seed_sequence_steps
            seed_sequence_steps(conn)
            conn.commit()
            return
        except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled, psycopg.errors.DeadlockDetected) as e:
            conn.rollback()
            log.warning("스키마 적용 락 대기 실패 (%d/%d): %s", i, attempts, e)
            if i == attempts:
                raise
            _t.sleep(10 * i)


# ---------- app_state ----------

def get_state(conn: psycopg.Connection, key: str) -> str | None:
    row = conn.execute("select value from app_state where key = %s", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: psycopg.Connection, key: str, value: str) -> None:
    conn.execute(
        "insert into app_state(key, value) values (%s, %s) "
        "on conflict (key) do update set value = excluded.value, updated_at = now()",
        (key, value),
    )


def ensure_first_run(conn: psycopg.Connection) -> datetime:
    v = get_state(conn, "first_run_at")
    if v:
        return datetime.fromisoformat(v)
    now = datetime.now(timezone.utc)
    set_state(conn, "first_run_at", now.isoformat())
    conn.commit()
    return now


# ---------- rt_trades ----------

TRADE_COLS = (
    "trade_key, prop_type, sgg_cd, umd_cd, umd_nm, region_key, jibun, name, dong, exclu_use_ar, "
    "land_ar, total_floor_ar, floor, build_year, deal_date, deal_amount, dealing_gbn, agent_sgg_nm, "
    "seller_gbn, buyer_gbn, rgst_date, cancel_flag, cancel_date, apt_seq, raw"
)

UPSERT_TRADE = f"""
insert into rt_trades ({TRADE_COLS})
values ({", ".join(["%s"] * 24)}, %s::jsonb)
on conflict (trade_key) do update set
  umd_cd       = coalesce(excluded.umd_cd, rt_trades.umd_cd),
  dealing_gbn  = coalesce(excluded.dealing_gbn, rt_trades.dealing_gbn),
  agent_sgg_nm = coalesce(excluded.agent_sgg_nm, rt_trades.agent_sgg_nm),
  seller_gbn   = coalesce(excluded.seller_gbn, rt_trades.seller_gbn),
  buyer_gbn    = coalesce(excluded.buyer_gbn, rt_trades.buyer_gbn),
  rgst_date    = coalesce(excluded.rgst_date, rt_trades.rgst_date),
  cancel_flag  = excluded.cancel_flag,
  cancel_date  = excluded.cancel_date,
  apt_seq      = coalesce(excluded.apt_seq, rt_trades.apt_seq),
  raw          = excluded.raw,
  last_seen_at = now()
"""


def upsert_trades(conn: psycopg.Connection, rows: Sequence[tuple]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_TRADE, rows)
    return len(rows)


# ---------- metrics ----------

UPSERT_METRIC = """
insert into metrics (kind, key, period, value, prev_value, pct_change, src_url, meta, collected_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, now())
on conflict (kind, key, period) do update set
  value = excluded.value, prev_value = excluded.prev_value, pct_change = excluded.pct_change,
  src_url = excluded.src_url, meta = excluded.meta, collected_at = now()
"""


def upsert_metrics(conn: psycopg.Connection, rows: Iterable[tuple]) -> int:
    rows = [(k, key, p, v, pv, pc, u, jsonb(meta) if meta is not None else None) for (k, key, p, v, pv, pc, u, meta) in rows]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_METRIC, rows)
    return len(rows)


# ---------- issues ----------

INSERT_ISSUE = """
insert into issues (kind, headline, evidence, region_key, related_lead_ids, actions, score, dedupe_key, expires_at)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (dedupe_key) do nothing
"""


def insert_issues(conn: psycopg.Connection, rows: Iterable[dict]) -> int:
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(INSERT_ISSUE, (
                r["kind"], r["headline"], jsonb(r.get("evidence") or {}), r.get("region_key"),
                r.get("related_lead_ids") or [], r.get("actions") or [], r.get("score"),
                r["dedupe_key"], r.get("expires_at"),
            ))
            n += cur.rowcount
    return n


def expire_issues(conn: psycopg.Connection) -> int:
    cur = conn.execute("update issues set status = 'expired' where status = 'open' and expires_at is not null and expires_at < now()")
    return cur.rowcount


def related_leads(conn: psycopg.Connection, region_key: str, limit: int) -> list[str]:
    rows = conn.execute(
        "select id from leads where region_key = %s and status <> 'rejected' "
        "order by score desc nulls last limit %s",
        (region_key, limit),
    ).fetchall()
    return [str(r[0]) for r in rows]
