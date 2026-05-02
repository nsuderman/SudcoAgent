"""Centralized config loading. All env-driven — no hardcoded credentials."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val or ""


@dataclass(frozen=True)
class Config:
    # Sudco API
    api_base: str
    admin_api_key: str

    # LLM
    llm_base_url: str
    llm_api_key: str
    text_model: str
    vision_model: str

    # Discovery
    foursquare_api_key: str
    pexels_api_key: str

    # SMTP
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    mail_from: str

    # IMAP (for saving a copy of every outbound to the Sent folder so it
    # shows up in webmail / Apple Mail / etc. Optional — leave imap_host
    # empty to disable.)
    imap_host: str
    imap_port: int
    imap_sent_folder: str

    # Outreach
    cold_demo_base_url: str

    # Behavior
    demo_expires_days: int
    followup_after_days: int
    dry_run: bool


def load() -> Config:
    return Config(
        api_base=_get("SUDCO_API_BASE", "https://sudcosolutions.com/api"),
        admin_api_key=_get("SUDCO_ADMIN_API_KEY", required=True),
        llm_base_url=_get("LLM_BASE_URL", "http://127.0.0.1:8011/v1"),
        llm_api_key=_get("LLM_API_KEY", "not-needed"),
        text_model=_get("LLM_TEXT_MODEL", "qwen3.6-35b-a3b"),
        vision_model=_get("LLM_VISION_MODEL", "qwen3-vl-30b-a3b-instruct"),
        foursquare_api_key=_get("FOURSQUARE_API_KEY"),
        pexels_api_key=_get("PEXELS_API_KEY"),
        smtp_host=_get("SMTP_HOST", "localhost"),
        smtp_port=int(_get("SMTP_PORT", "587")),
        smtp_user=_get("SMTP_USER", "nate@sudcosolutions.com"),
        smtp_pass=_get("SMTP_PASS"),
        mail_from=_get("MAIL_FROM", "Nate at Sudco Solutions <nate@sudcosolutions.com>"),
        imap_host=_get("IMAP_HOST", _get("SMTP_HOST", "")),
        imap_port=int(_get("IMAP_PORT", "993")),
        imap_sent_folder=_get("IMAP_SENT_FOLDER", "Sent"),
        cold_demo_base_url=_get("COLD_DEMO_BASE_URL",
                                "https://sudcosolutions.com/demos").rstrip("/"),
        demo_expires_days=int(_get("DEMO_EXPIRES_DAYS", "30")),
        followup_after_days=int(_get("FOLLOWUP_AFTER_DAYS", "7")),
        dry_run=_get("DRY_RUN", "false").lower() in ("1", "true", "yes"),
    )
