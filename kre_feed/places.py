"""제휴·금융 리드 수집: 카카오 로컬로 동네마다 세무사 / 법무사 / 2금융 점포를 찾아 leads 에 넣는다.

- regions: 법정동(region_key)마다 중심 좌표. 좌표는 그 동네 리드(중개사 등록부) 평균 → 없으면 카카오 주소 검색.
- 동네마다 SEARCHES 의 검색어를 반경 PLACES_RADIUS_M 안에서 최대 45개(3페이지)씩.
  제휴: "{동} 세무사", "{동} 법무사"
  금융: 새마을금고 · 신협 · 농협/축협(지역조합) · 수협(지역조합) · 저축은행 · 산림조합  ← 2금융권만.
        1금융(국민·신한·우리·하나·NH농협은행·기업·SC·씨티·인터넷은행·지방은행·우체국 등)은 버린다.
- 결과 → leads (source='kakao_local', external_id=카카오 place id). 이미 있으면 상호·주소·전화·좌표만 갱신 (status·email 보존).
- 검색 순위 → kakao_ranks (query_key='{type}:{region_key}'). 카카오 순위는 약한 신호로만 쓴다 (rank.py).
- 동네당 PLACES_REFRESH_DAYS 마다 다시 (한 주에 걸쳐 분산). 하루 예산 PLACES_DAILY_BUDGET 호출.
- SPEC_VERSION 이 바뀌면(검색어·분류 규칙 변경) 전 동네를 다시 돈다.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import psycopg
import requests
from psycopg.types.json import Jsonb

from .config import Settings
from .db import get_state, set_state
from .names import short_name
from .regions import ALL_SGG, GYEONGGI_SGG, SEOUL_SGG, region_label

log = logging.getLogger(__name__)

NAME2CODE = {v: k for k, v in ALL_SGG.items()}   # '송파구' / '성남시 분당구' / '광명시' → 코드
SPEC_VERSION = "2"   # 1: 세무사·법무사·은행(1금융 포함) / 2: 금융 = 2금융권만, 검색어 분리

EXCLUDE_NAME = ("ATM", "365", "자동화", "무인", "현금인출", "출장소")
# 1금융권·우체국·외국계·인터넷은행 — 상호에 이 말이 있으면 금융 리드에서 제외
FIRST_TIER = (
    "국민은행", "KB국민", "신한은행", "우리은행", "하나은행", "KEB", "외환은행",
    "농협은행", "NH농협은행", "NH은행", "기업은행", "IBK", "SC제일", "제일은행", "씨티은행", "한국씨티",
    "카카오뱅크", "케이뱅크", "토스뱅크", "산업은행", "KDB", "수출입은행", "수협은행", "SH수협은행",
    "부산은행", "BNK", "대구은행", "IM뱅크", "경남은행", "광주은행", "전북은행", "제주은행",
    "우체국", "한국은행", "도이치", "HSBC", "JP모간", "JP모건", "BNP", "미즈호", "MUFG", "SMBC", "중국은행", "공상은행",
    "건설은행", "교통은행", "농업은행", "국민은행365",
)


def finance_type(name: str | None, category: str | None) -> str | None:
    """2금융 점포 분류. 1금융·비금융이면 None."""
    n = (name or "").replace(" ", "").upper()
    cat = category or ""
    if not n or "금융" not in cat and "은행" not in cat:
        return None
    if any(x in n for x in EXCLUDE_NAME) or any(x.upper() in n for x in FIRST_TIER):
        return None
    sub = cat.split(">", 1)[1] if ">" in cat else cat      # 맨 앞 '금융,보험' 은 늘 있으니 그 뒤만 본다
    if any(x in sub for x in ("보험", "증권", "카드", "대부", "환전", "ATM")):
        return None
    if "새마을금고" in n or "새마을금고" in cat:
        return "mg"
    if "신협" in n or "신용협동조합" in n or "신협" in cat:
        return "cu"
    if "저축은행" in n or "저축은행" in cat:
        return "savings_bank"
    if "산림조합" in n:
        return "forest_coop"
    if "수협" in n:
        return "sh_coop"
    if "농협" in n or "축협" in n or "축산" in n or "원예" in n or "낙농" in n or "양돈" in n or "양계" in n or "인삼" in n:
        return "nh_coop"
    return None


def _fixed(ltype: str, must_any: tuple, exclude_cat: tuple):
    def f(name: str | None, category: str | None) -> str | None:
        cat = category or ""
        pn = (name or "").upper()
        if not any(m in cat for m in must_any):
            return None
        if any(x in cat for x in exclude_cat) or any(x in pn for x in EXCLUDE_NAME):
            return None
        return ltype
    return f


def _broker_type(name: str | None, category: str | None) -> str | None:
    cat = category or ""
    pn = (name or "").upper()
    if "중개" not in cat and "부동산" not in cat:
        return None
    if any(x in cat for x in ("분양", "모델하우스", "건설", "임대관리", "감정평가")) or any(x in pn for x in ("분양", "모델하우스")):
        return None
    return "broker"


# 검색 스펙: key(순위 query_key 접두) → 검색어, 카테고리 그룹, 분류 함수(상호, 카테고리 → type|None), lane
# 'broker'(카카오 중개업소 AG2)는 등록부가 없는 시군구(서울 밖)에서만 돈다.
SEARCHES: dict[str, dict] = {
    "broker":       {"query": "부동산",     "category": "AG2", "lane": "b2b2c", "classify": _broker_type, "only_outside_registry": True},
    "tax":          {"query": "세무사",     "category": None, "lane": "b2b2c", "classify": _fixed("tax", ("세무사", "세무법인", "세무,회계"), ("세무서", "행정기관", "회계사"))},
    "legal":        {"query": "법무사",     "category": None, "lane": "b2b2c", "classify": _fixed("legal", ("법무사",), ("법원", "법무부", "행정기관"))},
    "mg":           {"query": "새마을금고", "category": None, "lane": "bank",  "classify": finance_type},
    "cu":           {"query": "신협",       "category": None, "lane": "bank",  "classify": finance_type},
    "nh_coop":      {"query": "농협",       "category": None, "lane": "bank",  "classify": finance_type},
    "ax_coop":      {"query": "축협",       "category": None, "lane": "bank",  "classify": finance_type},
    "sh_coop":      {"query": "수협",       "category": None, "lane": "bank",  "classify": finance_type},
    "savings_bank": {"query": "저축은행",   "category": None, "lane": "bank",  "classify": finance_type},
}
FINANCE_TYPES = ("mg", "cu", "nh_coop", "sh_coop", "savings_bank", "forest_coop")
TYPE_LABEL = {"tax": "세무사", "legal": "법무사", "mg": "새마을금고", "cu": "신협", "nh_coop": "지역농협", "sh_coop": "지역수협",
              "savings_bank": "저축은행", "forest_coop": "산림조합"}


def accept(spec: dict, doc: dict) -> str | None:
    """이 검색 스펙으로 이 장소를 받을지 → type 또는 None"""
    return spec["classify"](doc.get("place_name"), doc.get("category_name"))


PAGE_SIZE = 15
MAX_PAGES = 3


def region_from_address(address_name: str | None) -> tuple[str, str, str] | None:
    """카카오 지번주소 → (region_key, sgg_cd, umd).
    '서울 송파구 가락동 98-1' → ('11710|가락동', …) / '경기 성남시 분당구 정자동 1' → ('41135|정자동', …) / '경기 광명시 철산동 1' → ('41210|철산동', …)
    서울·경기 밖이면 None."""
    toks = (address_name or "").split()
    if len(toks) < 3:
        return None
    if toks[0].startswith("서울"):
        code, rest = NAME2CODE.get(toks[1]), toks[2:]
    elif toks[0].startswith("경기"):
        code, rest = NAME2CODE.get(" ".join(toks[1:3])), toks[3:]      # 시 + 구
        if not code:
            code, rest = NAME2CODE.get(toks[1]), toks[2:]                # 구 없는 시·군
    else:
        return None
    if not code or not rest:
        return None
    umd = rest[0]
    if not (umd.endswith(("동", "가", "로", "읍", "면", "리")) and len(umd) >= 2):
        return None
    return f"{code}|{umd}", code, umd


def ensure_regions(conn: psycopg.Connection) -> int:
    """rt_trades · leads 에 나오는 법정동을 regions 에 등록."""
    cur = conn.execute(
        """
        insert into regions (region_key, sgg_cd, umd_nm)
        select region_key, max(sgg_cd), max(umd_nm) from rt_trades where region_key is not null group by region_key
        union
        select region_key, split_part(region_key, '|', 1), split_part(region_key, '|', 2)
        from leads where region_key like '%|%' group by region_key
        on conflict (region_key) do nothing
        """
    )
    conn.commit()
    return cur.rowcount


def geocode_regions(conn: psycopg.Connection, kakao, budget, active: list[str] | None = None) -> int:
    """동네 중심 좌표 (활성 시군구만). 1) 그 동네 리드 좌표 평균 (호출 없음) 2) 카카오 주소 검색."""
    rows = conn.execute(
        "select region_key, sgg_cd, umd_nm from regions where geocoded_at is null and (%(a)s::text[] is null or sgg_cd = any(%(a)s))",
        {"a": active},
    ).fetchall()
    n = 0
    for region_key, sgg_cd, umd_nm in rows:
        avg = conn.execute(
            "select avg(lat), avg(lng), count(*) from leads where region_key = %s and lat is not null and lat between 36.8 and 38.4 and lng between 126.3 and 128",
            (region_key,),
        ).fetchone()
        lat = lng = None
        src = None
        if avg and avg[2] and avg[2] >= 3:
            lat, lng, src = float(avg[0]), float(avg[1]), "leads_avg"
        elif kakao is not None and budget.take():
            try:
                docs = kakao.address(region_label(region_key, with_sido=True))
                if docs:
                    lat, lng, src = float(docs[0]["y"]), float(docs[0]["x"]), "kakao_address"
            except requests.RequestException as e:
                log.warning("동네 지오코딩 실패 %s: %s", region_key, e)
                continue
        if lat is None:
            continue
        conn.execute("update regions set lat=%s, lng=%s, geocode_src=%s, geocoded_at=now() where region_key=%s", (lat, lng, src, region_key))
        conn.commit()
        n += 1
    return n


def _priority_regions(conn: psycopg.Connection, refresh_days: int, active: list[str] | None = None) -> list[tuple[str, float, float]]:
    """수집 순서: 열린 이슈 동네 → 최근 60일 거래 많은 동네 → 나머지. 활성 시군구만. 최근 refresh_days 안에 한 곳은 제외."""
    return conn.execute(
        """
        select r.region_key, r.lat, r.lng
        from regions r
        left join (select region_key, count(*) n from rt_trades where deal_date >= current_date - 60 group by region_key) t using (region_key)
        where r.lat is not null and (r.places_at is null or r.places_at < now() - (%(days)s || ' days')::interval)
          and (%(active)s::text[] is null or split_part(r.region_key, '|', 1) = any(%(active)s))
        order by (exists (select 1 from issues i where i.region_key = r.region_key and i.status = 'open'
                          and (i.expires_at is null or i.expires_at > now()))) desc,
                 coalesce(t.n, 0) desc, r.region_key
        """,
        {"days": refresh_days, "active": active},
    ).fetchall()


def _lead_row(ltype: str, lane: str, doc: dict, region_key: str) -> tuple | None:
    name = (doc.get("place_name") or "").strip()
    ext = str(doc.get("id") or "").strip()
    if not name or not ext:
        return None
    contact = {k: v for k, v in {
        "phone": (doc.get("phone") or "").strip() or None,
        "address": (doc.get("road_address_name") or doc.get("address_name") or "").strip() or None,
        "url": doc.get("place_url") or None,
    }.items() if v}
    return (lane, ltype, name, region_key, float(doc["y"]), float(doc["x"]), Jsonb(contact), ext, short_name(name))


def _upsert_places(conn: psycopg.Connection, rows: list[tuple]) -> dict[str, str]:
    """한 번의 INSERT 로 여러 장소 upsert → {external_id: lead_id}. (DB가 멀어서 왕복을 아낀다)"""
    uniq: dict[str, tuple] = {}
    for r in rows:
        uniq.setdefault(r[7], r)          # 같은 place id 가 두 번 오면 한 번만 (ON CONFLICT 가 같은 행을 두 번 못 건드림)
    if not uniq:
        return {}
    values = ",".join(["(%s,%s,%s,%s,%s,%s,%s::jsonb,'new','kakao_local',%s,%s)"] * len(uniq))
    flat = [v for r in uniq.values() for v in r]
    cur = conn.execute(
        "insert into leads (lane, type, name, region_key, lat, lng, contact, status, source, external_id, short_name) values "
        + values
        + """ on conflict (source, external_id) where external_id is not null do update set
          name = excluded.name, region_key = excluded.region_key, lat = excluded.lat, lng = excluded.lng,
          contact = coalesce(leads.contact, '{}'::jsonb) || excluded.contact, updated_at = now()
        returning id, external_id""",
        flat,
    )
    return {ext: str(lid) for lid, ext in cur.fetchall()}


MAX_CONSECUTIVE_ERRORS = 5  # 연속 실패(쿼터 초과 등)면 이 날 수집을 멈춘다 — 빈손으로 '수집함' 표시가 나지 않게


def _search_pages(kakao, spec: dict, skey: str, region_key: str, umd: str, lat: float, lng: float, radius_m: int) -> dict:
    """한 검색어의 페이지들을 순서대로 호출 (스레드 하나). 결과 문서 목록과 호출 수·실패 여부."""
    docs_all: list[dict] = []
    pages = 0
    err = False
    first_failed = False
    for page in range(1, MAX_PAGES + 1):
        pages += 1
        try:
            docs = kakao.keyword(f"{umd} {spec['query']}" if not spec["category"] else spec["query"],
                                 x=float(lng), y=float(lat), radius=radius_m, category=spec["category"],
                                 page=page, size=PAGE_SIZE)
        except requests.RequestException as e:
            log.warning("장소 검색 실패 %s %s p%d: %s", region_key, skey, page, e)
            err = True
            first_failed = page == 1
            break
        docs_all.extend(docs)
        if len(docs) < PAGE_SIZE:
            break
    return {"skey": skey, "docs": docs_all, "pages": pages, "err": err, "first_failed": first_failed}


def collect_places(conn: psycopg.Connection, kakao, budget, refresh_days: int, radius_m: int, active: list[str] | None = None,
                   workers: int = 6) -> dict:
    """동네마다 검색어 9종을 스레드로 동시에 호출하고(API 왕복이 병목), DB 쓰기는 순서대로."""
    from concurrent.futures import ThreadPoolExecutor
    stats = {"regions_done": 0, "calls": 0, "errors": 0, "leads": 0, "by_type": {}, "aborted": False}
    consecutive = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for region_key, lat, lng in _priority_regions(conn, refresh_days, active):
            need = len(SEARCHES) * MAX_PAGES
            if budget.left < need or stats["aborted"]:
                break
            if active is not None and region_key.split("|")[0] not in active:
                continue
            umd = region_key.split("|")[-1]
            in_registry = region_key.split("|")[0] in SEOUL_SGG     # 서울은 중개사 등록부가 있다
            todo = [(k, sp) for k, sp in SEARCHES.items() if not (sp.get("only_outside_registry") and in_registry)]
            results = list(ex.map(lambda ks: _search_pages(kakao, ks[1], ks[0], region_key, umd, lat, lng, radius_m), todo))
            region_failed = False
            for res in results:
                stats["calls"] += res["pages"]
                budget.left -= res["pages"]
                if res["err"]:
                    stats["errors"] += 1
                    consecutive += 1
                    if res["first_failed"]:
                        region_failed = True
                else:
                    consecutive = 0
            if consecutive >= MAX_CONSECUTIVE_ERRORS:
                stats["aborted"] = True
                log.error("장소 검색 연속 %d회 실패 — 오늘 수집 중단", consecutive)
            for res in results:
                skey = res["skey"]
                spec = SEARCHES[skey]
                rows: list[tuple] = []          # 검색 순서대로 (= 카카오 순위)
                for d in res["docs"]:
                    ltype = accept(spec, d)
                    if not ltype:
                        continue
                    reg = region_from_address(d.get("address_name"))
                    if not reg or (active is not None and reg[1] not in active):   # 활성 시군구 밖 주소는 버린다
                        continue
                    row = _lead_row(ltype, spec["lane"], d, reg[0])
                    if row:
                        rows.append(row)
                ids = _upsert_places(conn, rows)
                for r in rows:
                    if r[7] in ids:
                        stats["by_type"][r[1]] = stats["by_type"].get(r[1], 0) + 1
                # 이 동네의 "{검색}" 순위 (첫 페이지부터 실패한 검색은 기존 순위 유지)
                if ids or not res["first_failed"]:
                    qkey = f"{skey}:{region_key}"
                    conn.execute("delete from kakao_ranks where query_key = %s", (qkey,))
                    seen: set[str] = set()
                    ranks = []
                    for r in rows:
                        lid = ids.get(r[7])
                        if not lid or lid in seen:
                            continue
                        seen.add(lid)
                        ranks.append((qkey, lid, len(seen)))
                    if ranks:
                        conn.execute(
                            "insert into kakao_ranks (query_key, lead_id, rank) values " + ",".join(["(%s,%s,%s)"] * len(ranks)) + " on conflict do nothing",
                            [v for r in ranks for v in r],
                        )
            if region_failed or stats["aborted"]:
                conn.commit()      # 건진 리드는 남기되, 이 동네는 다음 실행에 다시
                continue
            conn.execute("update regions set places_at = now() - (random() * interval '7 days') where region_key = %s", (region_key,))
            conn.commit()
            stats["regions_done"] += 1
            if stats["regions_done"] % 25 == 0:
                log.info("장소 수집 진행 %d동네, 호출 %d, 리드 %s", stats["regions_done"], stats["calls"], stats["by_type"])
    stats["leads"] = sum(stats["by_type"].values())
    return stats


def run_places(conn: psycopg.Connection, s: Settings, kakao=None, budget=None, active: list[str] | None = None) -> dict:
    """enrich 안에서 호출되거나 `python -m kre_feed places` 로 단독 실행. active=None 이면 서울+경기 타겟을 직접 계산."""
    from .enrich import Budget, Kakao  # 순환 import 방지
    from .targets import active_sgg
    if active is None:
        active = active_sgg(conn, s, persist=False)

    stats: dict = {"regions_new": ensure_regions(conn), "regions_geocoded": 0, "skipped": [], "respec": False}
    if get_state(conn, "places_spec") != SPEC_VERSION:
        # 규칙이 바뀌었다: 전 동네 다시 돌고, 지금 규칙으로 받지 않을 금융 리드(1금융 등)는 지운다 (이력 없는 것만)
        conn.execute("update regions set places_at = null")
        # 지울 대상: 옛 규칙으로 들어온 은행 리드 중 접촉·파이프라인·시퀀스 이력이 없는 것. 참조(issue_leads, 순위 테이블)부터 끊는다.
        conn.execute(
            """
            create temp table _victims on commit drop as
            select l.id from leads l where l.source = 'kakao_local' and l.type = 'bank_branch'
              and not exists (select 1 from events e where e.lead_id = l.id)
              and not exists (select 1 from pipeline p where p.lead_id = l.id)
              and not exists (select 1 from enrollments en where en.lead_id = l.id)
            """
        )
        for tbl in ("issue_leads", "kakao_ranks", "naver_ranks"):
            conn.execute(f"delete from {tbl} where lead_id in (select id from _victims)")
        cur = conn.execute("delete from leads where id in (select id from _victims)")
        stats["deleted"] = cur.rowcount
        conn.execute("delete from kakao_ranks where query_key like 'bank_branch:%'")
        set_state(conn, "places_spec", SPEC_VERSION)
        conn.commit()
        stats["respec"] = True
    if kakao is None:
        if not s.kakao_rest_key:
            stats["skipped"].append("kakao(KAKAO_REST_KEY 없음)")
            return stats
        kakao = Kakao(s.kakao_rest_key)
    budget = budget or Budget(s.places_daily_budget)
    stats["regions_geocoded"] = geocode_regions(conn, kakao, budget, active)
    stats.update(collect_places(conn, kakao, budget, s.places_refresh_days, s.places_radius_m, active))
    log.info("places %s", stats)
    return stats
