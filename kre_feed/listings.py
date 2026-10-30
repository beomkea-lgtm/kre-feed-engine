"""매물 수 CSV → leads.listing_count.

CSV 컬럼(헤더 필수, 순서 무관): name, phone, listing_count. 추가로 region_key 또는 address가 있으면 매칭 정확도↑.
매칭: 전화번호 끝 8자리 → (region_key 안에서) 상호 핵심어 → 상호 전체. 매칭 안 된 줄은 결과에 남긴다.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

import psycopg

from .names import phone_digits, short_name

log = logging.getLogger(__name__)


def _find(conn: psycopg.Connection, name: str, phone: str, region_key: str | None) -> str | None:
    pd = phone_digits(phone)
    if pd:
        row = conn.execute(
            "select id from leads where type='broker' and regexp_replace(coalesce(contact->>'phone',''), '\\D', '', 'g') like %s limit 1",
            (f"%{pd[-8:]}",),
        ).fetchone()
        if row:
            return str(row[0])
    sn = short_name(name)
    if sn:
        params: list = [f"%{sn}%"]
        q = "select id from leads where type='broker' and replace(name,' ','') like %s"
        if region_key:
            q += " and region_key = %s"
            params.append(region_key)
        row = conn.execute(q + " order by score desc nulls last limit 1", params).fetchone()
        if row:
            return str(row[0])
    return None


def import_listings(conn: psycopg.Connection, path: str) -> dict:
    p = Path(path)
    with p.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    matched = 0
    unmatched: list[str] = []
    for r in rows:
        name = (r.get("name") or r.get("상호") or "").strip()
        phone = (r.get("phone") or r.get("전화") or "").strip()
        cnt_raw = (r.get("listing_count") or r.get("매물수") or r.get("count") or "0").strip()
        try:
            cnt = int(float(cnt_raw))
        except ValueError:
            cnt = 0
        lead_id = _find(conn, name, phone, (r.get("region_key") or "").strip() or None)
        if not lead_id:
            unmatched.append(name or phone)
            continue
        conn.execute("update leads set listing_count = %s, listing_checked_at = now() where id = %s", (cnt, lead_id))
        matched += 1
    conn.commit()
    if unmatched:
        log.warning("매칭 안 된 %d건 (앞 10): %s", len(unmatched), unmatched[:10])
    return {"rows": len(rows), "matched": matched, "unmatched": len(unmatched)}
