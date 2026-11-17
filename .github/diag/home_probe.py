"""국민연금 가입 사업장 데이터 접근 가능 여부 진단 (값·키는 출력하지 않음)."""
import os, re, requests
K = os.environ.get("DATA_GO_KR_KEY", "")
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"
for did in ("15083277",):
    for u in (f"https://www.data.go.kr/data/{did}/fileData.do", f"https://www.data.go.kr/catalog/{did}/fileData.json"):
        try:
            r = S.get(u, timeout=30)
            t = r.text
            print("==", u, r.status_code, len(t))
            m = re.search(r"<title>(.*?)</title>", t, re.S); print("  title", (m.group(1).strip() if m else "")[:120])
            for pat in (r"파일데이터명[^<]*<[^>]*>\s*([^<]+)", r"수정일[^<]*<[^>]*>\s*([^<]+)", r"전체 행[^<]*<[^>]*>\s*([^<]+)"):
                m = re.search(pat, t); print("  ", pat[:8], m.group(1).strip()[:80] if m else None)
            ids = sorted(set(re.findall(r"(FILE_\d+)", t))); print("  atch", ids[:5])
            sn = sorted(set(re.findall(r"fileDetailSn['\"]?\s*[:=,]\s*['\"]?(\d+)", t))); print("  sn", sn[:5])
            dl = re.findall(r"(fileDownload[^\"' ]{0,120})", t); print("  dl", dl[:3])
            if u.endswith(".json"): print("  json", t[:1500])
        except Exception as e:
            print("==", u, "ERR", type(e).__name__, str(e)[:200])
# OpenAPI (활용신청 여부 확인)
for base in ("https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2/getBassInfoSearchV2",
             "https://apis.data.go.kr/B552015/NpsBplcInfoInqireService/getBassInfoSearch"):
    try:
        r = S.get(base, params={"serviceKey": K, "wkpl_nm": "브레이브모바일", "pageNo": 1, "numOfRows": 3, "dataType": "JSON"}, timeout=30)
        print("== api", base.split("/")[-2], r.status_code, r.text[:700].replace(K, "***"))
    except Exception as e:
        print("== api ERR", type(e).__name__, str(e)[:200])
