"""대출모집법인 컨택포인트 리서치용 목록 → CSV (암호화해서 저장소에 올리고 커맨드센터가 받아 엑셀로 만든다)."""
import csv, os, sys
import psycopg
Q = """select v.seg, v.core, v.lc_num, array_to_string(v.acts[1:5], ', ') acts,
  coalesce(l.contact->>'address', v.addr) addr, coalesce(l.contact->>'phone', v.tel) place_tel, array_to_string(c.phones, ', ') site_tel,
  c.homepage, array_to_string(c.emails, ', ') emails, c.ceo, c.biz_no,
  l.contact->>'url' kakao_place, s.local->0->>'link' naver_place_link, s.local->0->>'cat' naver_cat,
  c.src::text email_src, s.links::text mention_links, (l.lat is not null) on_map
 from loan_view v left join leads l on l.source='loanreg' and l.external_id=v.lc_num left join loan_contacts c on c.core=v.core
 left join loan_scan s on s.name=v.core
 where v.seg <> '플랫폼'
 order by v.seg, v.core"""
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    cur = c.execute(Q)
    cols = [d.name for d in cur.description]
    rows = cur.fetchall()
with open(sys.argv[1], "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(cols); w.writerows(rows)
print(len(rows))
