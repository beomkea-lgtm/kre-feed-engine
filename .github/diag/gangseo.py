import requests
for op in ("getEBBrokerInfo", "getEBOfficeInfo"):
    for base in ("https://api.vworld.kr/ned/data/", "http://api.vworld.kr/ned/data/"):
        try:
            r = requests.get(base + op, params={"ldCode": "11500", "format": "json", "numOfRows": "3", "pageNo": "1", "key": "TEST"}, timeout=30)
            print(f"::warning title={op}:{base[:5]}::{r.status_code} " + r.text.replace("\n", " ")[:600])
        except Exception as e:
            print(f"::warning title={op}:{base[:5]}::ERR {e}")
