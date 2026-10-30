"""커맨드센터(Claude) 직접 검색용 주고받기.

export-top <csv>   : 열린 이슈의 탭별 상위 5곳(중개·제휴·금융) 중 메일 없는 리드 → CSV (runlog 브랜치 exports/top_leads.csv 로 올라감)
import-emails <csv>: Claude가 찾은 메일 CSV(lead_id,email,source_url,note) → email_candidates(source_kind='claude_search') + leads.contact.email
"""
from __future__ import annotations

import csv
import json
import logging
import re

import psycopg

from .emails import plausible
from .regions import region_label

log = logging.getLogger(__name__)

FIN = ("mg", "cu", "nh_coop", "sh_coop", "savings_bank", "forest_coop", "bank_branch")


def export_top(conn: psycopg.Connection, path: str, per_tab: int = 5) -> int:
    rows = conn.execute(
        """
        with ranked as (
          select i.id issue_id, i.headline, l.id lead_id, l.name, l.type, l.region_key, l.contact, il.rank,
                 case when l.type = 'broker' then 'broker' when l.type = any(%(fin)s) then 'finance' else 'partner' end cat,
                 row_number() over (partition by i.id,
                   case when l.type = 'broker' then 'broker' when l.type = any(%(fin)s) then 'finance' else 'partner' end
                   order by il.rank) as pos
          from issues i
          join issue_leads il on il.issue_id = i.id
          join leads l on l.id = il.lead_id
          where i.status = 'open' and (i.expires_at is null or i.expires_at > now())
        )
        select distinct on (lead_id) lead_id, name, type, region_key, contact->>'phone', contact->>'address', contact->>'url', headline, pos
        from ranked
        where pos <= %(n)s and coalesce(contact->>'email', '') = ''
          and not exists (select 1 from email_candidates c where c.lead_id = ranked.lead_id and c.source_kind = 'claude_search')
        order by lead_id, pos
        """,
        {"fin": list(FIN), "n": per_tab},
    ).fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["lead_id", "name", "type", "region", "phone", "address", "place_url", "issue", "pos"])
        for lid, name, ltype, rk, phone, addr, url, headline, pos in rows:
            w.writerow([lid, name, ltype, region_label(rk or "", with_sido=True), phone or "", addr or "", url or "", headline, pos])
    log.info("export-top %d건 → %s", len(rows), path)
    return len(rows)


def import_emails(conn: psycopg.Connection, path: str) -> dict:
    st = {"rows": 0, "saved": 0, "skipped": 0}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            st["rows"] += 1
            lid = (r.get("lead_id") or "").strip()
            email = (r.get("email") or "").strip().lower()
            url = (r.get("source_url") or "").strip()[:500]
            if not re.fullmatch(r"[0-9a-f-]{36}", lid) or not email or not plausible(email):
                st["skipped"] += 1
                continue
            conn.execute(
                """
                insert into email_candidates (lead_id, email, score, signals, source_url, source_kind, status)
                values (%s, %s, 70, %s, %s, 'claude_search', 'auto')
                on conflict (lead_id, email) do update set source_url = excluded.source_url, source_kind = excluded.source_kind,
                  status = case when email_candidates.status in ('accepted','rejected') then email_candidates.status else 'auto' end
                """,
                (lid, email, ["claude", (r.get("note") or "")[:60]], url),
            )
            conn.execute(
                "update leads set contact = coalesce(contact, '{}'::jsonb) || jsonb_build_object('email', %s::text), email_src = %s "
                "where id = %s and coalesce(contact->>'email', '') = ''",
                (email, f"claude:{url[:400]}", lid),
            )
            st["saved"] += 1
    conn.commit()
    log.info("import-emails %s", st)
    return st


# 네이버·카카오·토스 계열은 제휴 대상에서 뺀다
_EXCLUDE = re.compile(r"(^|[\s(])(네이버|카카오|토스)|비바리퍼블리카|다음\s*부동산|\bnaver\b|\bkakao\b|\btoss\b", re.I)   # 집토스 등은 통과


def _domain(url: str | None) -> str | None:
    m = re.match(r"https?://(?:www\.|m\.)?([^/?#]+)", (url or "").strip(), re.I)
    return m.group(1).lower() if m else None


def import_platforms(conn: psycopg.Connection, path: str) -> dict:
    """Claude가 찾은 플랫폼·스타트업 CSV(name,category,homepage,email,email_kind,source_url,note)
    → leads(lane=b2b2c, type=platform). 같은 회사(도메인/이름)는 한 리드로 합치고, 제휴 메일이 일반 메일보다 우선."""
    st = {"rows": 0, "saved": 0, "emails": 0, "excluded": 0, "contacted": 0}
    rank = {"partnership": 3, "general": 2, "form_only": 1, "none": 0}
    best: dict[str, dict] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            st["rows"] += 1
            name = (r.get("name") or "").strip()
            if not name:
                continue
            if _EXCLUDE.search(name) and "당근" not in name:
                st["excluded"] += 1
                continue
            email = (r.get("email") or "").strip().lower()
            kind = (r.get("email_kind") or "").strip() or ("general" if email else "none")
            if email and not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", email):
                email, kind = "", "none"
            key = _domain(r.get("homepage")) or re.sub(r"\s*\(.*?\)\s*", "", name).strip().lower()
            cur = best.get(key)
            row = {"name": name, "category": (r.get("category") or "").strip(), "url": (r.get("homepage") or "").strip() or None,
                   "email": email or None, "kind": kind, "src": (r.get("source_url") or "").strip(), "note": (r.get("note") or "").strip()}
            if cur is None or rank.get(kind, 0) > rank.get(cur["kind"], 0):
                if cur and not row["note"]:
                    row["note"] = cur["note"]
                best[key] = row
    for key, r in best.items():
        contacted = "이미 접촉" in r["note"]
        contact = {k: v for k, v in {"email": r["email"], "url": r["url"], "note": r["note"], "category": r["category"],
                                      "email_kind": r["kind"]}.items() if v}
        conn.execute(
            """insert into leads (lane, type, name, contact, status, source, external_id, email_src, score)
               values ('b2b2c','platform',%s,%s::jsonb,%s,'claude_platform',%s,%s,%s)
               on conflict (source, external_id) where external_id is not null do update set
                 name = excluded.name,
                 contact = leads.contact || excluded.contact,
                 status = case when excluded.status = 'contacted' then 'contacted' else leads.status end,
                 email_src = coalesce(excluded.email_src, leads.email_src),
                 score = excluded.score, updated_at = now()""",
            (r["name"], json.dumps(contact, ensure_ascii=False),
             "contacted" if contacted else "new", key, f"claude:{r['src']}" if r["email"] else None,
             rank.get(r["kind"], 0) * 10 + (0 if contacted else 1)),
        )
        st["saved"] += 1
        st["emails"] += bool(r["email"])
        st["contacted"] += contacted
    # 제휴 시퀀스 1단계에 '제휴 안내' PDF 기본 첨부 (앱에서 바꿨으면 그대로 둠)
    conn.execute(
        """update sequence_steps set attachments = coalesce((select jsonb_agg(id) from mail_files where name like '%%제휴 안내%%'), '[]'::jsonb)
           where sequence = 'platform_intro' and step = 0 and attachments = '[]'::jsonb""")
    conn.commit()
    log.info("플랫폼·스타트업 반영: %s", st)
    return st
