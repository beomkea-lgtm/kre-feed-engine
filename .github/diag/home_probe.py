"""이사·인테리어 탭 준비 진단 (공개 로그라 메일·전화는 찍지 않는다 — 있음/없음만)."""
import collections, json, os
import psycopg, requests

H = {"Authorization": f"KakaoAK {os.environ['KAKAO_REST_KEY']}"}
CATS = ("이사", "인테리어", "입주", "청소", "홈서비스", "가전", "렌탈", "리빙", "하자")

with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    rows = c.execute("""
      select l.name, l.contact->>'category', coalesce(l.contact->>'email','')<>'' , l.contact->>'email_kind', l.status,
             (select string_agg(e.sequence||':'||e.status||':'||e.step, ',') from enrollments e where e.lead_id=l.id),
             (select string_agg(distinct ev.type, ',') from events ev where ev.lead_id=l.id)
      from leads l where l.type='platform' order by 2, 1""").fetchall()
    print("== 플랫폼 리드 전체", len(rows))
    hit = [r for r in rows if r[1] and any(k in r[1] for k in CATS)]
    print("== 이사·인테리어 관련", len(hit))
    for r in hit:
        print(" |", r[0], "|", r[1], "| 메일" if r[2] else "| 메일X", r[3], "|", r[4], "|", r[5], "|", r[6])
    print("== 플랫폼 카테고리 분포")
    print(collections.Counter(r[1] for r in rows).most_common(80))
    print("== 리드 type 분포", c.execute("select type, count(*) from leads group by 1 order by 2 desc").fetchall())
    print("== app_state 키", [r[0] for r in c.execute("select key from app_state order by 1").fetchall()])
    print("== target_sgg", c.execute("select value from app_state where key='target_sgg'").fetchone())

spots = {"강서 송정": (126.8115, 37.5612), "분당 정자": (127.1086, 37.3670), "송파 잠실": (127.1000, 37.5133)}
qs = ["인테리어", "아파트 인테리어", "리모델링", "포장이사", "이삿짐센터", "입주청소", "도배"]
for spot, (x, y) in spots.items():
    for q in qs:
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers=H,
                         params={"query": q, "x": x, "y": y, "radius": 3000, "size": 15}, timeout=10).json()
        docs = r.get("documents", [])
        cats = collections.Counter(d["category_name"] for d in docs)
        print(f"== {spot} '{q}' total={r.get('meta',{}).get('total_count')} phone={sum(1 for d in docs if d.get('phone'))}/{len(docs)}")
        print("   cats", cats.most_common(6))
        print("   names", [d["place_name"] for d in docs[:8]])
# 전체 서울·경기 규모 감 (사각형 하나 = 서울 전역)
for q in ["인테리어", "포장이사", "입주청소"]:
    r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers=H,
                     params={"query": q, "rect": "126.76,37.42,127.19,37.70", "size": 1}, timeout=10).json()
    print(f"== 서울 사각형 '{q}' total={r.get('meta',{}).get('total_count')} pageable={r.get('meta',{}).get('pageable_count')}")
