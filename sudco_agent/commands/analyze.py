"""`agent analyze` — fetch Google Maps rating for each prospect via Playwright.

Improvements layered on top of the original simple-pool design:

  1. Per-fetch BrowserContext. Each request opens a fresh context (fresh
     cookies, fresh fingerprint) and closes it after. One captcha no longer
     fate-shares with three other in-flight workers.
  2. Random user-agent per context, jittered per-worker delay, and a random
     startup offset per worker. Breaks the "4 requests every 1.5s on the dot"
     pattern that's easy for Google to flag.
  3. Graceful captcha back-off. First captcha is logged. Beyond that, the
     run halves concurrency, doubles delay, and inserts a 60s cooldown.
     Only aborts when we've already throttled to (concurrency=1, delay=30)
     and *still* hit captchas — at that point the IP is genuinely blocked
     and continuing wastes cycles.
  4. Persist `rating_lookup_status` + `last_rating_attempt` for every outcome.
     A future run skips prospects whose previous lookup said `not_found`
     within the last 30 days, so we stop re-hammering listings that legitimately
     don't have a rating.

The work is parallelized across N concurrent workers pulling from a queue.
Aggregate request rate is roughly `concurrency / delay`. Each captcha-driven
back-off halves throughput, so a run that starts at 4/1.5s ≈ 2.7 req/s ends
at 1/30s ≈ 0.03 req/s after two back-offs.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright
from rich.progress import (BarColumn, MofNCompleteColumn, Progress,
                           TextColumn, TimeElapsedColumn, TimeRemainingColumn)

from ..analysis.gmaps import fetch_rating, fetch_reviews
from ..api_client import APIError, SudcoAPI
from ..config import Config

# Top-N reviews to scrape per prospect. Five gives the LLM enough material
# to write something specific without bloating the prompt. Reviews come
# back chronologically newest-first when "Most relevant" is the default sort.
REVIEWS_TO_SCRAPE = 5

log = logging.getLogger(__name__)

# Hard floor / ceiling for back-off. We never throttle below 1 worker or
# stretch delay past 30s — beyond that we declare defeat and abort.
MIN_CONCURRENCY = 1
MAX_DELAY = 30.0
COOLDOWN_AFTER_BACKOFF_S = 60

# Recycle the shared chromium instance every N completed prospects. Long-lived
# browsers leak memory across thousands of BrowserContext create/destroy cycles
# and eventually crash, taking every in-flight worker down with them with
# "Connection closed while reading from the driver". 500 is well under the
# observed empirical failure point (~4000+ prospects per browser instance) and
# adds ~1s of dead time per cycle.
RECYCLE_EVERY_N_PROSPECTS = 500

# Skip windows by previous lookup outcome. Tuned for the assumption that
# Google's results are mostly stable: a "not_found" today is almost certainly
# still not_found next week.
SKIP_WINDOW_BY_STATUS = {
    "not_found": timedelta(days=30),
    "found":     timedelta(days=30),
    "captcha":   timedelta(hours=4),
    "timeout":   timedelta(hours=24),
    "error":     timedelta(days=7),
}

# UA pool — modern Chromium across major OSes. We rotate per fetch.
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
]


def run(
    cfg: Config,
    *,
    only_id: int | None = None,
    force: bool = False,
    delay_seconds: float = 1.5,
    concurrency: int = 4,
    limit: int | None = None,
) -> dict:
    """Returns a summary dict: { processed, rating_found, no_rating, skipped, errors,
    captchas, aborted }."""
    return asyncio.run(_run_async(
        cfg, only_id=only_id, force=force,
        delay_seconds=delay_seconds, concurrency=concurrency, limit=limit,
    ))


def _classify_result_error(err: str | None) -> str:
    """Map a RatingResult.error string to a short status label that we
    persist back to the API. Mirrors the SKIP_WINDOW_BY_STATUS keys."""
    if not err:
        return "found"
    if err.startswith("captcha:"):
        return "captcha"
    if err.startswith("timeout"):
        return "timeout"
    if err.startswith("no rating-shaped"):
        return "not_found"
    return "error"


def _should_skip(prospect: dict, *, force: bool, now: datetime) -> bool:
    """True if this prospect doesn't need a fresh lookup.

    A prospect is considered "complete" only when it has BOTH a rating AND
    a review_excerpts blob — those are skipped permanently. Otherwise, the
    `last_rating_attempt` timestamp is gated by SKIP_WINDOW_BY_STATUS so we
    don't re-hammer prospects whose previous lookup landed in any known
    status (including "found" — i.e. rating saved but reviews scrape came
    back empty — those wait 30d before re-attempting reviews).
    """
    if force:
        return False
    if prospect.get("rating") is not None and _has_reviews(prospect):
        return True
    status = prospect.get("rating_lookup_status")
    last_attempt_str = prospect.get("last_rating_attempt")
    if not status or not last_attempt_str:
        return False
    window = SKIP_WINDOW_BY_STATUS.get(status)
    if window is None:
        return False
    try:
        last_attempt = datetime.fromisoformat(last_attempt_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - last_attempt < window


def _has_reviews(prospect: dict) -> bool:
    """True if the prospect already has a non-empty review_excerpts blob.

    Stored as a JSON-encoded string by the API (see backend's `updateProspect`),
    so we parse defensively. Treat parse failure as "no reviews" so we
    re-attempt rather than poison-pill the prospect.
    """
    raw = prospect.get("review_excerpts")
    if not raw:
        return False
    if isinstance(raw, list):
        return len(raw) > 0
    if isinstance(raw, str):
        try:
            import json as _json
            parsed = _json.loads(raw)
        except (ValueError, TypeError):
            return False
        return isinstance(parsed, list) and len(parsed) > 0
    return False


async def _fetch_with_fresh_context(browser, prospect: dict, *, selector_wait_ms: int):
    """Each request gets its own BrowserContext so cookies and fingerprint
    state from a flagged request don't carry over to the next one.

    On a successful rating fetch this also scrapes the top-N review excerpts
    under the same context (shared cookies / UA — one fewer request that
    looks "different" to Google). Reviews are best-effort; failure here
    doesn't affect the rating result we return.

    Returns ``(RatingResult, list[dict])`` — reviews list is empty unless
    the rating was found AND the review-page scrape succeeded.
    """
    from playwright.async_api import Error as PWError

    try:
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
    except PWError as exc:
        # Browser already torn down (e.g. abort raced with this worker).
        from ..analysis.gmaps import RatingResult
        return RatingResult(error=f"browser closed: {exc}"), []
    try:
        result = await fetch_rating(
            context, prospect["business_name"], prospect.get("location"),
            selector_wait_ms=selector_wait_ms,
        )
        reviews: list[dict] = []
        # Only scrape reviews if the rating succeeded — captcha / timeout /
        # not_found all mean the place page isn't reachable for this prospect
        # right now, and a second navigation just wastes a request.
        if result.rating is not None:
            try:
                reviews = await fetch_reviews(
                    context, prospect["business_name"], prospect.get("location"),
                    n=REVIEWS_TO_SCRAPE, selector_wait_ms=selector_wait_ms,
                )
            except Exception as exc:
                log.debug("fetch_reviews errored for %s: %s",
                          prospect.get("business_name"), exc)
        return result, reviews
    finally:
        try:
            await context.close()
        except Exception:
            # TargetClosedError, browser already gone, etc. Nothing to do.
            pass


async def _run_async(
    cfg: Config,
    *,
    only_id: int | None,
    force: bool,
    delay_seconds: float,
    concurrency: int,
    limit: int | None,
) -> dict:
    summary = {"processed": 0, "rating_found": 0, "no_rating": 0, "skipped": 0,
               "errors": 0, "captchas": 0, "aborted": False, "backoffs": 0,
               "recycles": 0}

    with SudcoAPI.from_config(cfg) as api:
        if only_id is not None:
            single = api.get_prospect(only_id)
            if not single:
                raise RuntimeError(f"No prospect with id={only_id}")
            prospects = [single]
        else:
            prospects = list(api.iter_prospects())

        now = datetime.now(timezone.utc)
        todo: list[dict] = []
        for p in prospects:
            if _should_skip(p, force=force, now=now):
                summary["skipped"] += 1
                continue
            todo.append(p)

        if limit is not None and limit > 0:
            todo = todo[:limit]

        log.info("Analyzing %d prospects (concurrency=%d, base delay=%.1fs, "
                 "skipped=%d already-known)",
                 len(todo), concurrency, delay_seconds, summary["skipped"])

        if not todo:
            return summary

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            queue: asyncio.Queue = asyncio.Queue()
            for p in todo:
                queue.put_nowait(p)

            # Mutable run state — workers read these on every iteration so
            # back-off and recycle changes take effect immediately. The
            # browser lives here (not as a local) so the recycler task can
            # swap it out from under the workers.
            browser_ready = asyncio.Event()
            browser_ready.set()
            state = {
                "delay": delay_seconds,
                "concurrency_target": concurrency,
                "captchas_at_last_backoff": 0,
                "lock": asyncio.Lock(),
                "browser": browser,
                "browser_ready": browser_ready,
                "in_flight": 0,
                "completed_since_recycle": 0,
            }

            # Three stacked bars — one per outcome bucket. Each prospect
            # advances exactly one bar, so the three bars together visually
            # divide the run total into success / no-rating / problem fractions.
            with Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("{task.fields[suffix]}"),
                TimeElapsedColumn(),
                refresh_per_second=4,
            ) as bar:
                total = len(todo)
                task_found = bar.add_task(
                    "[green]found    [/green]", total=total,
                    suffix=f"• c={concurrency} d={delay_seconds:.1f}s",
                )
                task_norating = bar.add_task(
                    "[yellow]no rating[/yellow]", total=total, suffix="",
                )
                task_problem = bar.add_task(
                    "[red]captcha  [/red]", total=total, suffix="",
                )

                # Each prospect ends up in exactly one bucket. To make every
                # bar a "true 0→100% progress bar" whose denominator is the
                # discovered bucket size, decrement the OTHER two bars' totals
                # whenever this one advances. End of run: each bar = N/N.
                bar_totals = {task_found: total, task_norating: total, task_problem: total}

                def land(landing_task) -> None:
                    bar.advance(landing_task)
                    for t in (task_found, task_norating, task_problem):
                        if t is landing_task:
                            continue
                        bar_totals[t] -= 1
                        bar.update(t, total=bar_totals[t])

                async def maybe_backoff() -> None:
                    """Called after a captcha. Halves concurrency, doubles delay,
                    pauses for cooldown. Idempotent within a single back-off
                    decision — only acts when captcha count moved since last
                    back-off."""
                    async with state["lock"]:
                        if summary["captchas"] <= state["captchas_at_last_backoff"]:
                            return
                        state["captchas_at_last_backoff"] = summary["captchas"]
                        old_conc = state["concurrency_target"]
                        old_delay = state["delay"]
                        state["concurrency_target"] = max(MIN_CONCURRENCY, old_conc // 2)
                        state["delay"] = min(MAX_DELAY, old_delay * 2)
                        summary["backoffs"] += 1

                        already_floored = (old_conc == MIN_CONCURRENCY and old_delay >= MAX_DELAY)
                        if already_floored:
                            summary["aborted"] = True
                            log.error(
                                "Aborting analyze. Already at min concurrency=%d / max delay=%.1fs "
                                "and still hitting captchas (%d). Wait several hours and try again "
                                "from a different IP if possible. All progress so far is saved.",
                                MIN_CONCURRENCY, MAX_DELAY, summary["captchas"],
                            )
                            return

                        log.warning(
                            "BACK-OFF #%d: concurrency %d→%d, delay %.1fs→%.1fs, cooling %ds. "
                            "Captchas so far: %d.",
                            summary["backoffs"], old_conc, state["concurrency_target"],
                            old_delay, state["delay"], COOLDOWN_AFTER_BACKOFF_S, summary["captchas"],
                        )
                        bar.update(task_found,
                                   suffix=f"• c={state['concurrency_target']} "
                                          f"d={state['delay']:.1f}s "
                                          f"(backoff #{summary['backoffs']})")
                    await asyncio.sleep(COOLDOWN_AFTER_BACKOFF_S)

                async def patch_status(prospect_id: int, *, status: str,
                                        rating: float | None, review_count: int | None,
                                        review_excerpts: list[dict] | None = None) -> None:
                    """Persist outcome of one lookup back to the API. Best-effort —
                    an API blip here doesn't block the rest of the run."""
                    import json as _json
                    payload: dict = {
                        "rating_lookup_status": status,
                        "last_rating_attempt": datetime.now(timezone.utc).isoformat(),
                    }
                    if rating is not None:
                        payload["rating"] = rating
                        payload["review_count"] = review_count
                    # Only persist review_excerpts when we actually scraped some.
                    # Empty list = "tried, got nothing" — leave the field as-is
                    # so a later run could try again without overwriting good data.
                    if review_excerpts:
                        payload["review_excerpts"] = _json.dumps(review_excerpts)
                    try:
                        await asyncio.to_thread(api.update_prospect, prospect_id, payload)
                    except APIError as exc:
                        summary["errors"] += 1
                        log.error("API patch failed for prospect %d: %s", prospect_id, exc)

                async def recycler() -> None:
                    """Periodically close + relaunch the shared chromium instance.
                    Pauses workers via ``browser_ready``, drains in-flight requests,
                    swaps the browser, then re-enables workers. Exits when the
                    queue is fully drained or the run is aborted."""
                    try:
                        while not summary["aborted"]:
                            await asyncio.sleep(2.0)
                            if queue.empty() and state["in_flight"] == 0:
                                return
                            if state["completed_since_recycle"] < RECYCLE_EVERY_N_PROSPECTS:
                                continue

                            state["browser_ready"].clear()
                            # Drain workers that already grabbed a prospect.
                            while state["in_flight"] > 0:
                                await asyncio.sleep(0.2)

                            old_browser = state["browser"]
                            try:
                                await old_browser.close()
                            except Exception:
                                pass
                            try:
                                state["browser"] = await pw.chromium.launch(headless=True)
                            except Exception as exc:
                                log.error(
                                    "Failed to relaunch chromium during recycle: %s — "
                                    "aborting run.", exc,
                                )
                                summary["aborted"] = True
                                state["browser_ready"].set()  # let workers exit
                                return

                            state["completed_since_recycle"] = 0
                            summary["recycles"] += 1
                            log.info(
                                "Browser recycled (#%d) after %d prospects — fresh chromium up.",
                                summary["recycles"], RECYCLE_EVERY_N_PROSPECTS,
                            )
                            state["browser_ready"].set()
                    except asyncio.CancelledError:
                        return

                async def worker(worker_idx: int) -> None:
                    # Stagger the initial fire so all N workers don't hit Google
                    # in the same TCP-tick.
                    await asyncio.sleep(random.uniform(0, max(0.1, state["delay"])))
                    while not summary["aborted"]:
                        # Capacity drop: if back-off shrank the worker pool below
                        # this worker's index, retire cleanly.
                        if worker_idx >= state["concurrency_target"]:
                            return
                        # Pause here while the recycler swaps the browser. Cheap
                        # no-op when the event is already set.
                        await state["browser_ready"].wait()
                        if summary["aborted"]:
                            return
                        try:
                            prospect = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return

                        state["in_flight"] += 1
                        try:
                            result, reviews = await _fetch_with_fresh_context(
                                state["browser"], prospect, selector_wait_ms=4000,
                            )
                        finally:
                            state["in_flight"] -= 1
                        label = prospect["business_name"]
                        status = _classify_result_error(result.error)

                        if status == "captcha":
                            summary["captchas"] += 1
                            summary["errors"] += 1
                            log.error("CAPTCHA on %s — %s (total: %d)",
                                      label, result.error, summary["captchas"])
                            land(task_problem)
                            await patch_status(prospect["id"], status="captcha",
                                               rating=None, review_count=None)
                            await maybe_backoff()
                            if summary["aborted"]:
                                return
                        elif status == "found":
                            summary["rating_found"] += 1
                            if reviews:
                                summary.setdefault("reviews_scraped", 0)
                                summary["reviews_scraped"] += 1
                            land(task_found)
                            await patch_status(prospect["id"], status="found",
                                               rating=result.rating,
                                               review_count=result.review_count,
                                               review_excerpts=reviews)
                        elif status == "not_found":
                            summary["no_rating"] += 1
                            land(task_norating)
                            await patch_status(prospect["id"], status="not_found",
                                               rating=None, review_count=None)
                        elif status == "timeout":
                            summary["errors"] += 1
                            log.warning("%s → %s", label, result.error)
                            land(task_norating)
                            await patch_status(prospect["id"], status="timeout",
                                               rating=None, review_count=None)
                        else:  # generic error
                            summary["errors"] += 1
                            log.warning("%s → %s", label, result.error)
                            land(task_problem)
                            await patch_status(prospect["id"], status="error",
                                               rating=None, review_count=None)

                        state["completed_since_recycle"] += 1

                        # Jittered delay — ±40% of the current base delay.
                        await asyncio.sleep(state["delay"] * random.uniform(0.6, 1.4))

                # Spawn the maximum starting workers. Some will retire as
                # back-off shrinks `concurrency_target`.
                workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]
                recycler_task = asyncio.create_task(recycler())
                try:
                    await asyncio.gather(*workers, return_exceptions=True)
                finally:
                    recycler_task.cancel()
                    try:
                        await recycler_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    # Yield a tick so Playwright internal tasks (request
                    # listeners spun up inside contexts that closed mid-flight)
                    # finish before the browser is torn down. Without this,
                    # those tasks raise TargetClosedError into the loop after
                    # the parent disappears, polluting the log.
                    await asyncio.sleep(0.1)
                    try:
                        await state["browser"].close()
                    except Exception:
                        pass

    return summary
