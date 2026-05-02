"""`agent sweep` — run `discover` across the top N US cities.

Designed to be safely re-runnable: each city's search is recorded in
discovery_searches, and we skip cities searched within
`--skip-if-recent-days` (default 7).
"""
from __future__ import annotations

import logging
import time

from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)

from ..config import Config
from ..data.top_us_cities import top, TOP_TX_CITIES, TOP_US_CITIES
from . import discover

log = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    query: str | None,
    top_n: int = 200,
    region: str = "all",
    only_without_website: bool = False,
    skip_chains: bool = True,
    limit_per_city: int = 5000,
    delay_seconds: float = 3.0,
    skip_if_recent_days: int = 7,
) -> dict:
    cities = top(top_n, region=region)
    log.info("Sweeping top %d cities (region=%s) for query=%r (skip if searched within %dd)",
             len(cities), region, query, skip_if_recent_days)

    summary = {
        "cities": len(cities),
        "searched": 0,
        "skipped_recent": 0,
        "errors": 0,
        "total_stored": 0,
    }

    # Two stacked bars:
    #   1) cities  — fixed total = N, advances per city (whether searched or skipped)
    #   2) upserts — running prospect counter, total grows as we discover more.
    #      Total starts at 1 to give the bar a non-zero denominator; we bump it
    #      forward with each batch so the bar pulses without ever filling.
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[suffix]}"),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        refresh_per_second=4,
    ) as bar:
        cities_task = bar.add_task(
            "[bold blue]cities   [/bold blue]", total=len(cities),
            suffix="• [cyan]starting…[/cyan]",
        )
        upsert_task = bar.add_task(
            "[bold green]upserts  [/bold green]", total=1,
            suffix="• [dim]0 new prospects[/dim]",
        )

        for i, (rank, city, state, _pop) in enumerate(cities):
            area = f"{city}, {state}"
            bar.update(cities_task, suffix=f"• [cyan]{area}[/cyan]")
            stored_this_city = 0
            try:
                result = discover.run(
                    cfg,
                    area=area,
                    query=query,
                    only_without_website=only_without_website,
                    skip_chains=skip_chains,
                    limit=limit_per_city,
                    skip_if_recent_days=skip_if_recent_days,
                )
                if result.get("skipped_recent"):
                    summary["skipped_recent"] += 1
                else:
                    summary["searched"] += 1
                    stored_this_city = result.get("stored", 0)
                    summary["total_stored"] += stored_this_city
            except Exception as exc:
                summary["errors"] += 1
                log.exception("sweep failed for %s: %s", area, exc)

            # Cities bar: advance one slot.
            bar.advance(cities_task)

            # Upsert bar: advance by the prospects we just stored. Bump the
            # task total a step ahead of `completed` so the bar shows real
            # progress per city instead of pinning at 100% forever.
            if stored_this_city:
                cur_total = bar.tasks[upsert_task].total or 1
                bar.update(
                    upsert_task,
                    completed=summary["total_stored"],
                    total=max(cur_total, summary["total_stored"] + 1),
                    suffix=(
                        f"• [green]{summary['total_stored']} new "
                        f"prospects[/green] "
                        f"• [dim]skipped {summary['skipped_recent']}[/dim] "
                        f"• [yellow]err {summary['errors']}[/yellow]"
                    ),
                )
            else:
                # Even on a skip/err we want the suffix counters to update.
                bar.update(
                    upsert_task,
                    suffix=(
                        f"• [green]{summary['total_stored']} new "
                        f"prospects[/green] "
                        f"• [dim]skipped {summary['skipped_recent']}[/dim] "
                        f"• [yellow]err {summary['errors']}[/yellow]"
                    ),
                )

            # Politeness — don't hammer Foursquare even though they allow bursts
            if delay_seconds > 0 and i < len(cities) - 1:
                time.sleep(delay_seconds)

        # End of run: lock the upsert bar at 100%.
        final = max(summary["total_stored"], 1)
        bar.update(upsert_task, total=final, completed=final)

    return summary
