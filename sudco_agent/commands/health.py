"""`agent health` — sanity check that everything the agent depends on is reachable."""
from __future__ import annotations

import socket

import httpx
from rich.console import Console
from rich.table import Table

from ..api_client import SudcoAPI
from ..config import Config
from ..llm import LLMClient

console = Console()


def run(cfg: Config) -> bool:
    rows: list[tuple[str, str, str]] = []

    # Sudco API
    try:
        with SudcoAPI.from_config(cfg) as api:
            api.list_prospects(limit=1)
        rows.append(("Sudco admin API", "ok", cfg.api_base))
    except Exception as exc:
        rows.append(("Sudco admin API", f"FAIL — {exc}", cfg.api_base))

    # LLM endpoint
    try:
        with httpx.Client(timeout=5) as c:
            base = cfg.llm_base_url.removesuffix("/").removesuffix("/v1")
            r = c.get(f"{base}/v1/models")
            r.raise_for_status()
            ids = [m["id"] for m in r.json().get("data", [])]
            text_ok = cfg.text_model in ids
            vis_ok = cfg.vision_model in ids
            note = f"text={cfg.text_model} ({'present' if text_ok else 'MISSING'}), " \
                   f"vision={cfg.vision_model} ({'present' if vis_ok else 'MISSING'})"
            rows.append(("Local LLM /v1/models", "ok" if text_ok and vis_ok else "WARN", note))
    except Exception as exc:
        rows.append(("Local LLM /v1/models", f"FAIL — {exc}", cfg.llm_base_url))

    # LLM round-trip
    try:
        out = LLMClient.from_config(cfg).text_generate("Reply with one word: pong", max_tokens=8)
        rows.append(("LLM text generate", "ok", out.strip()[:60]))
    except Exception as exc:
        rows.append(("LLM text generate", f"FAIL — {exc}", ""))

    # SMTP reachability
    try:
        with socket.create_connection((cfg.smtp_host, cfg.smtp_port), timeout=3):
            pass
        rows.append(("SMTP socket", "ok", f"{cfg.smtp_host}:{cfg.smtp_port}"))
    except Exception as exc:
        rows.append(("SMTP socket", f"FAIL — {exc}", f"{cfg.smtp_host}:{cfg.smtp_port}"))

    # Discovery key
    rows.append(("Foursquare API key", "set" if cfg.foursquare_api_key else "MISSING", ""))
    rows.append(("Pexels API key", "set" if cfg.pexels_api_key else "MISSING (images skipped)", ""))

    t = Table(title="sudco-agent health", show_lines=False)
    t.add_column("Check", style="bold")
    t.add_column("Status")
    t.add_column("Detail", style="dim")
    for name, status, detail in rows:
        style = "green" if status.startswith("ok") or status == "set" else \
                "yellow" if status.startswith("WARN") or "MISSING" in status else "red"
        t.add_row(name, f"[{style}]{status}[/{style}]", detail)
    console.print(t)

    return all(not s.startswith("FAIL") for _, s, _ in rows)
