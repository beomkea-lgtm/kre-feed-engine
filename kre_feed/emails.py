"""제안·문의 받으려고 공개한 메일 찾기 (비용 0: 네이버 검색 API + 페이지 읽기).

원칙 (EMAIL_MODE=proposal, 기본)
  - 메일 앞뒤 80자 안에 제안·문의 맥락어(협업·제휴·제안·문의·상담·연락·광고·의뢰·컨택·contact·business…)가 있거나,
    우리 DB 전화번호가 메일 근처(150자)에 있어야 한다. 그냥 본문에 굴러다니는 메일은 안 집는다.
  - 페이지에 '이메일 무단수집 거부' 고지가 있으면 그 페이지는 통째로 건너뛴다.
  - 출처 URL·점수·근거를 남긴다. 거절(rejected)된 메일은 다시 안 붙인다.

병렬 경로 (한 실행 안에서 스레드로 동시에)
  A. 리드별  1) 네이버 지역검색이 준 홈페이지/블로그 링크 + 그 홈페이지 안 '문의·제휴·회사소개' 링크 2개
             2) 블로그 검색 "{상호} {동}", "{상호} 협업 문의"  — 상세글의 협업·제휴 안내
             3) 블로그 프로필(소개) — 작성자 이름에 상호가 들어간 블로그
             4) 웹문서 검색 "{상호} {동} {업종}", "{상호} 이메일"
             5) 카페글·지식iN 검색 "{상호} {동}" (스니펫 + 열리는 페이지)
             6) 카카오로 들어온 리드는 카카오 장소 상세의 홈페이지 링크
             7) 2금융 지점은 본점(금고·조합) 홈페이지 — 같은 조합 지점끼리 한 번만
             8) 위에서 메일을 못 찾았는데 자체 도메인 홈페이지가 있으면 info@도메인 을 메일 서버(MX) 확인 후 후보로만
  B. 동네 역방향  "{동} {업종} 문의 메일" 블로그·웹 검색 → 페이지의 (메일, 근처 전화) → 전화로 우리 리드에 매칭.
             한 번 검색으로 여러 리드를 잡는다.

근거 점수 (자동 채택: 합 ≥ EMAIL_MIN_AUTO(60) 이고 서로 다른 근거 ≥ 2 / 후보: EMAIL_MIN_CANDIDATE(40)~)
  phone +60 전화 일치 · title +25 제목에 상호(본문만 body +15) · region +10 · own +20 본인 채널 · context +15 제안·문의 맥락 · stale −20 2년 지난 글
"""
from __future__ import annotations

import copy
import html
import logging
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import psycopg
import requests

from .config import Settings
from .db import get_state, set_state
from .names import phone_digits, short_name
from .regions import sgg_name

log = logging.getLogger(__name__)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)   # 헤더 깨진 사이트 경고(수집엔 영향 없음)가 로그를 덮지 않게

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
PHONE_RE = re.compile(r"(?:0\d{1,2}|1\d{3})[\s\-.)]*\d{3,4}[\s\-.]*\d{4}")
A_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", re.S | re.I)
BAD_LOCAL = ("noreply", "no-reply", "no_reply", "webmaster", "postmaster", "mailer-daemon", "admin@", "help@", "support@",
             "privacy@", "abuse@", "sentry", "example", "test@", "user@", "email@", "name@", "your@", "someone@", "info@naver")
BAD_DOMAIN_PART = ("sentry", "example.", "domain.com", "email.com", "w3.org", "schema.org", "googleapis", "cloudfront", "wixpress",
                   "nhn.com", "navercorp", "tistory.com", "blog.naver", "mail.naver", "kakaocorp", "github", "wordpress", "squarespace")
CONTEXT_WORDS = ("협업", "제휴", "제안", "문의", "상담", "연락", "광고", "의뢰", "컨택", "이메일", "e-mail", "email", "메일",
                 "contact", "business", "biz", "partnership", "inquiry")
REFUSAL_WORDS = ("무단수집거부", "무단 수집 거부", "무단으로 수집되는 것을 거부", "이메일주소 무단수집", "전자우편주소 무단수집",
                 "이메일무단수집", "email 무단수집")
CONTACT_LINK = re.compile(r"contact|inquiry|문의|제휴|제안|about|company|회사소개|사무소소개|오시는|location|상담", re.I)
TYPE_WORD = {"broker": "부동산", "tax": "세무사", "legal": "법무사", "mg": "새마을금고", "cu": "신협", "nh_coop": "농협",
             "sh_coop": "수협", "savings_bank": "저축은행"}
REGION_WORDS = ("세무사", "법무사", "부동산", "새마을금고")
NEG_WORDS = ("개인정보", "보호책임자", "privacy", "개인정보보호")      # 개인정보 보호책임자 메일은 제안 창구가 아니다
FIN_TYPES = ("mg", "cu", "nh_coop", "sh_coop", "savings_bank", "forest_coop")
ORG_RE = re.compile(r"^(.*?(?:새마을금고|신용협동조합|신협|축산업협동조합|농업협동조합|축협|농협|수협|저축은행|산림조합))")
PLATFORM_DOMAINS = ("naver.", "kakao.", "daum.", "tistory.", "google.", "instagram.", "facebook.", "youtube.", "blogspot.", "modoo.at",
                    "imweb.", "wixsite.", "cafe24.", "band.us", "linktr.ee", "notion.", "x.com", "twitter.")
UA = {"User-Agent": "Mozilla/5.0 (compatible; kre-feed/0.5; +https://github.com/beomkea-lgtm/kre-feed-backend)"}


def clean_text(raw: str) -> str:
    return html.unescape(TAG_RE.sub(" ", raw))


def normalize_blog_url(url: str) -> str:
    """blog.naver.com/{id}/{no} 는 본문이 iframe 이라 모바일 주소로 읽는다."""
    m = re.match(r"https?://(?:m\.)?blog\.naver\.com/([A-Za-z0-9_\-.]+)/(\d+)", url or "")
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    m = re.match(r"https?://blog\.naver\.com/PostView\.naver\?.*?blogId=([^&]+).*?logNo=(\d+)", url or "")
    if m:
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    return url


def blog_id(url: str | None) -> str | None:
    m = re.match(r"https?://(?:m\.)?blog\.naver\.com/([A-Za-z0-9_\-.]+)", url or "")
    return m.group(1) if m and m.group(1) not in ("PostView.naver", "PostList.naver") else None


def fetch(session: requests.Session, url: str, timeout: float = 8.0, max_bytes: int = 1_500_000) -> str | None:
    try:
        r = session.get(url, headers=UA, timeout=timeout, stream=True, allow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or ("html" not in ct and "text" not in ct):
            return None
        buf = b""
        for chunk in r.iter_content(65536):
            buf += chunk
            if len(buf) >= max_bytes:
                break
        enc = r.encoding or "utf-8"
        try:
            return buf.decode(enc, errors="ignore")
        except LookupError:
            return buf.decode("utf-8", errors="ignore")
    except requests.RequestException:
        return None


def plausible(email: str) -> bool:
    e = email.lower()
    if len(e) > 64 or e.count("@") != 1:
        return False
    _local, domain = e.split("@")
    if any(b in e for b in BAD_LOCAL) or any(b in domain for b in BAD_DOMAIN_PART):
        return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp|js|css|woff2?)$", e) or re.search(r"@\d", e):   # 이미지 파일명·버전 문자열
        return False
    return True


def refuses_collection(text: str) -> bool:
    t = text.replace(" ", "")
    return any(w.replace(" ", "") in t for w in REFUSAL_WORDS)


def _context(low: str, start: int, end: int) -> bool:
    a, b = max(0, start - 80), min(len(low), end + 80)
    return any(w in low[a:b] for w in CONTEXT_WORDS)


def score_page(text: str, title: str, lead: dict, own: bool, postdate: str | None, require_context: bool = True) -> list[tuple[str, int, list[str]]]:
    """페이지 하나에서 (메일, 점수, 근거들). require_context 면 제안·문의 맥락이나 전화 근처가 아닌 메일은 버린다."""
    body = text
    low = body.lower()
    sn = lead["short_name"]
    pd = phone_digits(lead.get("phone"))
    phone_spans = [pm.start() for pm in PHONE_RE.finditer(body) if len(pd) >= 8 and pd[-8:] in re.sub(r"\D", "", pm.group(0))]
    phone_hit = bool(phone_spans) or (len(pd) >= 8 and pd[-8:] in re.sub(r"\D", "", body))
    name_title = bool(sn) and sn.replace(" ", "") in (title or "").replace(" ", "")
    name_body = bool(sn) and sn.replace(" ", "") in body.replace(" ", "")
    region_hit = any(w and w in body for w in (lead.get("umd"), lead.get("sgg")))
    stale = False
    if postdate and re.fullmatch(r"\d{8}", postdate):
        try:
            stale = datetime.strptime(postdate, "%Y%m%d") < datetime.now() - timedelta(days=730)
        except ValueError:
            stale = False
    best: dict[str, tuple[str, int, list[str]]] = {}
    for m in EMAIL_RE.finditer(body):
        email = m.group(0).strip(".").lower()
        if not plausible(email):
            continue
        near_phone = any(abs(p - m.start()) <= 150 for p in phone_spans)
        ctx = _context(low, m.start(), m.end())
        if any(w in low[max(0, m.start() - 60):m.start()] for w in NEG_WORDS):   # 라벨은 메일 앞에 온다
            continue
        if require_context and not (ctx or near_phone):
            continue
        signals: list[str] = []
        score = 0
        if phone_hit:
            score += 60; signals.append("phone")
        if name_title:
            score += 25; signals.append("title")
        elif name_body:
            score += 15; signals.append("body")
        if region_hit:
            score += 10; signals.append("region")
        if own:
            score += 20; signals.append("own")
        if ctx or near_phone:
            score += 15; signals.append("context")
        if stale:
            score -= 20; signals.append("stale")
        if email not in best or score > best[email][1]:
            best[email] = (email, score, signals)
    return list(best.values())


def email_focus(conn: psycopg.Connection) -> list[str]:
    """집중해서 메일을 찾을 리드 종류 (app_state.email_focus, 기본: 세무사 → 중개사)."""
    try:
        v = json.loads(get_state(conn, "email_focus") or '["tax","broker"]')
        return [str(x) for x in v] if isinstance(v, list) else ["tax", "broker"]
    except ValueError:
        return ["tax", "broker"]


# 자가 개선: 최근 30일, 종류별로 어떤 찾기 방법이 메일을 건졌는지 본다.
#  - 200곳 넘게 시도했는데 한 번도 못 건진 '검색' 방법은 그 종류에서 건너뛴다 (네이버·다음 하루 한도를 아껴 더 많은 리드를 본다)
#  - 단, 15%는 탐색용으로 건너뛰지 않고 다 해본다 (방법이 다시 통하기 시작하면 다시 켜진다)
SEARCH_KINDS = ("naver_blog", "naver_web", "phone_reverse", "naver_cafe", "naver_kin")
EXPLORE = 0.15
MIN_TRIED = 200


def learn_strategies(conn: psycopg.Connection) -> dict:
    tried = dict(conn.execute(
        "select type, count(*) from leads where email_checked_at > now() - interval '30 days' group by type").fetchall())
    hits: dict[str, dict[str, int]] = {}
    for ltype, kind, n in conn.execute(
            """select l.type, c.source_kind, count(distinct c.lead_id) from email_candidates c join leads l on l.id = c.lead_id
               where c.status in ('auto','accepted') and c.found_at > now() - interval '30 days' group by 1, 2""").fetchall():
        hits.setdefault(ltype, {})[kind or "?"] = n
    skip = {t: [k for k in SEARCH_KINDS if (tried.get(t, 0) >= MIN_TRIED and not hits.get(t, {}).get(k)
                                           and not (k == "naver_cafe" and hits.get(t, {}).get("naver_snippet")))]
            for t in tried}
    out = {"tried": tried, "hits": hits, "skip": skip}
    set_state(conn, "email_learning", json.dumps(out, ensure_ascii=False, default=str))
    conn.commit()
    return out


def candidate_leads(conn: psycopg.Connection, s: Settings, active: list[str] | None) -> list[dict]:
    rows = conn.execute(
        """
        with visible as (   -- 오늘 피드 카드의 탭별 상위 5 (화면에 보이는 리드)
          select lead_id from (
            select il.lead_id, row_number() over (
                     partition by il.issue_id, case when l2.type = 'broker' then 0
                                                   when l2.type in ('mg','cu','nh_coop','sh_coop','savings_bank','forest_coop','bank_branch') then 2 else 1 end
                     order by il.rank) pos
            from issue_leads il join issues i on i.id = il.issue_id join leads l2 on l2.id = il.lead_id
            where i.status = 'open' and (i.expires_at is null or i.expires_at > now())
          ) x where pos <= 5
        )
        select l.id, l.name, l.type, l.region_key, l.contact->>'phone', l.source, l.external_id,
               exists (select 1 from issue_leads il join issues i on i.id = il.issue_id
                       where il.lead_id = l.id and i.status = 'open' and (i.expires_at is null or i.expires_at > now())) as in_issue,
               l.id in (select lead_id from visible) as vis
        from leads l
        where l.status <> 'rejected'
          and coalesce(l.contact->>'email', '') = ''
          and (l.email_checked_at is null or l.email_checked_at < now() - (%(days)s || ' days')::interval
               or (l.id in (select lead_id from visible) and coalesce(l.email_depth, 0) < 2))   -- 화면에 보이는 리드는 깊게 한 번 더
          and (%(active)s::text[] is null or split_part(l.region_key, '|', 1) = any(%(active)s) or l.type = any(%(focus)s))
          and (%(shards)s = 1 or mod(abs(hashtext(l.id::text)), %(shards)s) = %(shard)s)
        order by coalesce(array_position(%(focus)s::text[], l.type), 999) asc,   -- 집중 대상(app_state.email_focus) 먼저
                 (l.type = 'broker') asc, (l.id in (select lead_id from visible)) desc, in_issue desc, l.score desc nulls last, l.email_checked_at nulls first   -- 제휴·금융 먼저, 중개사는 뒤
        limit %(n)s
        """,
        {"days": s.email_refresh_days, "active": active, "n": s.email_daily_leads, "shards": max(1, s.email_shards), "shard": s.email_shard,
         "focus": email_focus(conn)},
    ).fetchall()
    out = []
    for lid, name, ltype, region_key, phone, source, ext, _, vis in rows:
        sgg, _, umd = (region_key or "").partition("|")
        sn = short_name(name or "")
        org = None
        if ltype in FIN_TYPES:
            m = ORG_RE.match((name or "").replace(" ", ""))
            org = m.group(1) if m else None
            sn = org or sn
        out.append({"id": str(lid), "name": name or "", "type": ltype, "region_key": region_key, "phone": phone,
                    "short_name": sn, "umd": umd, "sgg": sgg_name(sgg) if sgg else "", "org": org,
                    "kakao_id": ext if source == "kakao_local" else None, "visible": bool(vis)})
    return out


def _skip_for(lead: dict, learning: dict) -> set[str]:
    if random.random() < EXPLORE:
        return set()
    return set((learning.get("skip") or {}).get(lead.get("type") or "", []))


class _Pages:
    """스레드 사이에 나눠 쓰는 페이지 예산."""

    def __init__(self, n: int):
        self.left = n
        self.used = 0
        self.lock = threading.Lock()

    def take(self) -> bool:
        with self.lock:
            if self.left <= 0:
                return False
            self.left -= 1
            self.used += 1
            return True


def _thread_naver(naver):
    nv = copy.copy(naver)
    if hasattr(nv, "s"):
        nv.s = requests.Session()
    return nv


class _SkipNaver(Exception):
    pass


class DaumSearch:
    """카카오 검색 API(다음 블로그·웹)를 네이버 검색과 같은 모양으로. 네이버 하루 한도가 차면 이걸로 이어 간다."""

    BLOG = "https://dapi.kakao.com/v2/search/blog"
    WEB = "https://dapi.kakao.com/v2/search/web"

    def __init__(self, key: str, timeout: float = 10):
        self.h = {"Authorization": f"KakaoAK {key}"}
        self.timeout = timeout
        self.s = requests.Session()

    def _get(self, url: str, q: str, size: int) -> list[dict]:
        r = self.s.get(url, headers=self.h, params={"query": q, "size": max(1, min(size, 50)), "sort": "accuracy"}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("documents", [])

    def blog(self, q: str, display: int = 5) -> list[dict]:
        out = []
        for d in self._get(self.BLOG, q, display):
            dt = (d.get("datetime") or "")[:10].replace("-", "")
            out.append({"title": d.get("title"), "link": d.get("url"), "description": d.get("contents"),
                        "bloggername": d.get("blogname") or "", "bloggerlink": d.get("url"), "postdate": dt})
        return out

    def web(self, q: str, display: int = 5) -> list[dict]:
        return [{"title": d.get("title"), "link": d.get("url"), "description": d.get("contents")} for d in self._get(self.WEB, q, display)]


def _contact_links(raw: str, base: str, limit: int = 2) -> list[str]:
    host = urlparse(base).netloc
    out: list[str] = []
    for href, text in A_RE.findall(raw):
        u = urljoin(base, href.strip())
        if urlparse(u).netloc != host or u == base:
            continue
        if CONTACT_LINK.search(href) or CONTACT_LINK.search(clean_text(text)):
            if u not in out:
                out.append(u)
        if len(out) >= limit:
            break
    return out


EMAIL_LOGIC_VER = "5"   # 3: 카카오 panel3 홈페이지 + 다음 검색 대체 · 4: 다음만 본 건 얕게 · 5: 보이는 리드 전화번호 역검색
GENERIC_HOMEPAGES = ("www.nonghyup.com", "nonghyup.com", "www.kfcc.co.kr", "kfcc.co.kr", "www.cu.co.kr", "cu.co.kr",
                     "www.suhyup.co.kr", "suhyup.co.kr", "www.fsb.or.kr", "www.nfcf.or.kr")


def kakao_homepage(session: requests.Session, place_id: str) -> list[str]:
    """카카오 장소 상세(place-api panel3)의 홈페이지 링크. 중앙회 대표 사이트(nonghyup.com 등)는 뺀다."""
    out: list[str] = []
    try:
        r = session.get(f"https://place-api.map.kakao.com/places/panel3/{place_id}", timeout=8,
                        headers={**UA, "Referer": "https://place.map.kakao.com/", "Origin": "https://place.map.kakao.com",
                                 "pf": "web", "Accept": "application/json"})
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            raw = r.text
            import json as _json
            j = _json.loads(raw)
            urls: list[str] = []

            def walk(o):
                if isinstance(o, dict):
                    for k, v in o.items():
                        if k == "homepages" and isinstance(v, list):
                            urls.extend(x for x in v if isinstance(x, str))
                        else:
                            walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(j)
            for u in urls:
                u = u.strip()
                if not u:
                    continue
                u = u if u.startswith("http") else "http://" + u
                host = urlparse(u).netloc.lower()
                if host in GENERIC_HOMEPAGES:
                    continue
                if u not in out:
                    out.append(u)
    except (requests.RequestException, ValueError):
        pass
    return out


def own_domain(url: str) -> str | None:
    host = urlparse(url or "").netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or any(p in host for p in PLATFORM_DOMAINS):
        return None
    return host


def has_mx(domain: str) -> bool:
    try:
        import dns.resolver
        return len(dns.resolver.resolve(domain, "MX", lifetime=4)) > 0
    except Exception:  # noqa: BLE001
        return False


def _read(session, url, pages: _Pages, stats: dict, lock: threading.Lock) -> tuple[str, str, str] | None:
    """페이지 → (raw, text, title). 수집 거부 고지면 None."""
    if not pages.take():
        return None
    raw = fetch(session, url)
    if not raw:
        return None
    text = clean_text(raw)
    if refuses_collection(text):
        with lock:
            stats["refused_pages"] += 1
        return None
    tm = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    return raw, text, (clean_text(tm.group(1)) if tm else "")


def _work_lead(lead: dict, links: list[str], naver, pages: _Pages, stats: dict, lock: threading.Lock,
               org_cache: dict | None = None) -> tuple[dict, bool]:
    """리드 하나: 검색·페이지 읽기 (스레드). 반환 ({email: (score, signals, url, kind)}, 검색 완료 여부)"""
    org = lead.get("org")
    if org and org_cache is not None:
        with lock:
            hit = org_cache.get(org)
        if hit is not None:                       # 같은 금고·조합 지점은 본점 결과 재사용
            return dict(hit), True
    found, complete = _work_lead_inner(lead, links, naver, pages, stats, lock)
    if not complete and not found and stats.get("naver_quota") and stats.get("_daum") is not None and not stats.get("daum_quota"):
        found, complete = _work_lead_inner(lead, links, naver, pages, stats, lock)   # 이 리드에서 네이버 한도 → 다음(카카오)으로 바로 다시
    if org and org_cache is not None and complete:
        with lock:
            org_cache[org] = dict(found)
    return found, complete


def _naver_quota(e: Exception) -> bool:
    """검색 한도 초과 응답인지 (네이버: 429/quota, 카카오: 'API limit has been exceeded' 400/429)."""
    r = getattr(e, "response", None)
    if r is None:
        return False
    body = (r.text or "").lower()
    return r.status_code == 429 or (r.status_code in (400, 403) and any(w in body for w in ("quota", "limit", "exceed")))


SEARCH_FAIL_STREAK = 15   # 같은 엔진이 연달아 이만큼 실패하면 한도로 본다 — 쓸데없는 호출 반복 방지


def _search_exhausted(stats: dict) -> bool:
    """네이버도 다음도 오늘 더 못 쓰는 상태 → 남은 리드는 손대지 않고 다음 실행으로."""
    return bool(stats.get("naver_quota") and (stats.get("_daum") is None or stats.get("daum_quota")))


def _work_lead_inner(lead: dict, links: list[str], naver, pages: _Pages, stats: dict, lock: threading.Lock) -> tuple[dict, bool]:
    if _search_exhausted(stats):
        return {}, False
    fallback = stats.get("_daum")
    using_daum = bool(stats.get("naver_quota") and fallback is not None)
    nv = _thread_naver(fallback if using_daum else naver)
    light = stats.get("_light", False) and not lead.get("visible")
    complete = using_daum or not stats.get("naver_quota", False)   # 다음 검색으로 끝까지 봤으면 완료
    session = requests.Session()
    sn, umd = lead["short_name"], lead["umd"]
    srcs: list[tuple[str, str, bool, str | None]] = [(u, "naver_local_link", True, None) for u in links]
    snippets: list[tuple[str, str, str]] = []       # (텍스트, 제목, url) — 카페·지식iN 스니펫
    api = 0
    if lead.get("kakao_id"):
        for u in kakao_homepage(session, lead["kakao_id"]):
            srcs.insert(0, (u, "kakao_homepage", True, None))
            with lock:
                stats["kakao_homepages"] += 1
    try:
        if stats.get("naver_quota") and not using_daum:
            raise _SkipNaver()
        if using_daum and stats.get("daum_quota"):
            raise _SkipNaver()
        if lead.get("org"):                        # 2금융: 본점 홈페이지
            api += 1
            for it in nv.web(f"{lead['org']} 홈페이지", 5):
                title = clean_text(it.get("title") or "")
                if lead["org"] in title.replace(" ", ""):
                    srcs.insert(0, (it.get("link") or "", "org_homepage", True, None))
                    break
        skipk = lead.get("skip") or set()
        seen_blogs: set[str] = set()
        for q in (() if "naver_blog" in skipk else (f"{sn} {umd}", f"{sn} 협업 문의")):
            api += 1
            for it in nv.blog(q, 5):
                title = clean_text(it.get("title") or "")
                desc = clean_text(it.get("description") or "")
                blogger = it.get("bloggername") or ""
                nsn = sn.replace(" ", "")
                if nsn not in title.replace(" ", "") and nsn not in desc.replace(" ", "") and nsn not in blogger.replace(" ", ""):
                    continue
                own = nsn in blogger.replace(" ", "")          # 작성자에 상호 = 본인 블로그
                srcs.append((normalize_blog_url(it.get("link") or ""), "naver_blog", own, it.get("postdate")))
                bid = blog_id(it.get("bloggerlink"))
                if own and bid and bid not in seen_blogs:       # 본인 블로그 프로필(소개)에 협업 메일을 적어두는 경우
                    seen_blogs.add(bid)
                    srcs.append((f"https://blog.naver.com/profile/intro.naver?blogId={bid}", "naver_blog_profile", True, None))
        web_qs = [f"{sn} {umd} {TYPE_WORD.get(lead['type'] or '', '')}".strip()] + ([] if light else [f"{sn} 이메일"])
        for q in ([] if "naver_web" in skipk else web_qs):
            api += 1
            for it in nv.web(q, 5):
                title = clean_text(it.get("title") or "")
                desc = clean_text(it.get("description") or "")
                if sn.replace(" ", "") not in (title + desc).replace(" ", ""):
                    continue
                link = it.get("link") or ""
                srcs.append((normalize_blog_url(link) if "blog.naver.com" in link else link, "naver_web", False, None))
        # 전화번호 역검색 (피드에 보이는 리드만): 같은 번호가 적힌 페이지 = 그 업체 페이지일 가능성 큼 → 열어서 메일 찾기
        ph = (lead.get("phone") or "").split(" ")[0].strip()
        if (lead.get("visible") or lead.get("focus")) and "phone_reverse" not in skipk and re.fullmatch(r"0\d{1,3}-\d{3,4}-\d{4}", ph):
            pdig = re.sub(r"\D", "", ph)
            for fn in (nv.web, nv.blog):
                api += 1
                for it in fn(f'"{ph}"', 10):
                    blob = clean_text((it.get("title") or "") + " " + (it.get("description") or ""))
                    if pdig not in re.sub(r"\D", "", blob):
                        continue
                    link = it.get("link") or ""
                    if link:
                        srcs.append((normalize_blog_url(link) if "blog.naver.com" in link else link, "phone_reverse", False, None))
        for kind, fn in (("naver_cafe", getattr(nv, "cafe", None)), ("naver_kin", getattr(nv, "kin", None))):
            if fn is None or light or kind in skipk:
                continue
            api += 1
            for it in fn(f"{sn} {umd}", 5):
                title = clean_text(it.get("title") or "")
                desc = clean_text(it.get("description") or "")
                if sn.replace(" ", "") not in (title + desc).replace(" ", ""):
                    continue
                snippets.append((f"{title} {desc}", title, it.get("link") or ""))
                if kind == "naver_kin":                 # 지식iN 은 열린다 (카페글은 보통 회원 전용)
                    srcs.append((it.get("link") or "", kind, False, None))
    except _SkipNaver:
        complete = False
    except requests.RequestException as e:
        complete = False
        if _naver_quota(e):
            with lock:
                if using_daum:
                    if not stats.get("daum_quota"):
                        log.warning("다음(카카오) 검색 한도도 도달 — 남은 리드는 다음 실행으로")
                    stats["daum_quota"] = True
                else:
                    if not stats.get("naver_quota"):
                        log.warning("네이버 검색 한도 도달 — %s", "다음(카카오) 검색으로 이어 간다" if fallback else "남은 리드는 확인 표시 없이 다음 실행으로")
                    stats["naver_quota"] = True
        else:
            log.warning("검색 실패 %s: %s", sn, e)
            with lock:
                key = "_fail_daum" if using_daum else "_fail_naver"
                stats[key] = stats.get(key, 0) + 1
                if stats[key] >= SEARCH_FAIL_STREAK:
                    if using_daum and not stats.get("daum_quota"):
                        log.warning("다음(카카오) 검색 %d회 연속 실패 — 한도로 보고 멈춘다", stats[key])
                        stats["daum_quota"] = True
                    elif not using_daum and not stats.get("naver_quota"):
                        log.warning("네이버 검색 %d회 연속 실패 — 한도로 보고 다음으로", stats[key])
                        stats["naver_quota"] = True
    else:
        if api:
            with lock:
                stats["_fail_daum" if using_daum else "_fail_naver"] = 0
    if using_daum and complete:
        with lock:
            stats["daum_leads"] = stats.get("daum_leads", 0) + 1
    with lock:
        stats["api_calls"] += api
        if not using_daum:
            stats["naver_calls"] = stats.get("naver_calls", 0) + api
            cap = stats.get("_naver_cap")
            if cap is not None and stats["naver_calls"] >= cap and not stats.get("naver_quota"):
                log.warning("메일 수집 네이버 하루 상한 도달 — 나머지는 다음(카카오) 검색으로 (한도 몰아쓰기 방지)")
                stats["naver_quota"] = True
                stats["_naver_capped"] = True

    found: dict[str, tuple[int, list[str], str, str]] = {}
    for text, title, url in snippets:
        for email, score, signals in score_page(text, title, lead, False, None):
            if email not in found or score > found[email][0]:
                found[email] = (score, signals, url, "naver_snippet")
    seen: set[str] = set()
    queue = [s for s in srcs if s[0] and s[0].startswith("http")]
    while queue and len(seen) < 7:
        url, kind, own, postdate = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        got = _read(session, url, pages, stats, lock)
        if not got:
            continue
        raw, text, title = got
        for email, score, signals in score_page(text, title, lead, own, postdate):
            if email not in found or score > found[email][0]:
                found[email] = (score, signals, url, kind)
        if kind in ("naver_local_link", "kakao_homepage", "org_homepage") and "blog.naver.com" not in url:   # 홈페이지면 문의·제휴 페이지도
            for u in _contact_links(raw, url):
                queue.insert(0, (u, "homepage_contact", True, None))
        time.sleep(0.1)

    # 못 찾았고 자체 도메인 홈페이지가 있으면: info@도메인 (메일 서버 있을 때만, 후보로만)
    if not found:
        for url, kind, _own, _p in srcs:
            if kind not in ("naver_local_link", "kakao_homepage", "org_homepage"):
                continue
            dom = own_domain(url)
            if dom and has_mx(dom):
                found[f"info@{dom}"] = (40, ["guess"], url, "mx_guess")
                with lock:
                    stats["mx_guesses"] += 1
                break
    return found, complete


def _save(conn: psycopg.Connection, s: Settings, lead_id: str, found: dict, stats: dict) -> None:
    best_auto = None
    for email, (score, signals, url, kind) in sorted(found.items(), key=lambda kv: -kv[1][0]):
        distinct = len({x for x in signals if x in ("phone", "title", "body", "region", "own", "context")})
        if score >= s.email_min_auto and distinct >= 2:
            status = "auto"
        elif score >= s.email_min_candidate:
            status = "candidate"
        else:
            continue
        cur = conn.execute(
            """
            insert into email_candidates (lead_id, email, score, signals, source_url, source_kind, status)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (lead_id, email) do update set
              score = greatest(email_candidates.score, excluded.score), signals = excluded.signals,
              source_url = excluded.source_url, source_kind = excluded.source_kind,
              status = case when email_candidates.status in ('accepted','rejected','quarantined') then email_candidates.status else excluded.status end
            returning status
            """,
            (lead_id, email, score, signals, (url or "")[:500], kind, status),
        ).fetchone()
        final = cur[0] if cur else status
        if final == "auto" and best_auto is None:
            best_auto = (email, url)
            stats["auto"] += 1
        elif final == "candidate":
            stats["candidates"] += 1
    if best_auto:
        conn.execute(
            "update leads set contact = coalesce(contact, '{}'::jsonb) || jsonb_build_object('email', %s::text), email_src = %s "
            "where id = %s and coalesce(contact->>'email', '') = ''",
            (best_auto[0], f"auto:{(best_auto[1] or '')[:400]}", lead_id),
        )


def _region_reverse(conn: psycopg.Connection, s: Settings, naver, active: list[str] | None, pages: _Pages, stats: dict,
                    lock: threading.Lock, ex: ThreadPoolExecutor) -> None:
    """동네 역방향: '{동} {업종} 문의 메일' 검색 결과 페이지에서 (메일, 근처 전화) → 전화로 리드 매칭."""
    regions = [r[0] for r in conn.execute(
        """
        select region_key from leads
        where coalesce(contact->>'email','') = '' and status <> 'rejected' and region_key like '%%|%%'
          and (%(a)s::text[] is null or split_part(region_key, '|', 1) = any(%(a)s))
        group by region_key order by count(*) desc limit %(n)s
        """,
        {"a": active, "n": s.email_daily_regions},
    ).fetchall()]
    if not regions:
        return
    by_phone: dict[str, tuple[str, str, str]] = {}   # 끝 8자리 → (lead_id, short_name, region_key)  메일 없는 리드만
    for lid, phone, name, rk in conn.execute(
        "select id, contact->>'phone', name, region_key from leads where coalesce(contact->>'email','') = '' and status <> 'rejected' "
        "and region_key = any(%s)", (regions,)
    ).fetchall():
        pd = phone_digits(phone)
        if len(pd) >= 8:
            by_phone.setdefault(pd[-8:], (str(lid), short_name(name or ""), rk))

    def work(region_key: str) -> list[tuple[str, str, int, list[str], str]]:
        nv = _thread_naver(naver)
        session = requests.Session()
        umd = region_key.split("|")[-1]
        urls: list[str] = []
        api = 0
        try:
            for w in REGION_WORDS:
                api += 1
                for it in nv.blog(f"{umd} {w} 문의 메일", 10):
                    snippet = clean_text((it.get("title") or "") + " " + (it.get("description") or "")).lower()
                    if any(c in snippet for c in CONTEXT_WORDS):
                        urls.append(normalize_blog_url(it.get("link") or ""))
        except requests.RequestException as e:
            log.warning("동네 역방향 검색 실패 %s: %s", region_key, e)
        with lock:
            stats["api_calls"] += api
        hits = []
        for url in list(dict.fromkeys(u for u in urls if u.startswith("http")))[:8]:
            got = _read(session, url, pages, stats, lock)
            if not got:
                continue
            _raw, text, title = got
            low = text.lower()
            phones = [(pm.start(), re.sub(r"\D", "", pm.group(0))[-8:]) for pm in PHONE_RE.finditer(text)]
            for m in EMAIL_RE.finditer(text):
                email = m.group(0).strip(".").lower()
                if not plausible(email):
                    continue
                near = [d for p, d in phones if abs(p - m.start()) <= 150 and d in by_phone]
                if not near:
                    continue
                lid, sn, rk = by_phone[near[0]]
                signals = ["phone", "context"] if _context(low, m.start(), m.end()) else ["phone"]
                score = 60 + (15 if "context" in signals else 0)
                if sn and sn in text.replace(" ", ""):
                    score += 15; signals.append("body")
                if umd in text:
                    score += 10; signals.append("region")
                hits.append((lid, email, score, signals, url))
            time.sleep(0.1)
        return hits

    for hits in ex.map(work, regions):
        try:
            for lid, email, score, signals, url in hits:
                _save(conn, s, lid, {email: (score, signals, url, "region_reverse")}, stats)
                stats["region_matches"] += 1
            conn.commit()
        except psycopg.Error as e:
            conn.rollback()
            log.warning("동네 역방향 저장 실패: %s", e)


VISIBLE_COVERAGE_SQL = """
with v as (
  select il.lead_id, l.type, l.contact, row_number() over (
           partition by il.issue_id, case when l.type = 'broker' then 0
                                         when l.type in ('mg','cu','nh_coop','sh_coop','savings_bank','forest_coop','bank_branch') then 2 else 1 end
           order by il.rank) pos
  from issue_leads il join issues i on i.id = il.issue_id join leads l on l.id = il.lead_id
  where i.status = 'open' and (i.expires_at is null or i.expires_at > now())
), u as (select distinct on (lead_id) lead_id, type, contact from v where pos <= 5)
select (type = 'broker') as broker, count(*),
       count(*) filter (where coalesce(contact->>'email','') <> ''),
       count(*) filter (where coalesce(contact->>'phone','') <> ''),
       count(*) filter (where coalesce(contact->>'email','') = '' and exists (
         select 1 from email_candidates c where c.lead_id = u.lead_id and c.status = 'candidate'))
from u group by 1 order by 1
"""


def visible_coverage(conn: psycopg.Connection) -> str:
    """피드에 보이는 리드(탭별 상위 5)의 메일·전화 확보율 한 줄."""
    parts = []
    for broker, n, em, ph, cand in conn.execute(VISIBLE_COVERAGE_SQL).fetchall():
        parts.append(f"{'중개' if broker else '제휴·금융'} 메일 {em}/{n} (+후보 {cand}) 전화 {ph}/{n}")
    return "보이는 리드: " + " · ".join(parts)


def backfill_phones(conn: psycopg.Connection, s: Settings, limit: int = 400) -> dict:
    """피드에 보이는 리드 중 전화 없는 곳: ① 네이버 지역검색이 준 전화 ② 카카오 키워드 검색(상호+동)으로 채운다."""
    st = {"naver": 0, "kakao": 0, "missing": 0}
    rows = conn.execute(
        VISIBLE_COVERAGE_SQL.split("select (type = 'broker')")[0]
        + "select u.lead_id, l.name, l.type, l.region_key from u join leads l on l.id = u.lead_id "
          "where coalesce(l.contact->>'phone','') = '' limit %s",
        (limit,),
    ).fetchall()
    st["missing"] = len(rows)
    if not rows:
        return st
    kakao = None
    if s.kakao_rest_key:
        from .enrich import Kakao
        kakao = Kakao(s.kakao_rest_key)
    for lid, name, ltype, rk in rows:
        phone = None
        r = conn.execute("select phone from naver_ranks where lead_id = %s and coalesce(phone,'') <> '' limit 1", (lid,)).fetchone()
        if r:
            phone, src = r[0], "naver"
        elif kakao and name:
            umd = (rk or "").split("|")[-1]
            try:
                docs = kakao.keyword(f"{umd} {name}", category="AG2" if ltype == "broker" else None, size=5)
            except requests.RequestException:
                docs = []
            sn = short_name(name).replace(" ", "") or name.replace(" ", "")
            for d in docs:
                if (d.get("phone") or "").strip() and sn and sn in (d.get("place_name") or "").replace(" ", ""):
                    phone, src = d["phone"].strip(), "kakao"
                    break
        if phone:
            conn.execute("update leads set contact = coalesce(contact, '{}'::jsonb) || jsonb_build_object('phone', %s::text) where id = %s", (phone, lid))
            st[src] += 1
    conn.commit()
    log.info("보이는 리드 전화 채움 %s", st)
    return st


def quarantine_extracted(conn: psycopg.Connection) -> dict:
    """EMAIL_MODE=off 로 돌릴 때: 자동으로 붙은 메일을 시퀀스에 못 쓰게 격리."""
    a = conn.execute(
        "update leads set contact = contact - 'email', email_src = 'quarantined:' || email_src where email_src like 'auto:%%'"
    ).rowcount
    b = conn.execute("update email_candidates set status = 'quarantined' where status in ('auto','candidate')").rowcount
    conn.commit()
    return {"leads_cleared": a, "candidates_quarantined": b}


def run_emails(conn: psycopg.Connection, s: Settings, naver, active: list[str] | None = None) -> dict:
    stats = {"leads_checked": 0, "leads_incomplete": 0, "pages": 0, "api_calls": 0, "auto": 0, "candidates": 0, "refused_pages": 0,
             "region_matches": 0, "kakao_homepages": 0, "mx_guesses": 0, "naver_quota": False, "skipped": [], "_light": s.email_light}
    if s.email_mode == "quarantine":
        stats["quarantine"] = quarantine_extracted(conn)
        return stats
    if s.email_mode == "off":
        stats["skipped"].append("EMAIL_MODE=off")
        return stats
    if naver is None or not hasattr(naver, "blog"):
        stats["skipped"].append("naver(키 없음)")
        return stats
    if s.email_shard == 0:
        # 찾는 방법이 늘었으면(카카오 홈페이지 등) 메일 못 찾은 '깊게 본' 리드를 다시 볼 수 있게
        if get_state(conn, "email_logic_ver") != EMAIL_LOGIC_VER:
            # 새 찾기 방식은 피드에 보이는 리드만 다시 깊게 (전체를 다시 열면 하루 한도를 다 쓴다)
            n = conn.execute(
                VISIBLE_COVERAGE_SQL.split("select (type = 'broker')")[0]
                + "update leads set email_depth = 1 where id in (select lead_id from u) and email_depth = 2 "
                  "and coalesce(contact->>'email','') = ''").rowcount
            set_state(conn, "email_logic_ver", EMAIL_LOGIC_VER)
            conn.commit()
            log.info("찾기 방식 %s — 깊게 다시 볼 리드 %d", EMAIL_LOGIC_VER, n)
        try:
            stats["phones"] = backfill_phones(conn, s)
            log.info("%s", visible_coverage(conn))
        except psycopg.Error as e:
            conn.rollback()
            log.warning("전화 채우기 실패: %s", e)
    learning = learn_strategies(conn) if s.email_shard == 0 else json.loads(get_state(conn, "email_learning") or "{}")
    focus = email_focus(conn)
    leads = candidate_leads(conn, s, active)
    for l in leads:
        l["skip"] = _skip_for(l, learning)
        l["focus"] = l.get("type") in focus
    stats["focus"] = {t: sum(1 for l in leads if l["type"] == t) for t in focus}
    stats["learning_skip"] = {t: k for t, k in (learning.get("skip") or {}).items() if k}
    log.info("집중 대상 %s · 건너뛰는 방법(30일 무수확) %s", stats["focus"], stats["learning_skip"])
    links: dict[str, list[str]] = {}
    if leads:
        for lid, link in conn.execute(
            "select distinct lead_id::text, link from naver_ranks where lead_id = any(%s::uuid[]) and coalesce(link,'') <> ''",
            ([l["id"] for l in leads],),
        ).fetchall():
            links.setdefault(lid, []).append(link)
    pages = _Pages(s.email_page_budget)
    lock = threading.Lock()
    if s.kakao_rest_key:
        stats["_daum"] = DaumSearch(s.kakao_rest_key)
    from datetime import timezone as _tz
    kst_today = (datetime.now(_tz.utc) + timedelta(hours=9)).date().isoformat()
    # 메일 수집이 하루에 쓸 수 있는 네이버 호출 상한 (조각 나눠 돌면 조각마다 나눔) — 인플루언서·순위 몫을 남긴다
    used = int(get_state(conn, f"naver_used_emails:{kst_today}") or 0)
    stats["_naver_cap"] = max(0, (s.email_naver_daily_cap - used) // max(1, s.email_shards))
    if stats["_naver_cap"] <= 0:
        stats["naver_quota"] = True
        stats["_naver_capped"] = True
        log.info("오늘 메일 수집용 네이버 몫(%d)을 다 썼다 — 다음(카카오) 검색으로", s.email_naver_daily_cap)
    if get_state(conn, "naver_quota_day") == kst_today:
        stats["naver_quota"] = True
        log.info("오늘 네이버 검색 한도는 이미 찼다 — 처음부터 다음(카카오) 검색으로")
    if get_state(conn, "daum_quota_day") == kst_today:
        stats["daum_quota"] = True
    if _search_exhausted(stats):
        log.info("오늘 검색 한도(네이버·다음) 모두 소진 — 메일 수집 건너뜀, 00시 이후 다시")
        stats["pages"] = 0
        stats.pop("_light", None); stats.pop("_daum", None)
        return stats
    log.info("메일 수집 대상 %d건 · 동네 역방향 %d곳 (페이지 예산 %d, 스레드 %d)", len(leads), s.email_daily_regions, s.email_page_budget, s.email_workers)

    with ThreadPoolExecutor(max_workers=max(1, s.email_workers)) as ex:
        todo = [l for l in leads if len(l["short_name"]) >= 2]
        skip = [l for l in leads if len(l["short_name"]) < 2]
        if skip:   # 너무 일반적인 상호는 못 찾는다 — 다음 주기까지 건너뜀
            # 병렬 샤드가 같은 행을 동시에 잡으면 교착 → 정렬된 순서로 잠그고, 실패해도 수집은 계속 (표시만 못 한 것)
            ids = sorted(str(l["id"]) for l in skip)
            for attempt in range(3):
                try:
                    conn.execute("update leads set email_checked_at = now() where id in (select id from leads where id = any(%s::uuid[]) order by id for update skip locked)", (ids,))
                    conn.commit()
                    break
                except psycopg.errors.OperationalError as e:
                    conn.rollback()
                    log.warning("건너뛸 리드 표시 재시도 %d: %s", attempt + 1, str(e).splitlines()[0])
                    time.sleep(1 + attempt * 2)
        org_cache: dict = {}
        futures = [(l, ex.submit(_work_lead, l, links.get(l["id"], []), naver, pages, stats, lock, org_cache)) for l in todo]
        stopped = False
        for lead, fut in futures:
            if not stopped and _search_exhausted(stats):
                stopped = True
                n = sum(1 for _, f in futures if f.cancel())
                log.warning("검색 한도 소진 — 남은 리드 %d개는 다음 실행으로 (호출 낭비 방지)", n)
            if fut.cancelled():
                continue
            try:
                found, complete = fut.result()
            except Exception as e:  # noqa: BLE001 — 리드 하나 실패로 전체를 멈추지 않는다
                log.warning("메일 수집 실패 %s: %s", lead["name"], e)
                found, complete = {}, False
            for attempt in range(3):   # 다른 잡과 같은 행을 동시에 쓰면 교착이 날 수 있다 → 되감고 재시도
                try:
                    _save(conn, s, lead["id"], found, stats)
                    if complete or found:      # 검색이 다 안 됐으면(한도 등) 확인 표시를 남기지 않는다 → 다음 실행에 다시
                        # 네이버 한도로 다음 검색만 썼으면 얕게 본 것으로 → 00시 이후 네이버로 다시
                        depth = 1 if ((s.email_light and not lead.get("visible")) or stats.get("naver_quota")) else 2
                        conn.execute("update leads set email_checked_at = now(), email_depth = greatest(coalesce(email_depth, 0), %s) where id = %s",
                                     (depth, lead["id"]))
                    else:
                        stats["leads_incomplete"] += 1
                    conn.commit()
                    break
                except psycopg.errors.OperationalError as e:
                    conn.rollback()
                    log.warning("저장 재시도 %s (%d): %s", lead["name"], attempt + 1, e)
                    time.sleep(1 + attempt)
            stats["leads_checked"] += 1
            if stats["leads_checked"] % 50 == 0:
                log.info("메일 수집 진행 %d/%d · 자동 %d · 후보 %d · 페이지 %d", stats["leads_checked"], len(todo),
                         stats["auto"], stats["candidates"], pages.used)
                try:
                    log.info("%s", visible_coverage(conn))
                except psycopg.Error:
                    conn.rollback()
        rev = stats.get("_daum") if stats["naver_quota"] else naver
        if s.email_shard == 0 and rev is not None and not stats.get("daum_quota"):
            _region_reverse(conn, s, rev, active, pages, stats, lock, ex)
    if stats.get("naver_calls"):
        conn.execute(
            "insert into app_state (key, value) values (%s, %s) on conflict (key) do update "
            "set value = (coalesce(nullif(app_state.value, ''), '0')::int + excluded.value::int)::text, updated_at = now()",
            (f"naver_used_emails:{kst_today}", str(stats["naver_calls"])),
        )
        conn.commit()
    if stats["naver_quota"] and not stats.get("_naver_capped"):   # 상한으로 멈춘 건 진짜 한도 소진이 아님
        set_state(conn, "naver_quota_day", kst_today)
        conn.commit()
    if stats.get("daum_quota"):
        set_state(conn, "daum_quota_day", kst_today)
        conn.commit()
    stats["pages"] = pages.used
    for k in ("_light", "_daum", "_fail_daum", "_fail_naver", "_naver_cap", "_naver_capped"):
        stats.pop(k, None)
    log.info("emails %s", stats)
    return stats
