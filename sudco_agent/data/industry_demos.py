"""Industry → demo-slug mapping for cold-outreach URLs.

A single demo on the frontend (e.g. ``apex-plumbing``) typically serves
multiple related industries (plumbing, HVAC, drain). The mapping is
one-to-many: each demo slug carries a list of industry keywords that
should route to it. ``find_slug(prospect)`` walks the prospect's
``industry`` + ``categories`` and returns the first slug whose keyword
list matches.

JSON dict ordering matters — earlier entries take priority on ambiguous
matches (e.g., a "fitness cafe" matches both ``bakery`` and
``urban-fitness``; insertion order decides). Keep the most-specific
demos first.

The mapping is the single source of truth for two things:
  * ``send-cold`` / ``followup-cold`` use it to pick the right URL slug
    per prospect when no ``--industry`` flag is given.
  * The same commands accept ``--industry <demo_slug>`` to filter to one
    demo's keyword set (so ``--industry bakery`` matches cafe + patisserie
    + donut prospects too, not just literal-substring "bakery").
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "industry_demos.json"


@lru_cache(maxsize=1)
def load() -> dict[str, list[str]]:
    """Returns {demo_slug: [industry_keyword, ...]}.

    Cached for the lifetime of the process — edit the JSON and restart
    to pick up changes."""
    with DATA_PATH.open() as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{DATA_PATH} must be a JSON object")
    return data


def slugs() -> list[str]:
    """All demo slugs in the mapping, in priority order."""
    return list(load().keys())


def keywords_for_slug(slug: str) -> list[str]:
    """Industry keywords associated with a demo slug. Empty list if the
    slug isn't in the mapping."""
    return load().get(slug, [])


def find_slug(prospect: dict) -> str | None:
    """Return the first demo slug whose keyword list matches this prospect.

    Matches against ``prospect['industry']`` and any entry in
    ``prospect['categories']`` (case-insensitive substring). Returns None
    if no slug's keywords match, meaning we have no demo to link this
    prospect to and should skip them in the cold-blast.

    Categories may come back from the API as a JSON-encoded TEXT field
    (the backend stringifies on write but doesn't parse on read), so we
    handle either a list or a string defensively.
    """
    haystack = _prospect_haystack(prospect)
    if not haystack:
        return None
    for slug, keywords in load().items():
        if _any_keyword_matches(haystack, keywords):
            return slug
    return None


def matches_slug(prospect: dict, slug: str) -> bool:
    """True if any of this slug's keywords substring-matches the prospect's
    industry/categories. Used by send-cold / followup-cold when the user
    explicitly passes ``--industry <slug>``."""
    keywords = keywords_for_slug(slug)
    if not keywords:
        # Slug isn't in the mapping; fall back to literal substring match
        # on the slug itself. Lets users target an industry we haven't
        # registered yet without editing the JSON.
        keywords = [slug]
    return _any_keyword_matches(_prospect_haystack(prospect), keywords)


def _prospect_haystack(prospect: dict) -> list[str]:
    """Lowercased list of [industry, *categories] for substring matching."""
    industry = (prospect.get("industry") or "").lower()
    cats_raw = prospect.get("categories")
    cats: list[str] = []
    if isinstance(cats_raw, list):
        cats = [(c or "").lower() for c in cats_raw if c]
    elif isinstance(cats_raw, str) and cats_raw:
        try:
            parsed = json.loads(cats_raw)
        except (ValueError, TypeError):
            parsed = []
        if isinstance(parsed, list):
            cats = [(c or "").lower() for c in parsed if c]
    out = [industry] if industry else []
    out.extend(cats)
    return out


def _any_keyword_matches(haystack: list[str], keywords: list[str]) -> bool:
    for kw in keywords:
        kw_l = (kw or "").lower()
        if not kw_l:
            continue
        for h in haystack:
            if kw_l in h:
                return True
    return False
