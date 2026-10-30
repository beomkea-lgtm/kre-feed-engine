"""경기권(서울·경기) 전수 수집: 세무사·중개사를 빠짐없이 leads 에 넣는다.

카카오 로컬은 한 번 검색에 최대 45곳(15×3페이지)만 준다. 그래서 지도를 사각형 칸으로 나눠
  - 칸 안 결과가 45곳 이하 → 3페이지까지 다 받는다
  - 45곳 넘으면 → 칸을 4등분해서 다시 (끝까지 쪼갠다)
칸 진행 상황은 sweep_cells 표에 남겨, 예산(하루 호출 수)이 끝나면 다음 실행이 이어서 돈다.
  - 세무사: 키워드 '세무사' (분류에 세무사·세무법인·세무,회계)
  - 중개사: 카테고리 AG2(중개업소). 서울은 중개사 등록부가 있어 경기만.
결과 → leads (source='kakao_local', external_id=카카오 place id) — 기존 places 수집과 같은 키라 중복 없음.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import psycopg
import requests

from .places import _broker_type, _fixed, _lead_row, _upsert_places, region_from_address

log = logging.getLogger(__name__)

KAKAO_KEYWORD = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_CATEGORY = "https://dapi.kakao.com/v2/local/search/category.json"
# 서울·경기 전체를 덮는 사각형 (경도 x, 위도 y)
BBOX = (126.35, 36.88, 127.90, 38.30)
GRID = 0.1          # 처음 칸 크기 (도) — 넘치면 4등분
MIN_SIZE = 0.0015   # 이보다 작게는 안 쪼갠다 (~150m) — 같은 건물에 45곳 넘게 몰린 경우

KINDS = {
    "tax":    {"query": "세무사", "category": None, "classify": _fixed("tax", ("세무사", "세무법인", "세무,회계"), ("세무서", "행정기관", "회계사")),
               "seoul": True},
    "broker": {"query": None, "category": "AG2", "classify": _broker_type, "seoul": False},
}

SCHEMA = """
create table if not exists sweep_cells (
  id bigserial primary key,
  kind text not null,
  x1 numeric not null, y1 numeric not null, x2 numeric not null, y2 numeric not null,
  depth int not null default 0,
  status text not null default 'todo',      -- todo | done | split
  total int, saved int, done_at timestamptz,
  unique (kind, x1, y1, x2, y2)
);
create index if not exists sweep_cells_todo on sweep_cells (kind, status, depth);
"""


def _seed(conn: psycopg.Connection, kind: str) -> int:
    if conn.execute("select 1 from sweep_cells where kind = %s limit 1", (kind,)).fetchone():
        return 0
    rows = []
    x = BBOX[0]
    while x < BBOX[2] - 1e-9:
        y = BBOX[1]
        while y < BBOX[3] - 1e-9:
            rows.append((kind, round(x, 5), round(y, 5), round(min(x + GRID, BBOX[2]), 5), round(min(y + GRID, BBOX[3]), 5)))
            y += GRID
        x += GRID
    conn.execute("insert into sweep_cells (kind, x1, y1, x2, y2) values " + ",".join(["(%s,%s,%s,%s,%s)"] * len(rows)) +
                 " on conflict do nothing", [v for r in rows for v in r])
    conn.commit()
    return len(rows)


class _Api:
    def __init__(self, key: str):
        self.h = {"Authorization": f"KakaoAK {key}"}
        self.s = requests.Session()

    def page(self, spec: dict, rect: str, page: int) -> tuple[list[dict], int]:
        p = {"rect": rect, "page": page, "size": 15}
        if spec["category"]:
            p["category_group_code"] = spec["category"]
            url = KAKAO_CATEGORY
        else:
            p["query"] = spec["query"]
            url = KAKAO_KEYWORD
        r = self.s.get(url, headers=self.h, params=p, timeout=10)
        r.raise_for_status()
        j = r.json()
        return j.get("documents", []), int((j.get("meta") or {}).get("total_count", 0))


def _cell(api: _Api, spec: dict, c: tuple) -> dict:
    cid, x1, y1, x2, y2, depth = c
    rect = f"{x1},{y1},{x2},{y2}"
    out = {"id": cid, "docs": [], "total": 0, "calls": 0, "split": False, "err": None, "cell": c}
    try:
        docs, total = api.page(spec, rect, 1)
        out["calls"] += 1
        out["total"] = total
        if total > 45 and (float(x2) - float(x1)) > MIN_SIZE:
            out["split"] = True
            return out
        out["docs"].extend(docs)
        for pg in (2, 3):
            if len(out["docs"]) >= min(total, 45):
                break
            more, _ = api.page(spec, rect, pg)
            out["calls"] += 1
            out["docs"].extend(more)
            if len(more) < 15:
                break
    except requests.RequestException as e:
        out["err"] = str(e)[:200]
    return out


def run_sweep(conn: psycopg.Connection, key: str, kinds: list[str], budget: int = 20000, workers: int = 6) -> dict:
    conn.execute(SCHEMA)
    conn.commit()
    stats: dict = {"calls": 0, "cells_done": 0, "cells_split": 0, "saved": {}, "errors": 0, "left": {}}
    api = _Api(key)
    errors_row = 0
    for kind in kinds:
        spec = KINDS[kind]
        # 다 돈 지 30일 지났으면 처음부터 다시 (새로 연 사무소 반영)
        stale = conn.execute("select count(*) filter (where status='todo'), max(done_at) < now() - interval '30 days' from sweep_cells where kind=%s", (kind,)).fetchone()
        if stale and stale[0] == 0 and stale[1]:
            conn.execute("delete from sweep_cells where kind=%s", (kind,))
            conn.commit()
            log.info("경기권 전수 수집 %s: 30일 지나 처음부터 다시", kind)
        stats["seeded_" + kind] = _seed(conn, kind)
        saved = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            while stats["calls"] < budget and errors_row < 20:
                cells = conn.execute(
                    "select id, x1, y1, x2, y2, depth from sweep_cells where kind = %s and status = 'todo' order by depth, id limit %s",
                    (kind, workers * 4)).fetchall()
                if not cells:
                    break
                for res in ex.map(lambda c: _cell(api, spec, c), cells):
                    stats["calls"] += res["calls"]
                    if res["err"]:
                        stats["errors"] += 1
                        errors_row += 1
                        continue
                    errors_row = 0
                    cid, x1, y1, x2, y2, depth = res["cell"]
                    if res["split"]:
                        mx, my = (float(x1) + float(x2)) / 2, (float(y1) + float(y2)) / 2
                        quads = [(x1, y1, mx, my), (mx, y1, x2, my), (x1, my, mx, y2), (mx, my, x2, y2)]
                        conn.execute("insert into sweep_cells (kind, x1, y1, x2, y2, depth) values " + ",".join(["(%s,%s,%s,%s,%s,%s)"] * 4) +
                                     " on conflict do nothing", [v for q in quads for v in (kind, round(float(q[0]), 6), round(float(q[1]), 6), round(float(q[2]), 6), round(float(q[3]), 6), depth + 1)])
                        conn.execute("update sweep_cells set status='split', total=%s, done_at=now() where id=%s", (res["total"], cid))
                        stats["cells_split"] += 1
                        continue
                    rows = []
                    for doc in res["docs"]:
                        if not spec["classify"](doc.get("place_name"), doc.get("category_name")):
                            continue
                        reg = region_from_address(doc.get("address_name"))
                        if not reg:                      # 서울·경기 밖
                            continue
                        if not spec["seoul"] and (doc.get("address_name") or "").startswith("서울"):
                            continue                     # 서울 중개사는 등록부로
                        row = _lead_row(kind, "b2b2c", doc, reg[0])
                        if row:
                            rows.append(row)
                    ids = _upsert_places(conn, rows)
                    saved += len(ids)
                    conn.execute("update sweep_cells set status='done', total=%s, saved=%s, done_at=now() where id=%s", (res["total"], len(ids), cid))
                    stats["cells_done"] += 1
                conn.commit()
                if stats["cells_done"] and stats["cells_done"] % 100 == 0:
                    log.info("경기권 전수 수집 %s: 칸 %d 완료 · 쪼갬 %d · 호출 %d · 저장 %d", kind, stats["cells_done"], stats["cells_split"], stats["calls"], saved)
        stats["saved"][kind] = saved
        stats["left"][kind] = conn.execute("select count(*) from sweep_cells where kind=%s and status='todo'", (kind,)).fetchone()[0]
        log.info("경기권 전수 수집 %s 끝 — 저장 %d, 남은 칸 %d", kind, saved, stats["left"][kind])
    stats["leads_now"] = dict(conn.execute("select type, count(*) from leads where type = any(%s) group by type", (kinds,)).fetchall())
    log.info("sweep %s", stats)
    return stats
