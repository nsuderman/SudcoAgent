"""`agent enrich` — fill in missing contact info on existing prospects.

Today: scrape the prospect's website for a contact email when Foursquare
didn't provide one. The enrichment metadata (which pages we hit, what we
found, errors) goes into the `enrichment` JSON column on the prospect.

Designed to be safely re-runnable: skips prospects already enriched in the
last `--max-age-days` (default 30) unless `--force` is set.

Concurrency: an async worker pool (default 4) parallelises the slow
website fetches. Each worker pulls one prospect off a queue, runs the
sync `crawl()` via ``asyncio.to_thread``, then patches the API the same
way. Skips (in-memory decisions: skip_has_email, skip_recent) are
processed up front and don't touch the queue. The "no website" case is
queued so its API record-keeping benefits from the worker pool too.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)

from ..api_client import APIError, SudcoAPI
from ..config import Config
from ..enrichment.email_scraper import crawl

log = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    only_id: int | None = None,
    max_age_days: int = 30,
    force: bool = False,
    concurrency: int = 4,
) -> dict:
    """Returns a summary dict: { processed, found_email, no_email, skipped, errors }."""
    return asyncio.run(_run_async(
        cfg, only_id=only_id, max_age_days=max_age_days,
        force=force, concurrency=max(1, concurrency),
    ))


async def _run_async(
    cfg: Config,
    *,
    only_id: int | None,
    max_age_days: int,
    force: bool,
    concurrency: int,
) -> dict:
    summary = {"processed": 0, "found_email": 0, "no_email": 0,
               "skipped": 0, "errors": 0}

    with SudcoAPI.from_config(cfg) as api:
        if only_id is not None:
            single = api.get_prospect(only_id)
            if not single:
                raise RuntimeError(f"No prospect with id={only_id}")
            prospects = [single]
        else:
            prospects = list(api.iter_prospects())

        # Decide the fate of each prospect up front.
        #   skip_has_email / skip_recent → in-memory only, count and move on.
        #   skip_no_website → queue with a marker so the API patch happens via workers.
        #   go              → queue for a real crawl.
        queue: asyncio.Queue = asyncio.Queue()
        queued = 0
        for p in prospects:
            decision = _decide(p, max_age_days=max_age_days, force=force)
            if decision in ("skip_has_email", "skip_recent"):
                summary["skipped"] += 1
                continue
            queue.put_nowait((p, decision))
            queued += 1

        log.info(
            "Enriching %d prospects with %d workers "
            "(skipped %d already-known up front)",
            queued, concurrency, summary["skipped"],
        )

        if queued == 0:
            return summary

        with Progress(
            TextColumn("[bold blue]enrich"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("• [green]found {task.fields[found]}[/green] "
                       "• [dim]none {task.fields[none]}[/dim] "
                       "• [dim]skipped {task.fields[skipped]}[/dim] "
                       "• [yellow]err {task.fields[errs]}[/yellow]"),
            TimeElapsedColumn(),
            TextColumn("ETA"),
            TimeRemainingColumn(),
            refresh_per_second=4,
        ) as bar:
            bar_id = bar.add_task(
                "running", total=queued,
                found=0, none=0, skipped=summary["skipped"], errs=0,
            )

            async def worker(idx: int) -> None:
                while True:
                    try:
                        prospect, decision = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await _process_one(api, prospect, decision, summary)
                    bar.update(bar_id,
                               found=summary["found_email"],
                               none=summary["no_email"],
                               skipped=summary["skipped"],
                               errs=summary["errors"])
                    bar.advance(bar_id)

            workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]
            await asyncio.gather(*workers, return_exceptions=True)

    return summary


async def _process_one(api: SudcoAPI, prospect: dict, decision: str,
                       summary: dict) -> None:
    """Handle a single prospect end-to-end (crawl + record)."""
    if decision == "skip_no_website":
        # Just mark enriched_at so future runs skip via skip_recent.
        try:
            await asyncio.to_thread(
                _record, api, prospect["id"], result=None,
                error="no website to crawl",
            )
        except Exception as exc:
            summary["errors"] += 1
            log.error("API record (no-website) failed for prospect %d: %s",
                      prospect["id"], exc)
        else:
            summary["skipped"] += 1
        return

    # decision == "go" — real crawl.
    summary["processed"] += 1
    try:
        result = await asyncio.to_thread(crawl, prospect["current_website"])
    except Exception as exc:
        summary["errors"] += 1
        log.exception("crawl failed for prospect %d: %s", prospect["id"], exc)
        try:
            await asyncio.to_thread(
                _record, api, prospect["id"], result=None, error=str(exc),
            )
        except APIError as api_exc:
            log.error("API patch (crawl error) failed for prospect %d: %s",
                      prospect["id"], api_exc)
        return

    try:
        await asyncio.to_thread(
            _record, api, prospect["id"], result=result, error=result.error,
        )
    except APIError as exc:
        summary["errors"] += 1
        log.error("API patch failed for prospect %d: %s", prospect["id"], exc)
        return

    if result.best_email():
        summary["found_email"] += 1
        log.info("%s → %s", prospect["business_name"], result.best_email())
    else:
        summary["no_email"] += 1


def _decide(p: dict, *, max_age_days: int, force: bool) -> str:
    if p.get("contact_email"):
        return "skip_has_email"
    if not force and p.get("enriched_at"):
        try:
            last = datetime.fromisoformat(p["enriched_at"].replace("Z", "+00:00"))
            cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
            if last > cutoff:
                return "skip_recent"
        except ValueError:
            pass  # bad timestamp — fall through to go
    if not p.get("current_website"):
        return "skip_no_website"
    return "go"


def _record(api: SudcoAPI, prospect_id: int, *, result, error: str | None) -> None:
    """Persist enrichment outcome — best email if any, plus full metadata."""
    payload: dict = {
        "enriched_at": datetime.now(timezone.utc).isoformat(),
        "enrichment": {
            "emails_found": result.emails if result else [],
            "pages_fetched": result.pages_fetched if result else [],
            "mx_rejected": result.mx_rejected if result else [],
            "error": error,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    if result and result.best_email():
        payload["contact_email"] = result.best_email()
    api.update_prospect(prospect_id, payload)
