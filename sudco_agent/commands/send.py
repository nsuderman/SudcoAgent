"""`agent send` — email approved demos to prospects."""
from __future__ import annotations

import logging

from ..api_client import SudcoAPI
from ..config import Config
from ..outreach import email as mailer

log = logging.getLogger(__name__)


def run(cfg: Config) -> int:
    """Send first-outreach emails for every demo with status=approved.
    Returns number of emails sent."""
    sent = 0
    with SudcoAPI.from_config(cfg) as api:
        approved = list(api.iter_demos(status="approved"))
        if not approved:
            log.info("No approved demos to send.")
            return 0

        log.info("%d approved demos awaiting send", len(approved))
        for demo in approved:
            email = demo.get("contact_email")
            if not email:
                log.warning("Skipping demo %d (%s) — no contact_email on prospect",
                            demo["id"], demo["business_name"])
                continue

            preview_url = f"{cfg.api_base.rstrip('/').removesuffix('/api')}/p/{demo['token']}"
            subject, body = mailer.render(
                "first_outreach.txt",
                business_name=demo["business_name"],
                recipient_name=None,
                preview_url=preview_url,
            )

            try:
                mailer.send(cfg, to=email, subject=subject, body=body, reply_to=cfg.smtp_user)
                api.set_demo_status(demo["id"], "sent")
                sent += 1
                log.info("→ sent to %s for %s", email, demo["business_name"])
            except Exception as exc:
                log.exception("Failed to send to %s: %s", email, exc)
    return sent
