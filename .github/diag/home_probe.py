import re, requests
S = requests.Session(); S.headers["User-Agent"] = "Mozilla/5.0"
t = S.get("https://www.data.go.kr/data/15083277/fileData.do", timeout=30).text
for m in re.finditer(r"(?i)(down|atchFile|uddi|publicDataDetailPk|publicDataPk)", t):
    s = t[max(0, m.start()-120): m.end()+160].replace("\n", " ")
    if any(k in s for k in ("onclick", "fn_", "atch", "uddi", "Pk", "href")):
        print("..", s)
