"""Scrape Google Maps for a business's rating + review count.

We use Playwright (headless Chromium) to load the public search page and
extract data from `aria-label` attributes — which contain a stable text
pattern like "Bella's Bakery, 4.5 stars 89 Reviews". CSS class names on
Google's site change frequently; aria-labels are part of the public a11y
surface and change rarely.

For solo-volume use this is fine. If Google starts blocking us:
  - Add residential proxies (paid)
  - Drop a 1×1 captcha-solver
  - Or migrate to vision-LLM extraction from a screenshot (heavier but
    immune to selector drift)
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import BrowserContext

log = logging.getLogger(__name__)

# "4.5 stars 89 Reviews" or "4.5 stars · 1,234 reviews"
RATING_WITH_REVIEWS_RE = re.compile(
    r"(\d(?:\.\d)?)\s*stars?\s*[·,\s]*(\d{1,3}(?:,\d{3})*|\d+)\s*[Rr]eviews?",
    re.IGNORECASE,
)
# "4.7 stars (245)" — parenthesized count immediately after, no "reviews" word
RATING_WITH_PARENS_RE = re.compile(
    r"(\d(?:\.\d)?)\s*stars?\s*\(\s*(\d{1,3}(?:,\d{3})*|\d+)\s*\)",
    re.IGNORECASE,
)
# Fallback: just the rating
RATING_ONLY_RE = re.compile(r"(\d(?:\.\d)?)\s*stars?", re.IGNORECASE)

# When Google rate-limits us, the response is one of these. Detection short-circuits
# the rest of the page work and surfaces a clear error string the runner can act on.
CAPTCHA_URL_MARKERS = ("/sorry/", "/recaptcha", "/robots/")
CAPTCHA_TEXT_MARKERS = (
    "unusual traffic",
    "automated requests",
    "before you continue",
    "i'm not a robot",
    "our systems have detected",
)
CAPTCHA_ERROR_PREFIX = "captcha:"


def looks_like_captcha(*, final_url: str = "", title: str = "", body_snippet: str = "") -> str | None:
    """Returns a short label like 'captcha:url:...' if any marker matched, else None.
    Pulled out for unit testing — the live fetch path uses it after each navigation."""
    if final_url:
        low = final_url.lower()
        for marker in CAPTCHA_URL_MARKERS:
            if marker in low:
                return f"{CAPTCHA_ERROR_PREFIX}url:{marker.strip('/')}"
    for source, label, blob in (("title", "title", title), ("body", "body", body_snippet)):
        low = (blob or "").lower()
        for marker in CAPTCHA_TEXT_MARKERS:
            if marker in low:
                return f"{CAPTCHA_ERROR_PREFIX}{label}:{marker[:24]}"
    return None


@dataclass
class RatingResult:
    rating: float | None = None
    review_count: int | None = None
    matched_label: str | None = None
    error: str | None = None


async def fetch_rating(
    context: "BrowserContext",
    business_name: str,
    location: str | None = None,
    *,
    timeout_ms: int = 15000,
    selector_wait_ms: int = 4000,
) -> RatingResult:
    """Look up the first matching place on Google Maps and pull its rating.

    Returns a result with rating=None if no rating-shaped aria-label was
    found on the page (e.g. a brand-new business with no reviews yet, or
    Google blocked us with a captcha — those aren't currently distinguished).
    """
    # Lazy-import so the parsing helpers in this module stay importable
    # without playwright (e.g. for unit tests of parse_rating_label).
    from playwright.async_api import TimeoutError as PWTimeoutError

    query_parts = [business_name]
    if location:
        query_parts.append(location)
    query = " ".join(query_parts)
    url = f"https://www.google.com/maps/search/{quote_plus(query)}/?hl=en"

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

        # Captcha tripwire: if Google bounced us to /sorry/ or showed a
        # "verify you're human" page, no amount of waiting will help.
        marker = looks_like_captcha(final_url=page.url, title=await page.title())
        if marker:
            return RatingResult(error=marker)

        # Wait for any rating-shaped aria-label to materialize. Google renders
        # results client-side, so DOMContentLoaded fires before the listings
        # are populated.
        try:
            await page.wait_for_selector(
                '[aria-label*="stars" i], [aria-label*="Stars"]',
                timeout=selector_wait_ms,
            )
        except PWTimeoutError:
            # Selectors didn't appear — could be a slow page, captcha rendered
            # mid-load, or just a business with no ratings. Re-check for captcha
            # indicators in the body before giving up.
            try:
                snippet = await page.evaluate(
                    "() => (document.body && document.body.innerText || '').slice(0, 600)"
                )
            except Exception:
                snippet = ""
            marker = looks_like_captcha(final_url=page.url, title=await page.title(),
                                        body_snippet=snippet)
            if marker:
                return RatingResult(error=marker)
            return RatingResult(error="timeout waiting for rating elements")

        labels = await page.evaluate(
            """() => {
                return Array.from(document.querySelectorAll('[aria-label]'))
                  .map(e => e.getAttribute('aria-label'))
                  .filter(s => s && /star/i.test(s));
            }"""
        )

        # Try patterns in order of specificity. The first label that yields
        # a rating wins — labels are ordered roughly by visual prominence,
        # and the top result's label appears first.
        for label in labels:
            rating, rc = parse_rating_label(label)
            if rating is not None:
                return RatingResult(rating=rating, review_count=rc, matched_label=label)

        return RatingResult(error="no rating-shaped aria-label found on page")
    except Exception as exc:
        return RatingResult(error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def fetch_reviews(
    context: "BrowserContext",
    business_name: str,
    location: str | None = None,
    *,
    n: int = 5,
    timeout_ms: int = 15000,
    selector_wait_ms: int = 5000,
) -> list[dict]:
    """Best-effort scrape of the top N Google reviews for this business.

    Navigates to the maps search URL, lets it redirect to the place page,
    waits for the reviews tab content to populate, and pulls each review's
    text + per-review star rating + author name.

    Returns a list of ``{author, rating, text}`` dicts (length 0..n). Empty
    list on any failure — callers must treat reviews as optional. We do NOT
    return errors here because the rating fetch is the primary signal; a
    missing reviews list is just "less personalization fuel" for the LLM.

    Selectors target ``[data-review-id]`` and its ``aria-label`` children,
    which have been stable across recent Google Maps layouts. Layout drift
    will silently degrade this to "no reviews scraped" — caller proceeds.
    """
    from playwright.async_api import TimeoutError as PWTimeoutError

    query_parts = [business_name]
    if location:
        query_parts.append(location)
    query = " ".join(query_parts)
    # Direct-place URL bypasses the search list and lands on the place page,
    # which is what reviews live on. `hl=en` keeps the aria-labels predictable.
    url = f"https://www.google.com/maps/search/{quote_plus(query)}/?hl=en"

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        # The /maps/search URL has two outcomes:
        #   (a) unambiguous match — Google auto-redirects to /maps/place/<name>
        #   (b) ambiguous match — page stays on /maps/search and renders a list
        # A fixed 2s sleep raced (a): the redirect lands between 2–5s, so the
        # subsequent place-link query found 0 anchors and we returned []. Wait
        # for whichever signal arrives first.
        try:
            await page.wait_for_function(
                "() => location.pathname.includes('/maps/place/') "
                "|| !!document.querySelector('a[href*=\"/maps/place/\"]')",
                timeout=5000,
            )
        except PWTimeoutError:
            pass

        marker = looks_like_captcha(final_url=page.url, title=await page.title())
        if marker:
            return []

        # Resolve the place URL we want to scrape.
        # The search list has no [data-review-id] — those only appear on the
        # place panel — so we either navigate INTO a result via its href or
        # we ride the auto-redirect Google fires for unambiguous matches.
        if "/maps/place/" not in page.url:
            try:
                place_href = await page.evaluate(
                    """(name) => {
                        const links = document.querySelectorAll('a[href*="/maps/place/"]');
                        if (!links.length) return null;
                        const lower = (name || '').toLowerCase();
                        // Prefer a link whose text/aria includes the business name —
                        // first result on Maps isn't always the intended one.
                        for (const a of links) {
                            const blob = ((a.textContent || '') + ' ' +
                                          (a.getAttribute('aria-label') || '')).toLowerCase();
                            if (lower && blob.includes(lower)) return a.href;
                        }
                        return links[0].href;
                    }""",
                    business_name,
                )
            except Exception:
                place_href = None
            if not place_href:
                return []
            target_url = place_href
            await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        else:
            target_url = page.url

        # Both paths above land us on a "lite" result-detail panel that only
        # exposes Overview + About tabs (no Reviews). An explicit (re-)goto to
        # the same URL forces the full standalone panel — same one a deep-link
        # click would render. Two hits to /maps/place/ are required no matter
        # which path got us here.
        await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
        marker = looks_like_captcha(final_url=page.url, title=await page.title())
        if marker:
            return []

        # The place panel is rendered client-side after domcontentloaded.
        # Wait for the tab bar to mount so the Reviews tab button is in the
        # DOM before we try to click it below. Short-circuits the moment a
        # tab appears; caps at 4s for genuinely slow Google responses (the
        # condition-driven wait means fast pages still return fast — only
        # the slow tail pays the higher ceiling).
        try:
            await page.wait_for_selector('button[role="tab"]', timeout=4000)
        except PWTimeoutError:
            pass

        # On the place panel the Reviews tab is collapsed by default — review
        # cards (the [data-review-id] nodes we want) only render after we click
        # it. Try the click; if it fails (no Reviews tab found, e.g. an unrated
        # place), the wait below times out and we return empty cleanly.
        # Prefer the actual tab; fall back to any review-labeled button. Skip
        # "Write a review" — that opens the star-rating overlay, not the list.
        try:
            await page.evaluate(
                """() => {
                    const tabs = document.querySelectorAll('button[role="tab"]');
                    for (const b of tabs) {
                        const blob = (b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '');
                        if (/review/i.test(blob) && !/write/i.test(blob)) { b.click(); return; }
                    }
                    const others = document.querySelectorAll('button[aria-label*="Review" i]');
                    for (const b of others) {
                        const aria = b.getAttribute('aria-label') || '';
                        if (!/write/i.test(aria)) { b.click(); return; }
                    }
                }"""
            )
        except Exception:
            pass

        # Now wait for review nodes to materialize (post-click). The Reviews
        # tab loads asynchronously even after the click registers.
        try:
            await page.wait_for_selector("[data-review-id]", timeout=selector_wait_ms)
        except PWTimeoutError:
            return []

        # Scroll the review list a bit so a few more reviews lazy-load past
        # the initial 3-4. We don't need many; just enough to filter for the
        # most-substantive ones. Wait for the card count to actually grow
        # rather than burning a fixed sleep — caps at 700ms if no more come.
        try:
            initial_count = await page.evaluate(
                "() => document.querySelectorAll('[data-review-id]').length"
            )
            await page.evaluate(
                """() => {
                    const list = document.querySelector('[data-review-id]')?.closest('[role="main"]') ||
                                 document.querySelector('[role="main"]');
                    if (list) list.scrollBy(0, 1200);
                }"""
            )
            try:
                await page.wait_for_function(
                    f"() => document.querySelectorAll('[data-review-id]').length > {initial_count}",
                    timeout=700,
                )
            except PWTimeoutError:
                pass
        except Exception:
            pass

        raw = await page.evaluate(
            f"""(maxN) => {{
                const out = [];
                const seenIds = new Set();
                const nodes = document.querySelectorAll('[data-review-id]');
                for (const node of nodes) {{
                    if (out.length >= maxN) break;
                    // Dedupe: Google nests review wrappers (outer + inner both
                    // carry the same data-review-id). Process each id once.
                    const id = node.getAttribute('data-review-id') || '';
                    if (!id || seenIds.has(id)) continue;
                    seenIds.add(id);

                    // Author: the .d4r55 element is just the display name —
                    // cleaner than the photo button's aria-label which prefixes
                    // "Photo of ...".
                    let author = '';
                    const authorEl = node.querySelector('[class*="d4r55"]');
                    if (authorEl) {{
                        author = (authorEl.textContent || '').trim();
                    }} else {{
                        const photoBtn = node.querySelector('button[aria-label^="Photo of"]');
                        if (photoBtn) {{
                            author = (photoBtn.getAttribute('aria-label') || '')
                                .replace(/^Photo of\\s+/i, '').trim();
                        }}
                    }}

                    // Per-review rating: aria-label like "5 stars" on a [role="img"].
                    let rating = null;
                    const starEl = node.querySelector('[role="img"][aria-label*="star" i]');
                    if (starEl) {{
                        const m = (starEl.getAttribute('aria-label') || '').match(/(\\d(?:\\.\\d)?)/);
                        if (m) rating = parseFloat(m[1]);
                    }}

                    // Review text: prefer the wiI7pd / MyEned text container.
                    // Skip the node if neither is present — the fallback we
                    // had before scraped reviewer metadata ("Local Guide · 202
                    // reviews · 654 photos") which is worse than nothing.
                    const textEl = node.querySelector('[class*="wiI7pd"], [class*="MyEned"]');
                    if (!textEl) continue;
                    const text = (textEl.innerText || textEl.textContent || '').trim();
                    if (!text || text.length < 20) continue;
                    out.push({{ author, rating, text }});
                }}
                return out;
            }}""",
            n,
        )

        reviews: list[dict] = []
        for item in raw or []:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            # Trim each excerpt — long reviews bloat the LLM prompt without
            # adding signal. 400 chars is plenty to communicate the gist.
            if len(text) > 400:
                text = text[:397].rstrip() + "..."
            reviews.append({
                "author": (item.get("author") or "").strip()[:60] or None,
                "rating": item.get("rating"),
                "text": text,
            })
            if len(reviews) >= n:
                break
        return reviews
    except Exception as exc:
        log.debug("fetch_reviews failed for %s: %s", business_name, exc)
        return []
    finally:
        try:
            await page.close()
        except Exception:
            pass


# ---- pure-logic helper exposed for tests ----

def parse_rating_label(label: str) -> tuple[float | None, int | None]:
    """Extract (rating, review_count) from a Google Maps aria-label string.

    Tries three patterns in order of specificity:
      1. "4.5 stars 89 Reviews"          — rating + count + "reviews" word
      2. "4.7 stars (245)"               — rating + parenthesized count
      3. "4.5 stars"                     — rating only
    """
    for pattern, has_count in ((RATING_WITH_REVIEWS_RE, True),
                               (RATING_WITH_PARENS_RE, True),
                               (RATING_ONLY_RE, False)):
        m = pattern.search(label)
        if not m:
            continue
        try:
            rating = float(m.group(1))
        except (ValueError, IndexError):
            continue
        if has_count:
            rc_str = (m.group(2) or "").replace(",", "")
            return (rating, int(rc_str) if rc_str.isdigit() else None)
        return (rating, None)
    return (None, None)
