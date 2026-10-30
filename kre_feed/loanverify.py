"""대출중개 리드 검증 (loan_pick 전부): 사람이 하던 검증 기준을 공개 자료로 자동 판정 + 근거 URL.
기준
 1) 3금융 주력 여부: 위탁처에 대부업체가 있거나, 본인 홈페이지·검색글에 대부중개·급전·저신용 문구가 주를 이룸
 2) 매매(주담대) 관련성: 주택담보·아파트·매매·경락·전세 언급 vs 신용대출·자동차 위주
 3) 모방 위험: 자체 앱·플랫폼·AI·특허·시세·감정평가·개발자 채용 언급
 4) 성향: 비전형(정보 비대칭·소비자 보호·내부통제) / 영업형(고수익·억대·급전·무직자)
 5) 최근 활동: 상호가 정확히 들어간 글 중 가장 최근 날짜
 6) 권원보험 충돌: 위탁처가 권원보험 판매처(하나)
검색은 상호가 그대로 들어간 글만 쓴다 (짧은 상호가 다른 회사 글과 섞이는 문제)."""
from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import psycopg
import requests

from .config import Settings
from .emails import DaumSearch, clean_text, fetch
from .enrich import make_naver

KW = {
    "maemae": ["주택담보", "아파트담보", "주담대", "아파트", "매매잔금", "매매", "경락", "전세", "생애최초", "보금자리", "디딤돌", "잔금대출", "후순위"],
    "credit": ["신용대출", "직장인대출", "자동차", "오토론", "개인회생", "햇살론", "비상금"],
    "daebu": ["대부중개", "대부업", "대부 중개", "급전", "당일대출", "무직자", "저신용", "신용불량", "작업대출"],
    "vision": ["정보 비대칭", "정보비대칭", "금융소비자", "소비자 보호", "소비자보호", "내부통제", "수수료를 요구", "수수료 요구", "불법", "투명", "윤리"],
    "aggr": ["고수익", "억대", "무자본", "누구나", "초보 가능", "초보가능", "100% 승인", "부결 없", "한도 최대"],
    "copy": ["앱 출시", "어플", "플랫폼", "AI", "인공지능", "특허", "시세", "감정평가", "AVM", "알고리즘", "개발자", "빅데이터", "권리분석", "등기"],
}
TITLE_SELLERS = ("하나은행", "하나생명", "하나손해보험", "하나캐피탈", "하나저축은행")
DAEBU_FIN = re.compile(r"대부|리드코프")
DATE_RE = re.compile(r"(20\d\d)[.\-/년 ]\s?(\d{1,2})[.\-/월 ]\s?(\d{1,2})")


def _cnt(text: str, words: list[str]) -> int:
    return sum(text.count(w) for w in words)


def run() -> dict:
    s = Settings()
    nv = make_naver(s)
    daum = DaumSearch(s.kakao_rest_key) if s.kakao_rest_key else None
    db = os.environ["DATABASE_URL"]
    with psycopg.connect(db, autocommit=True) as c:
        c.execute("""create table if not exists loan_verify (lc_num text primary key, core text, maemae text, daebu text, copy_risk text,
                     stance text, recent text, conflict text, verdict text, why text, evidence jsonb, updated_at timestamptz not null default now())""")
        c.execute("alter table loan_verify enable row level security")
        rows = c.execute("""select p.lc_num, p.core, v.acts, lc.homepage from loan_pick p join loan_view v on v.lc_num = p.lc_num
                            left join loan_contacts lc on lc.core = p.core""").fetchall()
    stats = {"firms": len(rows), "naver": 0, "naver_quota": False}
    lock = threading.Lock()
    out = []

    def search(q: str) -> list[dict]:
        items = []
        if nv is not None and not stats["naver_quota"]:
            for kind in ("blog", "web"):
                try:
                    items += [dict(i, _src="naver_" + kind) for i in getattr(nv, kind)(q, display=30)]
                    with lock:
                        stats["naver"] += 1
                except Exception as e:  # noqa: BLE001
                    r = getattr(e, "response", None)
                    if r is not None and r.status_code in (429, 400, 403):
                        stats["naver_quota"] = True
                    break
        if daum is not None:
            try:
                items += [dict(i, _src="daum_web") for i in daum.web(q, 30)]
            except Exception:  # noqa: BLE001
                pass
        return items

    def one(row):
        lc, core, acts, home = row
        acts = acts or []
        ev = []
        texts = []
        newest = ""
        # 홈페이지 (자기 소개라 가중치 2배)
        home_txt = ""
        if home:
            raw = fetch(requests.Session(), home)
            if raw:
                home_txt = clean_text(raw)
                if core in home_txt.replace(" ", ""):
                    ev.append({"src": "homepage", "url": home})
                else:
                    home_txt = ""
        # 검색: 상호가 그대로 들어간 글만
        for it in search(f"\"{core}\""):
            title = clean_text(re.sub(r"<[^>]+>", "", it.get("title") or ""))
            desc = clean_text(re.sub(r"<[^>]+>", "", it.get("description") or ""))
            t = title + " " + desc
            if core not in t.replace(" ", ""):
                continue
            texts.append(t)
            d = it.get("postdate") or ""
            if d and len(d) == 8:
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            else:
                m = DATE_RE.search(it.get("datetime") or t)
                d = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
            if d > newest:
                newest = d
            if len(ev) < 6:
                ev.append({"src": it["_src"], "url": it.get("link"), "date": d, "title": title[:60]})
        blob = " ".join(texts) + (" " + home_txt) * 2
        k = {name: _cnt(blob, words) for name, words in KW.items()}
        daebu_fin = [a for a in acts if DAEBU_FIN.search(a)]
        # 판정
        maemae = "높음" if k["maemae"] >= 3 and k["maemae"] >= k["credit"] else ("낮음" if k["credit"] > k["maemae"] else ("?" if not texts and not home_txt else "보통"))
        daebu = "주력 의심" if (daebu_fin and len(daebu_fin) >= len(acts) / 2) or k["daebu"] >= 6 else ("일부" if daebu_fin or k["daebu"] >= 2 else "아님")
        copy = "높음" if k["copy"] >= 8 else ("중간" if k["copy"] >= 3 else "낮음")
        stance = "비전형" if k["vision"] >= 3 and k["aggr"] == 0 else ("영업형" if k["aggr"] >= 2 else ("균형형" if k["vision"] >= 1 else "?"))
        recent = newest or "?"
        conflict = "하나(권원보험 판매처)" if any(a in TITLE_SELLERS for a in acts) else ""
        why = []
        if daebu == "주력 의심":
            verdict = "제외"; why.append("3금융 주력 의심")
        elif conflict:
            verdict = "제외"; why.append("권원보험 판매처 위탁")
        elif copy == "높음":
            verdict = "보류"; why.append("모방 위험")
        elif maemae == "낮음":
            verdict = "보류"; why.append("신용대출 위주")
        elif stance == "영업형":
            verdict = "보류"; why.append("공격적 영업 문구")
        elif recent != "?" and recent < "2025-01-01":
            verdict = "보류"; why.append("최근 활동 없음")
        elif maemae == "?" and recent == "?":
            verdict = "자료 없음"; why.append("공개 자료 거의 없음")
        else:
            verdict = "통과"
        if daebu == "일부":
            why.append("대부 일부")
        why.append(f"매매 {k['maemae']}·신용 {k['credit']}·대부 {k['daebu']}·비전 {k['vision']}·영업 {k['aggr']}·모방 {k['copy']}")
        with lock:
            out.append((lc, core, maemae, daebu, copy, stance, recent, conflict, verdict, " / ".join(why), json.dumps(ev, ensure_ascii=False)))

    with ThreadPoolExecutor(6) as ex:
        list(ex.map(one, rows))
    with psycopg.connect(db, autocommit=True) as c:
        for r in out:
            c.execute("""insert into loan_verify (lc_num, core, maemae, daebu, copy_risk, stance, recent, conflict, verdict, why, evidence, updated_at)
                         values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) on conflict (lc_num) do update set core=excluded.core, maemae=excluded.maemae,
                         daebu=excluded.daebu, copy_risk=excluded.copy_risk, stance=excluded.stance, recent=excluded.recent, conflict=excluded.conflict,
                         verdict=excluded.verdict, why=excluded.why, evidence=excluded.evidence, updated_at=now()""", r)
    from collections import Counter
    stats["verdict"] = dict(Counter(r[8] for r in out))
    return stats


if __name__ == "__main__":
    st = run()
    print(json.dumps(st, ensure_ascii=False))
    print("::warning title=loanverify::" + json.dumps(st, ensure_ascii=False))
