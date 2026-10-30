"""활성 지역: 카드·리드 수집·순위를 돌리는 시군구.

- 서울 25개 구는 항상 활성 (중개사 등록부 보유).
- 경기: TARGET_SGG 환경변수로 직접 지정하거나, 없으면 최근 60일 아파트·오피스텔 **거래대금 합계** 상위 GYEONGGI_TARGET_N개 자동 선정
  (건수가 아니라 금액 — 권원보험·거래 안전도는 돈이 움직이는 곳이 중요. 건수 기준이면 남양주·평택·시흥이 올라온다).
  매일 다시 계산하고 app_state['target_sgg'] 에 남긴다 (실행 로그에도 찍힘).
- 비활성 경기 시군구는 실거래 숫자만 쌓인다 (근거용).
"""
from __future__ import annotations

import logging

import psycopg

from .config import Settings
from .db import set_state
from .regions import GYEONGGI_SGG, SEOUL_SGG, sgg_name

log = logging.getLogger(__name__)


def select_gyeonggi_targets(conn: psycopg.Connection, s: Settings) -> list[str]:
    if s.target_sgg:
        return [c for c in s.target_sgg if c in GYEONGGI_SGG]
    rows = conn.execute(
        """
        select sgg_cd, sum(deal_amount) v
        from rt_trades
        where sgg_cd = any(%s) and not cancel_flag and prop_type in ('apt','offi') and deal_date >= current_date - 60
        group by sgg_cd order by v desc limit %s
        """,
        (list(GYEONGGI_SGG.keys()), s.gyeonggi_target_n),
    ).fetchall()
    return [r[0] for r in rows]


def active_sgg(conn: psycopg.Connection, s: Settings, *, persist: bool = True) -> list[str]:
    """서울 전부 + 경기 타겟. persist=True면 선정 결과를 app_state 에 기록."""
    gg = select_gyeonggi_targets(conn, s)
    if persist:
        set_state(conn, "target_sgg", ",".join(gg))
        conn.commit()
        log.info("경기 타겟 %s", [f"{c} {sgg_name(c)}" for c in gg])
    return list(SEOUL_SGG.keys()) + gg
