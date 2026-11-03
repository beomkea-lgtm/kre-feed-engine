"""콜·메일 컨택용 목록 → CSV 2개: 대출모집법인(지도·파이프라인 42곳), 중개사·세무사(지도 노출분 = 30일 이슈 분야별 상위 10)."""
import csv, os, sys
import psycopg
LOAN = """select l.id, l.name, l.region_key, l.contact->>'address' addr, l.contact->>'phone' phone, l.contact->>'email' email,
  l.contact->>'homepage' homepage, l.contact->>'ceo' ceo, l.contact->>'seg' seg, array_to_string(array(select jsonb_array_elements_text(l.contact->'contracts')), ', ') contracts,
  array_to_string(c.emails, ', ') emails_all, array_to_string(c.phones, ', ') phones_all,
  lv.final, lv.maemae, lv.daebu, lv.copy_risk, lv.stance, lv.recent, lv.why, lv.conflict, lc.touch_count, lc.last_contact_at, lc.last_channel, lc.last_reply_at, l.contact->>'url' kakao
 from leads l left join loan_verify lv on lv.lc_num = l.external_id left join loan_contacts c on c.lc_num = l.external_id
 left join lead_cards lc on lc.id = l.id
 where l.type='loan_broker' and l.status <> 'hidden'"""
MAP = """with n as (
  select il.lead_id, count(distinct il.issue_id) n_issues, max(i.score) max_issue_score
  from issue_leads il join issues i on i.id=il.issue_id
  where i.created_at > now() - interval '30 days' and i.kind in ('tx_14d','complex_14d','tx_spike','complex_cluster') group by 1)
select m.id, m.type, m.name, m.region_key, l.contact->>'address' addr, m.phone, m.email, m.url kakao, m.rank best_rank, n.n_issues, n.max_issue_score,
  m.issue_headline, array_to_string(m.reasons, ' · ') reasons, m.touch_count, m.last_contact_at, m.last_channel, m.last_reply_at, l.status
 from map_leads m join leads l on l.id=m.id left join n on n.lead_id=m.id
 where m.type in ('broker','tax')"""
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    for q, path in ((LOAN, sys.argv[1]), (MAP, sys.argv[2])):
        cur = c.execute(q); cols = [d.name for d in cur.description]; rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(cols); w.writerows(rows)
        print(path, len(rows))
