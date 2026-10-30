"""시퀀스 발송 잡.

enrollments 중 status='active' 이고 next_send_at <= now 인 건을 골라
  템플릿(step) 렌더 → Gmail 발송 → events(type='contact', channel=email) 기록
  → step+1, next_send_at = now + 간격. 마지막 단계면 done.
리드에 reply 이벤트가 있으면 발송 전에 자동으로 replied 처리하고 보내지 않는다.
메일 주소가 없거나 형식이 틀리면 stopped (meta.reason).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import psycopg

from .config import Settings
from .db import jsonb
from .mailer import Mail, Mailer, db_steps, list_sequences, load_template, render, valid_email
from .regions import sgg_name

log = logging.getLogger(__name__)

# 단계 간 간격(일). 템플릿 수가 이보다 많으면 마지막 값을 반복.
DEFAULT_GAPS_DAYS = [3, 4, 7]


def _region_label(region_key: str | None) -> str:
    if not region_key:
        return ""
    sgg, _, umd = region_key.partition("|")
    return f"{sgg_name(sgg)} {umd}".strip()


def stop_replied(conn: psycopg.Connection) -> int:
    """회신 온 리드의 active 시퀀스는 멈춘다."""
    cur = conn.execute(
        """
        update enrollments e set status = 'replied'
        where e.status = 'active'
          and exists (select 1 from events ev where ev.lead_id = e.lead_id and ev.type = 'reply'
                        and ev.occurred_at >= e.created_at)
        """
    )
    return cur.rowcount


def _advance(conn: psycopg.Connection, s: Settings, enr_id, step: int, total: int, as_of: datetime, rows: list) -> bool:
    """다음 단계로. 마지막이면 done(True). 간격은 다음 단계의 gap_days (없으면 MAIL_GAPS_DAYS)."""
    next_step = step + 1
    if next_step >= total:
        conn.execute("update enrollments set step=%s, status='done', last_sent_at=%s where id=%s", (next_step, as_of, enr_id))
        return True
    nxt = next((r for r in rows if r[0] == next_step), None)
    if nxt is not None:
        gap = nxt[2]
    else:
        gaps = s.mail_gaps_days or DEFAULT_GAPS_DAYS
        gap = gaps[min(step, len(gaps) - 1)]
    conn.execute("update enrollments set step=%s, last_sent_at=%s, next_send_at=%s where id=%s",
                 (next_step, as_of, as_of + timedelta(days=gap), enr_id))
    return False


def due_enrollments(conn: psycopg.Connection, as_of: datetime, limit: int) -> list[tuple]:
    return conn.execute(
        """
        select e.id, e.lead_id, e.sequence, e.step, l.name, l.region_key, l.contact->>'email'
        from enrollments e join leads l on l.id = e.lead_id
        where e.status = 'active' and e.next_send_at <= %s
          and e.plan is null          -- 앱 워크플로우(plan)는 앱 쪽 실행기(/api/sequence/run)가 처리
        order by e.next_send_at
        limit %s
        """,
        (as_of, limit),
    ).fetchall()


def run_sequences(conn: psycopg.Connection, s: Settings, as_of: datetime | None = None,
                  dry_run: bool | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    dry_run = s.mail_dry_run if dry_run is None else dry_run
    steps_db = db_steps(conn)                       # 앱과 같은 원본 (없으면 templates/ 파일)
    seqs = {k: len(v) for k, v in steps_db.items()} or list_sequences()
    stats = {"replied_stopped": stop_replied(conn), "sent": 0, "done": 0, "stopped": 0, "skipped": 0, "tasks": 0}
    conn.commit()

    rows = due_enrollments(conn, as_of, s.mail_daily_limit)
    if not rows:
        log.info("발송 대상 없음")
        return stats

    with Mailer(s.gmail_user, s.gmail_app_password, s.gmail_sender_name, dry_run=dry_run) as mailer:
        for enr_id, lead_id, sequence, step, name, region_key, email in rows:
            total = seqs.get(sequence, 0)
            row = next((r for r in steps_db.get(sequence, []) if r[0] == step), None)
            tpl = (row[3], row[4]) if row else load_template(sequence, step)
            channel = row[1] if row else "email"
            if row and channel != "email":
                # 전화·문자·방문 단계: 사람이 할 일 → 파이프라인 할 일, 다음 단계로
                conn.execute("insert into pipeline (lead_id, action, stage, due_at, note) values (%s,%s,'todo',%s,%s)",
                             (lead_id, channel, as_of, f"{sequence} {step + 1}/{total} {channel} — {render(row[3] or '', {'name': name or '', 'region': _region_label(region_key)})}"))
                _advance(conn, s, enr_id, step, total, as_of, steps_db.get(sequence, []))
                stats["tasks"] += 1
                conn.commit()
                continue
            if not total or tpl is None:
                conn.execute("update enrollments set status='stopped' where id=%s", (enr_id,))
                log.warning("템플릿 없음 %s/%s → stopped", sequence, step)
                stats["stopped"] += 1
                conn.commit()
                continue
            if not valid_email(email):
                conn.execute("update enrollments set status='stopped' where id=%s", (enr_id,))
                conn.execute("insert into events (lead_id, type, occurred_at, meta) values (%s,'contact',%s,%s)",
                             (lead_id, as_of, jsonb({"channel": "email", "result": "no_email", "sequence": sequence, "step": step})))
                stats["stopped"] += 1
                conn.commit()
                continue

            ctx = {"name": name or "", "region": _region_label(region_key),
                   "sender": s.gmail_user, "sender_name": s.gmail_sender_name or s.gmail_user,
                   "신청링크": f"https://bballang-twieo.vercel.app/apply/{lead_id}", "apply_link": f"https://bballang-twieo.vercel.app/apply/{lead_id}"}
            subject, body = render(tpl[0], ctx), render(tpl[1], ctx)
            try:
                msg_id = mailer.send(Mail(to=email.strip(), subject=subject, body=body))
            except Exception as e:  # SMTP 오류: 이번 건만 건너뛰고 다음 실행에 재시도
                log.error("발송 실패 %s → %s: %s", sequence, email, e)
                stats["skipped"] += 1
                continue

            conn.execute(
                "insert into events (lead_id, type, occurred_at, meta) values (%s,'contact',%s,%s)",
                (lead_id, as_of, jsonb({"channel": "email", "sequence": sequence, "step": step,
                                        "subject": subject, "message_id": msg_id, "dry_run": dry_run})),
            )
            conn.execute("update leads set status='contacted', updated_at=now() where id=%s and status='new'", (lead_id,))

            if _advance(conn, s, enr_id, step, total, as_of, steps_db.get(sequence, [])):
                stats["done"] += 1
            stats["sent"] += 1
            conn.commit()

    log.info("시퀀스 %s", stats)
    return stats
