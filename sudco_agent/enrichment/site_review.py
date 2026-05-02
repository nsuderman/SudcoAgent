"""Generate a one-to-two-sentence reputation-framed observation about a
prospect for cold-outreach emails.

Strategy: feed the prospect's site HTML (text excerpt) + their top-N Google
review excerpts + their rating/review_count to the local Qwen text model,
along with rating-tier guidance. The model writes a SPECIFIC compliment
that ties back to either:

  * how their reviews / ratings reflect the loyal customer base they have
    (high-rating prospects), or
  * a structural / personality detail about their site that's worth
    preserving (mid-rating or sparse-review prospects), or
  * a softer "you've got room to surface your happy customers" framing
    (low-rating / few-review prospects).

This pivot away from the vision LLM was driven by two facts:
  1. Reviews are a more emotionally resonant cold-email hook than design
     critique. "47 5-star reviews — your site should reflect that" lands
     harder than "I love your color palette."
  2. The vision path had a high screenshot-failure rate (~40% on small-biz
     sites), Qwen 3.x burned tokens on `<think>` reasoning, and required
     a model-swap from the text model on llama.cpp.

Cached on the prospect (``site_observation`` + ``site_observation_at``)
so re-blasts skip the LLM call.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from bs4 import BeautifulSoup

from ..api_client import APIError, SudcoAPI
from ..llm import LLMClient
from .email_scraper import fetch_html

log = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 90
MAX_TEXT_CHARS = 2000
MAX_OBSERVATION_CHARS = 320
MAX_OBSERVATION_WORDS = 50

NONE_TOKENS = {"none", "no specific hook", "no hook", "n/a", "skip"}


PROMPT_TEMPLATE = """You are writing ONE personalized opening line for a \
cold-outreach email from Nate Suderman, founder of Sudco Solutions, to a \
small business owner. Sudco helps small businesses with website refreshes, \
Google Business Profile optimization, review-funnel automation, and \
reputation monitoring.

The opening line you write must feel SPECIFIC to this exact business — \
either calling out something genuine about their reviews/reputation, \
something thoughtful about their website, or both. It is the FIRST line \
the reader sees; it has to earn the rest of the email.

YOUR DEFAULT IS TO PRODUCE AN OPENER, NOT TO OUTPUT NONE. With any review \
excerpt or any non-empty website text, you have enough material — your \
job is to find the most specific hook in what's given.

REQUIREMENTS:
- 1 to 2 sentences, max 35 words total.
- Start with "I love" / "I noticed" / "Your" / "It's clear" / "Looking at" \
or similar — natural, warm, conversational.
- Reference at least one SPECIFIC thing: a recurring phrase in the reviews \
("greeted warmly", "from scratch", "family-run"), the rating, the review \
count, a structural choice on the site (layout, navigation, voice, \
photography), or a service/value the site emphasizes.
- When reviews are present, prefer to anchor on a SPECIFIC PHRASE OR THEME \
from one review — repeat back what their customers actually say.
- DO NOT compliment specific products or menu items (that drifts into \
"I love your jumbo cupcakes" territory and reads as cheap flattery).
- DO NOT use vague platitudes ("nice site", "great reviews", "professional"). \
Be specific.
- DO NOT mention Nate's services in this line — that comes later in the \
email. This is just the opener.

GUIDANCE BY REPUTATION TIER:
{tier_guidance}

GOOD examples:
- "I noticed your 4.8★ across 124 reviews — customers keep mentioning how \
welcoming the staff is, and that warmth comes through on your homepage too."
- "Looking at your site, the way you've organized the menu by category \
makes it really easy to scan — and the reviews echo that 'easy to work \
with' feeling."
- "Your reviews keep mentioning the homemade-from-scratch quality — that \
story isn't really told on your homepage yet, which feels like a missed \
opportunity to lead with."

BAD examples (do NOT do this):
- "Great bakery, love the cupcakes!" (product-focused)
- "Nice website." (vague)
- "I see you have lots of reviews." (no specificity)

ONLY output NONE if BOTH the website excerpt is empty/unavailable AND there \
are zero review excerpts. If you have any review at all, OR any non-trivial \
website text, you must produce an opener — find the most specific thing \
you can and lean into it.

Business name: {business_name}
{location_line}
Rating: {rating_line}
Review count: {review_count_line}

Top reviews (most-relevant first):
{reviews_block}

Website excerpt (text only, truncated):
\"\"\"
{site_text}
\"\"\"

Output ONLY the opening line (1-2 sentences, 35 words max), or the literal \
word NONE (only if both inputs are empty)."""


HIGH_TIER_GUIDANCE = """This business has a STRONG reputation (rating \
≥ 4.4 with at least 20 reviews). Lean into that — call out a specific \
theme from the reviews (warmth, quality, consistency, a name people \
mention) and tie it to either how their site reflects that or a small \
gap where their site doesn't yet match their reputation. Tone: \
"customers love you; let's make sure more people see what they see." """

MID_TIER_GUIDANCE = """This business has a SOLID but not blockbuster \
reputation (rating 3.6-4.3, OR fewer than 20 reviews). Look for \
something genuine in the reviews OR site that signals personality / \
care / a clear value prop. Tone: "you've earned loyal customers; \
there's room to amplify them." Avoid implying any criticism of \
existing reviews."""

LOW_TIER_GUIDANCE = """This business has a WEAKER review profile \
(rating < 3.6, or extremely few reviews). DO NOT shame them for this. \
Instead, find something genuine about their site, their staff, their \
story, or a service they emphasize, and frame it as "this strength \
isn't yet showing up where customers look first." Be warm and assume \
good faith — bad reviews are often a vocal-minority artifact, not a \
verdict on the business."""

UNRATED_TIER_GUIDANCE = """This business doesn't have a Google rating \
captured yet (or it's null). Skip the reviews angle entirely and \
focus on something specific about their website — structure, voice, \
service emphasis, photography, or a value they lead with. Tone: \
warm and curious."""


def get_observation(
    api: SudcoAPI,
    llm: LLMClient,
    prospect: dict,
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
    force: bool = False,
) -> Optional[str]:
    """Return a cached or freshly-generated reputation-framed observation.

    Pulls the cached ``review_excerpts`` (populated by ``agent analyze``)
    and the live site HTML, hands both to the text LLM with rating-tier
    guidance, and caches the result on the prospect.
    """
    if not force and _cached_is_fresh(prospect, ttl_days):
        cached = prospect.get("site_observation")
        if cached and cached != "NONE":
            return cached
        if cached == "NONE":
            return None

    business_name = prospect.get("business_name", "")
    location_line = (
        f"Location: {prospect['location']}" if prospect.get("location") else ""
    )

    # Reviews come back from the API as a JSON-encoded string (the backend
    # JSON-stringifies on write but doesn't parse on read — same pattern as
    # `categories`). Parse defensively; treat any parse failure as no reviews.
    reviews = _parse_reviews(prospect.get("review_excerpts"))

    # Pull the site HTML once. If the fetch fails AND we have no reviews
    # either, there's nothing to feed the LLM — fall back to NONE.
    site_text = ""
    website = prospect.get("current_website")
    if website:
        html = fetch_html(website)
        if html:
            site_text = _extract_text(html)

    if not site_text and not reviews:
        # Don't poison the cache here — both inputs are empty for transient
        # reasons (network blip, reviews not yet scraped). Next run can retry.
        log.debug(
            "site_review: no site_text and no reviews for prospect %s — skipping",
            prospect.get("id"),
        )
        return None

    rating = prospect.get("rating")
    review_count = prospect.get("review_count")
    tier_guidance = _tier_guidance(rating, review_count)

    prompt = PROMPT_TEMPLATE.format(
        tier_guidance=tier_guidance,
        business_name=business_name,
        location_line=location_line,
        rating_line=(f"{rating:.1f}" if isinstance(rating, (int, float)) else "(unknown)"),
        review_count_line=(str(review_count) if review_count else "(unknown)"),
        reviews_block=_format_reviews(reviews),
        site_text=site_text[:MAX_TEXT_CHARS] if site_text else "(site unavailable)",
    )

    try:
        raw = llm.text_generate(
            prompt, temperature=0.6, max_tokens=300, disable_thinking=True,
        )
    except Exception as exc:
        log.warning(
            "site_review LLM call failed for prospect %s (%s): %s",
            prospect.get("id"), business_name, exc,
        )
        return None

    observation = _validate_output(raw)
    if observation is None:
        _persist(api, prospect["id"], "NONE")
        return None

    _persist(api, prospect["id"], observation)
    return observation


def _parse_reviews(raw) -> list[dict]:
    """Parse the review_excerpts payload from the API into a list of dicts.

    Tolerates already-parsed lists (in case the API ever starts hydrating
    JSON columns) and gracefully handles malformed/empty input."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)]
    return []


def _format_reviews(reviews: list[dict]) -> str:
    """Render the reviews list into a compact prompt block. Empty list
    renders as a sentinel string the LLM can recognize."""
    if not reviews:
        return "(no review excerpts available)"
    lines: list[str] = []
    for i, r in enumerate(reviews, start=1):
        rating = r.get("rating")
        author = r.get("author") or "anonymous"
        text = (r.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        rating_str = f"{rating}★" if rating is not None else "?★"
        lines.append(f'{i}. [{rating_str} — {author}] "{text}"')
    return "\n".join(lines) if lines else "(no review excerpts available)"


def _tier_guidance(rating: float | None, review_count: int | None) -> str:
    """Pick the tier guidance based on rating + review count.

    The thresholds are deliberately a little forgiving on the bottom — a
    business with 4.5★ and 6 reviews falls into MID, not HIGH, because
    six reviews don't yet establish a robust pattern."""
    if rating is None:
        return UNRATED_TIER_GUIDANCE
    if rating >= 4.4 and (review_count or 0) >= 20:
        return HIGH_TIER_GUIDANCE
    if rating < 3.6:
        return LOW_TIER_GUIDANCE
    return MID_TIER_GUIDANCE


def _cached_is_fresh(prospect: dict, ttl_days: int) -> bool:
    ts = prospect.get("site_observation_at")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return last > datetime.now(timezone.utc) - timedelta(days=ttl_days)


def _persist(api: SudcoAPI, prospect_id: int, observation: str) -> None:
    try:
        api.update_prospect(prospect_id, {
            "site_observation": observation,
            "site_observation_at": datetime.now(timezone.utc).isoformat(),
        })
    except APIError as exc:
        log.error("site_review: failed to persist for prospect %d: %s",
                  prospect_id, exc)


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(f"TITLE: {soup.title.string.strip()}")
    for h in soup.find_all(["h1", "h2", "h3"]):
        txt = (h.get_text(" ", strip=True) or "").strip()
        if txt:
            parts.append(f"HEADING: {txt}")
    body_text = soup.get_text(" ", strip=True)
    parts.append(body_text)
    combined = "\n".join(parts)
    combined = re.sub(r"\s+", " ", combined)
    return combined


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def _validate_output(raw: str) -> Optional[str]:
    """Sanity-check the model's response. Returns the cleaned observation
    or None if the output should be rejected."""
    if not raw:
        return None
    cleaned = _THINK_BLOCK.sub("", raw)
    cleaned = _THINK_OPEN.sub("", cleaned)
    cleaned = cleaned.strip().strip('"\'`').strip()
    # Take the first non-empty line — Qwen sometimes adds a follow-on
    # explanatory line which we don't want in the email.
    first_line: str | None = None
    for line in cleaned.split("\n"):
        candidate = line.strip().strip('"\'`').strip()
        if candidate:
            first_line = candidate
            break
    if not first_line:
        return None
    cleaned = first_line
    if cleaned.lower() in NONE_TOKENS:
        return None
    if len(cleaned) > MAX_OBSERVATION_CHARS:
        return None
    if len(cleaned.split()) > MAX_OBSERVATION_WORDS:
        return None
    if cleaned.lower().startswith(("i'm sorry", "i can't", "i cannot",
                                   "as an ai", "i don't")):
        return None
    return cleaned
