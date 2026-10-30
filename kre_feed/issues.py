"""이슈 빌더: metrics / events / news / pipeline → issues (피드 카드).

규칙 (한 종류당 한 함수):
- tx_14d          : 계약일 기준 최근 2주 계약 상위 동네 (n ≥ TX_SPIKE_MIN, 상위 RECENT_TOP_DONGS) → 동네당 1장,
                    수명 RECENT_TTL_DAYS, 처리/무시해도 RECENT_REPEAT_DAYS 동안은 다시 안 뜸. 첫날부터 유효.
- complex_14d     : 계약일 기준 최근 2주 계약 상위 단지 (n ≥ RECENT_COMPLEX_MIN, 상위 RECENT_TOP_COMPLEXES) → 같은 규칙
- tx_spike        : tx_new7d value ≥ TX_SPIKE_MIN 이고 (전주 대비 ≥ TX_SPIKE_PCT 또는 전주 0) → 지역·일당 1장
- complex_cluster : complex_new14d value ≥ COMPLEX_MIN → 단지·일당 1장
- lead_reaction   : 최근 24시간 리드 반응 이벤트 → 리드·일당 1장
- news            : 최근 24시간 등록된 태그 있는 기사 → 기사당 1장
- due             : pipeline.due_at 도래 & 미완료 → 항목·일당 1장
- coop_spike      : 경매 CRM 데이터 접근 확정 후 (metrics kind coop_auction 들어오면 자동 동작)
만료: expires_at 지난 open 이슈 → expired
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from functools import partial

import psycopg

from .config import Settings
from .db import ensure_first_run, expire_issues, insert_issues, related_leads
from .regions import SEOUL_SGG, sgg_name
from .targets import active_sgg

log = logging.getLogger(__name__)

DEFAULT_ACTIONS = ["convert", "content", "dm", "later", "ignore"]
REACTION_TYPES = ("scan", "visit", "reply", "email_open", "comment_reply", "report", "subscribe", "pay")
REACTION_LABEL = {"scan": "QR 스캔", "visit": "링크 접속", "reply": "회신", "email_open": "메일 열람",
                  "comment_reply": "댓글 답글", "report": "리포트 발급", "subscribe": "청약", "pay": "결제"}


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "전주 0건"
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.0f}%"


def _region_label(meta: dict, region_key: str) -> str:
    sgg = sgg_name(meta.get("sgg_cd") or region_key.split("|")[0])
    umd = meta.get("umd_nm") or region_key.split("|")[-1]
    return f"{sgg} {umd}".strip()


def _quiet(first_run: datetime, as_of: datetime, days: int) -> bool:
    return as_of < first_run + timedelta(days=days)


def _recent_bases(conn: psycopg.Connection, kind: str, days: int) -> set[str]:
    """최근 N일 안에 만든 같은 종류 카드의 dedupe 베이스(날짜 뺀 부분). 열려 있든 처리/무시/만료됐든 다시 만들지 않는다."""
    rows = conn.execute(
        "select dedupe_key from issues where kind = %s and created_at > %s",
        (kind, datetime.now(timezone.utc) - timedelta(days=days)),
    ).fetchall()
    return {r[0].rsplit(":", 1)[0] for r in rows}


def _pct(n: int, prev: int) -> float | None:
    return None if prev == 0 else round((n - prev) * 100.0 / prev, 1)


def _split_active(active: list[str]) -> list[tuple[list[str], str]]:
    """활성 시군구를 (서울, 경기 타겟) 두 묶음으로 — 리더보드를 따로 뽑아 섞는다."""
    seoul = [c for c in active if c in SEOUL_SGG]
    gg = [c for c in active if c not in SEOUL_SGG]
    out = [(seoul, "seoul")]
    if gg:
        out.append((gg, "gg"))
    return out


_CURRENT: dict[str, set[str]] = {}   # 이번 실행의 리더보드 (refresh 때 밀려난 카드 정리용)


def _drop_stale_leaderboard(conn: psycopg.Connection, s: Settings, kind: str, active: list[str]) -> int:
    """새 순위에서 밀려난 열린 리더보드 카드를 만료 처리 (피드 새로고침). 사용자가 '내일 다시'로 미룬 건 건드리지 않는다."""
    keep = _CURRENT.get(kind)
    if keep is None:
        return 0
    rows = conn.execute(
        "select id, dedupe_key from issues where kind = %s and status = 'open' "
        "and (snoozed_until is null or snoozed_until <= now())", (kind,),
    ).fetchall()
    drop = [r[0] for r in rows if r[1].rsplit(":", 1)[0] not in keep]
    if drop:
        conn.execute("update issues set status = 'expired', expires_at = now() where id = any(%s)", (drop,))
    return len(drop)


def build_tx_14d(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime, active: list[str] | None = None) -> list[dict]:
    """계약일 기준 최근 2주 계약이 많은 동네 (활성 시군구만). first_seen_at에 기대지 않아 수집 첫날부터 쓸 수 있다."""
    active = active or list(SEOUL_SGG.keys())
    rows: list[tuple] = []
    for codes, area in _split_active(active):
        rows += conn.execute(
            """
            with cur as (
              select region_key, max(sgg_cd) sgg_cd, max(umd_nm) umd_nm, count(*) n, round(avg(deal_amount)) avg_amt,
                     count(*) filter (where prop_type = 'rh') n_rh
              from rt_trades where not cancel_flag and deal_date >= current_date - 14 and sgg_cd = any(%s)
              group by region_key
            ),
            prev as (
              select region_key, count(*) n from rt_trades
              where not cancel_flag and deal_date >= current_date - 28 and deal_date < current_date - 14 and sgg_cd = any(%s)
              group by region_key
            )
            select c.region_key, c.sgg_cd, c.umd_nm, c.n, coalesce(p.n, 0), c.avg_amt, c.n_rh
            from cur c left join prev p using (region_key)
            where c.n >= %s
            order by c.n desc limit %s
            """,
            (codes, codes, s.tx_spike_min, s.recent_top_dongs if area == "seoul" else s.recent_top_dongs_gg),
        ).fetchall()
    _CURRENT["tx_14d"] = {f"tx_14d:{r[0]}" for r in rows}
    seen = _recent_bases(conn, "tx_14d", s.recent_repeat_days)
    expires = as_of + timedelta(days=s.recent_ttl_days)
    out = []
    for region_key, sgg_cd, umd_nm, n, pn, avg_amt, n_rh in rows:
        base = f"tx_14d:{region_key}"
        n_rh = int(n_rh or 0)
        if base in seen:
            continue
        n, pn = int(n), int(pn)
        pct = _pct(n, pn)
        label = _region_label({"sgg_cd": sgg_cd, "umd_nm": umd_nm}, region_key)
        out.append({
            "kind": "tx_14d",
            "headline": f"{label} 최근 2주 계약 {n}건{f' (빌라 {n_rh})' if n_rh else ''} · 직전 2주 {pn}건",
            "evidence": {"metric": "tx_14d", "key": region_key, "value": n, "prev_value": pn, "pct_change": pct, "rh_value": n_rh,
                         "avg_amount_manwon": int(avg_amt) if avg_amt is not None else None,
                         "basis": "deal_date_14d", "period": as_of.date().isoformat()},
            "region_key": region_key,
            "related_lead_ids": related_leads(conn, region_key, s.related_leads_max),
            "actions": DEFAULT_ACTIONS,
            "score": round(n * (1 + max(pct or 0, 0) / 100), 1),
            "dedupe_key": f"{base}:{as_of.date().isoformat()}",
            "expires_at": expires,
        })
    return out


def build_complex_14d(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime, active: list[str] | None = None) -> list[dict]:
    """계약일 기준 최근 2주 계약이 많은 단지 (아파트·오피스텔, 활성 시군구만). 빌라 비중은 중개사 채점(rank)에서 반영."""
    active = active or list(SEOUL_SGG.keys())
    rows: list[tuple] = []
    for codes, area in _split_active(active):
        rows += conn.execute(
            """
            select region_key, name, max(sgg_cd), max(umd_nm), count(*) n, round(avg(deal_amount)) avg_amt
            from rt_trades
            where not cancel_flag and prop_type in ('apt','offi') and name is not null and deal_date >= current_date - 14
              and sgg_cd = any(%s)
            group by region_key, name
            having count(*) >= %s
            order by count(*) desc limit %s
            """,
            (codes, s.recent_complex_min, s.recent_top_complexes if area == "seoul" else s.recent_top_complexes_gg),
        ).fetchall()
    _CURRENT["complex_14d"] = {f"complex_14d:{r[0]}|{r[1]}" for r in rows}
    seen = _recent_bases(conn, "complex_14d", s.recent_repeat_days)
    expires = as_of + timedelta(days=s.recent_ttl_days)
    out = []
    for region_key, name, sgg_cd, umd_nm, n, avg_amt in rows:
        base = f"complex_14d:{region_key}|{name}"
        if base in seen:
            continue
        n = int(n)
        label = _region_label({"sgg_cd": sgg_cd, "umd_nm": umd_nm}, region_key)
        avg_txt = f", 평균 {avg_amt / 10000:.1f}억" if avg_amt else ""
        out.append({
            "kind": "complex_14d",
            "headline": f"{label} {name} 최근 2주 계약 {n}건{avg_txt}",
            "evidence": {"metric": "complex_14d", "key": f"{region_key}|{name}", "name": name, "value": n,
                         "avg_amount_manwon": int(avg_amt) if avg_amt is not None else None,
                         "basis": "deal_date_14d", "period": as_of.date().isoformat()},
            "region_key": region_key,
            "related_lead_ids": related_leads(conn, region_key, s.related_leads_max),
            "actions": DEFAULT_ACTIONS,
            "score": float(n) * 2,
            "dedupe_key": f"{base}:{as_of.date().isoformat()}",
            "expires_at": expires,
        })
    return out


def build_tx_spike(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime, active: list[str] | None = None) -> list[dict]:
    rows = conn.execute(
        "select key, value, prev_value, pct_change, meta from metrics "
        "where kind = 'tx_new7d' and period = %s and value >= %s and split_part(key, '|', 1) = any(%s)",
        (as_of.date(), s.tx_spike_min, active or list(SEOUL_SGG.keys())),
    ).fetchall()
    out = []
    for key, value, prev, pct, meta in rows:
        value = int(value)
        if not (pct is None or pct >= s.tx_spike_pct):
            continue
        meta = meta or {}
        label = _region_label(meta, key)
        out.append({
            "kind": "tx_spike",
            "headline": f"{label} 이번 주 공개 계약 {value}건 ({_fmt_pct(pct)})",
            "evidence": {"metric": "tx_new7d", "key": key, "value": value, "prev_value": prev, "pct_change": pct,
                         "period": as_of.date().isoformat()},
            "region_key": key,
            "related_lead_ids": related_leads(conn, key, s.related_leads_max),
            "actions": DEFAULT_ACTIONS,
            "score": round(value * (1 + (pct or 100) / 100), 1),
            "dedupe_key": f"tx_spike:{key}:{as_of.date().isoformat()}",
            "expires_at": expires,
        })
    # 하루 상한: 점수 높은 동네만 (10/30 수집 7일차에 기준치가 비어 218장이 한꺼번에 뜸)
    out.sort(key=lambda x: -x["score"])
    return out[: s.tx_spike_max]


def build_complex_cluster(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime, active: list[str] | None = None) -> list[dict]:
    rows = conn.execute(
        "select key, value, prev_value, pct_change, meta from metrics "
        "where kind = 'complex_new14d' and period = %s and value >= %s and split_part(key, '|', 1) = any(%s)",
        (as_of.date(), s.complex_min, active or list(SEOUL_SGG.keys())),
    ).fetchall()
    out = []
    for key, value, prev, pct, meta in rows:
        meta = meta or {}
        region_key = "|".join(key.split("|")[:2])
        name = meta.get("name") or key.split("|")[-1]
        label = _region_label(meta, region_key)
        avg = meta.get("avg_amount_manwon")
        avg_txt = f", 평균 {avg / 10000:.1f}억" if avg else ""
        out.append({
            "kind": "complex_cluster",
            "headline": f"{label} {name} 최근 2주 공개 계약 {int(value)}건{avg_txt}",
            "evidence": {"metric": "complex_new14d", "key": key, "value": int(value), "prev_value": prev,
                         "pct_change": pct, "period": as_of.date().isoformat()},
            "region_key": region_key,
            "related_lead_ids": related_leads(conn, region_key, s.related_leads_max),
            "actions": DEFAULT_ACTIONS,
            "score": float(value) * 2,
            "dedupe_key": f"complex_cluster:{key}:{as_of.date().isoformat()}",
            "expires_at": expires,
        })
    return out


def build_lead_reaction(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime) -> list[dict]:
    rows = conn.execute(
        """
        select e.lead_id, l.name, l.region_key, count(*) as n,
               jsonb_object_agg(e.type, c) as by_type
        from (
          select lead_id, type, count(*) over (partition by lead_id, type) as c
          from events
          where occurred_at >= %s - interval '24 hours' and type = any(%s) and lead_id is not null
        ) e
        join leads l on l.id = e.lead_id
        group by e.lead_id, l.name, l.region_key
        """,
        (as_of, list(REACTION_TYPES)),
    ).fetchall()
    out = []
    for lead_id, name, region_key, n, by_type in rows:
        by_type = by_type or {}
        if set(by_type) <= {"email_open"}:   # 메일 열람만 있는 건 카드로 안 띄움 (회신·신청·접속 등 행동이 있을 때만)
            continue
        # 같은 리드의 이전 반응 카드는 정리 (리드당 한 장)
        conn.execute(
            "update issues set status = 'expired', expires_at = now() where kind = 'lead_reaction' and status = 'open' "
            "and evidence->>'lead_id' = %s and dedupe_key <> %s",
            (str(lead_id), f"lead_reaction:{lead_id}:{as_of.date().isoformat()}"),
        )
        parts = ", ".join(f"{REACTION_LABEL.get(t, t)} {c}" for t, c in sorted(by_type.items(), key=lambda x: -x[1]))
        out.append({
            "kind": "lead_reaction",
            "headline": f"{name or '리드'} 반응 {int(n)}건 — {parts}",
            "evidence": {"source": "events", "lead_id": str(lead_id), "by_type": by_type, "window_hours": 24},
            "region_key": region_key,
            "related_lead_ids": [str(lead_id)],
            "actions": ["dm", "convert", "later", "ignore"],
            "score": (90 if "reply" in by_type else 50) + float(n) * 5,
            "dedupe_key": f"lead_reaction:{lead_id}:{as_of.date().isoformat()}",
            "expires_at": expires,
        })
    return out


def build_news(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime) -> list[dict]:
    rows = conn.execute(
        "select id, title, url, tags, region_key from news "
        "where created_at >= %s - interval '24 hours' and tags is not null and array_length(tags, 1) > 0",
        (as_of,),
    ).fetchall()
    out = []
    for nid, title, url, tags, region_key in rows:
        out.append({
            "kind": "news",
            "headline": f"기사: {title}",
            "evidence": {"source": "news", "news_id": nid, "url": url, "tags": tags},
            "region_key": region_key,
            "related_lead_ids": related_leads(conn, region_key, s.related_leads_max) if region_key else [],
            "actions": ["content", "dm", "later", "ignore"],
            "score": 40.0,
            "dedupe_key": f"news:{nid}",
            "expires_at": expires,
        })
    return out


def build_due(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime) -> list[dict]:
    rows = conn.execute(
        """
        select p.id, p.action, p.due_at, p.note, l.id, l.name, l.region_key
        from pipeline p left join leads l on l.id = p.lead_id
        where p.stage <> 'done' and p.due_at is not null and p.due_at <= %s
          and coalesce(p.action, '') <> 'sequence'   -- 자동 시퀀스 예약은 사람이 할 일이 아니라 피드에 안 띄움
        """,
        (as_of,),
    ).fetchall()
    out = []
    for pid, action, due_at, note, lead_id, name, region_key in rows:
        out.append({
            "kind": "due",
            "headline": f"오늘: {name or '항목'} — {action or ''} {note or ''}".strip(),
            "evidence": {"source": "pipeline", "pipeline_id": str(pid), "due_at": due_at.isoformat()},
            "region_key": region_key,
            "related_lead_ids": [str(lead_id)] if lead_id else [],
            "actions": ["convert", "dm", "later"],
            "score": 90.0,
            "dedupe_key": f"due:{pid}:{as_of.date().isoformat()}",
            "expires_at": expires,
        })
    return out


def build_coop_spike(conn: psycopg.Connection, s: Settings, as_of: datetime, expires: datetime) -> list[dict]:
    """경매 데이터가 metrics(kind='coop_auction', key=coop_id)로 들어오면 동작. 지금은 데이터 없음 → 빈 리스트."""
    rows = conn.execute(
        "select key, value, prev_value, pct_change, meta from metrics "
        "where kind = 'coop_auction' and period = %s and (pct_change >= 50 or value >= 5)",
        (as_of.date(),),
    ).fetchall()
    out = []
    for key, value, prev, pct, meta in rows:
        meta = meta or {}
        out.append({
            "kind": "coop_spike",
            "headline": f"{meta.get('name') or key} 경매 {int(value)}건 ({_fmt_pct(pct)})",
            "evidence": {"metric": "coop_auction", "key": key, "value": int(value), "prev_value": prev, "pct_change": pct},
            "region_key": meta.get("region_key"),
            "related_lead_ids": [],
            "actions": ["convert", "dm", "later", "ignore"],
            "score": float(value) * 3,
            "dedupe_key": f"coop_spike:{key}:{as_of.date().isoformat()}",
            "expires_at": expires,
        })
    return out


def run_issues(conn: psycopg.Connection, s: Settings, as_of: datetime | None = None, refresh: bool | None = None) -> dict:
    """refresh=True(또는 ISSUES_REFRESH=1): 리더보드 카드를 지금 순위로 맞춘다 — 밀려난 열린 카드는 만료."""
    if refresh is None:
        refresh = s.issues_refresh
    as_of = as_of or datetime.now(timezone.utc)
    expires = as_of + timedelta(hours=s.issue_ttl_hours)
    first_run = ensure_first_run(conn)

    stats: dict[str, int] = {}
    active = active_sgg(conn, s)   # 서울 + 경기 타겟 (매일 재선정, app_state 기록)
    builders = [
        ("tx_14d", partial(build_tx_14d, active=active), 0),
        ("complex_14d", partial(build_complex_14d, active=active), 0),
        ("tx_spike", partial(build_tx_spike, active=active), s.bootstrap_quiet_days),
        ("complex_cluster", partial(build_complex_cluster, active=active), s.bootstrap_quiet_days * 2),
        ("lead_reaction", build_lead_reaction, 0),
        ("news", build_news, 0),
        ("due", build_due, 0),
        ("coop_spike", build_coop_spike, 0),
    ]
    for kind, fn, quiet_days in builders:
        if quiet_days and _quiet(first_run, as_of, quiet_days):
            log.info("%s: 첫 수집 후 %d일 동안은 생성 안 함 (first_run=%s)", kind, quiet_days, first_run.date())
            stats[kind] = 0
            continue
        rows = fn(conn, s, as_of, expires)
        stats[kind] = insert_issues(conn, rows)
        if refresh and kind in ("tx_14d", "complex_14d"):
            stats[f"{kind}_dropped"] = _drop_stale_leaderboard(conn, s, kind, active)
    stats["expired"] = expire_issues(conn)
    conn.commit()
    log.info("이슈 %s", stats)
    return stats
