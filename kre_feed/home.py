"""이사·인테리어 탭 (파이프라인 > 간접 정보 제공형 제휴처 > 이사·인테리어).

허수(원룸·전세 이사, 도배 소매 등)가 많아서 '동네'가 아니라 '업체 체급'으로 줄 세운다.
  1) 업종 단계 (매매 근접도): 인테리어 > 포장이사 > 입주청소
  2) 체급 = 국민연금 가입 사업장(공공데이터포털 15083277, 월 1회 갱신)
       - 가입자 수(직원 수) · 적용일자(업력) · 신규/상실(고용 증감)
       - 공개 범위가 '법인 3인 이상 · 개인 10인 이상'이라, 여기 잡히는 것 자체가 체급 문턱
  3) 상호 신호: 아파트·리모델링·올수리(+) / 원룸·용달·지물·설비·상가(−)
  4) 프랜차이즈 대리점·지점(한샘리하우스·LX지인·통인익스프레스 …)은 개별 영업 대신 본사로 묶는다 (brand)
결과 → leads.score, contact.{home_top, home_tier, nps_*, brand, note}.
basket_rows 는 home_top 인 지역 업체 + home_platform 인 플랫폼만 'home' 탭에 보여 준다.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import psycopg
import requests

log = logging.getLogger(__name__)

HOME_TYPES = ("interior", "moving", "clean")
TIER_BASE = {"interior": 30, "moving": 20, "clean": 10}   # 매매 근접도
TIER_LABEL = {"interior": "인테리어", "moving": "포장이사", "clean": "입주청소"}

NAME_PLUS = ("아파트", "리모델링", "올수리", "디자인", "스튜디오", "하우징", "공간", "포장이사", "익스프레스")
NAME_MINUS = ("지물", "도배", "장판", "설비", "바스", "욕실", "타일", "샷시", "새시", "창호", "커튼", "블라인드", "조명",
              "간판", "필름", "싱크", "철거", "용달", "원룸", "소형", "1톤", "화물", "상가", "사무실", "매장", "점포", "카페",
              "전시", "부스", "방수", "페인트", "도장", "에어컨", "보일러", "중문", "유리", "줄눈")
# 상호로 보는 프랜차이즈·체인 (카카오 분류 4단계에 브랜드가 붙는 경우는 자동)
BRANDS = ("한샘", "LX지인", "LX하우시스", "리바트", "집테리어", "아파트멘터리", "통인익스프레스", "통인", "KGB", "영구크린",
          "홈케어2424", "예스2404", "YES2404", "이사이사", "대한익스프레스", "현대이사몰", "아트이사몰", "굿데이이사몰",
          "이사친구", "국민골든팩", "청소연구소", "미소", "에몬스", "까사미아", "이케아", "도배나라", "도배청년단", "다한다")

NPS_PK = "15083277"
DATA_GO = "https://www.data.go.kr"


# ---------------------------------------------------------------- 이름·주소 정규화
_CORP = re.compile(r"\(주\)|㈜|주식회사|유한회사|합자회사|\(유\)|\(합\)|농업회사법인|사단법인")
_BRANCH = re.compile(r"(본점|본사|지점|대리점|직영점|[가-힣A-Za-z0-9]{1,6}점)$")


def norm(name: str | None) -> str:
    s = _CORP.sub("", name or "")
    s = re.sub(r"[\s\-_·.,'\"()\[\]&/]+", "", s).upper()
    return s


def road_key(addr: str | None) -> str | None:
    """'서울 강서구 공항대로 247 3층' → '공항대로247' (도로명+건물번호)"""
    m = re.search(r"([가-힣0-9]+(?:로|길))\s*(\d+(?:-\d+)?)", addr or "")
    return f"{m.group(1)}{m.group(2)}" if m else None


def brand_of(name: str | None, kcat: str | None) -> str | None:
    parts = [p.strip() for p in (kcat or "").split(">")]
    if len(parts) >= 4 and parts[2] in ("인테리어", "이삿짐센터", "청소대행"):
        return parts[3]
    n = (name or "").replace(" ", "")
    for b in BRANDS:
        if b.replace(" ", "") in n and (b not in ("미소", "통인") or _BRANCH.search(n)):
            return b
    return None


# ---------------------------------------------------------------- 국민연금 가입 사업장 파일
def nps_download(dest: Path) -> dict:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    page = s.get(f"{DATA_GO}/data/{NPS_PK}/fileData.do", timeout=60).text
    m = re.search(r'id="publicDataDetailPk"[^>]*value="([^"]+)"', page)
    detail = m.group(1) if m else "uddi:4f8f48cf-344b-471f-b7f2-c2aa98c210a7"
    t = re.search(r"<title>\s*([^<|]+)", page)
    r = s.post(f"{DATA_GO}/tcs/dss/selectFileDataDownload.do", timeout=60,
               data={"publicDataPk": NPS_PK, "publicDataDetailPk": detail, "atchFileId": "", "fileDetailSn": "1", "publicDataTyCode": "PR0051"})
    j = r.json()
    if not j.get("status"):
        raise RuntimeError(f"국민연금 파일 요청 실패: {str(j)[:300]}")
    info = j.get("dataSetFileDetailInfo") or {}
    params = {"atchFileId": j["atchFileId"], "fileDetailSn": j["fileDetailSn"], "dataNm": info.get("dataNm") or ""}
    with s.get(f"{DATA_GO}/cmm/cmm/fileDownload.do", params=params, timeout=600, stream=True) as d:
        d.raise_for_status()
        n = 0
        with open(dest, "wb") as f:
            for chunk in d.iter_content(1 << 20):
                f.write(chunk)
                n += len(chunk)
    head = dest.read_bytes()[:200]
    if head.lstrip().startswith(b"<"):
        raise RuntimeError(f"국민연금 파일 대신 HTML이 옴: {head[:200]!r}")
    return {"file": (t.group(1).strip() if t else ""), "bytes": n, "ctype": d.headers.get("content-type")}


def _open_text(path: Path) -> io.TextIOBase:
    raw = path.read_bytes()
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            raw = z.read(name)
    for enc in ("utf-8-sig", "cp949"):
        try:
            return io.StringIO(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return io.StringIO(raw.decode("cp949", errors="replace"))


def _col(header: list[str], *must: str, avoid: tuple[str, ...] = ()) -> int | None:
    for i, h in enumerate(header):
        hh = h.replace(" ", "")
        if all(m in hh for m in must) and not any(a in hh for a in avoid):
            return i
    return None


def nps_rows(path: Path):
    """서울·경기, 가입 상태(등록)인 사업장만 dict 로."""
    f = _open_text(path)
    rd = csv.reader(f)
    header = next(rd)
    c = {
        "name": _col(header, "사업장명"),
        "state": _col(header, "가입상태"),
        "road": _col(header, "도로명"),
        "jibun": _col(header, "지번"),
        "bjd": _col(header, "법정동주소코드", avoid=("광역", "시군구", "읍면동")) or _col(header, "법정동", "코드", avoid=("광역", "시군구", "읍면동")),
        "sido": _col(header, "광역시도코드"),
        "sgg": _col(header, "시군구코드", avoid=("읍면동",)),
        "form": _col(header, "형태"),
        "ind": _col(header, "업종코드명"),
        "adpt": _col(header, "적용일자"),
        "members": _col(header, "가입자수", avoid=("신규", "상실")),
        "new": _col(header, "신규"),
        "lost": _col(header, "상실"),
    }
    log.info("국민연금 컬럼 %s", {k: (header[v] if v is not None else None) for k, v in c.items()})

    def g(r, k):
        i = c[k]
        return (r[i].strip() if i is not None and i < len(r) else "")

    for r in rd:
        sido = g(r, "sido")
        bjd = g(r, "bjd")
        sgg5 = bjd[:5] if len(bjd) >= 5 else (sido + g(r, "sgg")).strip()
        if not sgg5.startswith(("11", "41")):
            continue
        st = g(r, "state")
        if st and st not in ("1", "등록"):
            continue
        try:
            members = int(float(g(r, "members") or 0))
        except ValueError:
            members = 0
        yield {"name": g(r, "name"), "sgg": sgg5, "road": g(r, "road") or g(r, "jibun"), "form": g(r, "form"),
               "ind": g(r, "ind"), "adpt": g(r, "adpt"), "members": members,
               "new": _int(g(r, "new")), "lost": _int(g(r, "lost"))}


def _int(x: str) -> int:
    try:
        return int(float(x or 0))
    except ValueError:
        return 0


def _years(adpt: str) -> float | None:
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", adpt or "")
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return max(0.0, (date.today() - d).days / 365.25)


# ---------------------------------------------------------------- 매칭 + 점수
def _leads(conn: psycopg.Connection) -> list[dict]:
    out = []
    for lid, ltype, name, rk, addr, kcat, c in conn.execute(
            """select id, type, name, region_key, contact->>'address', contact->>'kcat', contact from leads
               where type = any(%s) and status <> 'rejected'""", (list(HOME_TYPES),)).fetchall():
        c = c or {}
        prev = None
        if c.get("nps_members") is not None:   # 지난 실행의 국민연금 값 (이번에 파일을 못 받았을 때 그대로 씀)
            prev = {"members": int(c["nps_members"]), "adpt": "", "years": c.get("nps_years"), "new": 0, "lost": 0,
                    "form": c.get("nps_form") or "", "ind": c.get("nps_ind") or "", "name": c.get("nps_name") or ""}
        out.append({"id": str(lid), "type": ltype, "name": name or "", "sgg": (rk or "").split("|")[0], "addr": addr,
                    "kcat": kcat, "n": norm(name), "rk": road_key(addr), "prev": prev})
    return out


def match_nps(conn: psycopg.Connection, path: Path) -> dict[str, dict]:
    leads = _leads(conn)
    by_name: dict[tuple[str, str], list[dict]] = defaultdict(list)     # (sgg, 앞 3글자) → 리드
    by_road: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for l in leads:
        if len(l["n"]) >= 2:
            by_name[(l["sgg"], l["n"][:3])].append(l)
        if l["rk"]:
            by_road[(l["sgg"], l["rk"])].append(l)
    best: dict[str, tuple[int, dict]] = {}
    ind = Counter()
    seen = 0
    for row in nps_rows(path):
        seen += 1
        n = norm(row["name"])
        if len(n) < 2:
            continue
        hits: list[tuple[int, dict]] = []
        for l in by_name.get((row["sgg"], n[:3]), []):
            if l["n"] == n:
                hits.append((3, l))
            elif len(l["n"]) >= 3 and (n.startswith(l["n"]) or l["n"].startswith(n)):
                hits.append((2, l))
        rk = road_key(row["road"])
        if rk:
            for l in by_road.get((row["sgg"], rk), []):
                if len(set(_bigrams(l["n"])) & set(_bigrams(n))) >= 1:
                    hits.append((1, l))
        for q, l in hits:
            cur = best.get(l["id"])
            if cur is None or (q, row["members"]) > (cur[0], cur[1]["members"]):
                best[l["id"]] = (q, row)
    for q, row in best.values():
        ind[row["ind"]] += 1
    log.info("국민연금 서울·경기 사업장 %d곳 읽음 · 매칭 %d / 리드 %d", seen, len(best), len(leads))
    log.info("매칭 업종 상위 %s", ind.most_common(25))
    return {lid: {**row, "q": q} for lid, (q, row) in best.items()}


def _bigrams(s: str) -> list[str]:
    return [s[i:i + 2] for i in range(len(s) - 1)] if len(s) >= 2 else []


def score_all(conn: psycopg.Connection, nps: dict[str, dict] | None) -> dict:
    leads = _leads(conn)
    stats = Counter()
    brands = Counter()
    updates = []
    for l in leads:
        ltype, name = l["type"], l["name"]
        reasons: list[str] = [TIER_LABEL[ltype]]
        score = TIER_BASE[ltype]
        minus = [w for w in NAME_MINUS if w in name]
        plus = [w for w in NAME_PLUS if w in name]
        if plus:
            score += 10
        if minus:
            score -= 20
        brand = brand_of(name, l["kcat"])
        n = nps.get(l["id"]) if nps is not None else l["prev"]
        extra: dict = {}
        if n:
            m = n["members"]
            score += 50 if m >= 50 else 40 if m >= 20 else 30 if m >= 10 else 20 if m >= 5 else 10 if m >= 3 else 0
            y = _years(n["adpt"]) if n.get("adpt") else n.get("years")
            if y is not None:
                score += 25 if y >= 10 else 20 if y >= 5 else 10 if y >= 2 else 0
            if n["new"] > n["lost"]:
                score += 5
            form = "법인" if n["form"] in ("1", "법인") else "개인" if n["form"] in ("2", "개인") else n["form"]
            reasons += [f"직원 {m}명", f"업력 {int(y)}년" if y is not None else "", form, n["ind"]]
            extra = {"nps_members": m, "nps_years": round(y, 1) if y is not None else None, "nps_ind": n["ind"], "nps_form": form,
                     "nps_name": n["name"]}
        top = bool(n) and not minus and not brand
        if brand:
            brands[(ltype, brand)] += 1
            reasons.append(f"{brand} 계열 → 본사로")
        stats[f"{ltype}:{'top' if top else 'nps' if n else 'rest'}"] += 1
        note = " · ".join(r for r in reasons if r)
        updates.append((l["id"], score, json.dumps({"home_top": top, "home_tier": ltype, "brand": brand, "note": note,
                                                     **{k: v for k, v in extra.items() if v is not None}}, ensure_ascii=False)))
    for i in range(0, len(updates), 2000):
        chunk = updates[i:i + 2000]
        conn.execute(
            """update leads l set score = v.score::numeric,
                     contact = (coalesce(l.contact, '{}'::jsonb) - 'brand') || jsonb_strip_nulls(v.c::jsonb),
                     updated_at = now()
                from unnest(%s::uuid[], %s::numeric[], %s::text[]) v(id, score, c) where l.id = v.id""",
            ([u[0] for u in chunk], [u[1] for u in chunk], [u[2] for u in chunk]))
    if nps is not None:   # 지난달엔 잡혔는데 이번 달 파일에 없는 곳은 nps_* 를 지운다
        conn.execute("""update leads set contact = contact - 'nps_members' - 'nps_years' - 'nps_ind' - 'nps_form' - 'nps_name'
                        where type = any(%s) and id <> all(%s::uuid[]) and contact ? 'nps_members'""",
                     (list(HOME_TYPES), list(nps.keys())))
    conn.commit()
    log.info("이사·인테리어 점수 %s", dict(stats))
    log.info("프랜차이즈·체인 (지점 수) %s", brands.most_common(40))
    return {"stats": dict(stats), "brands": brands.most_common(40)}


# ---------------------------------------------------------------- 플랫폼 (B2B2C 탭에서 옮겨 옴)
# 이름 앞부분 → 점수 (같은 탭 안에서 지역 업체보다 위). 매매 근접도 순: 인테리어 > 이사 비교 > 홈서비스 > 청소 > 가전·렌탈 > 신축 사전점검
PLATFORM_TIER = [
    (300, ("집닥", "아파트멘터리", "하우스텝", "꾸민", "오늘의집", "집꾸미기", "한샘", "LX지인", "리바트", "인테리어")),
    (250, ("짐싸", "다이사", "이사타임", "이사로", "위매치", "이사", "통인", "익스프레스")),
    (200, ("숨고", "모두연결", "미소", "대리주부")),
    (150, ("클린벨", "청소연구소", "청연", "클린베테랑", "청소")),
    (100, ("렌트리", "가전나우", "렌탈", "가전")),
    (50, ("헬로입주", "채들", "홈체크", "하자렌즈", "아이체크", "사전점검", "하자")),
]
HOME_CATS = ("이사", "인테리어", "입주", "청소", "홈서비스", "가전", "렌탈", "하자", "프랜차이즈")


def tag_platforms(conn: psycopg.Connection) -> int:
    rows = conn.execute("select id, name, contact->>'category' from leads where type = 'platform'").fetchall()
    n = 0
    for lid, name, cat in rows:
        cat = cat or ""
        if not any(k in cat for k in HOME_CATS) or "코리빙" in cat or "1인가구" in cat:
            continue
        sc = next((s for s, keys in PLATFORM_TIER if any(k in (name or "") for k in keys)), None)
        if sc is None:
            sc = next((s for s, keys in PLATFORM_TIER if any(k in cat for k in keys)), 100)
        conn.execute("""update leads set score = %s, contact = coalesce(contact,'{}'::jsonb) || '{"home_platform": true}'::jsonb
                        where id = %s""", (sc, lid))
        n += 1
    conn.commit()
    log.info("이사·인테리어 플랫폼 %d곳 → home 탭", n)
    return n


def import_home_csv(conn: psycopg.Connection, path: str) -> dict:
    """Claude가 찾은 이사·인테리어 플랫폼·프랜차이즈 본사 CSV (name,category,homepage,email,email_kind,phone,source_url,note)
    → leads(type=platform, contact.home_platform). 기존 플랫폼 리드와 같은 키(도메인/이름)면 합친다."""
    from .handoff import _domain   # 같은 회사 키 규칙
    st = Counter()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            st["rows"] += 1
            email = (r.get("email") or "").strip().lower()
            if email and not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", email):
                email = ""
            kind = (r.get("email_kind") or "").strip() or ("general" if email else "none")
            key = _domain(r.get("homepage")) or re.sub(r"\s*\(.*?\)\s*", "", name).strip().lower()
            contact = {k: v for k, v in {"email": email or None, "url": (r.get("homepage") or "").strip() or None,
                                          "phone": (r.get("phone") or "").strip() or None, "note": (r.get("note") or "").strip() or None,
                                          "category": (r.get("category") or "").strip() or None, "email_kind": kind,
                                          "home_platform": True}.items() if v is not None}
            src = (r.get("source_url") or "").strip()
            # 메일은 기존 값이 있으면 덮지 않는다 (사람이 고친 값 보존)
            conn.execute(
                """insert into leads (lane, type, name, contact, status, source, external_id, email_src)
                   values ('b2b2c','platform',%s,%s::jsonb,'new','claude_platform',%s,%s)
                   on conflict (source, external_id) where external_id is not null do update set
                     name = excluded.name,
                     contact = excluded.contact || leads.contact
                               || jsonb_build_object('home_platform', true)
                               || case when coalesce(leads.contact->>'email','') = '' and excluded.contact ? 'email'
                                       then jsonb_build_object('email', excluded.contact->>'email', 'email_kind', excluded.contact->>'email_kind')
                                       else '{}'::jsonb end
                               || case when coalesce(leads.contact->>'phone','') = '' and excluded.contact ? 'phone'
                                       then jsonb_build_object('phone', excluded.contact->>'phone') else '{}'::jsonb end,
                     updated_at = now()""",
                (name, json.dumps(contact, ensure_ascii=False), key, f"claude:{src}" if email else None))
            st["saved"] += 1
            st["email"] += bool(email)
            st["phone"] += bool(contact.get("phone"))
    conn.commit()
    log.info("이사·인테리어 플랫폼·본사 반영 %s", dict(st))
    return dict(st)


def seed_sequence(conn: psycopg.Connection) -> None:
    """templates/home_intro/*.md → sequence_steps (앱에서 고쳤으면 그대로 둔다). 1단계엔 '제휴 안내' PDF 첨부."""
    from .mailer import load_template, list_sequences
    n = list_sequences().get("home_intro", 0)
    for step in range(n):
        t = load_template("home_intro", step)
        if not t:
            continue
        conn.execute("""insert into sequence_steps (sequence, step, channel, gap_days, subject, body)
                        values ('home_intro', %s, 'email', %s, %s, %s) on conflict (sequence, step) do nothing""",
                     (step, 0 if step == 0 else 4, t[0], t[1]))
    conn.execute(
        """update sequence_steps set attachments = coalesce((select jsonb_agg(id) from mail_files where name like '%%제휴 안내%%'), '[]'::jsonb)
           where sequence = 'home_intro' and step = 0 and attachments = '[]'::jsonb""")
    conn.commit()


def summary(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        """select coalesce(contact->>'home_tier', 'platform'), count(*),
                  count(*) filter (where coalesce(contact->>'email','') <> ''),
                  count(*) filter (where coalesce(contact->>'phone','') <> '')
           from leads where (type = any(%s) and contact->>'home_top' = 'true')
                         or (type = 'platform' and contact->>'home_platform' = 'true')
           group by 1 order by 1""", (list(HOME_TYPES),)).fetchall()
    log.info("이사·인테리어 탭 (유형, 곳, 메일, 전화) %s", rows)
    tot = conn.execute("select type, count(*) from leads where type = any(%s) group by 1", (list(HOME_TYPES),)).fetchall()
    log.info("수집 전체 %s", tot)
    sz = conn.execute(
        """select contact->>'home_tier',
                  count(*) filter (where (contact->>'nps_members')::int between 3 and 9),
                  count(*) filter (where (contact->>'nps_members')::int between 10 and 19),
                  count(*) filter (where (contact->>'nps_members')::int between 20 and 49),
                  count(*) filter (where (contact->>'nps_members')::int >= 50)
           from leads where type = any(%s) and contact->>'home_top' = 'true' group by 1""", (list(HOME_TYPES),)).fetchall()
    log.info("탭 노출 직원 규모 (유형, 3~9, 10~19, 20~49, 50+) %s", sz)


def run_home(conn: psycopg.Connection, nps_file: str | None = None) -> dict:
    tag_platforms(conn)
    seed_sequence(conn)
    nps = None
    path = Path(nps_file) if nps_file else Path("/tmp/nps.csv")
    try:
        if not nps_file:
            log.info("국민연금 파일 받기 %s", nps_download(path))
        nps = match_nps(conn, path)
    except Exception as e:   # 파일이 안 받아져도 상호 신호 점수는 갱신 (기존 국민연금 값 유지)
        log.warning("국민연금 매칭 건너뜀: %s: %s", type(e).__name__, str(e)[:300])
    out = score_all(conn, nps)
    summary(conn)
    return out
