import os, secrets, base64, subprocess, psycopg
p = secrets.token_hex(24)
with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
    c.execute("insert into app_state(key,value) values('stt_pass',%s) on conflict(key) do update set value=excluded.value", (p,))
e = subprocess.run(["openssl","pkeyutl","-encrypt","-pubin","-inkey",".github/export_pub.pem"], input=p.encode(), capture_output=True, check=True).stdout
print("::warning title=sttpass::" + base64.b64encode(e).decode())
