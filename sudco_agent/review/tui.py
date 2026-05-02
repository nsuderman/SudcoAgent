"""Terminal review UI for the human approval queue. Uses `rich` for layout.

Workflow per demo:
  1. Show prospect summary and the generated demo data
  2. Print the `/p/<token>` URL the user can open in a browser
  3. Prompt: [a]pprove, [d]ecline, [s]kip, [x] delete, [q]uit
"""
from __future__ import annotations

import json
import webbrowser
from typing import Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from ..api_client import SudcoAPI
from ..config import Config

console = Console()


def run(cfg: Config) -> None:
    with SudcoAPI.from_config(cfg) as api:
        demos = list(api.iter_demos(status="pending_review"))
        if not demos:
            console.print("[green]Nothing pending review.[/green] You're all caught up.")
            return

        console.print(f"\n[bold]{len(demos)} demos pending review[/bold]\n")

        for i, summary in enumerate(demos, 1):
            console.rule(f"[bold cyan]{i}/{len(demos)} — {summary['business_name']}[/bold cyan]")

            # Fetch full demo data via the public token endpoint (single source of truth)
            full = api.get_demo_by_token(summary["token"])
            data = full.get("demo", {})

            _render_summary(summary, data)

            preview_url = f"{cfg.api_base.rstrip('/').removesuffix('/api')}/p/{summary['token']}"
            console.print(f"\n[blue]Open in browser:[/blue] {preview_url}")

            choice = _prompt_action(cfg, api, summary, preview_url)
            if choice == "quit":
                console.print("[yellow]Stopping review.[/yellow]")
                return


def _render_summary(summary: dict, data: dict) -> None:
    info = Table.grid(padding=(0, 2))
    info.add_column(style="dim")
    info.add_column()
    info.add_row("Business", summary["business_name"])
    info.add_row("Industry", summary.get("industry") or "—")
    info.add_row("Location", summary.get("location") or "—")
    info.add_row("Email", summary.get("contact_email") or "—")
    info.add_row("Token", summary["token"])
    info.add_row("Created", summary["created_at"])
    console.print(Panel(info, title="Prospect", border_style="cyan"))

    if not data:
        console.print("[red]Could not load demo data — endpoint returned empty.[/red]")
        return

    palette = data.get("palette", {})
    services = data.get("services", [])
    hours = data.get("hours", [])

    md = []
    md.append(f"### {data.get('name', '?')}")
    md.append(f"_{data.get('tagline', '')}_\n")
    md.append("**Services**")
    for s in services:
        md.append(f"- **{s.get('title', '?')}** — {s.get('desc', '')}")
    md.append("\n**Hours**")
    for h in hours:
        md.append(f"- {h.get('day', '?')}: {h.get('time', '?')}")
    md.append("\n**Palette**")
    md.append(", ".join(f"{k}={v}" for k, v in palette.items()))
    if data.get("testimonial"):
        md.append(f"\n**Testimonial** — _{data['testimonial'].get('quote', '')}_")
        md.append(f"  — {data['testimonial'].get('author', '')}")
    md.append(f"\n**Cover image:** `{data.get('cover', '<none>')}`")
    md.append(f"**Gallery:** {len(data.get('gallery') or [])} images")
    console.print(Panel(Markdown("\n".join(md)), title="Generated demo", border_style="green"))


def _prompt_action(cfg: Config, api: SudcoAPI, summary: dict, preview_url: str) -> str:
    while True:
        choice = Prompt.ask(
            "[bold]Action[/bold]",
            choices=["a", "d", "s", "o", "x", "j", "q"],
            default="s",
            show_choices=False,
        ).lower()
        if choice == "a":
            api.set_demo_status(summary["id"], "approved")
            console.print("[green]→ approved[/green]")
            return "approved"
        if choice == "d":
            api.set_demo_status(summary["id"], "declined")
            console.print("[yellow]→ declined[/yellow]")
            return "declined"
        if choice == "s":
            console.print("[dim]→ skipped (still pending_review)[/dim]")
            return "skipped"
        if choice == "x":
            confirm = Prompt.ask("Type DELETE to confirm hard delete", default="")
            if confirm == "DELETE":
                api.delete_demo(summary["id"])
                console.print("[red]→ deleted[/red]")
                return "deleted"
            console.print("[dim]cancelled[/dim]")
            continue
        if choice == "o":
            try:
                webbrowser.open(preview_url)
            except Exception:
                pass
            console.print(f"[dim]opening {preview_url}[/dim]")
            continue
        if choice == "j":
            full = api.get_demo_by_token(summary["token"])
            console.print(json.dumps(full.get("demo", {}), indent=2))
            continue
        if choice == "q":
            return "quit"


# Help text printed on startup
HELP = """
Keys:
  [a] approve         [d] decline       [s] skip (leaves pending)
  [o] open in browser [j] dump full JSON
  [x] hard delete     [q] quit
"""
