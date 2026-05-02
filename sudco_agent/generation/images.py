"""Source images for generated demos. Pexels free API — generous limits."""
from __future__ import annotations

from typing import Optional

import httpx

PEXELS_SEARCH = "https://api.pexels.com/v1/search"


def search_images(api_key: str, query: str, count: int = 5, *, orientation: Optional[str] = None) -> list[str]:
    """Return up to `count` image URLs (large size) matching the query."""
    if not api_key:
        return []
    params: dict = {"query": query, "per_page": count}
    if orientation:
        params["orientation"] = orientation  # "landscape" | "portrait" | "square"

    with httpx.Client(timeout=15) as c:
        r = c.get(PEXELS_SEARCH, params=params, headers={"Authorization": api_key})
        r.raise_for_status()
        data = r.json()

    urls = []
    for photo in data.get("photos", []):
        # `large` is ~940px wide. `large2x` is ~1880px. We use the latter for hero crops.
        src = photo.get("src", {})
        urls.append(src.get("large2x") or src.get("large") or src.get("original"))
    return [u for u in urls if u]


def cover_and_gallery(api_key: str, *, industry: str, business_name: str) -> dict:
    """Convenience: returns one cover (landscape) + several gallery (square) images."""
    cover_query = f"{industry} interior"
    gallery_query = industry
    cover = search_images(api_key, cover_query, count=1, orientation="landscape")
    gallery = search_images(api_key, gallery_query, count=4, orientation="square")
    return {
        "cover": cover[0] if cover else "",
        "gallery": gallery,
    }
