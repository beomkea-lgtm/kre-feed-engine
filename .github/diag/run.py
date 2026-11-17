"""대출모집법인: 남의 회사 메일(채용·연구·법무 사이트 등)로 잘못 붙은 값 지우기. 값은 출력하지 않음."""
import os, psycopg
BAD = ["mordorintelligence.com", "rndcircle.io", "jobkorea.co.kr", "barunlaw.com", "wanderloch.com", "saramin.co.kr"]
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    n1 = c.execute("""update leads set contact = contact - 'email' - 'email_kind', email_src = null
                      where type = 'loan_broker' and split_part(contact->>'email', '@', 2) = any(%s)""", (BAD,)).rowcount
    n2 = c.execute("""update loan_contacts set emails = array(select e from unnest(emails) e where split_part(e, '@', 2) <> all(%s))
                      where exists (select 1 from unnest(emails) e where split_part(e, '@', 2) = any(%s))""", (BAD, BAD)).rowcount
    n3 = c.execute("""update email_candidates set status = 'rejected'
                      where split_part(email, '@', 2) = any(%s) and status = 'candidate'""", (BAD,)).rowcount
    print("지운 리드 메일", n1, "· loan_contacts", n2, "· 후보", n3)
    print("시트 대출모집법인 (곳, 메일, 전화):", c.execute("select count(*), count(*) filter (where coalesce(email,'')<>''), count(*) filter (where coalesce(phone,'')<>'') from sheet_rows where kind='loan'").fetchone())
    print("남은 메일 도메인:", c.execute("select split_part(email,'@',2), count(*) from sheet_rows where kind='loan' and coalesce(email,'')<>'' group by 1 order by 2 desc").fetchall())
