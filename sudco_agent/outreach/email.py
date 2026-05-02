"""SMTP sender — talks to the same docker-mailserver as sudcosolutions.com.

After each successful SMTP send, optionally connects via IMAP and APPENDs
the message to the Sent folder so it shows up in webmail / Apple Mail /
Outlook (SMTP doesn't write to mailboxes — that's a client-level concern).
"""
from __future__ import annotations

import imaplib
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Config

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(disabled_extensions=("txt",)),
        keep_trailing_newline=True,
    )


def render(template_name: str, **vars) -> tuple[str, str]:
    """Render a plaintext template — first line is the subject, rest is body."""
    tpl = _env().get_template(template_name)
    rendered = tpl.render(**vars)
    subject_line, _, body = rendered.partition("\n")
    if not subject_line.lower().startswith("subject:"):
        raise ValueError(f"Template {template_name} must start with `Subject: ...` line")
    return subject_line[len("Subject:"):].strip(), body.lstrip("\n")


def render_html(template_name: str, **vars) -> str | None:
    """Render an HTML template if it exists, else return None.

    HTML templates are body-only (no Subject: line) — the subject is taken
    from the matching .txt template via `render()`. Returning None lets
    `send()` fall back to plaintext-only when there's no HTML version.
    """
    try:
        tpl = _env().get_template(template_name)
    except Exception:
        return None
    return tpl.render(**vars)


def send(
    cfg: Config,
    *,
    to: str,
    subject: str,
    body: str,
    html_body: str | None = None,
    reply_to: str | None = None,
    unsubscribe_id: str | int | None = None,
) -> str:
    """Send an email. If `html_body` is provided, the message goes out as
    multipart/alternative with both plaintext and HTML versions — clients
    pick whichever they support. Returns the SMTP Message-ID.

    When `unsubscribe_id` is set (typically the prospect_id), the message
    includes a List-Unsubscribe header so recipients can opt out by replying
    to a tagged subject — Gmail/Outlook surface this as a one-click
    unsubscribe button. Required-ish for cold outreach; checked by
    mail-tester and per Gmail's 2024 bulk-sender guidelines.
    """
    msg = EmailMessage()
    msg["From"] = cfg.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    # Strict relays (amavisd-new in particular) bounce messages missing a
    # Date header. RFC 5322 requires it; most MTAs auto-stamp it but ours
    # doesn't. Set it explicitly with the local timezone offset.
    msg["Date"] = formatdate(localtime=True)
    if unsubscribe_id is not None:
        tag = f"UNSUBSCRIBE-{unsubscribe_id}"
    else:
        tag = "UNSUBSCRIBE"
    msg["List-Unsubscribe"] = f"<mailto:{cfg.smtp_user}?subject={tag}>"
    message_id = make_msgid(domain="sudcosolutions.com")
    msg["Message-ID"] = message_id
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    if cfg.dry_run:
        log.warning("DRY_RUN — would have sent to %s with subject %r", to, subject)
        return message_id

    if cfg.smtp_port == 465:
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ssl.create_default_context()) as s:
            s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(cfg.smtp_user, cfg.smtp_pass)
            s.send_message(msg)

    log.info("Sent %r to %s (Message-ID: %s)", subject, to, message_id)

    # After the SMTP relay accepts the message, optionally upload a copy to
    # the Sent IMAP folder so it shows up in webmail / Apple Mail / Outlook.
    # Best-effort: an IMAP failure here doesn't unsend the email — just log.
    if cfg.imap_host:
        _save_to_sent(cfg, msg)

    return message_id


def _save_to_sent(cfg: Config, msg: EmailMessage) -> None:
    """Upload `msg` to the configured IMAP Sent folder. Silent no-op on
    failure (the actual delivery already succeeded — Sent-folder bookkeeping
    is best-effort)."""
    try:
        with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
            imap.login(cfg.smtp_user, cfg.smtp_pass)
            status, _ = imap.append(
                cfg.imap_sent_folder,
                "\\Seen",
                imaplib.Time2Internaldate(time.time()),
                msg.as_bytes(),
            )
            if status != "OK":
                log.warning("IMAP APPEND to %r returned status %s",
                            cfg.imap_sent_folder, status)
    except Exception as exc:
        log.warning("Failed to save sent copy via IMAP %s:%d → %r (%s)",
                    cfg.imap_host, cfg.imap_port, cfg.imap_sent_folder, exc)
