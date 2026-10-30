"""이슈별 리드 순위 → issue_leads.

점수 (높을수록 먼저):
  단지 매칭   주소에 단지명 +100 / 상호에 단지명 +80      ← 워크인 대장 1순위 신호
  거리        단지(또는 동 거래 중심) 기준 ≤100m +60, ≤200m +45, ≤300m +30, ≤500m +15
  네이버 순위 지역검색 상위 5: 1위 +40, 2위 +32, 3위 +24, 4위 +16, 5위 +8   ← 중개 "{단지명} 부동산"(없으면 "{동} 부동산"), 세무사 "{동} 세무사", 법무사 "{동} 법무사"
  카카오 순위 1위 +12 … 10위 +1 (약한 신호 — 카카오맵은 이 시장에서 리뷰·순위가 잘 안 돈다)
  매물 수     listing_count × 2, 최대 +30            ← 외부 마이닝 CSV로 채움
  블로그      blog_total 5건당 +5, 최대 +20
  1층         +15
빌라 비중  동네 주거용 계약(최근 60일) 중 연립·다세대 비율 s 로 중개사 신호를 나눠 매긴다:
            아파트 신호(단지 매칭·단지 거리·단지 네이버 순위) × (1−s)/m, 빌라 신호 × s/m  (m = max(s, 1−s))
            빌라 신호: 빌라 건물(최근 연립 거래 건물명) 안 사무소 +100 / 상호에 건물명 +80, 상호에 빌라·주택·원룸 등 +40,
                      네이버 "{동} 빌라 부동산" 상위 5 (1위 +40 …)
            → 빌라가 많은 동네일수록 빌라 잘하는 중개사가 위로. 동네·단지 공통 신호(동 네이버·카카오·매물·블로그·1층)는 그대로.
이유(reasons)는 점수 준 항목만 사람이 읽을 문장으로.

카테고리별 상한: 중개(broker) RANK_LEADS_MAX, 제휴(세무사·조합·법무사·플랫폼·인플루언서) RANK_PARTNER_MAX,
금융(은행 지점 등) RANK_FINANCE_MAX. 프론트는 이 셋을 탭으로 나눠 5개씩 보여준다. rank는 전체 점수순.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

import psycopg

from .config import Settings
from .names import lead_matches_complex

log = logging.getLogger(__name__)

FINANCE_TYPES = {"bank_branch", "bank", "lender", "finance", "insurer", "mg", "cu", "nh_coop", "sh_coop", "savings_bank", "forest_coop"}


def lead_category(lead_type: str | None) -> str:
    """프론트 탭과 같은 기준: broker | partner | finance"""
    if lead_type == "broker":
        return "broker"
    if lead_type in FINANCE_TYPES:
        return "finance"
    return "partner"


@dataclass
class Scored:
    lead_id: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    distance_m: int | None = None
    category: str = "broker"


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
    dp = p2 - p1
    dl = math.radians(float(lng2) - float(lng1))
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _dist_points(d: float) -> tuple[float, str] | None:
    if d <= 100:
        return 60, f"{int(d)}m"
    if d <= 200:
        return 45, f"{int(d)}m"
    if d <= 300:
        return 30, f"{int(d)}m"
    if d <= 500:
        return 15, f"{int(d)}m"
    return None


def _kakao_points(rank: int) -> float:
    return max(0.0, 12 - (rank - 1) * 1.2)  # 1위 12 … 10위 1.2, 11위 이후 0


def _naver_points(rank: int) -> float:
    return max(0.0, 48 - rank * 8)          # 1위 40, 2위 32, 3위 24, 4위 16, 5위 8


VILLA_WORDS = re.compile(r"빌라|다세대|연립|주택|원룸|투룸|쓰리룸|신축|하우스|다가구")
VILLA_MIN_SHARE = 0.05   # 이보다 낮으면 빌라 신호는 안 본다


def villa_share(conn: psycopg.Connection, region_key: str, days: int = 60) -> float:
    """동네 주거용 계약 중 연립·다세대 비율. 동 표본이 적으면(10건 미만) 시군구 비율."""
    n_rh, n = conn.execute(
        "select count(*) filter (where prop_type = 'rh'), count(*) from rt_trades "
        "where not cancel_flag and region_key = %s and deal_date >= current_date - %s", (region_key, days),
    ).fetchone()
    if n < 10:
        n_rh, n = conn.execute(
            "select count(*) filter (where prop_type = 'rh'), count(*) from rt_trades "
            "where not cancel_flag and sgg_cd = %s and deal_date >= current_date - %s", (region_key.split("|")[0], days),
        ).fetchone()
    return (n_rh / n) if n else 0.0


def _villa_names(conn: psycopg.Connection, region_key: str, days: int = 60, limit: int = 15) -> list[str]:
    return [r[0] for r in conn.execute(
        "select name from rt_trades where region_key = %s and prop_type = 'rh' and not cancel_flag and name is not null "
        "and deal_date >= current_date - %s group by name order by count(*) desc limit %s", (region_key, days, limit),
    ).fetchall()]


def _issue_anchor(conn: psycopg.Connection, issue: dict) -> tuple[list[str], float | None, float | None, str | None]:
    """(매칭할 단지명들, 기준 lat, lng, 카카오 query_key)"""
    region_key = issue["region_key"]
    ev = issue.get("evidence") or {}
    kind = issue["kind"]
    if kind.startswith("complex"):  # complex_cluster, complex_14d: 단지 하나가 기준점
        name = ev.get("name") or issue["headline"].split(" 최근")[0].split(" ")[-1]
        row = conn.execute("select lat, lng from complexes where region_key=%s and name=%s", (region_key, name)).fetchone()
        lat, lng = (row if row else (None, None))
        return [name], lat, lng, f"complex:{region_key}|{name}"
    # tx_spike 등 동 단위: 최근 14일 계약 상위 단지들 + 그 좌표 평균
    rows = conn.execute(
        """
        select t.name, c.lat, c.lng, count(*) n
        from rt_trades t left join complexes c on c.region_key = t.region_key and c.name = t.name
        where t.region_key = %s and not t.cancel_flag and t.name is not null and t.prop_type <> 'rh'
          and t.deal_date >= current_date - %s
        group by t.name, c.lat, c.lng order by n desc limit 5
        """,
        (region_key, int(issue.get("_days", 14))),
    ).fetchall()
    names = [r[0] for r in rows]
    pts = [(float(r[1]), float(r[2]), r[3]) for r in rows if r[1] is not None]
    if pts:
        w = sum(p[2] for p in pts)
        lat = sum(p[0] * p[2] for p in pts) / w
        lng = sum(p[1] * p[2] for p in pts) / w
    else:
        lat = lng = None
    return names, lat, lng, f"dong:{region_key}"


# 2금융(금고·조합·저축은행)은 독립법인 본점이 의사결정 → 지점 여러 개를 본점 하나로 묶어 본점에 연락
ORG_RE = re.compile(r"^(.*?(?:새마을금고|신용협동조합|신협|축산업협동조합|농업협동조합|축협|농협|수협|저축은행|산림조합))")


def org_of(name: str | None) -> str | None:
    m = ORG_RE.match((name or "").replace(" ", ""))
    return m.group(1) if m else None


def _collapse_finance_to_hq(conn: psycopg.Connection, out: list[Scored], names: dict[str, str]) -> list[Scored]:
    best: dict[str, Scored] = {}
    rest: list[Scored] = []
    for sc in out:
        org = org_of(names.get(sc.lead_id)) if sc.category == "finance" else None
        if not org:
            rest.append(sc)
            continue
        if org not in best or sc.score > best[org].score:
            best[org] = sc
    for org, sc in best.items():
        branch = names.get(sc.lead_id) or ""
        hq = conn.execute(
            "select id::text, name from leads where status <> 'rejected' and replace(name, ' ', '') in (%s, %s) "
            "order by (replace(name, ' ', '') = %s) desc limit 1",
            (org + "본점", org, org + "본점"),
        ).fetchone()
        if hq and hq[0] != sc.lead_id:
            sc.reasons = [f"본점 제안 · 가까운 지점 {branch.replace(org, '').strip() or branch}"] + sc.reasons
            sc.lead_id = hq[0]
        elif hq:
            sc.reasons = ["본점"] + sc.reasons
        else:
            sc.reasons = [f"본점 리드 없음 — {org}"] + sc.reasons
        rest.append(sc)
    return rest


def rank_issue(conn: psycopg.Connection, s: Settings, issue: dict) -> list[Scored]:
    region_key = issue["region_key"]
    if not region_key:
        return []
    names, alat, alng, qkey = _issue_anchor(conn, issue)
    kakao = {str(lid): rk for lid, rk in conn.execute(
        "select lead_id, rank from kakao_ranks where query_key = %s and lead_id is not null", (qkey,)).fetchall()}
    if not kakao and qkey.startswith("complex:"):
        kakao = {str(lid): rk for lid, rk in conn.execute(
            "select lead_id, rank from kakao_ranks where query_key = %s and lead_id is not null", (f"dong:{region_key}",)).fetchall()}
    # 제휴·금융: 동네별 "{검색어}" 카카오 순위 (places 잡이 저장)
    for lid, rk in conn.execute(
        "select lead_id, rank from kakao_ranks where lead_id is not null and query_key = any(%s)",
        ([f"{t}:{region_key}" for t in ("tax", "legal", "mg", "cu", "nh_coop", "ax_coop", "sh_coop", "savings_bank")],),
    ).fetchall():
        kakao.setdefault(str(lid), rk)
    # 네이버 지역검색 순위: 단지 키 → 동 키 → 세무사·법무사 키
    naver: dict[str, int] = {}
    nkeys = [qkey] if qkey.startswith("complex:") else []
    nkeys += [f"dong:{region_key}", f"tax:{region_key}", f"legal:{region_key}"]
    naver_complex: dict[str, int] = {}
    for qk, lid, rk in conn.execute(
        "select query_key, lead_id, rank from naver_ranks where lead_id is not null and rank > 0 and query_key = any(%s)", (nkeys,)
    ).fetchall():
        if qk.startswith("complex:") or str(lid) not in naver:
            naver[str(lid)] = rk
        if qk.startswith("complex:"):
            naver_complex[str(lid)] = rk

    share = villa_share(conn, region_key)
    villa_on = share >= VILLA_MIN_SHARE
    m = max(share, 1 - share)
    w_apt, w_villa = (1 - share) / m, share / m
    vnames = _villa_names(conn, region_key) if villa_on else []
    vnaver = {str(lid): rk for lid, rk in conn.execute(
        "select lead_id, rank from naver_ranks where lead_id is not null and rank > 0 and query_key = %s", (f"villa:{region_key}",),
    ).fetchall()} if villa_on else {}
    complex_naver = qkey.startswith("complex:")

    leads = conn.execute(
        """
        select id, name, contact->>'address', lat, lng, score, blog_total, listing_count, blog_src, type
        from leads where region_key = %s and status <> 'rejected'
        """,
        (region_key,),
    ).fetchall()

    out: list[Scored] = []
    for lid, name, addr, lat, lng, base, blog, listing, blog_src, ltype in leads:
        sc = Scored(lead_id=str(lid), category=lead_category(ltype))
        broker = sc.category == "broker"
        geo = broker or issue.get("_partner_geo", True)   # 콜드비짓: 세무사 등은 단지 근접 대신 검색 순위·블로그로만
        wa = w_apt if (broker and villa_on) else 1.0
        wv = w_villa if (broker and villa_on) else 0.0

        def add(pts: float, reason: str, w: float = 1.0) -> None:
            if pts * w <= 0:
                return
            sc.score += pts * w
            sc.reasons.append(reason)

        # 아파트 신호: 단지명 매칭
        best = None
        for cname in (names if geo else []):
            mm = lead_matches_complex(name, addr, cname)
            if mm and (best is None or mm[0] == "address"):
                best = (mm, cname)
                if mm[0] == "address":
                    break
        if best:
            (where, _tok), cname = best
            add(100 if where == "address" else 80, f"{cname} 단지 내 사무소" if where == "address" else f"상호에 {cname}", wa)
        # 아파트 신호: 단지(또는 동 거래 중심) 거리
        if alat is not None and lat is not None and lng is not None:
            d = haversine_m(alat, alng, lat, lng)
            sc.distance_m = int(d)
            dp = _dist_points(d) if geo else None
            if dp:
                add(dp[0], dp[1], wa)
        # 네이버 지역검색 순위 (큰 신호). 단지 키 순위는 아파트 신호, 동·세무사·법무사 키는 공통
        nrk = naver.get(str(lid))
        if nrk:
            add(_naver_points(nrk), f"네이버 {nrk}위", wa if complex_naver and nrk == naver_complex.get(str(lid)) else 1.0)
        # 빌라 신호 (중개사, 빌라 비중 있는 동네만)
        if wv > 0:
            vbest = None
            for vname in vnames:
                mm = lead_matches_complex(name, addr, vname)
                if mm and (vbest is None or mm[0] == "address"):
                    vbest = (mm, vname)
                    if mm[0] == "address":
                        break
            if vbest:
                (where, _tok), vname = vbest
                add(100 if where == "address" else 80, f"{vname}(빌라) 건물 내 사무소" if where == "address" else f"상호에 {vname}", wv)
            elif name and VILLA_WORDS.search(name):
                add(40, f"빌라 상호({VILLA_WORDS.search(name).group(0)})", wv)
            vrk = vnaver.get(str(lid))
            if vrk:
                add(_naver_points(vrk), f"네이버 빌라 {vrk}위", wv)
            if any(r.startswith(("네이버 빌라", "빌라 상호")) or "(빌라)" in r for r in sc.reasons):
                sc.reasons.append(f"빌라 거래 {round(share * 100)}% 동네")
        # 카카오 순위 (약한 신호)
        rk = kakao.get(str(lid))
        if rk:
            kp = _kakao_points(rk)
            if kp > 0:
                sc.score += kp
                sc.reasons.append(f"카카오 {rk}위")
        # 매물 수
        if listing:
            sc.score += min(listing * 2, 30)
            sc.reasons.append(f"매물 {listing}건")
        # 블로그
        if blog:
            bp = min(blog // 5 * 5, 20)
            if bp:
                sc.score += bp
                sc.reasons.append(f"{'웹 노출' if blog_src == 'daum_web' else '블로그'} {blog}건")
        # 1층
        if base and float(base) >= 1:
            sc.score += 15
            sc.reasons.append("1층")
        out.append(sc)

    if s.rank_finance_hq:
        out = _collapse_finance_to_hq(conn, out, {str(r[0]): r[1] for r in leads})
    out.sort(key=lambda x: (-x.score, x.distance_m if x.distance_m is not None else 10**9))
    if issue.get("_all"):
        return out
    # 카테고리별 상한 (전체 순서는 점수순 유지)
    quota = {"broker": s.rank_leads_max, "partner": s.rank_partner_max, "finance": s.rank_finance_max}
    taken: dict[str, int] = {}
    kept: list[Scored] = []
    for sc in out:
        if taken.get(sc.category, 0) < quota.get(sc.category, 0):
            taken[sc.category] = taken.get(sc.category, 0) + 1
            kept.append(sc)
    return kept


def run_rank(conn: psycopg.Connection, s: Settings, *, rerank_all: bool = False) -> dict:
    """열린 이슈 중 issue_leads가 없는 것(또는 전부)에 순위를 매긴다."""
    q = "select id, kind, headline, evidence, region_key from issues where status = 'open' and (expires_at is null or expires_at > now())"
    if not rerank_all:
        q += " and not exists (select 1 from issue_leads il where il.issue_id = issues.id)"
    issues = [dict(id=r[0], kind=r[1], headline=r[2], evidence=r[3], region_key=r[4]) for r in conn.execute(q).fetchall()]
    n_issues = n_leads = 0
    for issue in issues:
        ranked = rank_issue(conn, s, issue)
        if not ranked:
            continue
        conn.execute("delete from issue_leads where issue_id = %s", (issue["id"],))
        for i, r in enumerate(ranked, 1):
            conn.execute(
                "insert into issue_leads (issue_id, lead_id, rank, score, reasons, distance_m) values (%s,%s,%s,%s,%s,%s)",
                (issue["id"], r.lead_id, i, round(r.score, 1), r.reasons, r.distance_m),
            )
        conn.execute("update issues set related_lead_ids = %s where id = %s", ([r.lead_id for r in ranked], issue["id"]))
        conn.commit()
        n_issues += 1
        n_leads += len(ranked)
    stats = {"issues_ranked": n_issues, "leads_ranked": n_leads}
    log.info("rank %s", stats)
    return stats
