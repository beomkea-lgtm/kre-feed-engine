"""등록 대출모집법인 → 카카오 장소검색으로 사무실 좌표·전화 → leads(type='loan_broker', source='loanreg', external_id=등록번호).
이름이 맞는 장소만 받는다(추측 금지). 네이버 스캔(loan_scan.local)에 주소가 있으면 그걸 먼저 주소 검색으로 쓴다."""
from __future__ import annotations

import json
import os
import re

import psycopg

from .config import Settings
from .enrich import Kakao
from .places import region_from_address

STRIP = re.compile(r"주식회사|유한회사|\(주\)|㈜|주\)|\s|\(.*?\)")


def core(n: str) -> str:
    return STRIP.sub("", n or "")


def seg_of(acts: list[str]) -> str:
    ins = [a for a in acts if re.search(r"생명|해상|화재|손해보험|보험", a)]
    bank = [a for a in acts if a.endswith("은행") and "저축" not in a]
    cu = [a for a in acts if "신협" in a]
    sav = [a for a in acts if "저축" in a]
    if ins:
        return "보험"
    if bank:
        return "1금융" if any(not re.search(r"전북|광주", b) for b in bank) else "지방은행"
    if cu:
        return "신협"
    if sav:
        return "저축은행"
    return "캐피탈·카드"


def run() -> dict:
    s = Settings()
    kk = Kakao(s.kakao_rest_key)
    stats = {"firms": 0, "placed": 0, "by_scan_addr": 0, "no_match": 0, "outside_metro": 0}
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        firms = c.execute("""select f.lc_num, f.name, coalesce(array_agg(k.fin_name) filter (where k.active), '{}')
                             from loan_firms f left join loan_contracts k using (lc_num)
                             where f.n_active > 0 group by f.lc_num, f.name""").fetchall()
        scan = {core(n): loc for n, loc in c.execute("select name, local from loan_scan where local is not null and local::text <> '[]'").fetchall()}
        done = {r[0] for r in c.execute("select external_id from leads where source='loanreg' and lat is not null").fetchall()}
        for lc, name, acts in firms:
            stats["firms"] += 1
            if lc in done:
                continue
            cn = core(name)
            if len(cn) < 2:
                continue
            hit = None
            try:
                docs = kk.keyword(cn, size=15)
            except Exception:
                docs = []
            for d in docs:
                pn = core(d.get("place_name"))
                if cn in pn or (len(pn) >= 3 and pn in cn):
                    hit = d
                    break
            if hit is None and scan.get(cn):
                a = (scan[cn][0] or {}).get("a")
                if a:
                    try:
                        ad = kk.address(a)
                    except Exception:
                        ad = []
                    if ad:
                        hit = {"x": ad[0]["x"], "y": ad[0]["y"], "address_name": ad[0].get("address_name"), "road_address_name": a,
                               "phone": (scan[cn][0] or {}).get("tel"), "place_url": (scan[cn][0] or {}).get("link")}
                        stats["by_scan_addr"] += 1
            if hit is None:
                stats["no_match"] += 1
                continue
            reg = region_from_address(hit.get("address_name"))
            if reg is None:
                stats["outside_metro"] += 1
            rk = reg[0] if reg else "etc|" + (hit.get("address_name") or "").split(" ")[0]
            seg = seg_of(list(acts))
            contact = {"phone": hit.get("phone") or None, "address": hit.get("road_address_name") or hit.get("address_name"),
                       "url": hit.get("place_url"), "lc_num": lc, "seg": seg, "contracts": list(acts)[:40]}
            c.execute("""insert into leads (lane, type, name, region_key, lat, lng, contact, status, source, external_id, updated_at)
                         values ('bank', 'loan_broker', %s, %s, %s, %s, %s, 'new', 'loanreg', %s, now())
                         on conflict (source, external_id) where external_id is not null do update set name=excluded.name,
                         region_key=excluded.region_key, lat=excluded.lat, lng=excluded.lng,
                         contact=coalesce(leads.contact, '{}'::jsonb) || excluded.contact, updated_at=now()""",
                      (STRIP.sub("", name) or name, rk, float(hit["y"]), float(hit["x"]), json.dumps(contact, ensure_ascii=False), lc))
            stats["placed"] += 1
    return stats


if __name__ == "__main__":
    out = run()
    print(json.dumps(out, ensure_ascii=False))
    print("::warning title=loanmap::" + json.dumps(out, ensure_ascii=False))
