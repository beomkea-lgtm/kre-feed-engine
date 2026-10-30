"""DATABASE_URL 진단: 값은 절대 출력하지 않고 모양만 GitHub 경고(annotation)로 남긴다."""
import os
import re
from urllib.parse import urlsplit

import psycopg

u = os.environ.get("DATABASE_URL", "")
notes = []
if not u:
    notes.append("DATABASE_URL 비어 있음")
else:
    notes.append(f"길이 {len(u)} · 앞뒤 공백 {u != u.strip()} · 대괄호 {'[' in u or ']' in u} · '@' 개수 {u.count('@')}")
    try:
        p = urlsplit(u.strip())
        pw = p.password or ""
        specials = sorted(set(re.sub(r"[A-Za-z0-9]", "", pw)))
        notes.append(f"scheme {p.scheme} · host {p.hostname} · port {p.port} · user {p.username} · db {p.path} · 비번 길이 {len(pw)} · 비번 특수문자 {''.join(specials) or '없음'}")
    except Exception as e:
        notes.append(f"주소 해석 실패: {type(e).__name__}")
    try:
        with psycopg.connect(u.strip(), connect_timeout=10) as c:
            notes.append("접속 OK: " + str(c.execute("select current_user").fetchone()[0]))
    except Exception as e:
        msg = str(e)
        if p.password:
            msg = msg.replace(p.password, "***")
        notes.append(f"접속 실패 {type(e).__name__}: {msg[:300]}".replace("\n", " "))
for n in notes:
    print(f"::warning title=dbcheck::{n}")
