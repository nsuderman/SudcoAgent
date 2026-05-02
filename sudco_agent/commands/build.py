"""`agent build` — generate a demo for a prospect using Qwen, store via API."""
from __future__ import annotations

import logging

from ..api_client import SudcoAPI
from ..config import Config
from ..generation.content import build_demo

log = logging.getLogger(__name__)


def run(cfg: Config, *, prospect_id: int) -> dict:
    with SudcoAPI.from_config(cfg) as api:
        prospect = api.get_prospect(prospect_id)
        if not prospect:
            raise RuntimeError(f"No prospect with id={prospect_id}")
        return _build_for(api, cfg, prospect)


def _build_for(api: SudcoAPI, cfg: Config, prospect: dict) -> dict:
    log.info("Building demo for prospect %d: %s", prospect["id"], prospect["business_name"])
    demo_data = build_demo(prospect, cfg)
    result = api.create_demo(
        prospect_id=prospect["id"],
        data=demo_data,
        status="pending_review",
        expires_in_days=cfg.demo_expires_days,
    )
    log.info("Created demo: token=%s url=%s", result["demo"]["token"], result["url"])
    return result


def run_all_pending(cfg: Config, *, min_rating: float = 4.0) -> int:
    """Build demos for every prospect that doesn't have one yet AND meets the
    rating threshold. Prospects without a rating are skipped — run `agent
    analyze` first to fetch them."""
    with SudcoAPI.from_config(cfg) as api:
        prospects = list(api.iter_prospects())
        existing_demo_prospects = {d["prospect_id"] for d in api.iter_demos()}

        skipped_no_rating = 0
        skipped_low_rating = 0
        skipped_chain = 0
        todo: list[dict] = []
        for p in prospects:
            if p["id"] in existing_demo_prospects:
                continue
            if p.get("is_chain"):
                skipped_chain += 1
                continue
            rating = p.get("rating")
            if rating is None:
                skipped_no_rating += 1
                continue
            if rating < min_rating:
                skipped_low_rating += 1
                continue
            todo.append(p)

        log.info(
            "%d prospects need demos (skipped %d chains, %d unrated, %d below %.1f stars)",
            len(todo), skipped_chain, skipped_no_rating, skipped_low_rating, min_rating,
        )

        built = 0
        for p in todo:
            try:
                _build_for(api, cfg, p)
                built += 1
            except Exception as exc:
                log.exception("Failed to build demo for prospect %d: %s", p["id"], exc)
        return built
