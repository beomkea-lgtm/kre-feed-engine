"""대출모집법인 전체 목록 → CSV (암호화해서 저장소에 올리고 커맨드센터가 받아 엑셀로 만든다)."""
import csv, os, sys
import psycopg
Q = """select v.seg, v.core, v.lc_num, v.n_acts, array_to_string(v.acts, ', ') acts, round(v.score) sc, v.docs, v.consult, v.vision, v.aggr, v.daebu, v.trust,
 coalesce(l.contact->>'address', v.addr) addr, coalesce(l.contact->>'phone', c.phones[1], v.tel) tel, c.homepage, array_to_string(c.emails, ', ') em, c.ceo,
 h.n_cu_hot, h.hot_trades, array_to_string(h.hot_cus, ', ') hot_cus, (l.lat is not null) on_map, l.region_key
 from loan_view v left join leads l on l.source='loanreg' and l.external_id=v.lc_num left join loan_contacts c on c.core=v.core left join loan_cu_hot h on h.lc_num=v.lc_num
 order by v.seg, v.score desc nulls last"""
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    cur = c.execute(Q)
    cols = [d.name for d in cur.description]
    rows = cur.fetchall()
with open(sys.argv[1], "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(cols); w.writerows(rows)
print(len(rows))
