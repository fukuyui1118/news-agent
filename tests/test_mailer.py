from unittest.mock import MagicMock, patch

from news_agent.mailer import (
    DigestEntry,
    DigestPayload,
    Mailer,
    MailerConfig,
    P1BatchEntry,
    P1BatchPayload,
)


def _cfg():
    return MailerConfig(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="user@example.com",
        smtp_password="secret",
        email_from="from@example.com",
        email_to="to@example.com",
    )


# --- legacy single-P1 path (kept for backwards compat) ---------------------


def test_p1_dry_run_prints_japanese_subject_and_body(capsys):
    m = Mailer(_cfg(), dry_run=True)
    m.send_p1(
        headline_ja="東京海上、Q4決算で予想を上回る",
        original_title="Tokio Marine reports Q4 earnings beat",
        source="Test Source",
        url="https://example.com/x",
        summary_bullets="- 利益10億ドル\n- 株価3%上昇",
        entity="Tokio Marine",
    )
    out = capsys.readouterr().out
    assert "[DRY-RUN P1 EMAIL]" in out
    assert "[News Agent P1] 東京海上" in out
    assert "ソース: Test Source" in out


def test_p1_dry_run_does_not_call_smtp():
    m = Mailer(_cfg(), dry_run=True)
    with patch("news_agent.mailer.smtplib.SMTP") as mock_smtp:
        m.send_p1(
            headline_ja="h",
            original_title="t",
            source="s",
            url="https://example.com/x",
            summary_bullets="- x",
            entity=None,
        )
    mock_smtp.assert_not_called()


# --- P1 batch path (Phase 4) ------------------------------------------------


def _p1_batch_payload():
    return P1BatchPayload(
        timestamp_label="05/10 15:00",
        entries=[
            P1BatchEntry(
                headline_ja="東京海上、Q4決算で予想を上回る",
                original_title="Tokio Marine reports Q4 earnings beat",
                source="Reinsurance News",
                url="https://example.com/a",
                summary_bullets="- 利益10億ドル\n- 株価3%上昇",
                entity="Tokio Marine",
            ),
            P1BatchEntry(
                headline_ja="ソムポ、ロンドン再保険トップ任命",
                original_title="Sompo Re names new London head",
                source="Reinsurance News",
                url="https://example.com/b",
                summary_bullets="- 業界経験28年",
                entity="Sompo",
            ),
        ],
    )


def test_p1_batch_dry_run_subject_and_body(capsys):
    m = Mailer(_cfg(), dry_run=True)
    m.send_p1_batch(_p1_batch_payload())
    out = capsys.readouterr().out
    assert "[DRY-RUN P1-BATCH EMAIL]" in out
    assert "[News Agent P1] 05/10 15:00 (2件)" in out
    assert "東京海上" in out
    assert "ソムポ" in out


def test_p1_batch_empty_does_nothing():
    m = Mailer(_cfg(), dry_run=False)
    with patch("news_agent.mailer.smtplib.SMTP") as mock_smtp:
        m.send_p1_batch(P1BatchPayload(timestamp_label="05/10 15:00", entries=[]))
    mock_smtp.assert_not_called()


# --- digest path (P1+P2 in Phase 4) -----------------------------------------


def _digest_payload():
    return DigestPayload(
        date_label="05/10",
        entries=[
            DigestEntry(
                priority="P1",
                headline_ja="東京海上、Q4決算で予想を上回る",
                original_title="Tokio Marine Q4 beats estimates",
                source="Reinsurance News",
                url="https://example.com/a",
                summary_bullets="- 利益10億ドル",
                entity="Tokio Marine",
            ),
            DigestEntry(
                priority="P2",
                headline_ja="アリアンツ、サイバー業務をMGAへ移行",
                original_title="Allianz Commercial Transitions...",
                source="Insurance Journal",
                url="https://example.com/b",
                summary_bullets="- ポイント1",
                entity="Allianz",
            ),
        ],
    )


def test_digest_subject_format(capsys):
    m = Mailer(_cfg(), dry_run=True)
    m.send_digest(_digest_payload())
    out = capsys.readouterr().out
    assert "Subject: Daily Insurance news 05/10" in out
    assert "P1: Japan-impact" in out
    assert "P2: Global majors" in out
    assert "東京海上" in out
    assert "アリアンツ" in out


def test_digest_empty_does_nothing():
    m = Mailer(_cfg(), dry_run=False)
    with patch("news_agent.mailer.smtplib.SMTP") as mock_smtp:
        m.send_digest(DigestPayload(date_label="05/10", entries=[]))
    mock_smtp.assert_not_called()


def test_digest_live_send_calls_smtp():
    m = Mailer(_cfg(), dry_run=False)
    with patch("news_agent.mailer.smtplib.SMTP") as mock_smtp:
        mock_conn = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_conn
        m.send_digest(_digest_payload())
    mock_conn.send_message.assert_called_once()
