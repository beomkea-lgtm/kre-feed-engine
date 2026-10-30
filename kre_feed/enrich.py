"""외부 신호 수집 (매일 06시, 활성 이슈 동네·단지만).

1. complexes   : 실거래에 등장한 단지 → 카카오 키워드/주소 검색으로 좌표
2. kakao_ranks : "{단지명} 부동산" / "{동} 부동산" 카카오 검색 순위 → 전화·상호로 리드 매칭
3. blog_total  : 네이버 블로그 검색 결과 수 (리드별, 30일마다 갱신)

키가 없으면 해당 단계는 건너뛴다. 하루 호출 예산은 settings.
대상: 열린 이슈의 region_key + 최근 14일 계약 상위 동네 (이슈 없는 날 미리 채우기).
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import psycopg
import requests

from .config import Settings
from .names import phone_digits, short_name
from .regions import region_label
from .targets import active_sgg

log = logging.getLogger(__name__)

KAKAO_KEYWORD = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_ADDRESS = "https://dapi.kakao.com/v2/local/search/address.json"
KAKAO_WEB = "https://dapi.kakao.com/v2/search/web"                             # 다음 웹 검색 (블로그 노출 대체 신호)
NAVER_BLOG_LEGACY = "https://openapi.naver.com/v1/search/blog.json"            # 개발자센터 키 (2027-06 종료)
NAVER_BLOG_HUB = "https://naverapihub.apigw.ntruss.com/search/v1/blog"        # NAVER API HUB (네이버클라우드) 키
NAVER_LOCAL_LEGACY = "https://openapi.naver.com/v1/search/local.json"          # 지역 검색 (display 최대 5)
NAVER_LOCAL_HUB = "https://naverapihub.apigw.ntruss.com/search/v1/local"
NAVER_WEB_LEGACY = "https://openapi.naver.com/v1/search/webkr.json"            # 웹문서 검색
NAVER_WEB_HUB = "https://naverapihub.apigw.ntruss.com/search/v1/webkr"
NAVER_CAFE_LEGACY = "https://openapi.naver.com/v1/search/cafearticle.json"     # 카페글
NAVER_CAFE_HUB = "https://naverapihub.apigw.ntruss.com/search/v1/cafearticle"
NAVER_KIN_LEGACY = "https://openapi.naver.com/v1/search/kin.json"              # 지식iN
NAVER_KIN_HUB = "https://naverapihub.apigw.ntruss.com/search/v1/kin"


class Budget:
    def __init__(self, n: int):
        self.left = n

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _region_label(region_key: str) -> str:
    return region_label(region_key, with_sido=True)   # '서울 송파구 가락동' / '경기 성남시 분당구 정자동'


# ---------- 대상 선정 ----------

def target_regions(conn: psycopg.Connection, extra_top: int, active: list[str] | None = None) -> list[str]:
    """열린 이슈 동네 + 활성 시군구 안에서 최근 2주 거래 상위 동네."""
    rows = conn.execute(
        """
        select region_key from issues where status = 'open' and region_key is not null
          and (expires_at is null or expires_at > now())
          and (%(active)s::text[] is null or split_part(region_key, '|', 1) = any(%(active)s))
        union
        select region_key from (
          select region_key, count(*) n from rt_trades
          where not cancel_flag and deal_date >= current_date - 14
            and (%(active)s::text[] is null or sgg_cd = any(%(active)s))
          group by region_key
          order by n desc limit %(top)s
        ) t
        """,
        {"active": active, "top": extra_top},
    ).fetchall()
    return [r[0] for r in rows]


def ensure_complexes(conn: psycopg.Connection, regions: list[str]) -> int:
    """최근 28일 계약이 있는 단지를 complexes에 등록 (좌표는 아직)."""
    if not regions:
        return 0
    cur = conn.execute(
        """
        insert into complexes (region_key, name, prop_type, jibun)
        select region_key, name, max(prop_type), (array_agg(jibun) filter (where jibun is not null))[1]
        from rt_trades
        where not cancel_flag and prop_type in ('apt','offi') and name is not null
          and region_key = any(%s) and deal_date >= current_date - 28
        group by region_key, name
        on conflict (region_key, name) do nothing
        """,
        (regions,),
    )
    conn.commit()
    return cur.rowcount


# ---------- 카카오 ----------

class Kakao:
    def __init__(self, key: str, timeout: float = 10):
        self.h = {"Authorization": f"KakaoAK {key}"}
        self.timeout = timeout
        self.s = requests.Session()

    def keyword(self, query: str, *, x=None, y=None, radius=None, category=None, page=1, size=15) -> list[dict]:
        p = {"query": query, "page": page, "size": size, "sort": "accuracy"}
        if x is not None and y is not None:
            p.update({"x": x, "y": y})
            if radius:
                p["radius"] = radius
        if category:
            p["category_group_code"] = category
        r = self.s.get(KAKAO_KEYWORD, headers=self.h, params=p, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("documents", [])

    def address(self, query: str) -> list[dict]:
        r = self.s.get(KAKAO_ADDRESS, headers=self.h, params={"query": query, "size": 5}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("documents", [])

    def web_total(self, query: str) -> int:
        r = self.s.get(KAKAO_WEB, headers=self.h, params={"query": query, "size": 1}, timeout=self.timeout)
        r.raise_for_status()
        return int((r.json().get("meta") or {}).get("total_count", 0))


def geocode_complexes(conn: psycopg.Connection, kakao: Kakao, regions: list[str], budget: Budget) -> int:
    rows = conn.execute(
        "select id, region_key, name, jibun from complexes where geocoded_at is null and region_key = any(%s) limit 200",
        (regions,),
    ).fetchall()
    n = 0
    for cid, region_key, name, jibun in rows:
        if not budget.take():
            break
        label = _region_label(region_key)
        lat = lng = None
        src = "none"
        try:
            docs = kakao.keyword(f"{label} {name}", size=5)
            apt = [d for d in docs if "아파트" in (d.get("category_name") or "") or name.replace(" ", "") in (d.get("place_name") or "").replace(" ", "")]
            pick = (apt or docs)[:1]
            if pick:
                lat, lng, src = float(pick[0]["y"]), float(pick[0]["x"]), "kakao_keyword"
            elif jibun and budget.take():
                docs = kakao.address(f"{label} {jibun}")
                if docs:
                    lat, lng, src = float(docs[0]["y"]), float(docs[0]["x"]), "kakao_address"
        except requests.RequestException as e:
            log.warning("지오코딩 실패 %s %s: %s", label, name, e)
            continue
        conn.execute("update complexes set lat=%s, lng=%s, geocode_src=%s, geocoded_at=now() where id=%s", (lat, lng, src, cid))
        conn.commit()
        n += 1
        time.sleep(0.05)
    return n


class LeadIndex:
    """리드 전체를 한 번 메모리에 올려 전화 끝 8자리 → 리드, (region_key, 상호 정규화) → 리드로 매칭."""

    def __init__(self, conn: psycopg.Connection):
        self.by_phone: dict[str, str] = {}
        self.by_region: dict[str, list[tuple[str, str, float]]] = {}
        rows = conn.execute(
            "select id, region_key, name, contact->>'phone', coalesce(score, 0) from leads where type = 'broker' and status <> 'rejected'"
        ).fetchall()
        for lid, rk, name, phone, score in rows:
            lid = str(lid)
            pd = phone_digits(phone)
            if len(pd) >= 8:
                self.by_phone.setdefault(pd[-8:], lid)
            if rk and name:
                self.by_region.setdefault(rk, []).append(((name or "").replace(" ", ""), lid, float(score)))
        for v in self.by_region.values():
            v.sort(key=lambda t: -t[2])

    def match(self, region_key: str, place_name: str, phone: str) -> str | None:
        pd = phone_digits(phone)
        if len(pd) >= 8 and pd[-8:] in self.by_phone:
            return self.by_phone[pd[-8:]]
        sn = short_name(place_name)
        if len(sn) >= 2:
            for nm, lid, _ in self.by_region.get(region_key, []):
                if sn in nm:
                    return lid
        return None


def kakao_ranks(conn: psycopg.Connection, kakao: Kakao, regions: list[str], budget: Budget, refresh_days: int) -> int:
    """단지별 '{단지명} 부동산' + 동별 '{동} 부동산' 검색 → 상위 45 → 리드 매칭 저장."""
    stale = datetime.now(timezone.utc) - timedelta(days=refresh_days)
    targets: list[tuple[str, str, float | None, float | None]] = []
    for region_key, name, lat, lng, at in conn.execute(
        "select region_key, name, lat, lng, kakao_rank_at from complexes where region_key = any(%s) and geocoded_at is not null",
        (regions,),
    ).fetchall():
        if at is None or at < stale:
            targets.append((f"complex:{region_key}|{name}", f"{name} 부동산", lat, lng))
    for region_key in regions:
        last = conn.execute("select max(collected_at) from kakao_ranks where query_key = %s", (f"dong:{region_key}",)).fetchone()[0]
        if last is None or last < stale:
            targets.append((f"dong:{region_key}", f"{_region_label(region_key)} 부동산", None, None))

    index = LeadIndex(conn)
    done = 0
    for qkey, query, lat, lng in targets:
        docs: list[dict] = []
        try:
            for page in (1, 2, 3):
                if not budget.take():
                    break
                part = kakao.keyword(query, x=lng, y=lat, radius=1500 if lat else None, category="AG2", page=page, size=15)
                docs.extend(part)
                if len(part) < 15:
                    break
        except requests.RequestException as e:
            log.warning("카카오 검색 실패 %s: %s", query, e)
            continue
        if not docs:
            continue
        parts = qkey.split(":", 1)[1].split("|")
        region_key = "|".join(parts[:2])          # sgg|umd  (단지명에도 '|'가 없다고 가정)
        conn.execute("delete from kakao_ranks where query_key = %s", (qkey,))
        for i, d in enumerate(docs, 1):
            lead_id = index.match(region_key, d.get("place_name") or "", d.get("phone") or "")
            conn.execute(
                "insert into kakao_ranks (query_key, lead_id, rank, place_name, phone) values (%s,%s,%s,%s,%s) on conflict (query_key, rank) do nothing",
                (qkey, lead_id, i, d.get("place_name"), d.get("phone")),
            )
        if qkey.startswith("complex:"):
            nm = "|".join(parts[2:])
            conn.execute("update complexes set kakao_rank_at = now() where region_key=%s and name=%s", (region_key, nm))
        conn.commit()
        done += 1
        time.sleep(0.05)
    return done


# ---------- 네이버 블로그 ----------

class Naver:
    """NAVER API HUB(기본) 또는 개발자센터 레거시 키. hub=True면 ntruss 엔드포인트·헤더."""

    def __init__(self, cid: str, secret: str, timeout: float = 10, hub: bool = True):
        self.hub = hub
        if hub:
            self.url, self.local_url, self.web_url = NAVER_BLOG_HUB, NAVER_LOCAL_HUB, NAVER_WEB_HUB
            self.cafe_url, self.kin_url = NAVER_CAFE_HUB, NAVER_KIN_HUB
            self.h = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret}
        else:
            self.url, self.local_url, self.web_url = NAVER_BLOG_LEGACY, NAVER_LOCAL_LEGACY, NAVER_WEB_LEGACY
            self.cafe_url, self.kin_url = NAVER_CAFE_LEGACY, NAVER_KIN_LEGACY
            self.h = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
        self.timeout = timeout
        self.s = requests.Session()

    def blog_total(self, query: str) -> int:
        params = {"query": query, "display": 1}
        if self.hub:
            params["format"] = "json"
        r = self.s.get(self.url, headers=self.h, params=params, timeout=self.timeout)
        r.raise_for_status()
        return int(r.json().get("total", 0))

    def _search(self, url: str, query: str, display: int) -> list[dict]:
        params = {"query": query, "display": display, "sort": "sim"}
        if self.hub:
            params["format"] = "json"
        r = self.s.get(url, headers=self.h, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("items", [])

    def blog(self, query: str, display: int = 5) -> list[dict]:
        """블로그 글: title(<b>), link, description, bloggername, bloggerlink, postdate(YYYYMMDD)"""
        return self._search(self.url, query, display)

    def cafe(self, query: str, display: int = 5) -> list[dict]:
        """카페글: title, link, description, cafename"""
        return self._search(self.cafe_url, query, display)

    def kin(self, query: str, display: int = 5) -> list[dict]:
        """지식iN: title, link, description"""
        return self._search(self.kin_url, query, display)

    def web(self, query: str, display: int = 5) -> list[dict]:
        """웹문서: title(<b>), link, description"""
        return self._search(self.web_url, query, display)

    def local(self, query: str, display: int = 5) -> list[dict]:
        """네이버 지역 검색 상위 (최대 5). items: title(<b> 포함), telephone, address, roadAddress, link, category"""
        params = {"query": query, "display": min(display, 5), "sort": "random"}
        if self.hub:
            params["format"] = "json"
        r = self.s.get(self.local_url, headers=self.h, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("items", [])


_TAG = re.compile(r"<[^>]+>")


def _clean_title(s: str | None) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


class AllLeadIndex:
    """모든 유형 리드: 전화 끝 8자리 → id, (region_key) → [(상호 공백제거, 핵심어, id, type)]"""

    def __init__(self, conn: psycopg.Connection):
        self.by_phone: dict[str, str] = {}
        self.by_region: dict[str, list[tuple[str, str, str, str]]] = {}
        for lid, rk, name, phone, ltype in conn.execute(
            "select id, region_key, name, contact->>'phone', type from leads where status <> 'rejected'"
        ).fetchall():
            lid = str(lid)
            pd = phone_digits(phone)
            if len(pd) >= 8:
                self.by_phone.setdefault(pd[-8:], lid)
            if rk and name:
                self.by_region.setdefault(rk, []).append(((name or "").replace(" ", ""), short_name(name), lid, ltype))

    def match(self, region_key: str | None, title: str, phone: str, types: tuple[str, ...] | None = None) -> str | None:
        pd = phone_digits(phone)
        if len(pd) >= 8 and pd[-8:] in self.by_phone:
            return self.by_phone[pd[-8:]]
        if not region_key:
            return None
        t_full = title.replace(" ", "")
        t_core = short_name(title)
        for nm, core, lid, ltype in self.by_region.get(region_key, []):
            if types and ltype not in types:
                continue
            if nm == t_full or (len(t_core) >= 2 and t_core in nm) or (len(core) >= 2 and core in t_full):
                return lid
        return None


class NaverPool:
    """네이버 키 여러 개를 순서대로: 앞 키가 하루 한도(429)면 다음 키로. 모두 차면 마지막 오류를 그대로 올린다."""

    def __init__(self, clients: list):
        self.clients = clients
        self.i = 0
        self.url = clients[0].url if clients else ""

    def _call(self, name: str, *a, **kw):
        last = None
        while self.i < len(self.clients):
            try:
                return getattr(self.clients[self.i], name)(*a, **kw)
            except requests.RequestException as e:
                r = getattr(e, "response", None)
                if r is not None and (r.status_code == 429 or (r.status_code in (400, 403) and any(w in (r.text or "").lower() for w in ("quota", "limit", "exceed")))):
                    log.warning("네이버 키 %d 한도 → 다음 키", self.i + 1)
                    self.i += 1
                    last = e
                    continue
                raise
        raise last or requests.HTTPError("no naver keys")

    def blog(self, *a, **kw): return self._call("blog", *a, **kw)
    def web(self, *a, **kw): return self._call("web", *a, **kw)
    def cafe(self, *a, **kw): return self._call("cafe", *a, **kw)
    def kin(self, *a, **kw): return self._call("kin", *a, **kw)
    def local(self, *a, **kw): return self._call("local", *a, **kw)
    def blog_total(self, *a, **kw): return self._call("blog_total", *a, **kw)


def make_naver(s: Settings):
    """설정된 네이버 키들(기본 + NAVER_CLIENT_ID_2 …)로 풀. 키가 없으면 None."""
    import os
    keys = [(s.naver_client_id, s.naver_client_secret, s.naver_hub)]
    for n in range(2, 6):
        cid, sec = os.environ.get(f"NAVER_CLIENT_ID_{n}", ""), os.environ.get(f"NAVER_CLIENT_SECRET_{n}", "")
        if cid and sec:
            hub = os.environ.get(f"NAVER_HUB_{n}", "false").lower() not in ("0", "false", "no")
            keys.append((cid, sec, hub))
    clients = [Naver(c, k, hub=h) for c, k, h in keys if c and k]
    if not clients:
        return None
    return clients[0] if len(clients) == 1 else NaverPool(clients)


def _quota_hit(e: Exception) -> bool:
    """429 또는 한도 초과 문구 → 오늘은 그만."""
    r = getattr(e, "response", None)
    if r is None:
        return False
    body = (r.text or "").lower()
    return r.status_code == 429 or (r.status_code in (400, 403) and any(w in body for w in ("quota", "limit", "exceed")))


def naver_local_ranks(conn: psycopg.Connection, naver, regions: list[str], budget: Budget, refresh_days: int) -> int:
    """네이버 지역검색 상위 5 → naver_ranks. 단지 '{단지명} 부동산', 동 '{동} 부동산', '{동} 세무사', '{동} 법무사'."""
    from .places import region_from_address  # 주소 → region_key
    if not hasattr(naver, "local"):
        return 0
    stale = datetime.now(timezone.utc) - timedelta(days=refresh_days)
    last_by_key = dict(conn.execute("select query_key, max(collected_at) from naver_ranks group by query_key").fetchall())

    targets: list[tuple[str, str, tuple[str, ...]]] = []   # (query_key, 검색어, 매칭 허용 type)
    for region_key, name in conn.execute(
        "select region_key, name from complexes where region_key = any(%s) order by region_key, name", (regions,)
    ).fetchall():
        targets.append((f"complex:{region_key}|{name}", f"{name} 부동산", ("broker",)))
    # 빌라 거래가 15% 넘는 동네: "{동} 빌라 부동산" 순위도 (중개사 채점의 빌라 신호)
    villa_regions = {r[0] for r in conn.execute(
        "select region_key from rt_trades where region_key = any(%s) and not cancel_flag and deal_date >= current_date - 60 "
        "group by region_key having count(*) filter (where prop_type = 'rh') >= greatest(3, 0.15 * count(*))", (regions,),
    ).fetchall()}
    for region_key in regions:
        umd = region_key.split("|")[-1]
        targets.append((f"dong:{region_key}", f"{_region_label(region_key)} 부동산", ("broker",)))
        if region_key in villa_regions:
            targets.append((f"villa:{region_key}", f"{_region_label(region_key)} 빌라 부동산", ("broker",)))
        targets.append((f"tax:{region_key}", f"{umd} 세무사", ("tax",)))
        targets.append((f"legal:{region_key}", f"{umd} 법무사", ("legal",)))
    targets = [tg for tg in targets if (last_by_key.get(tg[0]) is None or last_by_key[tg[0]] < stale)]

    index = AllLeadIndex(conn)
    log.info("네이버 지역검색 대상 %d건 (예산 %d)", len(targets), budget.left)
    done = 0
    for qkey, query, types in targets:
        if not budget.take():
            break
        if done and done % 100 == 0:
            log.info("네이버 지역검색 진행 %d/%d", done, len(targets))
        try:
            items = naver.local(query)
            fails = 0
        except requests.RequestException as e:
            log.warning("네이버 지역검색 실패 %s: %s", query, e)
            fails = locals().get("fails", 0) + 1
            if _quota_hit(e) or fails >= 10:
                log.warning("네이버 지역검색 한도/연속 실패 — 남은 %d건은 다음 실행으로 (호출 낭비 방지)", len(targets) - done)
                break
            continue
        parts = qkey.split(":", 1)[1].split("|")
        q_region = "|".join(parts[:2])
        rows: list[tuple] = []
        for i, it in enumerate(items, 1):
            title = _clean_title(it.get("title"))
            reg = region_from_address(it.get("address") or it.get("roadAddress"))
            region_key = reg[0] if reg else q_region
            lead_id = index.match(region_key, title, it.get("telephone") or "", types)
            if lead_id is None and region_key != q_region:
                lead_id = index.match(q_region, title, "", types)
            rows.append((qkey, lead_id, i, title, it.get("telephone"), (it.get("link") or "")[:500] or None))
        if not rows:    # 결과 없음도 '확인함'으로 남긴다 (rank 0 자리)
            rows.append((qkey, None, 0, None, None, None))
        # 왕복 2번 (DB가 멀다): 지우고, 한 번에 넣는다
        conn.execute("delete from naver_ranks where query_key = %s", (qkey,))
        conn.execute(
            "insert into naver_ranks (query_key, lead_id, rank, place_name, phone, link) values "
            + ",".join(["(%s,%s,%s,%s,%s,%s)"] * len(rows)) + " on conflict (query_key, rank) do nothing",
            [v for r in rows for v in r],
        )
        conn.commit()
        done += 1
        time.sleep(0.05)
    return done


def blog_totals(conn: psycopg.Connection, naver, regions: list[str], budget: Budget, refresh_days: int, src: str = "naver") -> int:
    """naver: .blog_total(query) 또는 .web_total(query)를 가진 객체. src 는 leads.blog_src 에 기록."""
    search = naver.blog_total if hasattr(naver, "blog_total") else naver.web_total
    conn.execute("update leads set short_name = '' where short_name is null and type <> 'broker'")
    rows = conn.execute(
        """
        select id, name, region_key from leads
        where type = 'broker' and region_key = any(%s) and status <> 'rejected'
          and (blog_checked_at is null or blog_checked_at < now() - (%s || ' days')::interval
               or (blog_src is distinct from %s and %s = 'naver'))
        order by score desc nulls last, blog_checked_at nulls first
        limit 2000
        """,
        (regions, refresh_days, src, src),
    ).fetchall()
    n = 0
    log.info("노출 검색(%s) 대상 %d건 (예산 %d)", src, len(rows), budget.left)
    for lead_id, name, region_key in rows:
        if n and n % 200 == 0:
            log.info("노출 검색(%s) 진행 %d/%d", src, n, len(rows))
        sn = short_name(name)
        if not sn:
            conn.execute("update leads set short_name = '', blog_total = null, blog_checked_at = now() where id = %s", (lead_id,))
            continue
        if not budget.take():
            break
        umd = region_key.split("|")[-1] if region_key else ""
        try:
            total = search(f"{sn} {umd} 부동산".strip())
            fails = 0
        except requests.RequestException as e:
            log.warning("노출 검색 실패(%s) %s: %s", src, sn, e)
            fails = locals().get("fails", 0) + 1
            if _quota_hit(e) or fails >= 10:
                log.warning("노출 검색(%s) 한도/연속 실패 — 나머지는 다음 실행으로 (호출 낭비 방지)", src)
                break
            continue
        conn.execute("update leads set short_name = %s, blog_total = %s, blog_src = %s, blog_checked_at = now() where id = %s", (sn, total, src, lead_id))
        n += 1
        if n % 50 == 0:
            conn.commit()
        time.sleep(0.03)
    conn.commit()
    return n


# ---------- 실행 ----------

def run_enrich(conn: psycopg.Connection, s: Settings) -> dict:
    active = active_sgg(conn, s, persist=False)
    regions = target_regions(conn, s.enrich_extra_regions, active)
    stats: dict = {"regions": len(regions), "complexes_new": ensure_complexes(conn, regions),
                   "geocoded": 0, "kakao_queries": 0, "blog_checked": 0, "skipped": []}
    if not regions:
        return stats

    if s.kakao_rest_key:
        kakao = Kakao(s.kakao_rest_key)
        budget = Budget(s.kakao_daily_budget)
        stats["geocoded"] = geocode_complexes(conn, kakao, regions, budget)
        stats["kakao_queries"] = kakao_ranks(conn, kakao, regions, budget, s.enrich_refresh_days)
        # 제휴·금융 리드 (세무사·법무사·은행 지점) — 별도 예산
        from .places import run_places
        stats["places"] = run_places(conn, s, kakao=kakao, budget=Budget(s.places_daily_budget), active=active)
    else:
        stats["skipped"].append("kakao(KAKAO_REST_KEY 없음)")

    if s.naver_client_id and s.naver_client_secret:
        naver = make_naver(s)
        stats["naver_local"] = naver_local_ranks(conn, naver, regions, Budget(s.naver_local_budget), s.enrich_refresh_days)
        stats["blog_checked"] = blog_totals(conn, naver, regions, Budget(s.naver_daily_budget), s.enrich_refresh_days, src="naver")
        from .emails import run_emails
        stats["emails"] = run_emails(conn, s, naver, active)
    elif s.kakao_rest_key:
        # 네이버 키 없으면 다음 웹 검색으로 대체 (같은 카카오 키). 네이버 키가 들어오면 naver 로 다시 채운다.
        stats["blog_checked"] = blog_totals(conn, Kakao(s.kakao_rest_key), regions, Budget(min(s.kakao_daily_budget // 2, 600)),
                                            s.enrich_refresh_days, src="daum_web")
        stats["skipped"].append("naver(키 없음 → 다음 웹검색 대체)")
    else:
        stats["skipped"].append("naver(NAVER_CLIENT_ID/SECRET 없음)")

    log.info("enrich %s", stats)
    return stats
