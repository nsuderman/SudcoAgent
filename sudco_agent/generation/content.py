"""Generate demo-site content using the local Qwen model."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..config import Config
from ..llm import LLMClient
from . import images

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "generate_demo.txt"

REQUIRED_KEYS = ("name", "tagline", "industry", "location", "palette", "fontDisplay",
                 "services", "hours", "contact")


def build_demo(prospect: dict, cfg: Config) -> dict[str, Any]:
    """Generate a complete demo data object for a prospect.

    The output matches the same JSON shape as src/data/demos.js so the existing
    DemoTemplate component in the React app renders it directly.
    """
    profile = _profile_for_prompt(prospect)
    template = PROMPT_PATH.read_text()
    prompt = template.replace("{business_profile}", profile)

    llm = LLMClient.from_config(cfg)
    log.info("Generating demo content for %s via %s", prospect.get("business_name"), cfg.text_model)
    demo = llm.json_generate(prompt)

    # Validate required structure
    missing = [k for k in REQUIRED_KEYS if k not in demo]
    if missing:
        raise ValueError(f"LLM omitted required fields: {missing}\n\nGot: {json.dumps(demo, indent=2)[:500]}")

    # Source images. If no Pexels key, leave the cover empty — the prospect
    # demo page will still render with a colored bg from the palette.
    if cfg.pexels_api_key:
        log.info("Fetching cover + gallery images from Pexels")
        media = images.cover_and_gallery(
            cfg.pexels_api_key,
            industry=demo.get("industry") or prospect.get("industry") or "small business",
            business_name=prospect.get("business_name", ""),
        )
        demo["cover"] = media["cover"]
        demo["gallery"] = media["gallery"]
    else:
        demo.setdefault("cover", "")
        demo.setdefault("gallery", [])

    # Stamp the canonical contact info from the prospect when known — the LLM
    # makes plausible placeholders, but real phone/email beats invented ones.
    if prospect.get("contact_phone"):
        demo.setdefault("contact", {})["phone"] = prospect["contact_phone"]
    if prospect.get("contact_email"):
        demo.setdefault("contact", {})["email"] = prospect["contact_email"]

    return demo


def _profile_for_prompt(p: dict) -> str:
    parts = [
        f"Name: {p.get('business_name', '')}",
        f"Industry: {p.get('industry', 'unknown')}",
        f"Location: {p.get('location', 'unknown')}",
    ]
    if p.get("rating") is not None:
        parts.append(f"Rating: {p['rating']} ({p.get('review_count', 0)} reviews)")
    if p.get("current_website"):
        parts.append(f"Existing website: {p['current_website']} (likely outdated — we are replacing it)")
    if p.get("contact_phone"):
        parts.append(f"Phone: {p['contact_phone']}")
    if p.get("notes"):
        parts.append(f"Notes: {p['notes']}")
    return "\n".join(parts)
