"""loan_scan 결과·네이버 사용 현황을 GitHub 경고(annotation)로 남긴다. python .github/loanscan_report.py <part>"""
import json, os, sys
import psycopg
part = int(sys.argv[1]) if len(sys.argv) > 1 else 0
out = []
with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=15) as c:
    if part == 0:
        for k, v in c.execute("select key, value from app_state where key like 'naver%' or key = 'loan_scan_last' order by key desc limit 12").fetchall():
            out.append(f"{k} = {str(v)[:900]}")
        for name, st, upd, body in c.execute("select name, status, updated_at, body from run_logs order by updated_at desc limit 8").fetchall():
            lines = [l for l in (body or "").splitlines() if any(w in l.lower() for w in ("naver", "네이버", "daum", "다음", "quota", "한도", "cap", "checked", "found"))]
            out.append(f"[{name}] {st} {upd:%m-%d %H:%M} :: " + " | ".join(lines[-6:])[:1500])
    else:
        rows = c.execute("select name, score, mentions, banks, sig, local, totals from loan_scan order by score desc nulls last offset %s limit 60", ((part - 1) * 60,)).fetchall()
        buf = ""
        for n, sc, m, b, s, loc, t in rows:
            l0 = (loc or [{}])[0] if loc else {}
            line = (f"{n}|{sc}|m{m}|{','.join(f'{k}{v}' for k, v in (b or {}).items())}|v{s.get('vision',0)} a{s.get('aggr',0)} d{s.get('daebu',0)} "
                    f"i{s.get('ins',0)} dev{s.get('dev',0)} t{s.get('trust',0)} docs{s.get('docs',0)} c{s.get('consultant',0)} new{s.get('newest','')}|"
                    f"{(l0.get('a') or '')[:30]} {l0.get('tel') or ''} {(l0.get('link') or '')[:40]}|tot{json.dumps(t, ensure_ascii=False)}")
            if len(buf) + len(line) > 3800:
                out.append(buf); buf = ""
            buf += line + "\n"
        if buf:
            out.append(buf)
for n in out[:10]:
    print("::warning title=loanscan%d::%s" % (part, n.replace("%", "%25").replace("\r", "").replace("\n", "%0A")))
