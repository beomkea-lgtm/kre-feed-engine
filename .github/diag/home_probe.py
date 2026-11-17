import re, requests
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"
B = "https://www.data.go.kr"
t = S.get(B + "/data/15083277/fileData.do", timeout=30).text
js = re.findall(r'<script[^>]+src="([^"]+)"', t)
for src in js:
    if "file" in src.lower() or "detail" in src.lower() or "dss" in src.lower():
        u = src if src.startswith("http") else B + src
        try:
            body = S.get(u, timeout=20).text
        except Exception:
            continue
        i = body.find("fn_fileDataDown")
        if i >= 0:
            print("== js", src); print(body[i:i+2500].replace("\n", " "))
            break
i = t.find("fn_fileDataDown")
for m in re.finditer(r"fn_fileDataDown\s*[:=(]\s*function|fn_fileDataDown\s*:\s*\(", t):
    print("== inline", t[m.start():m.start()+2500].replace("\n", " "))
try:
    r = S.post(B + "/tcs/dss/selectFileDataDownload.do", data={"publicDataPk": "15083277", "publicDataDetailPk": "uddi:4f8f48cf-344b-471f-b7f2-c2aa98c210a7", "fileDetailSn": "1"}, timeout=30)
    print("== selectFileDataDownload", r.status_code, r.text[:600])
except Exception as e:
    print("== ERR", e)
