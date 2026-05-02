"""`agent followup-cold` — send a follow-up to prospects who got the
cold-blast email but haven't replied.

Mirrors `agent send-cold` but with a different filter: targets prospects
who already have ``cold_outreach_at`` set (got the first email), don't
have ``cold_followup_at`` set yet (haven't been followed up), and aren't
flagged as having replied (``cold_replied_at`` null).

Default cadence: only follow up if the cold email was sent at least 7
days ago (controlled via ``--days-since-outreach``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table

from ..api_client import APIError, SudcoAPI
from ..config import Config
from ..data import industry_demos
from ..outreach import email as mailer

log = logging.getLogger(__name__)


def run(
    cfg: Config,
    *,
    industry: str | None = None,
    limit: int | None = None,
    days_since_outreach: int = 7,
    min_rating: float | None = None,
    dry_run: bool = False,
    yes: bool = False,
) -> dict:
    """Send a follow-up to prospects who got the cold email at least N
    days ago and haven't been followed up or replied.

    Args:
      industry: optional demo slug. Same semantics as ``send-cold``: when
        provided, only prospects matching that slug's keyword set are
        targeted; when omitted, all qualifying prospects are matched
        against ``industry_demos.json`` and follow-ups go to whichever
        slug each prospect maps to. Prospects with no slug match are
        skipped (no demo URL to link to).
      limit: cap on emails this run.
      days_since_outreach: only follow up if cold_outreach_at is at least
        N days old (default 7).
      min_rating: optional Google rating floor.
      dry_run: don't actually send.
      yes: skip the interactive confirmation prompt.

    Returns: summary dict.
    """
    summary: dict = {
        "matched": 0,
        "filtered_out": 0,
        "would_send": 0,
        "sent": 0,
        "errors": 0,
        "no_outreach_yet": 0,
        "already_followed_up": 0,
        "replied": 0,
        "too_recent": 0,
        "no_demo_match": 0,
        "per_slug": {},
    }
    industry_filter = industry.strip().lower() if industry else None

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_since_outreach)
    console = Console()

    with SudcoAPI.from_config(cfg) as api:
        prospects = list(api.iter_prospects())
        log.info("Loaded %d total prospects from API", len(prospects))

        targets: list[tuple[dict, str | None]] = []
        for p in prospects:
            slug = _slug_for_prospect(p, industry_filter)
            if industry_filter is not None and slug is None:
                continue
            summary["matched"] += 1
            if slug is None:
                summary["no_demo_match"] += 1

            decision = _decide(
                p, min_rating=min_rating, cutoff=cutoff, summary=summary,
            )
            if decision == "go":
                targets.append((p, slug))
                slug_key = slug if slug else "homepage"
                summary["per_slug"][slug_key] = summary["per_slug"].get(slug_key, 0) + 1
            else:
                summary["filtered_out"] += 1

        if limit is not None and limit > 0:
            targets = targets[:limit]
            summary["per_slug"] = {}
            for _, slug in targets:
                slug_key = slug if slug else "homepage"
                summary["per_slug"][slug_key] = summary["per_slug"].get(slug_key, 0) + 1

        summary["would_send"] = len(targets)

        if not targets:
            label = f"slug={industry_filter!r}" if industry_filter else "(all industries)"
            console.print(
                f"[yellow]No prospects eligible for follow-up {label}. "
                f"matched={summary['matched']} "
                f"no_outreach_yet={summary['no_outreach_yet']} "
                f"already_followed_up={summary['already_followed_up']} "
                f"replied={summary['replied']} "
                f"too_recent={summary['too_recent']})[/yellow]"
            )
            return summary

        _print_preview(console, targets[:5], industry_filter, cfg, summary,
                       days_since_outreach)

        if dry_run:
            console.print(f"\n[bold yellow]DRY RUN — {len(targets)} follow-ups NOT sent.[/bold yellow]")
            return summary

        if not yes and not _confirm(console, len(targets), industry_filter, cfg, summary):
            console.print("[yellow]cancelled[/yellow]")
            return summary

        with Progress(
            TextColumn("[bold blue]followup-cold[/bold blue]"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("• [green]sent {task.fields[sent]}[/green] "
                       "• [yellow]err {task.fields[errs]}[/yellow]"),
            TimeElapsedColumn(),
            TextColumn("ETA"),
            TimeRemainingColumn(),
            refresh_per_second=4,
        ) as bar:
            bar_id = bar.add_task("running", total=len(targets), sent=0, errs=0)
            for p, slug in targets:
                try:
                    _send_one(api, cfg, p, industry_slug=slug)
                    summary["sent"] += 1
                except Exception as exc:
                    summary["errors"] += 1
                    log.exception("Failed to follow up prospect %d (%s): %s",
                                  p["id"], p.get("business_name"), exc)
                bar.update(bar_id, sent=summary["sent"], errs=summary["errors"])
                bar.advance(bar_id)

    return summary


def _slug_for_prospect(prospect: dict, industry_filter: str | None) -> str | None:
    if industry_filter is not None:
        return industry_filter if industry_demos.matches_slug(prospect, industry_filter) else None
    return industry_demos.find_slug(prospect)


def _decide(p: dict, *, min_rating: float | None, cutoff: datetime,
            summary: dict) -> str:
    """Return 'go' to send the follow-up, or a short reason to skip."""
    if not p.get("contact_email"):
        return "no_email"
    if p.get("is_chain"):
        return "is_chain"
    if p.get("bounced_at"):
        return "bounced"
    if p.get("unsubscribed_at"):
        return "unsubscribed"
    # Same pipeline gate as send-cold: require analyze to have attempted
    # this prospect. (In practice followup-cold targets prospects that
    # already received the cold email, so they'd usually already have
    # been analyzed before — but the gate is defensive in case someone
    # manually wrote cold_outreach_at without running the full pipeline.)
    if p.get("rating") is None and not p.get("last_rating_attempt"):
        return "not_analyzed_yet"
    if min_rating is not None:
        rating = p.get("rating")
        if rating is None or rating < min_rating:
            return "below_min_rating"

    # Must have received the cold email already.
    cold_at = p.get("cold_outreach_at")
    if not cold_at:
        summary["no_outreach_yet"] += 1
        return "no_outreach_yet"

    # Already followed up — don't double-followup.
    if p.get("cold_followup_at"):
        summary["already_followed_up"] += 1
        return "already_followed_up"

    # If we have any signal of a reply, stop.
    if p.get("cold_replied_at"):
        summary["replied"] += 1
        return "replied"

    # Wait at least N days after the cold send.
    try:
        ts = datetime.fromisoformat(cold_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "bad_outreach_timestamp"
    if ts > cutoff:
        summary["too_recent"] += 1
        return "too_recent"

    return "go"


def _send_one(api: SudcoAPI, cfg: Config, prospect: dict, *,
              industry_slug: str | None) -> None:
    """Send one follow-up email. Mirrors send_cold: per-industry demo URL
    when slug is set, homepage fallback when None."""
    from urllib.parse import urlparse
    if industry_slug:
        demo_url = f"{cfg.cold_demo_base_url}/{industry_slug}?from={prospect['id']}"
        has_demo = True
    else:
        parsed = urlparse(cfg.cold_demo_base_url
                          if "://" in cfg.cold_demo_base_url
                          else f"https://{cfg.cold_demo_base_url}")
        demo_url = f"{parsed.scheme}://{parsed.netloc}?from={prospect['id']}"
        has_demo = False

    parsed = urlparse(cfg.cold_demo_base_url
                      if "://" in cfg.cold_demo_base_url
                      else f"https://{cfg.cold_demo_base_url}")
    demo_visible_url = parsed.netloc or cfg.cold_demo_base_url

    industry = prospect.get("industry") or (industry_slug or "small business")
    # Reuse any cached site_observation generated during send-cold so the
    # follow-up gets the same personalized opener. We don't generate a fresh
    # one here — by definition we already wrote one when we cold-blasted, and
    # re-running the LLM at follow-up time wastes calls on prospects who
    # are unlikely to engage anyway.
    site_observation = prospect.get("site_observation")
    if site_observation == "NONE":
        site_observation = None

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
    subject, body = mailer.render("follow_up_cold.txt", **template_vars)
    html_body = mailer.render_html("follow_up_cold.html",
                                   subject_preview=subject, **template_vars)

    # When replying to the original thread, set Reply-To so threading works
    # in the recipient's client. We don't have the original Message-ID
    # stored on the prospect, so the follow-up is a fresh thread for now.
    # (Future improvement: persist message_id on the prospect at send-cold
    # time, then set In-Reply-To here.)
    mailer.send(cfg, to=prospect["contact_email"], subject=subject, body=body,
                html_body=html_body, reply_to=cfg.smtp_user,
                unsubscribe_id=prospect["id"])
    try:
        api.update_prospect(prospect["id"], {
            "cold_followup_at": datetime.now(timezone.utc).isoformat(),
        })
    except APIError as exc:
        log.error("API patch (cold_followup_at) failed for prospect %d: %s",
                  prospect["id"], exc)


def _print_preview(console: Console, sample: list[tuple[dict, str]],
                   industry: str | None, cfg: Config, summary: dict,
                   days_since: int) -> None:
    title_scope = f"slug={industry!r}" if industry else "all-industries"
    table = Table(title=f"Follow-up preview — {title_scope}, "
                        f"≥{days_since}d since cold send",
                  show_lines=False)
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Business")
    table.add_column("Location")
    table.add_column("Email", style="green")
    table.add_column("Cold sent")
    if industry is None:
        table.add_column("Demo", style="cyan")
    for p, slug in sample:
        cold_at = (p.get("cold_outreach_at") or "")[:10]
        row = [
            str(p["id"]),
            p.get("business_name", "—"),
            p.get("location") or "—",
            p.get("contact_email") or "—",
            cold_at or "—",
        ]
        if industry is None:
            row.append(slug)
        table.add_row(*row)
    console.print(table)
    if industry is None and summary.get("per_slug"):
        breakdown = ", ".join(
            f"{s}={c}" for s, c in sorted(summary["per_slug"].items(),
                                          key=lambda kv: -kv[1])
        )
        console.print(f"\nPer-demo breakdown: [cyan]{breakdown}[/cyan]")
    console.print(
        f"matched=[bold]{summary['matched']}[/bold] "
        f"no_outreach_yet=[dim]{summary['no_outreach_yet']}[/dim] "
        f"already_followed_up=[dim]{summary['already_followed_up']}[/dim] "
        f"replied=[dim]{summary['replied']}[/dim] "
        f"too_recent=[dim]{summary['too_recent']}[/dim] "
        f"→ would_send=[bold green]{summary['would_send']}[/bold green]"
    )


def _confirm(console: Console, n: int, industry: str | None,
             cfg: Config, summary: dict) -> bool:
    if industry:
        console.print(
            f"\n[bold]About to send {n} follow-ups[/bold] for slug={industry!r}."
        )
    else:
        console.print(
            f"\n[bold]About to send {n} follow-ups[/bold] across "
            f"{len(summary.get('per_slug', {}))} demos."
        )
    if cfg.dry_run:
        console.print("[yellow]DRY_RUN=true in config — emails will be logged, not sent.[/yellow]")
    resp = input("Proceed? [y/N] ").strip().lower()
    return resp == "y"
