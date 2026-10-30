"""집계 잡: rt_trades → metrics.

- tx_count       : 지역(region_key) × 계약월  매매 건수 (해제 제외). 전월 대비.
- tx_new7d       : 지역별 최근 7일 '새로 공개된' 계약 수 (first_seen_at 기준). 직전 7일 대비. ← 피드용 신호
- complex_new14d : 단지(region_key|단지명)별 최근 14일 새로 공개된 계약 수. 직전 14일 대비. 아파트·오피스텔만.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import psycopg

from .config import Settings
from .db import upsert_metrics

log = logging.getLogger(__name__)


def _pct(value: float, prev: float | None) -> float | None:
    if prev is None or prev == 0:
        return None
    return round((value - prev) / prev * 100, 1)


def tx_count_rows(conn: psycopg.Connection, months: int) -> list[tuple]:
    sql = """
    with by_type as (
      select region_key, max(sgg_cd) as sgg_cd, max(umd_nm) as umd_nm,
             date_trunc('month', deal_date)::date as period, prop_type, count(*) as cnt
      from rt_trades
      where not cancel_flag
        and deal_date >= (date_trunc('month', current_date) - (%s || ' months')::interval)::date
      group by region_key, date_trunc('month', deal_date), prop_type
    ), m as (
      select region_key, max(sgg_cd) as sgg_cd, max(umd_nm) as umd_nm, period,
             sum(cnt) as value, jsonb_object_agg(prop_type, cnt) as by_type
      from by_type
      group by region_key, period
    )
    select region_key, sgg_cd, umd_nm, period, value, by_type,
           lag(value) over (partition by region_key order by period) as prev_value
    from m
    """
    rows = conn.execute(sql, (months,)).fetchall()
    out = []
    for region_key, sgg_cd, umd_nm, period, value, by_type, prev in rows:
        out.append(("tx_count", region_key, period, value, prev, _pct(value, prev), None,
                    {"sgg_cd": sgg_cd, "umd_nm": umd_nm, "by_type": by_type}))
    return out


def tx_new7d_rows(conn: psycopg.Connection, as_of: datetime) -> list[tuple]:
    sql = """
    select region_key, max(sgg_cd), max(umd_nm),
           count(*) filter (where first_seen_at >= %(a)s - interval '7 days') as cur,
           count(*) filter (where first_seen_at >= %(a)s - interval '14 days' and first_seen_at < %(a)s - interval '7 days') as prev,
           jsonb_object_agg(prop_type, 1) as types
    from rt_trades
    where not cancel_flag and first_seen_at >= %(a)s - interval '14 days'
    group by region_key
    """
    rows = conn.execute(sql, {"a": as_of}).fetchall()
    period = as_of.date()
    return [("tx_new7d", rk, period, cur, prev, _pct(cur, prev), None,
             {"sgg_cd": sgg, "umd_nm": umd}) for rk, sgg, umd, cur, prev, _types in rows]


def complex_new14d_rows(conn: psycopg.Connection, as_of: datetime) -> list[tuple]:
    sql = """
    select region_key, name, max(sgg_cd), max(umd_nm), max(prop_type),
           count(*) filter (where first_seen_at >= %(a)s - interval '14 days') as cur,
           count(*) filter (where first_seen_at >= %(a)s - interval '28 days' and first_seen_at < %(a)s - interval '14 days') as prev,
           round(avg(deal_amount) filter (where first_seen_at >= %(a)s - interval '14 days')) as avg_amount
    from rt_trades
    where not cancel_flag and prop_type in ('apt', 'offi') and name is not null
      and first_seen_at >= %(a)s - interval '28 days'
    group by region_key, name
    """
    rows = conn.execute(sql, {"a": as_of}).fetchall()
    period = as_of.date()
    return [("complex_new14d", f"{rk}|{name}", period, cur, prev, _pct(cur, prev), None,
             {"sgg_cd": sgg, "umd_nm": umd, "name": name, "prop_type": pt, "avg_amount_manwon": int(avg) if avg else None})
            for rk, name, sgg, umd, pt, cur, prev, avg in rows]


def run_aggregate(conn: psycopg.Connection, settings: Settings, as_of: datetime | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    n1 = upsert_metrics(conn, tx_count_rows(conn, settings.months_back + 1))
    n2 = upsert_metrics(conn, tx_new7d_rows(conn, as_of))
    n3 = upsert_metrics(conn, complex_new14d_rows(conn, as_of))
    conn.commit()
    stats = {"tx_count": n1, "tx_new7d": n2, "complex_new14d": n3}
    log.info("집계 %s", stats)
    return stats
