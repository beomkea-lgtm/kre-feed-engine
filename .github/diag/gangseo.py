import os, re, requests, psycopg, json
K = os.environ.get("KAKAO_REST_KEY", "")
NID, NSEC = os.environ.get("NAVER_CLIENT_ID", ""), os.environ.get("NAVER_CLIENT_SECRET", "")
T = [("곰돌이공인중개사사무소", "송파구 석촌동", "백제고분로39길 18"),
     ("초록공인중개사사무소", "분당구 서현동", "중앙공원로40번길 42"),
     ("역세권공인중개사사무소", "안성시 공도읍", "용머리큰길 31-45"),
     ("케이파트너스부동산중개법인", "마포구 서교동", "성미산로2길 33")]
def core(n): return re.sub(r"(공인중개사사무소|부동산중개법인|주식회사|\s)", "", n)
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for name, area, road in T:
        out = []
        try:
            r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", params={"query": f"{area} {core(name)}", "size": 5},
                             headers={"Authorization": f"KakaoAK {K}"}, timeout=20).json()
            for d in r.get("documents", []):
                if core(name) in d.get("place_name", "").replace(" ", ""):
                    out.append(f"카카오 {d.get('place_name')} {d.get('phone') or '(번호없음)'} {d.get('road_address_name')}")
        except Exception as e:
            out.append(f"kakao ERR {e}")
        try:
            r = requests.get("https://openapi.naver.com/v1/search/local.json", params={"query": f"{area} {core(name)}", "display": 5},
                             headers={"X-Naver-Client-Id": NID, "X-Naver-Client-Secret": NSEC}, timeout=20).json()
            for it in r.get("items", []):
                t = re.sub("<[^>]+>", "", it.get("title", ""))
                if core(name) in t.replace(" ", ""):
                    out.append(f"네이버 {t} {it.get('telephone') or '(번호없음)'} {it.get('roadAddress')}")
        except Exception as e:
            out.append(f"naver ERR {e}")
        rows = c.execute("select name, contact->>'phone', contact->>'address' from leads where type='broker' and name_core(name)=name_core(%s) and contact->>'address' like %s limit 3",
                         (name, "%" + area.split()[0] + "%")).fetchall()
        out += [f"DB {a} {b} {d}" for a, b, d in rows]
        print(f"::warning title={name}::" + (" || ".join(out) or "없음"))
