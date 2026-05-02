"""`agent clean-emails` — re-validate every prospect's contact_email against
the current scraper rules and null out the ones that no longer pass.

Useful one-shot after tightening the scraper. Catches garbage written by
older buggy enrichments (e.g. `registr@ion.but` produced when the
deobfuscator collapsed prose) without re-fetching every website.
"""
from __future__ import annotations

import logging

from rich.console import Console
from rich.progress import (BarColumn, MofNCompleteColumn, Progress, TextColumn,
                           TimeElapsedColumn)
from rich.table import Table

from ..api_client import APIError, SudcoAPI
from ..config import Config
from ..enrichment.email_scraper import _is_junk, _looks_like_email

log = logging.getLogger(__name__)


def run(cfg: Config, *, dry_run: bool = False, yes: bool = False) -> dict:
    summary = {"checked": 0, "valid": 0, "invalid": 0, "cleaned": 0, "errors": 0}
    console = Console()

    with SudcoAPI.from_config(cfg) as api:
        prospects = list(api.iter_prospects())
        log.info("Loaded %d prospects from API", len(prospects))

        suspect: list[tuple[dict, str]] = []
        for p in prospects:
            email = (p.get("contact_email") or "").strip()
            if not email:
                continue
            summary["checked"] += 1
            if _looks_like_email(email) and not _is_junk(email.lower()):
                summary["valid"] += 1
                continue
            summary["invalid"] += 1
            suspect.append((p, email))

        if not suspect:
            console.print(f"[green]No invalid contact_email values out of "
                          f"{summary['checked']} checked.[/green]")
            return summary

        _print_preview(console, suspect, summary)

        if dry_run:
            console.print(f"\n[bold yellow]DRY RUN — {len(suspect)} emails NOT modified.[/bold yellow]")
            return summary

        if not yes and not _confirm(console, len(suspect)):
            console.print("[yellow]cancelled[/yellow]")
            return summary

        with Progress(
            TextColumn("[bold blue]clean-emails[/bold blue]"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("• [green]cleaned {task.fields[cleaned]}[/green] "
                       "• [yellow]err {task.fields[errs]}[/yellow]"),
            TimeElapsedColumn(),
            refresh_per_second=4,
        ) as bar:
            bar_id = bar.add_task("running", total=len(suspect), cleaned=0, errs=0)
            for prospect, _bad_email in suspect:
                try:
                    # Null out the bad email AND clear `enriched_at` so the
                    # next `agent enrich` run will revisit this prospect with
                    # the new (fixed) scraper rules.
                    api.update_prospect(prospect["id"], {
                        "contact_email": None,
                        "enriched_at": None,
                    })
                    summary["cleaned"] += 1
                except APIError as exc:
                    summary["errors"] += 1
                    log.error("Failed to clean prospect %d: %s", prospect["id"], exc)
                bar.update(bar_id, cleaned=summary["cleaned"], errs=summary["errors"])
                bar.advance(bar_id)

    return summary


def _print_preview(console: Console, suspect: list[tuple[dict, str]], summary: dict) -> None:
    table = Table(title="Suspect contact_email values (first 15)", show_lines=False)
    table.add_column("ID", style="dim", justify="right")
    table.add_column("Business")
    table.add_column("Bad email", style="red")
    for p, email in suspect[:15]:
        table.add_row(str(p["id"]), p.get("business_name", "—"), email)
    console.print(table)
    console.print(
        f"\nchecked=[bold]{summary['checked']}[/bold] "
        f"valid=[green]{summary['valid']}[/green] "
        f"invalid=[red]{summary['invalid']}[/red]"
    )


def _confirm(console: Console, n: int) -> bool:
    console.print(
        f"\n[bold]About to null contact_email + enriched_at on {n} prospects[/bold] "
        f"(so the next `agent enrich` will revisit them)."
    )
    resp = input("Proceed? [y/N] ").strip().lower()
    return resp == "y"
