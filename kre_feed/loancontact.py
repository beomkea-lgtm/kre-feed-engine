"""대출모집법인 컨택포인트: 홈페이지 → 대표 메일(연락 맥락·자사 도메인만), 대표 전화, 대표자, 사업자번호.
홈페이지 후보: 카카오 장소의 홈페이지 링크 → 네이버 웹 → 다음 웹. 페이지 본문에 법인명이 있어야 그 회사 사이트로 본다.
수집 거부 문구가 있는 페이지는 건너뛴다. 추측 주소는 만들지 않는다. 결과 → loan_contacts, leads(loan_broker).contact 에 합침."""
from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import psycopg
import requests

from .emails import (EMAIL_RE, _contact_links, _context, clean_text, fetch, kakao_homepage, own_domain, plausible,
                     refuses_collection, DaumSearch)
from .enrich import make_naver
from .config import Settings

MG12 = ["파인텍", "보금자리", "제이모기지", "앰지투게더", "소금", "형남이지", "나이스모기지", "누리에프에이", "글로벌라비스", "엠지에프엘",
        "피플스테이션", "휴먼뱅크모기지"]
CEO_RE = re.compile(r"대표(?:이사|자명|자)\s*[:：|]?\s*([가-힣]{2,4})(?![가-힣])")
AGG = re.compile(r"saramin|jobkorea|incruit|albamon|alba\.|wanted|jumpit|catch\.|thevc|innoforest|nicebiz|bizno|localbiz|rndcircle|"
                 r"news|daily|dailian|ilbo|times|press|media|\.go\.kr|\.ac\.kr|wiki|teamblind|zdnet|edaily|mt\.co|hankyung|mk\.co|"
                 r"fntimes|asiae|newsis|yna\.co|joins|chosun|donga|khan|hani|seoul\.co|kfcc|k-lca|loanconsultant|fss\.or|kofia|"
                 r"credit|kisline|dart\.|bizinfo|find|kmong|soomgo|moneyday|bank-?mall|finda|toss|kakaopay|banksalad|lib\.|go\.kr")
BIZ_RE = re.compile(r"사업자(?:등록)?번호\s*[:：|]?\s*(\d{3}-\d{2}-\d{5})")
TEL_RE = re.compile(r"(?:대표\s*(?:전화|번호)|TEL|Tel|전화|고객센터)\s*[:：|.]?\s*(0\d{1,2}[-.)\s]?\d{3,4}[-.\s]?\d{4}|1\d{3}[-.\s]?\d{4})")
CEO_STOP = {"번호", "전화", "이메일", "메일", "상담", "문의", "주소", "이사", "인사말", "인사", "자명", "메시지", "상담사", "방송", "이름으로",
            "님이세요", "인사드", "소개", "약력", "취임", "직속", "명의", "직함", "성명"}


def _tidy(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def run() -> dict:
    s = Settings()
    db = os.environ["DATABASE_URL"]
    nv = make_naver(s)
    daum = DaumSearch(s.kakao_rest_key) if s.kakao_rest_key else None
    with psycopg.connect(db, autocommit=True) as c:
        c.execute("""create table if not exists loan_contacts (core text primary key, lc_num text, homepage text, emails text[], phones text[],
                     ceo text, biz_no text, src jsonb, updated_at timestamptz not null default now())""")
        c.execute("alter table loan_contacts enable row level security")
        rows = c.execute("""select v.core, v.lc_num, l.contact->>'url', l.contact->>'phone'
                            from loan_view v left join leads l on l.source='loanreg' and l.external_id = v.lc_num
                            where v.seg <> '플랫폼' and v.core <> '공간의가치'
                              and (v.seg in ('보험','1금융','신협','기타') or v.core like '%%모기지%%')""").fetchall()
        done = set() if os.environ.get("LOAN_CONTACT_ALL") == "1" else {
            r[0] for r in c.execute("select core from loan_contacts where updated_at > now() - interval '7 days'").fetchall()}
    targets = [(core, lc, purl, ph) for core, lc, purl, ph in rows if core not in done]
    targets += [(n, None, None, None) for n in MG12 if n not in done]
    lock = threading.Lock()
    stats = {"targets": len(targets), "homepage": 0, "email": 0, "ceo": 0, "naver_calls": 0, "naver_quota": False}
    out = []

    def search_links(core: str) -> list[str]:
        links: list[str] = []
        for q in (f"\"{core}\" 대출", f"{core} 대출모집법인"):
            items = []
            if nv is not None and not stats["naver_quota"]:
                try:
                    items = nv.web(q, display=10)
                    with lock:
                        stats["naver_calls"] += 1
                except Exception as e:  # noqa: BLE001
                    r = getattr(e, "response", None)
                    if r is not None and r.status_code in (429, 400, 403):
                        stats["naver_quota"] = True
            if not items and daum is not None:
                try:
                    items = daum.web(q, 10)
                except Exception:  # noqa: BLE001
                    items = []
            for it in items:
                u = it.get("link") or it.get("url") or ""
                t = clean_text(_tidy(it.get("title", "")) + " " + _tidy(it.get("description") or it.get("contents") or ""))
                path = urlparse(u).path.strip("/")
                if own_domain(u) and not AGG.search(u.lower()) and path.count("/") <= 1 and core in t.replace(" ", "") and u not in links:
                    links.append(u)
            if links:
                break
        return links[:3]

    def one(t):
        core, lc, purl, ph = t
        sess = requests.Session()
        cands: list[str] = []
        m = re.search(r"place\.map\.kakao\.com/(\d+)", purl or "")
        if m:
            cands += kakao_homepage(sess, m.group(1))
        if not cands:
            cands += search_links(core)
        home, emails, phones, ceo, biz, src = None, [], [], None, None, []
        for u in [x for x in cands if not AGG.search(x.lower())][:3]:
            raw = fetch(sess, u)
            if not raw:
                continue
            txt = clean_text(raw)
            if core not in txt.replace(" ", ""):
                continue
            if refuses_collection(txt):
                src.append({"url": u, "skip": "수집거부"})
                home = home or u
                continue
            home = home or u
            pages = [(u, raw, txt)]
            for cu in _contact_links(raw, u, limit=3):
                r2 = fetch(sess, cu)
                if r2:
                    t2 = clean_text(r2)
                    if not refuses_collection(t2):
                        pages.append((cu, r2, t2))
            dom = own_domain(u)
            for pu, praw, ptxt in pages:
                low = ptxt.lower()
                for mm in EMAIL_RE.finditer(ptxt):
                    e = mm.group(0).strip(".").lower()
                    if not plausible(e):
                        continue
                    ed = e.split("@")[1]
                    if (dom and (ed == dom or ed.endswith("." + dom))) or _context(low, mm.start(), mm.end()):
                        if e not in emails:
                            emails.append(e)
                            src.append({"email": e, "url": pu})
                for mm in TEL_RE.finditer(ptxt):
                    p = re.sub(r"[^\d]", "-", mm.group(1)).strip("-")
                    if p not in phones:
                        phones.append(p)
                if not ceo:
                    for mm in CEO_RE.finditer(ptxt):
                        if mm.group(1) not in CEO_STOP:
                            ceo = mm.group(1)
                            break
                if not biz:
                    mb = BIZ_RE.search(ptxt)
                    biz = mb.group(1) if mb else None
            break
        if ph and ph not in phones:
            phones.insert(0, ph)
        with lock:
            stats["homepage"] += bool(home)
            stats["email"] += bool(emails)
            stats["ceo"] += bool(ceo)
            out.append((core, lc, home, emails[:5], phones[:4], ceo, biz, json.dumps(src[:8], ensure_ascii=False)))

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, targets))
    with psycopg.connect(db, autocommit=True) as c:
        for core, lc, home, emails, phones, ceo, biz, src in out:
            c.execute("""insert into loan_contacts (core, lc_num, homepage, emails, phones, ceo, biz_no, src, updated_at)
                         values (%s,%s,%s,%s,%s,%s,%s,%s,now()) on conflict (core) do update set lc_num=excluded.lc_num,
                         homepage=coalesce(excluded.homepage, loan_contacts.homepage), emails=excluded.emails, phones=excluded.phones,
                         ceo=coalesce(excluded.ceo, loan_contacts.ceo), biz_no=coalesce(excluded.biz_no, loan_contacts.biz_no),
                         src=excluded.src, updated_at=now()""", (core, lc, home, emails, phones, ceo, biz, src))
            if lc:
                patch = {k: v for k, v in {"homepage": home, "email": emails[0] if emails else None, "ceo": ceo}.items() if v}
                if patch:
                    c.execute("update leads set contact = coalesce(contact,'{}'::jsonb) || %s::jsonb, updated_at=now() "
                              "where source='loanreg' and external_id=%s", (json.dumps(patch, ensure_ascii=False), lc))
    return stats


if __name__ == "__main__":
    st = run()
    print(json.dumps(st, ensure_ascii=False))
    print("::warning title=loancontact::" + json.dumps(st, ensure_ascii=False))
