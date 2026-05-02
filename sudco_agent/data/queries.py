"""Loader for the curated set of `--query` strings used by `agent sweep`.

The queries themselves live in ``queries.json`` next to this module so they
can be edited without touching Python. Use :func:`packs` to get the full
mapping, :func:`pack` to fetch one named pack, or :func:`all_queries` for a
flat deduped list across every pack.

Example — sweep an entire vertical:

    from sudco_agent.data.queries import pack
    from sudco_agent.commands import sweep

    for q in pack("beauty"):
        sweep.run(cfg, query=q, region="hou")
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

QUERIES_PATH = Path(__file__).parent / "queries.json"


@lru_cache(maxsize=1)
def _load() -> dict:
    with QUERIES_PATH.open() as f:
        return json.load(f)


def packs() -> dict[str, list[str]]:
    """Return a copy of the full {pack_name: [query, ...]} mapping."""
    return {k: list(v) for k, v in _load()["packs"].items()}


def pack(name: str) -> list[str]:
    """Return the queries in a named pack. Raises KeyError if missing."""
    return list(_load()["packs"][name])


def pack_names() -> list[str]:
    """Return the available pack names in declaration order."""
    return list(_load()["packs"].keys())


def all_queries() -> list[str]:
    """Flat list of every query across every pack, deduped, declaration order."""
    seen: set[str] = set()
    out: list[str] = []
    for queries in _load()["packs"].values():
        for q in queries:
            if q in seen:
                continue
            seen.add(q)
            out.append(q)
    return out
