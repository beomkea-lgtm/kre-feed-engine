"""테크·투자 '정성' 크리에이터 발굴: 플랫폼 블로그가 아니라 자기 채널(자체 도메인 블로그·뉴스레터·브런치)을 공들여 운영하는 사람.

1) 다음(카카오) 웹문서 검색 × 테크·투자 키워드 → 결과를 도메인(채널) 단위로 묶는다. 네이버·티스토리 등 플랫폼·포털·언론·쇼핑은 뺀다.
2) 채널 홈(과 소개 페이지)을 읽어 정성 신호로 점수: 자체 도메인, 뉴스레터 구독 창구, 소개 페이지, 글 깊이, 다른 채널 연결
   / 감점: 쿠팡 파트너스·제휴 수익 문구, 광고 스크립트 도배.
3) 리드(type=influencer_tech, source=web, external_id=채널키) + influencer_stats(segment=tech, blog_id=채널키).
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urljoin, urlparse

import psycopg
import requests

from .db import jsonb
from .emails import clean_text, fetch, plausible, refuses_collection, score_page
from .influencers import RE_TECH, TECH_TOPICS

log = logging.getLogger(__name__)

PLATFORM = re.compile(r"(^|\.)(naver\.com|tistory\.com|daum\.net|kakao\.com|blogspot\.com|egloos\.com|youtube\.com|instagram\.com|facebook\.com|"
                      r"twitter\.com|x\.com|namu\.wiki|wikipedia\.org|google\.com|coupang\.com|11st\.co\.kr|gmarket\.co\.kr|"
                      r"chosun\.com|joongang\.co\.kr|donga\.com|hankyung\.com|mk\.co\.kr|sedaily\.com|edaily\.co\.kr|yna\.co\.kr|"
                      r"news\.|hani\.co\.kr|khan\.co\.kr|mt\.co\.kr|fnnews\.com|asiae\.co\.kr|heraldcorp\.com|biz\.chosun|zdnet|"
                      r"investing\.com|tradingview\.com|kiwoom|samsungpop|miraeasset|nhqv|toss\.im|kbstar|shinhan|"
                      r"gov\.kr|go\.kr|or\.kr|ac\.kr|apple\.com|play\.google|velog\.io|github\.io|github\.com|notion\.site|linkedin\.com)$", re.I)
KINDS = [("newsletter", re.compile(r"(stibee\.com|maily\.so|substack\.com|page\.stibee|beehiiv\.com)", re.I)),
         ("brunch", re.compile(r"brunch\.co\.kr", re.I)),
         ("medium", re.compile(r"medium\.com", re.I))]
AFFIL = re.compile(r"쿠팡\s*파트너스|파트너스\s*활동|일정액의\s*수수료|제휴\s*마케팅의\s*일환|adsbygoogle|googlesyndication", re.I)
NEWSLETTER_HINT = re.compile(r"뉴스레터|구독하기|subscribe|stibee|maily|substack", re.I)
ABOUT_HINT = re.compile(r'href="([^"]*(?:about|소개|profile|who|intro)[^"]*)"', re.I)
SOCIAL = re.compile(r"youtube\.com/|instagram\.com/|linkedin\.com/in/|x\.com/|twitter\.com/", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def channel_key(url: str) -> tuple[str, str] | None:
    """(채널키, 종류). 브런치·미디엄·서브스택은 작가 단위, 나머지는 도메인 단위."""
    u = urlparse(url or "")
    host = (u.netloc or "").lower().removeprefix("www.")
    if not host:
        return None
    for kind, rx in KINDS:
        if rx.search(host):
            seg = (u.path.strip("/").split("/") or [""])[0]
            if kind in ("brunch", "medium") and seg.startswith("@"):
                return f"{host}/{seg}", kind
            if kind == "newsletter":
                return host if host.count(".") >= 2 else f"{host}/{seg}", kind
            return None
    if PLATFORM.search(host):
        return None
    return host, "own"


def collect_web(searchers, queries: list[str], per_query: int = 30, pause: float = 0.12) -> tuple[dict, dict]:
    """searchers: web(q, n) 을 가진 것들 — 네이버(웹문서) 먼저, 한도면 다음(카카오)."""
    if not isinstance(searchers, (list, tuple)):
        searchers = [searchers]
    agg: dict[str, dict] = {}
    st = {"queries": 0, "results": 0, "kept": 0, "errors": 0, "engine": None}
    idx = 0
    for q in queries:
        items = None
        while idx < len(searchers):
            try:
                items = searchers[idx].web(q, per_query)
                st["engine"] = type(searchers[idx]).__name__
                break
            except requests.RequestException as e:
                r = getattr(e, "response", None)
                if r is not None and r.status_code in (400, 403, 429):
                    log.warning("%s 웹검색 한도/오류 → 다음 엔진: %s", type(searchers[idx]).__name__, e)
                    idx += 1
                    continue
                st["errors"] += 1
                items = []
                break
        if items is None:
            break
        st["queries"] += 1
        for rank, it in enumerate(items, 1):
            st["results"] += 1
            ck = channel_key(it.get("link") or "")
            if not ck:
                continue
            st["kept"] += 1
            key, kind = ck
            a = agg.setdefault(key, {"name": "", "kind": kind, "hits": 0, "queries": set(), "topics": set(), "regions": set(),
                                     "best_rank": 999, "last_post": None, "posts": [], "home": None})
            a["hits"] += 1
            a["queries"].add(q)
            a["topics"].add(q)
            a["best_rank"] = min(a["best_rank"], rank)
            link = it.get("link") or ""
            if not a["home"]:
                u = urlparse(link)
                a["home"] = f"{u.scheme}://{key}" if kind == "own" or kind == "newsletter" and "/" not in key else f"{u.scheme}://{key}"
            if len(a["posts"]) < 5:
                a["posts"].append({"title": clean_text(it.get("title") or "")[:80], "url": link, "date": None, "q": q, "rank": rank})
        time.sleep(pause)
    return agg, st


def quality(session: requests.Session, a: dict, key: str) -> dict:
    """채널 홈(+소개 페이지)을 읽어 정성 신호."""
    home = a.get("home") or f"https://{key}"
    raw = fetch(session, home) or ""
    m = {"own_domain": a["kind"] == "own", "newsletter": a["kind"] == "newsletter", "brunch": a["kind"] == "brunch",
         "affiliate": False, "about": None, "social": False, "depth": 0, "name": "", "text": "", "tech_ratio": None}
    if raw:
        t = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
        m["name"] = clean_text(t.group(1)).strip()[:60] if t else ""
        m["affiliate"] = bool(AFFIL.search(raw))
        m["newsletter"] = m["newsletter"] or bool(NEWSLETTER_HINT.search(raw))
        m["social"] = bool(SOCIAL.search(raw))
        text = clean_text(TAG_RE.sub(" ", re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)))
        text = re.sub(r"\s+", " ", text)
        m["text"] = text[:20000]
        m["depth"] = len(text)
        words = re.findall(r"[가-힣A-Za-z]{2,}", text[:20000])
        if words:
            m["tech_ratio"] = min(1.0, sum(1 for w in words if RE_TECH.search(w)) / max(30, len(words) / 10))
        ab = ABOUT_HINT.search(raw)
        if ab:
            about_url = urljoin(home, ab.group(1))
            if urlparse(about_url).netloc.endswith(urlparse(home).netloc.removeprefix("www.")):
                araw = fetch(session, about_url)
                if araw:
                    m["about"] = about_url
                    m["text"] += " " + re.sub(r"\s+", " ", clean_text(TAG_RE.sub(" ", araw)))[:8000]
    return m


def score_channel(a: dict, m: dict, has_email: bool) -> tuple[float, list[str]]:
    s, why = 0.0, []
    if m["own_domain"]:
        s += 25; why.append("자체 도메인")
    if m["newsletter"]:
        s += 25; why.append("뉴스레터")
    if m["brunch"]:
        s += 15; why.append("브런치 작가")
    if m["about"]:
        s += 10; why.append("소개 페이지")
    if m["social"]:
        s += 5; why.append("다른 채널 연결")
    if m["depth"] >= 6000:
        s += 10; why.append("글 깊이")
    exp = min(a["hits"] * 5, 30)
    if exp:
        s += exp; why.append(f"검색 노출 {a['hits']}회")
    if has_email:
        s += 15; why.append("연락 메일")
    if m["affiliate"]:
        s -= 40; why.append("제휴수익·광고 도배 (감점)")
    return s, why


def run_creators(conn: psycopg.Connection, searchers, min_hits: int = 1, max_channels: int = 600) -> dict:
    queries = list(TECH_TOPICS) + ["뉴스레터 재테크", "투자 뉴스레터", "경제 뉴스레터 구독", "미국주식 뉴스레터", "테크 뉴스레터",
                                   "브런치 재테크", "브런치 투자", "프롭테크 블로그", "개인 블로그 투자 기록", "투자 일지 블로그"]
    agg, st = collect_web(searchers, queries)
    picked = sorted(((k, a) for k, a in agg.items() if a["hits"] >= min_hits), key=lambda x: (-x[1]["hits"], x[1]["best_rank"]))
    # 한 번 본 채널은 다시 보지 않는다 — 새 채널만
    recent = {r[0] for r in conn.execute("select blog_id from influencer_stats").fetchall()}
    n_all = len(picked)
    picked = [(k, a) for k, a in picked if k not in recent][:max_channels]
    st.update({"channels": len(agg), "picked": len(picked), "skipped_recent": n_all - len(picked), "saved": 0, "emails": 0, "affiliate_dropped": 0})
    from concurrent.futures import ThreadPoolExecutor
    import threading
    local = threading.local()

    def _q(item):
        key, a = item
        sess = getattr(local, "s", None) or requests.Session()
        local.s = sess
        m = quality(sess, a, key)
        em = None
        if m["text"] and not refuses_collection(m["text"]):
            lead = {"short_name": m["name"] or key, "phone": None, "umd": None, "sgg": None}
            found = [f for f in score_page(m["text"], m["name"], lead, own=True, postdate=None, require_context=True) if plausible(f[0])]
            if found:
                e, sc, _ = max(found, key=lambda x: x[1])
                em = (e, m.get("about") or a.get("home"), sc)
        return key, a, m, em

    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, a, m, em in ex.map(_q, picked):
            has_email = bool(em and em[2] >= 40)
            score, why = score_channel(a, m, has_email)
            if m["affiliate"] and not (m["newsletter"] or m["own_domain"] and m["about"]):
                st["affiliate_dropped"] += 1
                continue
            name = m["name"] or key
            url = a.get("home") or f"https://{key}"
            row = conn.execute(
                """
                insert into leads (lane, type, name, contact, score, status, source, external_id, short_name, updated_at)
                values ('influencer', 'influencer_tech', %s, %s, %s, 'new', 'web', %s, %s, now())
                on conflict (source, external_id) where external_id is not null
                do update set name = excluded.name, score = excluded.score, updated_at = now(),
                              contact = coalesce(leads.contact, '{}'::jsonb) || jsonb_build_object('url', %s::text)
                returning id
                """,
                (name, jsonb({"url": url}), round(score, 1), key, name, url),
            ).fetchone()
            lid = str(row[0])
            if has_email:
                conn.execute(
                    "update leads set contact = contact || jsonb_build_object('email', %s::text), email_src = %s, email_checked_at = now(), email_depth = 2 "
                    "where id = %s and coalesce(contact->>'email', '') = ''", (em[0], f"auto:{em[1]}", lid))
                st["emails"] += 1
            kind_label = {"own": "자체 블로그", "newsletter": "뉴스레터", "brunch": "브런치", "medium": "미디엄"}.get(a["kind"], a["kind"])
            conn.execute(
                """
                insert into influencer_stats (lead_id, blog_id, hits, best_rank, topics, regions, queries, last_post, influencer, reasons, posts,
                                              buddies, intro, influencer_url, topic_ratio, segment, updated_at)
                values (%s,%s,%s,%s,%s,%s,%s,null,null,%s,%s,null,%s,null,%s,'tech', now())
                on conflict (lead_id) do update set hits = excluded.hits, best_rank = excluded.best_rank, topics = excluded.topics,
                  queries = excluded.queries, reasons = excluded.reasons, posts = excluded.posts, intro = excluded.intro,
                  topic_ratio = excluded.topic_ratio, segment = 'tech', updated_at = now()
                """,
                (lid, key, a["hits"], a["best_rank"], sorted(a["topics"])[:8], [], sorted(a["queries"])[:20], [kind_label] + why,
                 jsonb(a["posts"]), (m["text"] or "")[:300], m.get("tech_ratio")),
            )
            st["saved"] += 1
            if st["saved"] % 50 == 0:
                conn.commit()
                log.info("정성 채널 저장 %d/%d · 메일 %d", st["saved"], len(picked), st["emails"])
    conn.commit()
    log.info("creators %s", st)
    return st
