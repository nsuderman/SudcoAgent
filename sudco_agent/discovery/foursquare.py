"""Foursquare Places API discovery (new platform — places-api.foursquare.com).

Notes on the platform shift (2025): the new Foursquare Places API removed
ratings, tips, stats, and popularity from the developer surface. We use it
purely for discovery (name, address, website, category, phone). Rating
filtering happens later in `analyze` via Google Maps lookup.

Free tier: 1000 calls/day. Sign up at https://foursquare.com/developers/.
"""
from __future__ import annotations

import logging
import re
from typing import Iterator

import httpx

log = logging.getLogger(__name__)

PLACES_SEARCH = "https://places-api.foursquare.com/places/search"
API_VERSION = "2025-06-17"
PAGE_SIZE = 50  # Foursquare's per-call max

# RFC 5988 Link header. Foursquare returns: <URL>; rel="next"
LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"', re.IGNORECASE)


def search(
    api_key: str,
    *,
    near: str,
    query: str | None = None,
    categories: str | None = None,
    only_without_website: bool = False,
    skip_chains: bool = True,
    limit: int = 50,
) -> Iterator[dict]:
    """Yield Foursquare places in our prospect schema, paginating as needed.

    Foursquare caps each API call at 50 results, so for `limit > 50` we
    follow the `Link: <URL>; rel="next"` header and keep paging until we
    have enough or run out of results.

    Args:
      near: e.g. "Pasco, WA"
      query: e.g. "bakery" — Foursquare matches against name + category
      categories: comma-separated Foursquare category IDs
      only_without_website: if True, drop results that have a website URL.
      skip_chains: if True (default), drop chain locations.
      limit: total max results to yield across all pages (no hard cap).
    """
    if not api_key:
        raise RuntimeError("FOURSQUARE_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Places-Api-Version": API_VERSION,
        "Accept": "application/json",
    }

    # First page: build params normally. Subsequent pages: use the next-URL
    # which Foursquare returns fully-formed (cursor + original filters baked in).
    params: dict | None = {"near": near, "limit": PAGE_SIZE}
    if query:
        params["query"] = query
    if categories:
        params["fsq_category_ids"] = categories
    url: str | None = PLACES_SEARCH

    yielded = 0
    page = 0

    with httpx.Client(timeout=15) as c:
        while url and yielded < limit:
            page += 1
            r = c.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
            results = data.get("results", []) or []
            log.debug("page %d: %d raw results", page, len(results))

            for place in results:
                if only_without_website and place.get("website"):
                    continue
                if skip_chains and (place.get("chains") or place.get("store_id")):
                    log.debug("dropping chain: %s", place.get("name"))
                    continue
                yield _to_prospect(place)
                yielded += 1
                if yielded >= limit:
                    return

            # Empty page = no more results from Foursquare even if Link says otherwise
            if not results:
                break

            url = parse_next_url(r.headers.get("link", "") or r.headers.get("Link", ""))
            params = None  # next URL has all params baked in


def parse_next_url(link_header: str) -> str | None:
    """Extract the `rel="next"` URL from an RFC 5988 Link header. Returns
    None if absent."""
    if not link_header:
        return None
    m = LINK_NEXT_RE.search(link_header)
    return m.group(1) if m else None


def _to_prospect(place: dict) -> dict:
    """Map a Foursquare result into our prospect schema."""
    cats = place.get("categories") or []
    industry = cats[0].get("name") if cats else None
    category_names = [c.get("name") for c in cats if c.get("name")]

    loc = place.get("location") or {}
    location_str = ", ".join(filter(None, [loc.get("locality"), loc.get("region")]))

    address_parts = [
        loc.get("address"),
        loc.get("locality"),
        loc.get("region"),
        loc.get("postcode"),
    ]
    full_address = ", ".join([p for p in address_parts if p]) or None

    is_chain = bool(place.get("chains")) or bool(place.get("store_id"))

    return {
        "business_name": place["name"],
        "industry": industry,
        "location": location_str or None,
        "contact_phone": place.get("tel"),
        "contact_email": place.get("email"),
        "current_website": place.get("website"),
        "rating": None,  # not available from Foursquare anymore — analyze step fills this
        "review_count": None,
        "source": "foursquare",
        "source_id": place["fsq_place_id"],
        "notes": full_address,
        # Extended fields:
        "latitude": place.get("latitude"),
        "longitude": place.get("longitude"),
        "social_media": place.get("social_media") or None,
        "categories": category_names or None,
        "placemaker_url": place.get("placemaker_url"),
        "is_chain": is_chain,
        "data_refreshed": place.get("date_refreshed"),
    }
