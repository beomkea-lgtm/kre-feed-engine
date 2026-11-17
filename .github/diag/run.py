"""원모기지: 통화 메모에 사용자가 적어 둔 메일을 리드 메일로 (비어 있을 때만)."""
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    n = c.execute("""update leads set contact = contact || '{"email":"kwonsh0913@naver.com","email_kind":"general"}'::jsonb, email_src = 'manual'
                     where type = 'loan_broker' and name = '원모기지' and coalesce(contact->>'email','') = ''""").rowcount
    print("원모기지", n)
    print("시트 대출모집법인 (곳, 메일, 전화):", c.execute("select count(*), count(*) filter (where coalesce(email,'')<>''), count(*) filter (where coalesce(phone,'')<>'') from sheet_rows where kind='loan'").fetchone())
