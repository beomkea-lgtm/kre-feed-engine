"""대출모집법인 발굴 (네이버 검색 API 전용, 1회성 집중 스캔)
1) 금융사별·일반 질의로 웹문서·블로그·뉴스·카페를 넓게 훑어 법인명을 뽑는다 (모집·상담사 맥락 안에서만)
2) 뽑힌 법인마다 웹·블로그·뉴스·지역검색으로 신호를 모은다: 위탁 금융사, 비전형 문구, 영업형·대부 문구, 보험·개발(모방 역량) 문구
3) loan_scan 표에 저장. 네이버 한도(429)에 닿으면 그 자리에서 멈추고 몇 번 썼는지 남긴다."""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import psycopg
import requests

HUB = "https://naverapihub.apigw.ntruss.com/search/v1/"
LEG = "https://openapi.naver.com/v1/search/"
EP = {"web": ("webkr", "webkr.json"), "blog": ("blog", "blog.json"), "news": ("news", "news.json"),
      "cafe": ("cafearticle", "cafearticle.json"), "kin": ("kin", "kin.json"), "local": ("local", "local.json")}

BANKS1 = ["KB국민은행", "신한은행", "하나은행", "우리은행", "NH농협은행", "IBK기업은행", "SC제일은행", "Sh수협은행", "iM뱅크", "대구은행",
          "부산은행", "경남은행", "광주은행", "전북은행", "제주은행", "씨티은행"]
FIN2 = ["새마을금고", "신협", "지역농협", "농협 상호금융", "수협 상호금융", "산림조합", "저축은행", "SBI저축은행", "OK저축은행", "웰컴저축은행",
        "페퍼저축은행", "한국투자저축은행", "애큐온저축은행", "다올저축은행", "상상인저축은행", "모아저축은행", "JT친애저축은행", "KB저축은행",
        "신한저축은행", "하나저축은행", "우리금융저축은행", "키움저축은행", "OSB저축은행", "바로저축은행", "대신저축은행", "NH저축은행",
        "캐피탈", "현대캐피탈", "KB캐피탈", "하나캐피탈", "우리금융캐피탈", "JB우리캐피탈", "BNK캐피탈", "iM캐피탈", "롯데캐피탈", "메리츠캐피탈",
        "한국캐피탈", "애큐온캐피탈", "삼성생명", "한화생명", "교보생명", "흥국생명", "미래에셋생명", "삼성화재", "KB손해보험", "DB손해보험", "현대해상"]
BANK_KEYS = {  # 스니펫 안에서 위탁 금융사 판별용
    "KB": ["국민은행", "KB은행", "KB국민"], "신한": ["신한은행"], "하나": ["하나은행", "KEB하나"], "우리": ["우리은행"],
    "농협은행": ["농협은행", "NH농협은행"], "IBK": ["기업은행", "IBK"], "SC": ["SC제일", "제일은행"], "수협": ["수협"],
    "iM/대구": ["대구은행", "iM뱅크", "DGB"], "부산": ["부산은행"], "경남": ["경남은행"], "광주": ["광주은행"], "전북": ["전북은행"],
    "새마을금고": ["새마을금고"], "신협": ["신협"], "지역농협": ["지역농협", "단위농협", "상호금융"], "산림조합": ["산림조합"],
    "저축은행": ["저축은행"], "캐피탈": ["캐피탈"], "보험": ["생명", "화재", "손해보험"], "대부": ["대부"]}
GENERIC = ["대출모집법인", "대출모집법인 대표", "대출모집법인 설립", "대출모집법인 등록", "대출성상품협회 회원사", "대출성상품협회",
           "대출모집인 법인 주택담보대출", "주택담보대출 대출상담사 모집 법인", "모기지 대출상담사 채용", "대출상담사 소속 법인",
           "은행 위탁 대출모집법인", "대출모집법인 우수상담사", "대출모집법인 수수료", "대출모집법인 영업정지", "대출모집법인 MOU",
           "대출모집법인 플랫폼", "대출모집법인 앱", "대출모집법인 비전", "대출모집법인 금융소비자", "대출모집법인 정보 비대칭",
           "주담대 상담사 법인 위탁계약", "아파트담보대출 상담사 소속", "경락잔금대출 대출모집법인", "사업자 주택담보대출 모집법인",
           "대출상담사 협회 등록 법인", "loan consultant 법인 모기지", "모기지 주식회사 대출", "대출모집법인 순위", "대출모집법인 추천",
           "대출상담사 법인 연봉", "대출상담사 법인 후기"]
TEMPLATES = ["{f} 대출모집법인", "{f} 대출상담사 모집", "{f} 위탁 대출상담사 채용"]

SEED_NAMES = ["원모기지", "하나엠엠씨", "에스에이치리더스", "하나피엠씨", "윈모기지", "에이플러스모기지", "하나지엠지", "유모기지", "인카모기지",
              "모기지리더스", "베스트엘씨", "글로벌모기지", "모기지파트너스", "에프씨모기지", "케이플러스모기지", "케이스타모기지", "월드모기지",
              "트러스트모기지", "모기지인사이트", "하나탑스모기지", "하나더모기지", "에이치엔홈앤모기지", "에이스모기지", "동서에프인",
              "대구글로벌모기지", "와이즈모기지", "비에스유니온", "비에스원탑", "제이씨솔루션", "니케벤처스", "뱅크몰", "에이피더핀", "파운트파이낸스",
              "공간의가치", "플러스플랫폼", "파인텍", "제이모기지", "앰지투게더", "형남이지", "나이스모기지", "누리에프에이", "글로벌라비스",
              "엠지에프엘", "피플스테이션", "휴먼뱅크모기지", "홈앤모기지파트너스"]

CTX = re.compile(r"대출\s?모집|대출\s?상담사|모집\s?법인|위탁|대출모집인|모기지")
SUFFIX = r"(?:모기지파트너스|모기지|엠엠씨|피엠씨|지엠지|에프앤아이|에프엔아이|파이낸셜|파이낸스|리더스|금융서비스|금융파트너스|에프씨|엘씨|홀딩스|솔루션|파트너스|론|MMC|PMC|LC)"
RX_CORP = [re.compile(r"(?:㈜|\(주\)|주식회사)\s?([가-힣A-Za-z0-9&]{2,14})"),
           re.compile(r"([가-힣A-Za-z0-9&]{2,14})\s?(?:㈜|\(주\)|주식회사)"),
           re.compile(r"([가-힣A-Za-z]{1,10}" + SUFFIX + r")(?![가-힣])")]
STOP = set("은행 저축은행 캐피탈 대출 대출상담사 모집 법인 대출모집법인 회사 금융 생명 화재 보험 협회 위탁 상담 주담대 고객 당사 본사 지점 "
           "국민은행 신한은행 하나은행 우리은행 농협은행 기업은행 수협은행 부산은행 대구은행 경남은행 광주은행 전북은행 제주은행 새마을금고 신협 "
           "네이버 카카오 토스 핀테크 플랫폼 모기지 파트너스 솔루션 홀딩스 파이낸셜 파이낸스 리더스 금융서비스 론 에프씨 엘씨 한국 대한 서울".split())
VISION = ["정보 비대칭", "정보비대칭", "금융소비자", "소비자 보호", "투명", "비전", "사명", "올바른", "공정", "정직", "신뢰", "금융 교육", "상생", "ESG", "사회공헌"]
AGGR = ["고수익", "억대", "무자본", "누구나", "초보 가능", "당일", "급전", "작업대출", "신용불량", "저신용", "무직자", "한도 최대", "100% 승인", "부결 없"]
DAEBU = ["대부중개", "대부업", "대부 중개", "사금융"]
INS = ["보험대리점", "GA", "법인보험대리점", "보험 교차", "교차판매", "보험설계사"]
DEV = ["개발자", "앱 출시", "플랫폼 출시", "서비스 출시", "특허", "알고리즘", "AI", "빅데이터", "AVM", "시세 산정", "감정평가", "권리분석", "등기"]
TRUST = ["우수", "협회", "부회장", "회장", "표창", "인증", "수상", "MOU", "업무협약"]

_TAG = re.compile(r"<[^>]+>")


def clean(s: str | None) -> str:
    return html.unescape(_TAG.sub("", s or "")).replace(" ", " ").strip()


class Quota(Exception):
    pass


class Nv:
    def __init__(self):
        cid, sec = os.environ.get("NAVER_CLIENT_ID", ""), os.environ.get("NAVER_CLIENT_SECRET", "")
        self.hub = os.environ.get("NAVER_HUB", "true").lower() not in ("0", "false", "no")
        self.h = ({"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec} if self.hub
                  else {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec})
        self.calls = 0
        self.errors = Counter()
        self.quota = False
        self.cap = int(os.environ.get("LOAN_NAVER_CAP", "30000"))
        self.lock = threading.Lock()
        self.dead: set[str] = set()
        self.local = threading.local()

    def _s(self):
        if not hasattr(self.local, "s"):
            self.local.s = requests.Session()
        return self.local.s

    def q(self, kind: str, query: str, display: int = 100, start: int = 1) -> tuple[int, list[dict]]:
        if self.quota or kind in self.dead:
            raise Quota() if self.quota else KeyError(kind)
        with self.lock:
            if self.calls >= self.cap:
                self.quota = True
                raise Quota()
            self.calls += 1
        hub_ep, leg_ep = EP[kind]
        url = HUB + hub_ep if self.hub else LEG + leg_ep
        p = {"query": query, "display": min(display, 5) if kind == "local" else display, "start": start,
             "sort": "random" if kind == "local" else "sim"}
        if self.hub:
            p["format"] = "json"
        for attempt in range(3):
            try:
                r = self._s().get(url, headers=self.h, params=p, timeout=12)
            except requests.RequestException:
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or (r.status_code in (400, 403) and re.search(r"quota|limit|exceed", r.text or "", re.I)):
                if "speed" in (r.text or "").lower() or "rate" in (r.text or "").lower():
                    time.sleep(1.5 + attempt)
                    continue
                self.quota = True
                self.errors[f"{r.status_code}:{(r.text or '')[:80]}"] += 1
                raise Quota()
            if r.status_code == 404:
                self.dead.add(kind)
                raise KeyError(kind)
            if r.status_code >= 400:
                self.errors[f"{kind}:{r.status_code}"] += 1
                return 0, []
            j = r.json()
            return int(j.get("total", 0) or 0), j.get("items", []) or []
        self.errors[f"{kind}:net"] += 1
        return 0, []


def names_in(text: str) -> set[str]:
    out = set()
    for rx in RX_CORP:
        for m in rx.finditer(text):
            n = m.group(1).strip("&")
            if len(n) < 2 or n in STOP or re.fullmatch(r"[0-9A-Za-z]{1,3}", n):
                continue
            if any(n.endswith(b) for b in ("은행", "저축은행", "캐피탈", "생명", "화재", "손해보험", "금고", "협회", "중앙회", "신문", "일보", "뉴스", "경제")):
                continue
            out.add(n)
    return out


def banks_in(text: str) -> set[str]:
    return {k for k, ws in BANK_KEYS.items() if any(w in text for w in ws)}


def hits(text: str, words: list[str]) -> int:
    return sum(text.count(w) for w in words)


def run() -> dict:
    t0 = time.time()
    kst = datetime.now(timezone.utc) + timedelta(hours=9)
    today = kst.date().isoformat()
    nv = Nv()
    db = os.environ["DATABASE_URL"]
    with psycopg.connect(db, autocommit=True) as c:
        st = dict(c.execute("select key, value from app_state where key in (%s, 'naver_quota_day')", (f"naver_used_emails:{today}",)).fetchall())
        c.execute("""create table if not exists loan_scan (
            name text primary key, mentions int, banks jsonb, sig jsonb, local jsonb, links jsonb, totals jsonb,
            score numeric, updated_at timestamptz not null default now())""")
        c.execute("alter table loan_scan enable row level security")
    before = {"emails_used_today": st.get(f"naver_used_emails:{today}"), "quota_day": st.get("naver_quota_day")}

    mention = Counter()
    nbanks: dict[str, Counter] = defaultdict(Counter)
    links: dict[str, list] = defaultdict(list)
    lk = threading.Lock()

    def harvest(kind: str, query: str, pages: int):
        for pg in range(pages):
            try:
                total, items = nv.q(kind, query, 100, 1 + pg * 100)
            except (Quota, KeyError):
                return
            for it in items:
                txt = clean(it.get("title")) + " " + clean(it.get("description"))
                if not CTX.search(txt):
                    continue
                ns = names_in(txt)
                if not ns:
                    continue
                bs = banks_in(txt)
                with lk:
                    for n in ns:
                        mention[n] += 1
                        nbanks[n].update(bs)
                        if len(links[n]) < 4:
                            links[n].append(it.get("link") or it.get("originallink"))
            if total <= (pg + 1) * 100:
                return

    # 1) 넓게 훑기
    jobs = []
    for g in GENERIC:
        for k, pages in (("web", 5), ("blog", 3), ("news", 3), ("cafe", 2)):
            jobs.append((k, g, pages))
    for f in BANKS1 + FIN2:
        for t in TEMPLATES:
            for k, pages in (("web", 2), ("blog", 1), ("news", 1)):
                jobs.append((k, t.format(f=f), pages))
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(lambda j: harvest(*j), jobs))
    p1_calls = nv.calls

    # 2) 법인별 심층
    for n in SEED_NAMES:
        mention[n] += 0
    cand = [n for n, _ in mention.most_common() if mention[n] >= 2 or n in SEED_NAMES]
    cand = list(dict.fromkeys(SEED_NAMES + cand))[: int(os.environ.get("LOAN_MAX_NAMES", "700"))]
    rows = {}

    def deep(n: str):
        sig = Counter()
        bk = Counter(nbanks.get(n, {}))
        tot = {}
        texts = []
        newest = ""
        for kind, q, pages in (("web", f"\"{n}\" 대출", 1), ("news", f"\"{n}\"", 1), ("blog", f"\"{n}\" 대출상담사", 1),
                               ("web", f"\"{n}\" 대표 인터뷰", 1), ("kin", f"\"{n}\"", 1)):
            try:
                total, items = nv.q(kind, q, 100)
            except (Quota, KeyError):
                continue
            tot[kind + (":인터뷰" if "인터뷰" in q else "")] = total
            for it in items:
                txt = clean(it.get("title")) + " " + clean(it.get("description"))
                if n not in txt.replace(" ", "") and n not in txt:
                    continue
                texts.append(txt)
                bk.update(banks_in(txt))
                d = it.get("postdate") or it.get("pubDate") or ""
                if d and str(d) > newest:
                    newest = str(d)
        loc = []
        try:
            _, li = nv.q("local", n, 5)
            loc = [{"t": clean(x.get("title")), "a": x.get("roadAddress") or x.get("address"), "tel": x.get("telephone"),
                    "cat": x.get("category"), "link": x.get("link")} for x in li if n[:2] in clean(x.get("title"))][:3]
        except (Quota, KeyError):
            pass
        blob = " ".join(texts)
        for key, ws in (("vision", VISION), ("aggr", AGGR), ("daebu", DAEBU), ("ins", INS), ("dev", DEV), ("trust", TRUST)):
            sig[key] = hits(blob, ws)
        sig["docs"] = len(texts)
        sig["consultant"] = blob.count("상담사")
        rows[n] = {"mentions": mention.get(n, 0), "banks": dict(bk.most_common(6)), "sig": dict(sig), "local": loc,
                   "links": links.get(n, [])[:4], "totals": tot, "newest": newest[:16]}

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(deep, cand))

    def score(r):
        s = r["sig"]
        fin = sum(v for k, v in r["banks"].items() if k != "대부")
        return round(min(r["mentions"], 30) * 2 + min(fin, 40) + min(s.get("docs", 0), 60) * 0.5 + min(s.get("consultant", 0), 40) * 0.5
                     + s.get("vision", 0) * 3 + s.get("trust", 0) * 2 - s.get("aggr", 0) * 2 - r["banks"].get("대부", 0) * 2, 1)

    with psycopg.connect(db, autocommit=True) as c:
        for n, r in rows.items():
            r["sig"]["newest"] = r.pop("newest")
            c.execute("""insert into loan_scan (name, mentions, banks, sig, local, links, totals, score, updated_at)
                         values (%s,%s,%s,%s,%s,%s,%s,%s,now()) on conflict (name) do update set mentions=excluded.mentions,
                         banks=excluded.banks, sig=excluded.sig, local=excluded.local, links=excluded.links, totals=excluded.totals,
                         score=excluded.score, updated_at=now()""",
                      (n, r["mentions"], json.dumps(r["banks"], ensure_ascii=False), json.dumps(r["sig"], ensure_ascii=False),
                       json.dumps(r["local"], ensure_ascii=False), json.dumps(r["links"], ensure_ascii=False),
                       json.dumps(r["totals"], ensure_ascii=False), score(r)))
        summary = {"naver_before": before, "calls_total": nv.calls, "calls_phase1": p1_calls, "quota_hit": nv.quota,
                   "dead_endpoints": sorted(nv.dead), "errors": dict(nv.errors.most_common(5)), "names_found": len(mention),
                   "deep_scanned": len(rows), "minutes": round((time.time() - t0) / 60, 1)}
        c.execute("insert into app_state (key, value) values ('loan_scan_last', %s) on conflict (key) do update set value=excluded.value",
                  (json.dumps(summary, ensure_ascii=False),))
        if nv.quota:
            c.execute("insert into app_state (key, value) values ('naver_quota_day', %s) on conflict (key) do update set value=excluded.value", (today,))
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
