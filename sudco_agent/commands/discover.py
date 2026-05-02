"""`agent discover` — find prospects via Foursquare and store via the API.

Each run is recorded in the `discovery_searches` audit log so we can see
which (area, query) pairs we've already swept and skip recent re-runs.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..api_client import APIError, SudcoAPI
from ..config import Config
from ..discovery import foursquare

log = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    area: str,
    query: str | None = None,
    only_without_website: bool = False,
    skip_chains: bool = True,
    limit: int = 50,
    skip_if_recent_days: int | None = None,
) -> dict:
    """Search Foursquare and upsert each result as a prospect.

    Returns: {"stored": N, "raw": M, "skipped_recent": bool}
    """
    if not cfg.foursquare_api_key:
        raise RuntimeError("FOURSQUARE_API_KEY not set in .env")

    with SudcoAPI.from_config(cfg) as api:
        # Skip if same (area, query) ran recently
        if skip_if_recent_days and skip_if_recent_days > 0:
            last = api.last_search(area=area, query=query)
            if last and last.get("ran_at"):
                try:
                    when = datetime.fromisoformat(last["ran_at"].replace("Z", "+00:00"))
                    cutoff = datetime.now(timezone.utc) - timedelta(days=skip_if_recent_days)
                    if when > cutoff:
                        log.info("skip discover %r %r — last run at %s (within %dd)",
                                 area, query, last["ran_at"], skip_if_recent_days)
                        return {"stored": 0, "raw": 0, "skipped_recent": True}
                except ValueError:
                    pass

        log.info("Searching Foursquare: area=%r query=%r only_without_website=%s skip_chains=%s limit=%s",
                 area, query, only_without_website, skip_chains, limit)

        raw = 0
        stored = 0
        error_msg: str | None = None
        try:
            for prospect in foursquare.search(
                cfg.foursquare_api_key,
                near=area,
                query=query,
                only_without_website=only_without_website,
                skip_chains=skip_chains,
                limit=limit,
            ):
                raw += 1
                try:
                    saved = api.upsert_prospect(prospect)
                    stored += 1
                    log.info("upserted: id=%d %s — %s [rating=%s]",
                             saved["id"], saved["business_name"],
                             saved.get("location") or "?", saved.get("rating"))
                except APIError as exc:
                    log.error("Failed to upsert %s: %s", prospect.get("business_name"), exc)
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            log.exception("discover failed: %s", exc)
        finally:
            try:
                api.record_search(area=area, query=query, source="foursquare",
                                  raw_results=raw, stored=stored, error=error_msg)
            except APIError as exc:
                log.warning("could not record search log: %s", exc)

        if error_msg:
            raise RuntimeError(error_msg)
        return {"stored": stored, "raw": raw, "skipped_recent": False}
