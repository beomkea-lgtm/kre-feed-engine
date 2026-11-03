import os, requests, json
K = os.environ.get("DATA_GO_KR_KEY", "")
B = "https://apis.data.go.kr/1611000/nsdi/EstateBrkpgService/attr/"
tests = [
  ("office_ldcode", B + "getEBOfficeInfo", {"ldCode": "11500", "format": "json", "numOfRows": "3", "pageNo": "1"}),
  ("office_name", B + "getEBOfficeInfo", {"ldCode": "11500", "bsnmCmpnm": "힐스테이트강서탑", "format": "json", "numOfRows": "3", "pageNo": "1"}),
  ("broker_ldcode", B + "getEBBrokerInfo", {"ldCode": "11500", "format": "json", "numOfRows": "5", "pageNo": "1"}),
  ("broker_name", B + "getEBBrokerInfo", {"ldCode": "11500", "bsnmCmpnm": "힐스테이트강서탑", "format": "json", "numOfRows": "10", "pageNo": "1"}),
]
for name, url, p in tests:
    for k in ("serviceKey", "ServiceKey"):
        try:
            r = requests.get(url, params={**p, k: K}, timeout=30)
            txt = r.text.replace("\n", " ")[:900]
            print(f"::warning title={name}:{k}::{r.status_code} {txt}")
            if r.status_code == 200 and "SERVICE" not in txt[:200]:
                break
        except Exception as e:
            print(f"::warning title={name}:{k}::ERR {e}")
