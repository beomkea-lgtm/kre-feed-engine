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
                 r"kisline|dart\.|bizinfo|kmong|soomgo|moneyday|bank-?mall|finda|toss|kakaopay|banksalad|lib\.|go\.kr")
BIZ_RE = re.compile(r"사업자(?:등록)?번호\s*[:：|]?\s*(\d{3}-\d{2}-\d{5})")
TEL_RE = re.compile(r"(?:대표\s*(?:전화|번호)|TEL|Tel|전화|고객센터)\s*[:：|.]?\s*(0\d{1,2}[-.)\s]?\d{3,4}[-.\s]?\d{4}|1\d{3}[-.\s]?\d{4})")
CEO_STOP = {"번호", "전화", "이메일", "메일", "상담", "문의", "주소", "이사", "인사말", "인사", "자명", "메시지", "상담사", "방송", "이름으로",
            "님이세요", "인사드", "소개", "약력", "취임", "직속", "명의", "직함", "성명"}


FREE_MAIL = {"naver.com", "hanmail.net", "daum.net", "gmail.com", "nate.com", "kakao.com", "hotmail.com", "outlook.com", "korea.com", "empas.com"}


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
    if os.environ.get("LOAN_CONTACT_SHEET") == "1":   # 시트 '대출모집법인' 줄만 (검증 통과 + 서울·경기 등 42곳 안팎)
        with psycopg.connect(db, autocommit=True) as c:
            targets = [(core or name, lc, purl, ph) for name, core, lc, purl, ph in c.execute(
                """select l.name, v.core, l.external_id, l.contact->>'url', l.contact->>'phone'
                   from leads l left join loan_view v on v.lc_num = l.external_id
                   where l.type = 'loan_broker' and l.status <> 'hidden'""").fetchall()]
    lock = threading.Lock()
    stats = {"targets": len(targets), "homepage": 0, "email": 0, "ceo": 0, "naver_calls": 0, "naver_quota": False}
    out = []

    def search_links(core: str) -> list[str]:
        links: list[str] = []
        for q in (f"\"{core}\" 대출", f"{core} 대출모집법인", f"{core} 모기지 대출상담"):
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
        return links[:5]

    kh = {"Authorization": f"KakaoAK {s.kakao_rest_key}"} if s.kakao_rest_key else None

    def kakao_place(core: str):
        """상호로 카카오 장소 → (전화, 장소 URL). 이름이 들어간 곳만."""
        if not kh:
            return None, None
        for q in (core, f"{core} 대출"):
            try:
                docs = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers=kh,
                                    params={"query": q, "size": 15}, timeout=10).json().get("documents", [])
            except Exception:  # noqa: BLE001
                continue
            hit = [d for d in docs if core in (d.get("place_name") or "").replace(" ", "")]
            if hit:
                d = sorted(hit, key=lambda d: (not d.get("phone"), "본사" not in d["place_name"]))[0]
                return (d.get("phone") or None), d.get("place_url")
        return None, None

    def web_emails(core: str, sess) -> list[tuple[str, str]]:
        """홈페이지에 없을 때: '상호 + 이메일/제휴' 웹 문서·블로그에서, 본문에 상호가 있고 메일 맥락(문의·제휴·이메일)이 있는 주소만."""
        found: list[tuple[str, str]] = []
        for q in (f"\"{core}\" 이메일", f"\"{core}\" 제휴 문의", f"{core} 대출 메일"):
            items = []
            if nv is not None and not stats["naver_quota"]:
                for kind in ("web", "blog"):
                    try:
                        items += getattr(nv, kind)(q, display=10)
                        with lock:
                            stats["naver_calls"] += 1
                    except Exception as e:  # noqa: BLE001
                        r = getattr(e, "response", None)
                        if r is not None and r.status_code in (429, 400, 403):
                            stats["naver_quota"] = True
                        break
            if not items and daum is not None:
                try:
                    items = daum.web(q, 10)
                except Exception:  # noqa: BLE001
                    items = []
            for it in items[:8]:
                u = it.get("link") or it.get("url") or ""
                snip = clean_text(_tidy(it.get("title", "")) + " " + _tidy(it.get("description") or it.get("contents") or ""))
                texts = [(u, snip)]
                if core in snip.replace(" ", ""):
                    raw = fetch(sess, u)
                    if raw:
                        texts.append((u, clean_text(raw)))
                for pu, txt in texts:
                    if core not in txt.replace(" ", "") or refuses_collection(txt):
                        continue
                    low = txt.lower()
                    for mm in EMAIL_RE.finditer(txt):
                        e = mm.group(0).strip(".").lower()
                        # 상호 근처(앞뒤 300자)에 있고 메일 맥락이 있어야 — 다른 회사 주소를 붙이지 않게
                        near = txt[max(0, mm.start() - 300): mm.end() + 300].replace(" ", "")
                        ed = e.split("@")[1]
                        # 무료메일(대표 개인 메일) 이거나, 그 회사 자기 도메인 메일만 — 채용·연구·법무 사이트 등 남의 회사 주소 차단
                        own_ok = bool(own_domain(pu)) and not AGG.search(pu.lower()) and (ed == own_domain(pu) or ed.endswith("." + (own_domain(pu) or "")))
                        if not (ed in FREE_MAIL or own_ok):
                            continue
                        if plausible(e) and core in near and _context(low, mm.start(), mm.end()) and e not in [f[0] for f in found]:
                            found.append((e, pu))
            if found:
                break
        return found[:3]

    def one(t):
        core, lc, purl, ph = t
        sess = requests.Session()
        cands: list[str] = []
        if not ph or not purl:
            kp, ku = kakao_place(core)
            ph = ph or kp
            purl = purl or ku
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
        if not emails:
            for e, pu in web_emails(core, sess):
                emails.append(e)
                src.append({"email": e, "url": pu, "via": "web"})
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
                patch = {k: v for k, v in {"homepage": home, "ceo": ceo}.items() if v}
                if patch:
                    c.execute("update leads set contact = coalesce(contact,'{}'::jsonb) || %s::jsonb, updated_at=now() "
                              "where type='loan_broker' and external_id=%s", (json.dumps(patch, ensure_ascii=False), lc))
                # 메일·전화는 비어 있을 때만 (사람이 넣은 값·통화 메모는 보존)
                if emails:
                    c.execute("""update leads set contact = contact || jsonb_build_object('email', %s::text, 'email_kind', 'general'),
                                 email_src = coalesce(email_src, 'loancontact') where type='loan_broker' and external_id=%s
                                 and coalesce(contact->>'email','') = ''""", (emails[0], lc))
                if phones:
                    c.execute("""update leads set contact = contact || jsonb_build_object('phone', %s::text)
                                 where type='loan_broker' and external_id=%s and coalesce(contact->>'phone','') = ''""", (phones[0], lc))
    return stats


if __name__ == "__main__":
    st = run()
    print(json.dumps(st, ensure_ascii=False))
    print("::warning title=loancontact::" + json.dumps(st, ensure_ascii=False))
