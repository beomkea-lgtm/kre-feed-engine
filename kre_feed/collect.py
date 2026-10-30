"""수집 잡: 시군구 × 유형 × 최근 N개월 → rt_trades upsert.

유형(apt/rh/sh/offi)별로 스레드 하나씩 병렬 호출하고, DB 쓰기는 메인 스레드에서 순서대로 한다.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import psycopg

from .config import Settings
from .db import get_state, set_state, upsert_trades
from .molit import MolitClient, Trade, deal_ymds

log = logging.getLogger(__name__)


def build_combos(settings: Settings, today: date, resume_from: tuple[str, str, str] | None = None) -> list[tuple[str, str, str]]:
    """실행 순서: 최신 월 먼저, 유형별로 시군구 순회. 예산이 모자라면 다음 날 이어서 돌 수 있게
    마지막 실패 조합부터 재시작한다."""
    combos = [
        (pt, sgg, ymd)
        for ymd in deal_ymds(today, settings.months_back)
        for pt in settings.prop_types
        for sgg in settings.regions
    ]
    if resume_from and resume_from in combos:
        i = combos.index(resume_from)
        combos = combos[i:] + combos[:i]
    return combos


def _make_client(settings: Settings, max_calls: int) -> MolitClient:
    return MolitClient(
        settings.data_go_kr_key,
        apt_endpoint=settings.apt_endpoint,
        num_rows=settings.num_rows,
        timeout=settings.request_timeout,
        sleep=settings.sleep_between_calls,
        max_calls=max_calls,
    )


def _fetch_group(settings: Settings, max_calls: int, combos: list[tuple[str, str, str]]):
    """한 유형의 조합들을 순서대로 호출. (결과 리스트, 호출 수, 인증실패 유형)"""
    client = _make_client(settings, max_calls)
    results: list[tuple[tuple[str, str, str], list[Trade] | None]] = list(client.fetch_many(combos))
    return results, client.calls, getattr(client, "auth_failed", set())


def run_collect(conn: psycopg.Connection, settings: Settings, today: date | None = None, _retry: bool = True) -> dict:
    settings.require("data_go_kr_key")
    today = today or date.today()

    resume_raw = get_state(conn, "collect_resume")
    resume = tuple(resume_raw.split(",")) if resume_raw else None  # type: ignore[assignment]
    combos = build_combos(settings, today, resume)  # type: ignore[arg-type]

    # 유형별 그룹 (원래 순서 유지)
    groups: dict[str, list[tuple[str, str, str]]] = {}
    for c in combos:
        groups.setdefault(c[0], []).append(c)
    per_type_budget = max(1, settings.max_calls // max(1, len(groups)))

    stats = {"combos": len(combos), "done": 0, "rows": 0, "calls": 0, "stopped_at": None, "auth_failed": []}
    with ThreadPoolExecutor(max_workers=max(1, min(len(groups), settings.collect_workers))) as ex:
        futures = {pt: ex.submit(_fetch_group, settings, per_type_budget, cs) for pt, cs in groups.items()}
        for pt, fut in futures.items():
            results, calls, auth_failed = fut.result()
            stats["calls"] += calls
            stats["auth_failed"] += sorted(auth_failed)
            for combo, trades in results:
                if trades is None:  # 이 유형 예산 소진 → 다음 실행에 여기부터
                    if stats["stopped_at"] is None:
                        stats["stopped_at"] = combo
                    break
                n = upsert_trades(conn, [t.as_row() for t in trades])
                conn.commit()
                stats["done"] += 1
                stats["rows"] += n
                log.info("%s %s %s → %d건", *combo, n)

    set_state(conn, "collect_resume", ",".join(stats["stopped_at"]) if stats["stopped_at"] else "")
    if stats["auth_failed"]:
        log.warning("인증 실패로 건너뛴 유형: %s — data.go.kr 활용신청 승인 후 보통 1~2시간 뒤 풀린다", stats["auth_failed"])
    if stats["combos"] and stats["rows"] == 0:
        # 10/28: 국토부 API가 그날 통째로 빈 응답 → 새 계약 0건. 5분 뒤 한 번 더, 그래도 0이면 다음 날 실행이 메운다
        log.error("실거래 수집 0건 (조합 %d · 호출 %d) — 국토부 API 장애 의심", stats["combos"], stats["calls"])
        if _retry:
            import time as _t
            _t.sleep(300)
            return run_collect(conn, settings, today, _retry=False)
        set_state(conn, "collect_alert", today.isoformat())
    set_state(conn, "collect_last_run", today.isoformat())
    conn.commit()
    return stats
