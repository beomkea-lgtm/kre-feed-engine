"""온라인 제휴 레인: 매수 여정 콘텐츠를 쓰는 네이버 블로거 발굴.

1) 키워드(내집마련·첫집·신혼집·상급지·정책·임장·청약·매수 주의·전세사기 …) × 지역(분당·판교·남양주·노도강 + 활성 지역)으로
   블로그 검색 (네이버 블로그 API, 한도면 카카오(다음) 블로그 API) → 네이버 블로그 글만 → 블로거 단위로 묶는다.
2) 점수: 네이버가 밀어주는 블로거(인플루언서 배지) > 노출력(여러 검색 상위 반복) > 활동성(최근 글) > 연락(프로필 협업 메일) > 지역.
3) 리드(type=influencer, lane=influencer, source=naver_blog, external_id=블로그 ID)로 저장 → 시퀀스·파이프라인·검색 그대로.
   세부 근거는 influencer_stats.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta

import psycopg
import requests

from .config import Settings
from .db import jsonb
from .emails import EMAIL_RE, UA, blog_id, clean_text, fetch, plausible, refuses_collection, score_page
from .regions import SEOUL_SGG, sgg_name

log = logging.getLogger(__name__)

TOPICS = ["내집마련", "첫집 매수", "신혼집 아파트", "상급지 갈아타기", "부동산 정책", "아파트 매수 후기", "임장 후기",
          "청약 당첨 후기", "매매 계약 주의사항", "잔금 등기 셀프", "전세사기 예방", "갭투자 아파트"]
COMBO = ["아파트", "임장", "내집마련", "신혼집", "상급지"]
# ② 잠재 고지불층: 부동산 정보는 덜 소비해도 큰 거래의 안전장치에 돈을 쓰는 테크·투자 관심층
TECH_TOPICS = ["프롭테크", "부동산 데이터 분석", "부동산 앱 추천", "재테크 공부", "재테크 블로그", "미국주식", "미국주식 배당", "ETF 투자",
               "S&P500 적립식", "나스닥 투자", "배당주 투자", "자산관리", "자산배분", "파이어족", "경제적 자유", "월급쟁이 투자",
               "경제 공부", "경제 뉴스 정리", "금리 전망", "환율 투자", "IT 신기술", "AI 활용", "AI 툴 추천", "챗GPT 활용",
               "테크 리뷰", "IT 리뷰", "스타트업", "핀테크", "신기술 트렌드", "반도체 주식", "테슬라 주식", "엔비디아 주식",
               "연금저축 ISA", "절세 재테크", "직장인 투자", "부업 재테크", "가계부 재테크", "부자 습관", "투자 공부", "개발자 재테크"]
FIXED_REGIONS = ["분당", "판교", "남양주", "노도강", "노원", "도봉", "강북", "목동", "마용성", "강남", "송파", "광명", "과천",
                 "수지", "광교", "동탄", "일산", "평촌", "위례", "하남", "구리", "산본", "부천"]
# 이미 본 블로거·채널은 다시 확인하지 않는다 (인플루언서는 배경 채널로 두기로 — 새로 뜬 사람만 본다)

INFLUENCER_RE = re.compile(r'"(?:isInfluencer|influencer)"\s*:\s*true|influencerId"\s*:\s*"[^"]+"')


def region_words(active: list[str] | None) -> list[str]:
    words = list(FIXED_REGIONS)
    for code in (active or []):
        nm = sgg_name(code).split(" ")[-1]
        w = re.sub(r"(시|구|군)$", "", nm) if len(nm) > 2 else nm
        if w and w not in words:
            words.append(w)
    for code in SEOUL_SGG:
        w = sgg_name(code).split(" ")[-1]
        w = re.sub(r"구$", "", w) if len(w) > 2 else w
        if w and w not in words:
            words.append(w)
    return words


def build_queries(active: list[str] | None, limit: int = 220) -> list[tuple[str, str, str | None]]:
    """(검색어, 주제, 지역)"""
    qs: list[tuple[str, str, str | None]] = [(t, t, None) for t in TOPICS]
    for r in region_words(active):
        for t in COMBO:
            qs.append((f"{r} {t}", t, r))
    return qs[:limit]


def _postdate(it: dict) -> date | None:
    pd = (it.get("postdate") or "").replace("-", "")[:8]
    try:
        return datetime.strptime(pd, "%Y%m%d").date() if re.fullmatch(r"\d{8}", pd) else None
    except ValueError:
        return None


def collect(searchers: list, queries: list[tuple[str, str, str | None]], per_query: int = 50, pause: float = 0.12) -> tuple[dict, dict]:
    """블로거별 집계. searchers: blog(q, n) 를 가진 객체들 (앞에서부터, 한도 오류면 다음으로)."""
    agg: dict[str, dict] = {}
    st = {"queries": 0, "posts": 0, "naver_posts": 0, "engine": None, "errors": 0}
    idx = 0
    for q, topic, region in queries:
        items = None
        while idx < len(searchers):
            try:
                items = searchers[idx].blog(q, per_query)
                st["engine"] = type(searchers[idx]).__name__
                break
            except requests.RequestException as e:
                r = getattr(e, "response", None)
                if r is not None and (r.status_code == 429 or (r.status_code in (400, 403) and re.search(r"(?i)quota|limit|exceed", r.text or ""))):
                    log.warning("%s 한도 → 다음 검색엔진", type(searchers[idx]).__name__)
                    idx += 1
                    continue
                st["errors"] += 1
                items = []
                break
        if items is None:
            log.warning("검색엔진 한도 모두 소진 — %d개 검색어에서 멈춤", st["queries"])
            break
        st["queries"] += 1
        for rank, it in enumerate(items, 1):
            st["posts"] += 1
            link = it.get("link") or ""
            bid = blog_id(it.get("bloggerlink") or "") or blog_id(link)
            if not bid:
                continue
            st["naver_posts"] += 1
            a = agg.setdefault(bid, {"name": "", "hits": 0, "queries": set(), "topics": set(), "regions": set(),
                                     "best_rank": 999, "last_post": None, "posts": []})
            a["name"] = a["name"] or clean_text(it.get("bloggername") or "").strip()
            a["hits"] += 1
            a["queries"].add(q)
            a["topics"].add(topic)
            if region:
                a["regions"].add(region)
            a["best_rank"] = min(a["best_rank"], rank)
            pd = _postdate(it)
            if pd and (a["last_post"] is None or pd > a["last_post"]):
                a["last_post"] = pd
            if len(a["posts"]) < 5 and link not in [p["url"] for p in a["posts"]]:
                a["posts"].append({"title": clean_text(it.get("title") or "")[:80], "url": link, "date": pd.isoformat() if pd else None, "q": q, "rank": rank})
        time.sleep(pause)
    return agg, st


BUDDY_RE = re.compile(r"([\d,]+)\s*명의 이웃")
INFL_LINK_RE = re.compile(r"in\.naver\.com/([A-Za-z0-9_\-.]+)")
OG_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')


def blog_meta(session: requests.Session, bid: str) -> dict:
    """모바일 블로그 홈: 이웃 수, 소개글(og:description), 인플루언서 홈 링크. 인플루언서 홈(in.naver.com/{id})도 확인."""
    out = {"influencer": None, "buddies": None, "intro": "", "influencer_url": None}
    raw = fetch(session, f"https://m.blog.naver.com/{bid}")
    if raw:
        m = BUDDY_RE.search(raw)
        out["buddies"] = int(m.group(1).replace(",", "")) if m else None
        m = OG_DESC_RE.search(raw)
        out["intro"] = clean_text(m.group(1)) if m else ""
        m = INFL_LINK_RE.search(raw)
        if m:
            out["influencer"], out["influencer_url"] = True, f"https://in.naver.com/{m.group(1)}"
        else:
            out["influencer"] = False
            # 블로그 페이지엔 공식 배지 표시가 없다 — 소개글에 '인플루언서'라고 적은 경우는 추정으로만
            out["self_declared"] = "인플루언서" in (out["intro"] or "")
    if not out["influencer"]:
        home = fetch(session, f"https://in.naver.com/{bid}")
        if home and "네이버 인플루언서" in home:
            out["influencer"], out["influencer_url"] = True, f"https://in.naver.com/{bid}"
    return out


RE_TECH = re.compile(r"테크|IT|AI|인공지능|챗GPT|GPT|스타트업|핀테크|프롭테크|주식|미국주식|ETF|배당|나스닥|S&P|재테크|투자|코인|비트코인|경제|금리|환율|자산|금융|연금|ISA|절세|반도체|엔비디아|테슬라|애플|리뷰|앱|개발|코딩|파이어|부자", re.I)
RE_TOPIC = re.compile(r"부동산|아파트|매수|매매|임장|청약|내집|내 집|신혼집|첫집|잔금|등기|전세|월세|분양|재건축|재개발|갭투자|상급지|대출|금리|정책|집값|시세|입주|빌라|오피스텔|경매|투자")
RSS_TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)


RSS_DESC_RE = re.compile(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", re.S)
RSS_DATE_RE = re.compile(r"<pubDate>(.*?)</pubDate>")
AFFIL_RE = re.compile(r"쿠팡\s*파트너스|파트너스\s*활동|일정액의\s*수수료|소정의\s*수수료|원고료를\s*받아|제휴\s*마케팅")


def rss_quality(session: requests.Session, bid: str) -> dict:
    """RSS 로 정성 신호: 최근 30일 글 수(꾸준함/공장형), 평균 본문 미리보기 길이(깊이), 제휴·광고 문구 비율."""
    raw = fetch(session, f"https://rss.blog.naver.com/{bid}.xml")
    out = {"posts30": None, "avg_len": None, "affil_ratio": None}
    if not raw:
        return out
    from email.utils import parsedate_to_datetime
    descs = [clean_text(d) for d in RSS_DESC_RE.findall(raw)[1:]][:30]
    dates = []
    for d in RSS_DATE_RE.findall(raw):
        try:
            dates.append(parsedate_to_datetime(d.strip()).date())
        except (TypeError, ValueError):
            pass
    if dates:
        out["posts30"] = sum(1 for d in dates if (date.today() - d).days <= 30)
    if descs:
        out["avg_len"] = sum(len(d) for d in descs) // len(descs)
        out["affil_ratio"] = sum(1 for d in descs if AFFIL_RE.search(d)) / len(descs)
    return out


def topic_ratio(session: requests.Session, bid: str, rx: re.Pattern | None = None) -> tuple[float | None, int]:
    """네이버 블로그 RSS 최근 글 제목 중 부동산·매수 글 비율 (실제 이 주제를 쓰는 블로거인지)."""
    raw = fetch(session, f"https://rss.blog.naver.com/{bid}.xml")
    if not raw:
        return None, 0
    titles = [clean_text(t).strip() for t in RSS_TITLE_RE.findall(raw)[1:]]   # 첫 title 은 블로그 이름
    titles = [t for t in titles if t][:30]
    if not titles:
        return None, 0
    hit = sum(1 for t in titles if (rx or RE_TOPIC).search(t))
    return hit / len(titles), len(titles)


def profile_email(session: requests.Session, bid: str, name: str, intro: str = "") -> tuple[str, str, int] | None:
    """블로그 소개글(협업 문의 메일) → 프로필 소개 페이지 순."""
    lead = {"short_name": name, "phone": None, "umd": None, "sgg": None}
    best = None
    for url, text in ((f"https://m.blog.naver.com/{bid}", intro), (f"https://blog.naver.com/profile/intro.naver?blogId={bid}", None)):
        if text is None:
            raw = fetch(session, url)
            text = clean_text(raw) if raw else ""
        if not text or refuses_collection(text):
            continue
        found = [f for f in score_page(text, name, lead, own=True, postdate=None, require_context=True) if plausible(f[0])]
        if found:
            email, score, _sig = max(found, key=lambda x: x[1])
            if best is None or score > best[2]:
                best = (email, url, score)
            if score >= 40:
                break
    return best


def score_tech(a: dict, influencer: bool | None, has_email: bool, buddies: int | None, self_declared: bool,
               topic_ratio: float | None, q: dict) -> tuple[float, list[str]]:
    """테크·투자(잠재 고지불층): 네이버 인플루언서 최우선, 다음은 정성(깊이·꾸준함·광고 도배 여부·이웃) — 노출·지역은 거의 안 봄."""
    s, why = 0.0, []
    if influencer:
        s += 60; why.append("네이버 인플루언서")
    elif self_declared:
        s += 40; why.append("인플루언서(소개글)")
    if buddies:
        b = 20 if buddies >= 10000 else 14 if buddies >= 3000 else 8 if buddies >= 1000 else 3 if buddies >= 300 else 0
        if b:
            s += b; why.append(f"이웃 {buddies:,}")
    al = q.get("avg_len")
    if al is not None:
        if al >= 180:
            s += 15; why.append("글 깊이")
        elif al < 60:
            s -= 5
    p30 = q.get("posts30")
    if p30 is not None:
        if 3 <= p30 <= 30:
            s += 10; why.append(f"꾸준함(30일 {p30}개)")
        elif p30 > 45:
            s -= 20; why.append(f"공장형(30일 {p30}개)")
    ar = q.get("affil_ratio")
    if ar is not None and ar >= 0.3:
        s -= 30; why.append("제휴수익 글 많음 (감점)")
    if topic_ratio is not None:
        pct = round(topic_ratio * 100)
        if topic_ratio >= 0.5:
            s += 15; why.append(f"테크·투자 글 {pct}%")
        elif topic_ratio >= 0.25:
            s += 8; why.append(f"테크·투자 글 {pct}%")
        else:
            s -= 30; why.append(f"테크·투자 글 {pct}% (주제 다름)")
    if has_email:
        s += 15; why.append("협업 메일")
    if a["hits"] >= 2:
        s += min(a["hits"], 5); why.append(f"검색 노출 {a['hits']}회")
    return s, why


def score_of(a: dict, influencer: bool | None, has_email: bool, today: date | None = None, buddies: int | None = None,
             self_declared: bool = False, topic_ratio: float | None = None, topic_label: str = "부동산") -> tuple[float, list[str]]:
    today = today or date.today()
    s, why = 0.0, []
    if influencer:
        s += 60; why.append("네이버 인플루언서")
    elif self_declared:
        s += 40; why.append("인플루언서(소개글)")
    if buddies:
        b = 20 if buddies >= 10000 else 14 if buddies >= 3000 else 8 if buddies >= 1000 else 3 if buddies >= 300 else 0
        if b:
            s += b; why.append(f"이웃 {buddies:,}")
    exp = min(a["hits"] * 4, 40)
    if exp:
        s += exp; why.append(f"검색 노출 {a['hits']}회")
    if a["best_rank"] <= 3:
        s += 10; why.append(f"최고 {a['best_rank']}위")
    if len(a["topics"]) >= 3:
        s += 5; why.append(f"주제 {len(a['topics'])}개")
    lp = a.get("last_post")
    if lp:
        d = (today - lp).days
        if d <= 30:
            s += 15; why.append("최근 30일 글")
        elif d <= 90:
            s += 8; why.append("최근 90일 글")
    if has_email:
        s += 15; why.append("협업 메일")
    if topic_ratio is not None:
        pct = round(topic_ratio * 100)
        if topic_ratio >= 0.5:
            s += 20; why.append(f"{topic_label} 글 {pct}%")
        elif topic_ratio >= 0.25:
            s += 10; why.append(f"{topic_label} 글 {pct}%")
        else:
            s -= 30; why.append(f"{topic_label} 글 {pct}% (주제 다름)")
    if a["regions"]:
        s += min(len(a["regions"]) * 2, 10); why.append("지역 " + "·".join(sorted(a["regions"])[:3]))
    return s, why


def save(conn: psycopg.Connection, bid: str, a: dict, meta: dict, email: tuple[str, str, int] | None, score: float, why: list[str],
         segment: str = "direct") -> str:
    influencer = meta.get("influencer")
    ltype = "influencer" if segment == "direct" else "influencer_tech"
    url = f"https://blog.naver.com/{bid}"
    contact = {"url": url}
    row = conn.execute(
        """
        insert into leads (lane, type, name, contact, score, status, source, external_id, short_name, updated_at)
        values ('influencer', %s, %s, %s, %s, 'new', 'naver_blog', %s, %s, now())
        on conflict (source, external_id) where external_id is not null
        do update set name = excluded.name, score = excluded.score, updated_at = now(),
                      contact = coalesce(leads.contact, '{}'::jsonb) || jsonb_build_object('url', %s::text)
        returning id
        """,
        (ltype, a["name"] or bid, jsonb(contact), round(score, 1), bid, a["name"] or bid, url),
    ).fetchone()
    lid = str(row[0])
    if email and email[2] >= 40:
        conn.execute(
            "update leads set contact = contact || jsonb_build_object('email', %s::text), email_src = %s, email_checked_at = now(), email_depth = 2 "
            "where id = %s and coalesce(contact->>'email', '') = ''",
            (email[0], f"auto:{email[1]}", lid),
        )
    conn.execute(
        """
        insert into influencer_stats (lead_id, blog_id, hits, best_rank, topics, regions, queries, last_post, influencer, reasons, posts,
                                      buddies, intro, influencer_url, topic_ratio, segment, updated_at)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        on conflict (lead_id) do update set hits = excluded.hits, best_rank = excluded.best_rank, topics = excluded.topics,
          regions = excluded.regions, queries = excluded.queries, last_post = excluded.last_post,
          influencer = coalesce(excluded.influencer, influencer_stats.influencer), reasons = excluded.reasons, posts = excluded.posts,
          buddies = coalesce(excluded.buddies, influencer_stats.buddies), intro = coalesce(nullif(excluded.intro, ''), influencer_stats.intro),
          influencer_url = coalesce(excluded.influencer_url, influencer_stats.influencer_url),
          topic_ratio = coalesce(excluded.topic_ratio, influencer_stats.topic_ratio), segment = excluded.segment, updated_at = now()
        """,
        (lid, bid, a["hits"], a["best_rank"], sorted(a["topics"]), sorted(a["regions"]), sorted(a["queries"])[:30],
         a.get("last_post"), influencer, why, jsonb(a["posts"]), meta.get("buddies"), (meta.get("intro") or "")[:400], meta.get("influencer_url"),
         meta.get("topic_ratio"), segment),
    )
    assign_segment(conn, lid, segment, score)
    return lid


INFL_SEQS = ("influencer_intro", "influencer_tech_intro")


def assign_segment(conn: psycopg.Connection, lid: str, segment: str, score: float) -> str:
    """부동산(direct)·투자경제(tech) 양쪽에 걸린 블로거는 점수 높은 쪽 한 곳에만 둔다.
    이미 둘 중 한 시퀀스를 받은 리드는 바꾸지 않는다 (시퀀스가 두 번 나가지 않게)."""
    col = "score_tech" if segment == "tech" else "score_direct"
    conn.execute(f"update influencer_stats set {col} = %s where lead_id = %s", (round(score, 1), lid))
    row = conn.execute(
        "select l.type, s.score_direct, s.score_tech, "
        "exists (select 1 from enrollments e where e.lead_id = l.id and e.sequence = any(%s)) "
        "from leads l join influencer_stats s on s.lead_id = l.id where l.id = %s", (list(INFL_SEQS), lid)).fetchone()
    if not row:
        return segment
    ltype, sd, stc, enrolled = row
    if enrolled:
        seg = "tech" if ltype == "influencer_tech" else "direct"
    else:
        seg = "tech" if (stc is not None and (sd is None or float(stc) > float(sd))) else "direct"
    win = stc if seg == "tech" else sd
    conn.execute("update influencer_stats set segment = %s where lead_id = %s", (seg, lid))
    conn.execute("update leads set type = %s, score = coalesce(%s, score) where id = %s",
                 ("influencer_tech" if seg == "tech" else "influencer", win, lid))
    return seg


def basket_line(conn: psycopg.Connection, ltype: str = "influencer") -> str:
    """파이프라인 '인플루언서 바구니' 현황 한 줄 (앱이 그대로 읽는 숫자)."""
    n, em, fit, on = conn.execute(
        """
        select count(*), count(*) filter (where coalesce(l.contact->>'email','') <> ''),
               count(*) filter (where coalesce(l.contact->>'email','') <> '' and coalesce(s.topic_ratio, 0) >= 0.25),
               count(*) filter (where exists (select 1 from enrollments e where e.lead_id = l.id and e.status = 'active'))
        from leads l join influencer_stats s on s.lead_id = l.id where l.type = %s
        """, (ltype,),
    ).fetchone()
    return f"바구니[{'직접수요' if ltype == 'influencer' else '테크·투자'}]: 블로거 {n} · 메일 {em} · 발송 대상 {fit} · 시퀀스 진행 {on}"


def run_influencers(conn: psycopg.Connection, s: Settings, searchers: list, active: list[str] | None = None,
                    min_hits: int = 2, max_profiles: int = 2500, segment: str = "direct") -> dict:
    """segment: direct(직접수요 — 매수 여정) | tech(잠재 고지불층 — 테크·IT·재테크·투자)"""
    tech = segment == "tech"
    queries = [(t, t, None) for t in TECH_TOPICS] if tech else build_queries(active)
    rx, label = (RE_TECH, "테크·투자") if tech else (RE_TOPIC, "부동산")
    log.info("인플루언서[%s] 검색어 %d개 (%s)", segment, len(queries), ", ".join(type(x).__name__ for x in searchers))
    agg, st = collect(searchers, queries)
    # 양쪽에 걸린 블로거도 프로필을 보고 점수를 매긴 뒤, 저장할 때 점수 높은 쪽 한 곳으로 정한다 (assign_segment)
    st["bloggers"] = len(agg)
    need = 1 if tech else min_hits   # 테크·투자는 검색어가 적고 노출보다 정성을 보므로 한 번만 걸려도 프로필까지 본다
    picked = sorted(((bid, a) for bid, a in agg.items() if a["hits"] >= need), key=lambda x: (-x[1]["hits"], x[1]["best_rank"]))
    st["picked"] = len(picked)
    st.update({"influencers": 0, "presumed": 0, "emails": 0, "saved": 0})
    # 프로필(이웃 수·소개글·인플루언서 링크·협업 메일)은 블로그마다 2~3페이지 → 8개씩 병렬로 읽고, 저장은 순서대로
    from concurrent.futures import ThreadPoolExecutor
    import threading
    local = threading.local()

    def _profile(item):
        bid, a = item
        sess = getattr(local, "s", None) or requests.Session()
        local.s = sess
        meta = blog_meta(sess, bid)
        meta["topic_ratio"], meta["rss_posts"] = topic_ratio(sess, bid, rx)
        if tech:
            meta["quality"] = rss_quality(sess, bid)
        em = profile_email(sess, bid, a["name"], meta.get("intro") or "")
        return bid, a, meta, em

    # 한 번 본 블로거는 다시 보지 않는다 (매일 3천 명을 처음부터 다시 보느라 90분·네이버 한도를 쓰던 것) — 새 블로거만
    col = "score_tech" if tech else "score_direct"
    recent = {r[0] for r in conn.execute(f"select blog_id from influencer_stats where {col} is not null").fetchall()}
    fresh = [(bid, a) for bid, a in picked if bid not in recent]
    st["skipped_recent"] = len(picked) - len(fresh)
    todo = fresh[:max_profiles]
    log.info("인플루언서[%s] 후보 %d명 중 이미 본 %d명 건너뜀 → 새로 볼 %d명", segment, len(picked), st["skipped_recent"], len(todo))
    with ThreadPoolExecutor(max_workers=8) as ex:
        for bid, a, meta, em in ex.map(_profile, todo):
            if tech:
                score, why = score_tech(a, meta.get("influencer"), bool(em and em[2] >= 40), meta.get("buddies"),
                                        bool(meta.get("self_declared")), meta.get("topic_ratio"), meta.get("quality") or {})
            else:
                score, why = score_of(a, meta.get("influencer"), bool(em and em[2] >= 40), buddies=meta.get("buddies"),
                                      self_declared=bool(meta.get("self_declared")), topic_ratio=meta.get("topic_ratio"), topic_label=label)
            save(conn, bid, a, meta, em, score, why, segment)
            st["saved"] += 1
            st["influencers"] += int(bool(meta.get("influencer")))
            st["presumed"] += int(bool(meta.get("self_declared")))
            st["emails"] += int(bool(em and em[2] >= 40))
            if st["saved"] % 100 == 0:
                conn.commit()
                log.info("인플루언서 저장 %d/%d · 배지 %d · 추정 %d · 메일 %d", st["saved"], len(todo), st["influencers"], st["presumed"], st["emails"])
                log.info("%s", basket_line(conn, "influencer_tech" if tech else "influencer"))
    conn.commit()
    log.info("%s", basket_line(conn, "influencer_tech" if tech else "influencer"))
    log.info("influencers %s", st)
    return st
