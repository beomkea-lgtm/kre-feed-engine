"""은행연합회 대출성 금융상품판매대리·중개업자 통합조회 → 등록 법인 전체 + 위탁계약 금융사.
loan_firms(lc_num, name, fss, n_active, updated_at) / loan_contracts(lc_num, fin_name, con_date, cancel_date, active)"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import requests

BASE = "https://www.loanconsultant.or.kr"
URL = BASE + "/loanconsultant/viewConsultantDetail.do"
TOKENS = (["주식회사", "㈜", "유한", "모기지", "론", "금융", "파이낸", "파트너스", "에셋", "컨설팅", "플러스", "리더스", "엠씨", "엘씨", "에프",
           "홀딩스", "솔루션", "뱅크", "머니", "캐피탈", "크레딧", "인베스트", "네트웍스", "코리아", "코퍼레이션", "앤", "투", "원", "더", "케이",
           "에이", "비", "씨", "디", "이", "지", "에스", "티", "제이", "엠", "와이", "핀", "오", "아이", "엔", "알", "유", "피", "큐", "에이치",
           "브이", "글로벌", "스타", "드림", "한", "대", "신", "하나", "우리"]
          + list("가나다라마바사아자차카타파하고노도로모보소조초코토포호구누두루무부수우주추쿠투푸후기니리미시치키히그느드르므브스으즈츠크트프흐"
                 "개내래매배새재해게네레메베세제헤강남당랑망방상장창탕팡항경명성영정청평형민빈인진린선연전현준윤운문순은금림임김박최")
          + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789&"))


def _sess() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest", "Referer": BASE + "/loan_main.do"})
    s.get(BASE + "/loan_main.do", timeout=20)
    return s


def _post(s: requests.Session, data: dict) -> dict:
    for i in range(4):
        try:
            r = s.post(URL, data=data, timeout=20)
            if r.status_code == 200:
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        time.sleep(1 + i)
    return {}


def run() -> dict:
    s = _sess()
    corps: dict[str, str] = {}
    for t in TOKENS:
        j = _post(s, {"corp_name": t, "gubun": "C"})
        for c in (j.get("DetailCorp") or j.get("Detail") or []):
            corps[c["corpLcNum"]] = c["corpName"]
    det: dict[str, dict] = {}

    def one(lc: str):
        j = _post(s, {"corp_lc_num": lc, "gubun": "C"})
        det[lc] = j

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, list(corps)))
    rows, cons = [], []
    for lc, name in corps.items():
        j = det.get(lc) or {}
        bl = j.get("bankList") or []
        act = 0
        for b in bl:
            cd = (b.get("cancelDate") or "").replace(".", "").strip()
            a = cd == ""
            act += a
            cons.append((lc, b.get("finInfoName"), b.get("conDate"), b.get("cancelDate"), a))
        rows.append((lc, name, j.get("fssCorpLcYn"), act, len(j.get("vioList") or [])))
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute("""create table if not exists loan_firms (lc_num text primary key, name text, fss text, n_active int, n_vio int,
                     updated_at timestamptz not null default now())""")
        c.execute("""create table if not exists loan_contracts (lc_num text, fin_name text, con_date text, cancel_date text, active boolean,
                     primary key (lc_num, fin_name, con_date))""")
        c.execute("alter table loan_firms enable row level security")
        c.execute("alter table loan_contracts enable row level security")
        if rows:
            with c.cursor() as cur:
                cur.executemany("""insert into loan_firms (lc_num, name, fss, n_active, n_vio, updated_at) values (%s,%s,%s,%s,%s,now())
                                   on conflict (lc_num) do update set name=excluded.name, fss=excluded.fss, n_active=excluded.n_active,
                                   n_vio=excluded.n_vio, updated_at=now()""", rows)
                cur.execute("delete from loan_contracts where lc_num = any(%s)", ([r[0] for r in rows],))
                cur.executemany("""insert into loan_contracts (lc_num, fin_name, con_date, cancel_date, active) values (%s,%s,%s,%s,%s)
                                   on conflict do nothing""", cons)
    return {"firms": len(rows), "with_detail": sum(1 for v in det.values() if v), "contracts": len(cons),
            "active_contracts": sum(1 for x in cons if x[4])}


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, ensure_ascii=False))
    print("::warning title=loanreg::" + json.dumps(out, ensure_ascii=False))
