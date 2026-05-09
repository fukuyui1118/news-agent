from __future__ import annotations

import smtplib
import sys
from dataclasses import dataclass, field
from email.message import EmailMessage


@dataclass
class MailerConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: str


@dataclass
class DigestEntry:
    priority: str
    headline_ja: str
    original_title: str
    source: str
    url: str
    summary_bullets: str
    entity: str | None = None


@dataclass
class DigestPayload:
    date_label: str
    entries: list[DigestEntry] = field(default_factory=list)


@dataclass
class P1BatchEntry:
    headline_ja: str
    original_title: str
    source: str
    url: str
    summary_bullets: str
    entity: str | None = None


@dataclass
class P1BatchPayload:
    timestamp_label: str  # "MM/DD HH:00"
    entries: list[P1BatchEntry] = field(default_factory=list)


class Mailer:
    def __init__(self, config: MailerConfig, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run

    # -------- single P1 (legacy, retained for backwards compat with tests) --
    def send_p1(
        self,
        *,
        headline_ja: str,
        original_title: str,
        source: str,
        url: str,
        summary_bullets: str,
        entity: str | None,
    ) -> None:
        subject = f"[News Agent P1] {headline_ja}"
        body = _compose_p1_body(
            headline_ja=headline_ja,
            original_title=original_title,
            source=source,
            url=url,
            summary_bullets=summary_bullets,
            entity=entity,
        )
        self._dispatch(subject=subject, body=body, label="P1")

    # -------- P1 batch (Phase 4) --------------------------------------------
    def send_p1_batch(self, payload: P1BatchPayload) -> None:
        if not payload.entries:
            return
        n = len(payload.entries)
        subject = f"[News Agent P1] {payload.timestamp_label} ({n}件)"
        body = _compose_p1_batch_body(payload=payload)
        self._dispatch(subject=subject, body=body, label="P1-BATCH")

    # -------- daily digest --------------------------------------------------
    def send_digest(self, payload: DigestPayload) -> None:
        if not payload.entries:
            return
        subject = f"Daily Insurance news {payload.date_label}"
        body = _compose_digest_body(payload=payload)
        self._dispatch(subject=subject, body=body, label="DIGEST")

    # -------- internal ------------------------------------------------------
    def _dispatch(self, *, subject: str, body: str, label: str) -> None:
        if self.dry_run:
            print("=" * 70, file=sys.stdout)
            print(f"[DRY-RUN {label} EMAIL]", file=sys.stdout)
            print(f"Subject: {subject}", file=sys.stdout)
            print(f"From: {self.config.email_from}", file=sys.stdout)
            print(f"To: {self.config.email_to}", file=sys.stdout)
            print("", file=sys.stdout)
            print(body, file=sys.stdout)
            print("=" * 70, file=sys.stdout)
            return

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.config.email_from
        msg["To"] = self.config.email_to
        msg.set_content(body)
        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(self.config.smtp_user, self.config.smtp_password)
            smtp.send_message(msg)


# ---- body composition ------------------------------------------------------


def _compose_p1_body(
    *,
    headline_ja: str,
    original_title: str,
    source: str,
    url: str,
    summary_bullets: str,
    entity: str | None,
) -> str:
    entity_line = f"監視対象企業: {entity}\n" if entity else ""
    return (
        f"{headline_ja}\n"
        f"\n"
        f"ソース: {source}\n"
        f"{entity_line}"
        f"リンク: {url}\n"
        f"\n"
        f"{summary_bullets}\n"
        f"\n"
        f"---\n"
        f"原題: {original_title}\n"
    )


def _compose_p1_batch_body(*, payload: P1BatchPayload) -> str:
    n = len(payload.entries)
    parts: list[str] = [
        f"{payload.timestamp_label} 時点の P1（Japan-impact）アラート {n} 件です。",
        "",
        "━━━ P1: Japan-impact ━━━",
        "",
    ]
    for i, entry in enumerate(payload.entries, start=1):
        parts.extend(_format_entry(i, entry, entry.entity))
    return "\n".join(parts) + "\n"


def _compose_digest_body(*, payload: DigestPayload) -> str:
    p1 = [e for e in payload.entries if e.priority == "P1"]
    p2 = [e for e in payload.entries if e.priority == "P2"]
    parts: list[str] = [
        f"{payload.date_label} の日次ダイジェストです。",
        f"合計 {len(payload.entries)} 件 (P1: {len(p1)}件 / P2: {len(p2)}件)",
        "",
    ]
    if p1:
        parts.append("━━━ P1: Japan-impact ━━━")
        parts.append("")
        for i, entry in enumerate(p1, start=1):
            parts.extend(_format_entry(i, entry, entry.entity))
    if p2:
        if p1:
            parts.append("")
        parts.append("━━━ P2: Global majors ━━━")
        parts.append("")
        for i, entry in enumerate(p2, start=1):
            parts.extend(_format_entry(i, entry, entry.entity))
    return "\n".join(parts) + "\n"


def _format_entry(idx: int, entry, entity: str | None) -> list[str]:
    block = [f"【{idx}】{entry.headline_ja}", f"ソース: {entry.source}"]
    if entity:
        block.append(f"銘柄: {entity}")
    block.extend([f"リンク: {entry.url}", "", entry.summary_bullets, "", f"原題: {entry.original_title}", ""])
    return block
