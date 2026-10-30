"""커맨드센터용 임시 조회: 특정 지번의 실거래(매매·전월세)를 국토부 API로 바로 뽑아 마크다운으로.

  python -m kre_feed lookup lookups/<이름>.json
  json: {"lawd": "52111", "umd": "서완산동2가", "jibun": "80-135", "months": 12, "types": ["rh","sh","apt","offi","rh_rent","sh_rent","apt_rent","offi_rent"]}
DB 불필요. GitHub 잡(lookup.yml)이 lookups/*.json push 를 보고 돌려서 결과를 runlog 브랜치 lookups/<이름>.md 에 올린다.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import requests

from .molit import ENDPOINTS, parse_response

RENT_ENDPOINTS = {
    "apt_rent": "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
    "rh_rent": "https://apis.data.go.kr/1613000/RTMSDataSvcRHRent/getRTMSDataSvcRHRent",
    "sh_rent": "https://apis.data.go.kr/1613000/RTMSDataSvcSHRent/getRTMSDataSvcSHRent",
    "offi_rent": "https://apis.data.go.kr/1613000/RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent",
}
LABEL = {"apt": "아파트 매매", "rh": "연립다세대 매매", "sh": "단독다가구 매매", "offi": "오피스텔 매매",
         "apt_rent": "아파트 전월세", "rh_rent": "연립다세대 전월세", "sh_rent": "단독다가구 전월세", "offi_rent": "오피스텔 전월세"}


def _endpoint(t: str) -> str:
    if t in RENT_ENDPOINTS:
        return RENT_ENDPOINTS[t]
    return ENDPOINTS["apt_dev"] if t == "apt" else ENDPOINTS[t]


def _months(n: int, today: date | None = None) -> list[str]:
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _g(it: dict, *keys: str) -> str:
    for k in keys:
        v = it.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def run_lookup(req: dict, key: str) -> str:
    lawd, umd, jibun = req["lawd"], req.get("umd", ""), req.get("jibun", "")
    months = int(req.get("months", 12))
    types = req.get("types") or ["rh", "sh", "apt", "offi", "rh_rent", "sh_rent", "apt_rent", "offi_rent"]
    s = requests.Session()
    lines = [f"# 실거래 조회 — LAWD {lawd} · {umd} {jibun} · 최근 {months}개월", ""]
    for t in types:
        rows, errors, calls = [], [], 0
        for ymd in _months(months):
            try:
                r = s.get(_endpoint(t), params={"serviceKey": key, "LAWD_CD": lawd, "DEAL_YMD": ymd, "numOfRows": 1000, "pageNo": 1}, timeout=30)
                calls += 1
                code, msg, items, total = parse_response(r.text)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{ymd}: {type(e).__name__} {e}")
                continue
            if code not in ("000", "00"):
                errors.append(f"{ymd}: {code} {msg}")
                if code in ("30", "31", "32", "33", "22"):
                    break   # 인증·쿼터는 달을 바꿔도 같다
                continue
            for it in items:
                if umd and _g(it, "umdNm", "법정동").replace(" ", "") != umd.replace(" ", ""):
                    continue
                jb = _g(it, "jibun", "지번")
                if jibun and not (jb == jibun or jb.startswith(jibun)):
                    continue
                rows.append(it)
        lines.append(f"## {LABEL.get(t, t)} — {len(rows)}건 (호출 {calls})")
        if errors:
            lines.append("오류: " + " / ".join(errors[:3]))
        if rows:
            is_rent = t.endswith("_rent")
            lines.append("| 계약일 | 건물명 | 지번 | 전용(㎡) | 층 | 건축 | " + ("보증금(만) | 월세(만) | 계약 | 갱신 |" if is_rent else "거래금액(만) | 거래구분 | 해제 |"))
            lines.append("|---|---|---|---|---|---|" + ("---|---|---|---|" if is_rent else "---|---|---|"))
            def k(it):
                return (_g(it, "dealYear", "년"), _g(it, "dealMonth", "월").zfill(2), _g(it, "dealDay", "일").zfill(2))
            for it in sorted(rows, key=k, reverse=True):
                dt = "-".join(k(it))
                name = _g(it, "mhouseNm", "aptNm", "offiNm", "연립다세대", "아파트", "houseType", "주택유형")
                base = f"| {dt} | {name} | {_g(it, 'jibun', '지번')} | {_g(it, 'excluUseAr', 'totalFloorAr', '전용면적', '연면적')} | {_g(it, 'floor', '층')} | {_g(it, 'buildYear', '건축년도')} | "
                if is_rent:
                    lines.append(base + f"{_g(it, 'deposit', '보증금액')} | {_g(it, 'monthlyRent', '월세금액') or '0'} | {_g(it, 'contractType', '계약구분')} | {_g(it, 'useRRRight', '갱신요구권사용')} |")
                else:
                    lines.append(base + f"{_g(it, 'dealAmount', '거래금액')} | {_g(it, 'dealingGbn', '거래유형')} | {_g(it, 'cdealType', '해제여부')} |")
        lines.append("")
    return "\n".join(lines)


def main(path: str) -> int:
    req = json.loads(open(path, encoding="utf-8").read())
    key = os.environ.get("DATA_GO_KR_KEY", "")
    if not key:
        print("DATA_GO_KR_KEY 없음", file=sys.stderr)
        return 2
    from urllib.parse import unquote
    print(run_lookup(req, unquote(key)))
    return 0
