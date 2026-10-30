"""Gmail SMTP 발송 (앱 비밀번호). 하루 한도는 Workspace 2,000통 / 개인 500통.

템플릿: templates/<sequence>/<step>.md
  첫 줄 '제목: ...' 이 제목, 빈 줄 뒤가 본문. {name} {region} {sender} {sender_name} 치환.
"""
from __future__ import annotations

import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass
class Mail:
    to: str
    subject: str
    body: str


class Mailer:
    def __init__(self, user: str, app_password: str, sender_name: str = "", dry_run: bool = False):
        self.user = user
        self.app_password = app_password
        self.sender_name = sender_name or user
        self.dry_run = dry_run
        self._smtp: smtplib.SMTP_SSL | None = None

    def __enter__(self):
        if not self.dry_run:
            if not (self.user and self.app_password):
                raise SystemExit("GMAIL_USER / GMAIL_APP_PASSWORD 비어 있음")
            ctx = ssl.create_default_context()
            self._smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=30)
            self._smtp.login(self.user, self.app_password)
        return self

    def __exit__(self, *exc):
        if self._smtp:
            try:
                self._smtp.quit()
            except Exception:
                pass
            self._smtp = None

    def send(self, mail: Mail) -> str:
        msg = EmailMessage()
        msg["From"] = formataddr((self.sender_name, self.user))
        msg["To"] = mail.to
        msg["Subject"] = mail.subject
        msg.set_content(mail.body)
        if self.dry_run:
            log.info("[dry-run] → %s | %s", mail.to, mail.subject)
            return "dry-run"
        assert self._smtp is not None
        self._smtp.send_message(msg)
        return msg["Message-ID"] or "sent"


# ---------- 템플릿 ----------

def list_sequences(root: Path = TEMPLATES_DIR) -> dict[str, int]:
    """{시퀀스명: 단계 수}"""
    out = {}
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if d.is_dir():
            n = len([p for p in d.iterdir() if re.fullmatch(r"\d+\.md", p.name)])
            if n:
                out[d.name] = n
    return out


def load_template(sequence: str, step: int, root: Path = TEMPLATES_DIR) -> tuple[str, str] | None:
    p = root / sequence / f"{step}.md"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    subject = ""
    body_start = 0
    for i, line in enumerate(lines):
        if line.lower().startswith(("제목:", "subject:")):
            subject = line.split(":", 1)[1].strip()
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip("\n")
    return subject, body


def render(text: str, ctx: dict) -> str:
    def rep(m):
        k = m.group(1)
        return str(ctx.get(k, m.group(0)))
    return re.sub(r"\{(\w+)\}", rep, text)


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(s: str | None) -> bool:
    return bool(s and EMAIL_RE.match(s.strip()))


def seed_sequence_steps(conn, root: Path = TEMPLATES_DIR, gaps: list[int] | None = None) -> int:
    """templates/<seq>/<n>.md → sequence_steps (이미 있는 단계는 건드리지 않음 — 앱에서 고친 문구 보존)."""
    gaps = gaps or [3, 4, 7]
    n = 0
    for seq, total in list_sequences(root).items():
        for step in range(total):
            tpl = load_template(seq, step, root)
            if tpl is None:
                continue
            gap = 0 if step == 0 else gaps[min(step - 1, len(gaps) - 1)]
            n += conn.execute(
                "insert into sequence_steps (sequence, step, channel, gap_days, subject, body) values (%s,%s,'email',%s,%s,%s) "
                "on conflict (sequence, step) do nothing", (seq, step, gap, tpl[0], tpl[1]),
            ).rowcount
    return n


def db_steps(conn) -> dict[str, list[tuple[int, str, int, str, str]]]:
    """{시퀀스: [(step, channel, gap_days, subject, body), ...]} — DB가 비면 빈 dict."""
    out: dict[str, list] = {}
    for seq, step, ch, gap, subj, body in conn.execute(
        "select sequence, step, channel, gap_days, coalesce(subject,''), coalesce(body,'') from sequence_steps order by sequence, step"
    ).fetchall():
        out.setdefault(seq, []).append((step, ch, gap, subj, body))
    return out
