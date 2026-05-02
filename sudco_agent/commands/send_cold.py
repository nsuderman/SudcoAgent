"""`agent send-cold` — blast a shared per-industry demo URL to many prospects.

Different from `agent send`:
  * No per-prospect demo row exists. The link is a stable URL like
    ``https://sudcosolutions.com/demos/bakery?from=<prospect_id>`` — one
    polished demo for all bakery prospects, the ``from=`` param is the only
    per-prospect breadcrumb.
  * Personalization happens in the EMAIL BODY (Jinja: business name, city)
    rather than in the demo content. That carries the "for you" feel without
    the cost of generating per-prospect demos.
  * Reaches prospects who have NO demo yet. The high-effort `agent build` →
    `review` → `send` flow is reserved for prospects who reply.

Each cold send is recorded by patching ``cold_outreach_at`` on the prospect
so we don't double-blast. Re-running honors a max-age window (default 30d).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table

from ..api_client import APIError, SudcoAPI
from ..config import Config
from ..data import industry_demos
from ..enrichment import site_review
from ..llm import LLMClient
from ..outreach import email as mailer

log = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    industry: str | None = None,
    limit: int | None = None,
    min_rating: float | None = None,
    require_no_website: bool = False,
    require_email: bool = True,
    dry_run: bool = False,
    yes: bool = False,
    site_review_enabled: bool = True,
) -> dict:
    """Blast the cold-outreach email — single-industry or all-industries.

    Args:
      industry: optional demo slug (e.g. ``"bakery"``, ``"apex-plumbing"``).
        When provided, only prospects matching that slug's keyword set
        (from ``industry_demos.json``) are emailed, and they're all linked
        to ``{base}/<industry>?from=<id>``. When omitted, every qualifying
        prospect is matched against the full mapping; each gets the demo
        URL for whichever slug they map to. Prospects that don't map to
        any slug are skipped (no demo to link them to).
      limit: cap on emails sent this run (None = no cap).
      min_rating: optional Google rating floor; None includes unrated.
      require_no_website: only target prospects whose ``current_website``
        is null (cold-blast people who *need* the demo).
      require_email: skip prospects with no ``contact_email``.
      dry_run: don't actually send. Prints intended recipients + URLs.
      yes: skip the interactive confirmation prompt.

    Returns: summary dict with counts (plus ``per_slug`` breakdown when
    running in all-industries mode).
    """
    summary: dict = {
        "matched": 0,
        "filtered_out": 0,
        "would_send": 0,
        "sent": 0,
        "errors": 0,
        "skipped_already_sent": 0,
        "skipped_not_analyzed": 0,
        "no_demo_match": 0,
        "with_site_observation": 0,
        "per_slug": {},
    }
    llm = LLMClient.from_config(cfg) if site_review_enabled else None
    industry_filter = industry.strip().lower() if industry else None

    console = Console()

    with SudcoAPI.from_config(cfg) as api:
        prospects = list(api.iter_prospects())
        log.info("Loaded %d total prospects from API", len(prospects))

        # Each target carries its own demo slug — for single-industry runs
        # the slug is the same for every target; for all-industries runs
        # the slug is per-prospect from the mapping. ``None`` means "no
        # per-industry demo for this prospect" — in all-industries mode they
        # still get sent (linked to the homepage); in single-industry mode
        # they're skipped because the operator picked a specific slug.
        targets: list[tuple[dict, str | None]] = []
        for p in prospects:
            slug = _slug_for_prospect(p, industry_filter)
            if industry_filter is not None and slug is None:
                # Operator picked a specific slug; this prospect doesn't
                # match it — not a candidate, don't bump matched.
                continue

            summary["matched"] += 1
            if slug is None:
                # All-industries mode + no slug match → falls back to the
                # homepage URL. Track this so operators can see how much
                # of the send goes to the generic landing page vs polished
                # per-industry demos.
                summary["no_demo_match"] += 1

            decision = _decide(
                p,
                min_rating=min_rating,
                require_no_website=require_no_website,
                require_email=require_email,
            )
            if decision == "skip_already_sent":
                summary["skipped_already_sent"] += 1
                continue
            if decision == "not_analyzed_yet":
                summary["skipped_not_analyzed"] += 1
                continue
            if decision != "go":
                summary["filtered_out"] += 1
                continue
            targets.append((p, slug))
            slug_key = slug if slug else "homepage"
            summary["per_slug"][slug_key] = summary["per_slug"].get(slug_key, 0) + 1

        if limit is not None and limit > 0:
            targets = targets[:limit]
            # Recompute per_slug after the limit so it reflects what we'll
            # actually send, not what we'd have sent without the cap.
            summary["per_slug"] = {}
            for _, slug in targets:
                slug_key = slug if slug else "homepage"
                summary["per_slug"][slug_key] = summary["per_slug"].get(slug_key, 0) + 1

        summary["would_send"] = len(targets)

        if not targets:
            label = f"slug={industry_filter!r}" if industry_filter else "(all industries)"
            console.print(
                f"[yellow]No prospects match {label}. "
                f"matched={summary['matched']} "
                f"filtered_out={summary['filtered_out']} "
                f"skipped_already_sent={summary['skipped_already_sent']} "
                f"skipped_not_analyzed={summary['skipped_not_analyzed']} "
                f"no_demo_match={summary['no_demo_match']})[/yellow]"
            )
            return summary

        _print_preview(console, targets[:5], industry_filter, cfg, summary)

        if dry_run:
            console.print(f"\n[bold yellow]DRY RUN — {len(targets)} emails NOT sent.[/bold yellow]")
            return summary

        if not yes and not _confirm(console, len(targets), industry_filter, cfg, summary):
            console.print("[yellow]cancelled[/yellow]")
            return summary

        with Progress(
            TextColumn("[bold blue]send-cold[/bold blue]"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("• [green]sent {task.fields[sent]}[/green] "
                       "• [cyan]w/obs {task.fields[obs]}[/cyan] "
                       "• [yellow]err {task.fields[errs]}[/yellow]"),
            TimeElapsedColumn(),
            TextColumn("ETA"),
            TimeRemainingColumn(),
            refresh_per_second=4,
        ) as bar:
            bar_id = bar.add_task("running", total=len(targets),
                                  sent=0, obs=0, errs=0)
            for p, slug in targets:
                observation: str | None = None
                if llm is not None:
                    try:
                        observation = site_review.get_observation(api, llm, p)
                    except Exception as exc:
                        log.warning("site_review failed for prospect %d: %s",
                                    p["id"], exc)
                if observation:
                    summary["with_site_observation"] += 1
                try:
                    _send_one(api, cfg, p, industry_slug=slug,
                              site_observation=observation)
                    summary["sent"] += 1
                except Exception as exc:
                    summary["errors"] += 1
                    log.exception("Failed to cold-send prospect %d (%s): %s",
                                  p["id"], p.get("business_name"), exc)
                bar.update(bar_id, sent=summary["sent"],
                           obs=summary["with_site_observation"],
                           errs=summary["errors"])
                bar.advance(bar_id)

    return summary


def _bare_domain(url: str) -> str:
    """Strip scheme and path from a URL — `https://sudcosolutions.com/demos`
    becomes `sudcosolutions.com`. Used as the visible link text in the email
    so recipients see a clean domain rather than the full tracking URL."""
    from urllib.parse import urlparse
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc or url


def _homepage_url(cold_demo_base_url: str) -> str:
    """Strip the path off the demo base URL to get the bare site root.
    `https://sudcosolutions.com/demos` → `https://sudcosolutions.com`. Used
    as the fallback landing page for prospects whose industry doesn't map
    to any per-industry demo slug."""
    from urllib.parse import urlparse
    p = urlparse(
        cold_demo_base_url if "://" in cold_demo_base_url
        else f"https://{cold_demo_base_url}"
    )
    return f"{p.scheme}://{p.netloc}"


def _slug_for_prospect(prospect: dict, industry_filter: str | None) -> str | None:
    """Pick the demo slug to use for this prospect.

    Single-industry mode (``industry_filter`` set): returns the filter
    value if the prospect matches that slug's keyword set, else None.
    All-industries mode (``industry_filter`` is None): returns whatever
    slug the prospect maps to via ``industry_demos.find_slug``, or None
    if no slug covers them.
    """
    if industry_filter is not None:
        return industry_filter if industry_demos.matches_slug(prospect, industry_filter) else None
    return industry_demos.find_slug(prospect)


def _decide(
    p: dict,
    *,
    min_rating: float | None,
    require_no_website: bool,
    require_email: bool,
) -> str:
    """Return 'go' to send, or a short reason string to skip.

    First-touch only: if ``cold_outreach_at`` is set at all, this prospect
    is permanently skipped here (and handled by ``agent followup-cold``
    instead). No re-blast path.
    """
    if require_email and not p.get("contact_email"):
        return "no_email"
    if require_no_website and p.get("current_website"):
        return "has_website"
    if p.get("is_chain"):
        return "is_chain"
    if p.get("bounced_at"):
        return "bounced"
    if p.get("unsubscribed_at"):
        return "unsubscribed"
    # Pipeline gate: don't email a prospect that hasn't been seen by `analyze`
    # yet. Either a rating is set OR an attempt was logged is sufficient —
    # both mean we tried to fetch Google data. Without this gate, freshly-
    # discovered prospects can be emailed before analyze populates ratings
    # and review_excerpts, which permanently caches a weaker (site-only)
    # site_observation and locks them out of future sends.
    if p.get("rating") is None and not p.get("last_rating_attempt"):
        return "not_analyzed_yet"
    if min_rating is not None:
        rating = p.get("rating")
        if rating is None or rating < min_rating:
            return "below_min_rating"
    if p.get("cold_outreach_at"):
        return "skip_already_sent"
    return "go"


def _send_one(api: SudcoAPI, cfg: Config, prospect: dict, *,
              industry_slug: str | None,
              site_observation: str | None = None) -> None:
    """Send one cold email.

    If ``industry_slug`` is set, link to ``{base}/<slug>?from=<id>`` (the
    polished per-industry demo). If it's None — the prospect's industry
    doesn't map to any demo we've built — fall back to linking to the
    main site (``sudcosolutions.com?from=<id>``). The ``?from=<id>`` is
    preserved either way so the click still gets logged via
    ``POST /api/showcase_views`` (the homepage needs the same JS hook
    the demo pages have)."""
    if industry_slug:
        demo_url = (
            f"{cfg.cold_demo_base_url}/{industry_slug}?from={prospect['id']}"
        )
        has_demo = True
    else:
        demo_url = f"{_homepage_url(cfg.cold_demo_base_url)}?from={prospect['id']}"
        has_demo = False
    # Visible link text is the bare domain in either case — clean and
    # consistent regardless of which path the href actually points to.
    demo_visible_url = _bare_domain(cfg.cold_demo_base_url)
    industry = prospect.get("industry") or (industry_slug or "small business")
    template_vars = dict(
        business_name=prospect["business_name"],
        recipient_name=None,
        industry=industry,
        industry_lower=industry.lower(),
        location=prospect.get("location"),
        demo_url=demo_url,
        demo_visible_url=demo_visible_url,
        site_observation=site_observation,
        has_demo=has_demo,
    )
    subject, body = mailer.render("first_outreach_cold.txt", **template_vars)
    html_body = mailer.render_html("first_outreach_cold.html",
                                   subject_preview=subject, **template_vars)
    mailer.send(cfg, to=prospect["contact_email"], subject=subject, body=body,
                html_body=html_body, reply_to=cfg.smtp_user,
                unsubscribe_id=prospect["id"])
    try:
        api.update_prospect(prospect["id"], {
            "cold_outreach_at": datetime.now(timezone.utc).isoformat(),
        })
    except APIError as exc:
        # Don't blow up the run — the email already went out. Log and move on.
        log.error("API patch (cold_outreach_at) failed for prospect %d: %s",
                  prospect["id"], exc)


def _print_preview(console: Console, sample: list[tuple[dict, str]],
                   industry: str | None, cfg: Config, summary: dict) -> None:
    title = (f"Cold-blast preview — slug={industry!r}" if industry
             else "Cold-blast preview — all-industries (per-prospect demo)")
    table = Table(title=title, show_lines=False)
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Business")
    table.add_column("Location")
    table.add_column("Email", style="green")
    table.add_column("Rating", justify="right")
    # Show per-prospect demo slug only in all-industries mode — in single-
    # industry mode the slug is identical for every row, so the column is
    # noise.
    if industry is None:
        table.add_column("Demo", style="cyan")
    for p, slug in sample:
        row = [
            str(p["id"]),
            p.get("business_name", "—"),
            p.get("location") or "—",
            p.get("contact_email") or "—",
            f"{p['rating']:.1f}" if p.get("rating") is not None else "—",
        ]
        if industry is None:
            row.append(slug)
        table.add_row(*row)
    console.print(table)

    if industry:
        sample_url = f"{cfg.cold_demo_base_url}/{industry}?from=<id>"
        console.print(f"\nLink template: [cyan]{sample_url}[/cyan]")
    elif summary.get("per_slug"):
        breakdown = ", ".join(
            f"{s}={c}" for s, c in sorted(summary["per_slug"].items(),
                                          key=lambda kv: -kv[1])
        )
        console.print(f"\nPer-demo breakdown: [cyan]{breakdown}[/cyan]")

    extras = []
    if summary.get("skipped_not_analyzed"):
        extras.append(f"skipped_not_analyzed=[dim]{summary['skipped_not_analyzed']}[/dim]")
    if summary.get("no_demo_match"):
        extras.append(f"no_demo_match=[dim]{summary['no_demo_match']}[/dim]")
    extras_str = " " + " ".join(extras) if extras else ""
    console.print(
        f"matched=[bold]{summary['matched']}[/bold] "
        f"filtered_out=[dim]{summary['filtered_out']}[/dim] "
        f"skipped_already_sent=[dim]{summary['skipped_already_sent']}[/dim]"
        f"{extras_str} "
        f"→ would_send=[bold green]{summary['would_send']}[/bold green]"
    )


def _confirm(console: Console, n: int, industry: str | None,
             cfg: Config, summary: dict) -> bool:
    if industry:
        console.print(
            f"\n[bold]About to send {n} cold emails[/bold] for slug={industry!r} "
            f"(linking to {cfg.cold_demo_base_url}/{industry}/...)."
        )
    else:
        console.print(
            f"\n[bold]About to send {n} cold emails[/bold] across "
            f"{len(summary.get('per_slug', {}))} demos "
            f"(per-prospect URL from industry_demos.json)."
        )
    if cfg.dry_run:
        console.print("[yellow]DRY_RUN=true in config — emails will be logged, not sent.[/yellow]")
    resp = input("Proceed? [y/N] ").strip().lower()
    return resp == "y"
