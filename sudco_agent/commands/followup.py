"""`agent followup` — send follow-up email to demos that were sent N days ago
and haven't moved to opened/replied/converted/declined.

STUB. Planned: query the email_sends table on the API side (need a new
admin endpoint that returns sends + status), filter by age + current demo
status, render follow_up_1.txt, send.
"""
from __future__ import annotations

import logging

from ..config import Config

log = logging.getLogger(__name__)


def run(cfg: Config) -> int:
    log.warning("`agent followup` is a stub. Wire up after first batch of sends has had time to receive responses.")
    return 0
