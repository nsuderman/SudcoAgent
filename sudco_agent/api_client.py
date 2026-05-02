"""Client for the Sudco Solutions admin API. Source of truth for prospect +
demo state. The agent owns no local DB — everything goes through here."""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterator, Optional

import httpx

from .config import Config

log = logging.getLogger(__name__)

# 429 retry tuning. Backend's express-rate-limit cap is 2000 req/min across
# all admin endpoints. With parallel sweeps + enrich + analyze + send-cold
# crons running together, brief bursts blow past that. Exponential backoff
# with respect for Retry-After lets the agent ride out rate-limit windows
# instead of dropping work on the floor.
RATE_LIMIT_RETRIES = 6
RATE_LIMIT_INITIAL_DELAY_S = 1.0
RATE_LIMIT_MAX_DELAY_S = 30.0


class APIError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class SudcoAPI:
    def __init__(self, base: str, admin_key: str):
        self.base = base.rstrip("/")
        self.client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        # Cache of prospects already fetched on this instance. Populated by
        # iter_prospects/list_prospects/get_prospect; lets repeated lookups
        # avoid re-scanning the API. Cleared on mutations.
        self._prospect_cache: dict[int, dict] = {}
        # Flipped to False the first time GET /admin/prospects/{id} returns
        # 405, after which we permanently fall back to scanning. 404 is not
        # ambiguous here — PATCH/DELETE are defined on this path, so 404
        # means "no such id" and 405 means "GET not implemented".
        self._direct_get_supported: bool = True

    @classmethod
    def from_config(cls, cfg: Config) -> "SudcoAPI":
        return cls(cfg.api_base, cfg.admin_api_key)

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---- helpers ----
    def _req(self, method: str, path: str, **kw) -> Any:
        """HTTP request with built-in 429-retry. The Sudco admin API caps
        admin endpoints at 2000 req/min, which we can burst past during
        parallel sweeps; transparent retry-with-backoff means callers never
        need their own rate-limit handling.

        Honors the Retry-After header when the server provides one (seconds
        or HTTP-date format). Otherwise falls back to exponential backoff
        with jitter, capped at RATE_LIMIT_MAX_DELAY_S. Non-429 4xx/5xx
        responses raise immediately as before — this only protects 429s.
        """
        delay = RATE_LIMIT_INITIAL_DELAY_S
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            r = self.client.request(method, f"{self.base}{path}", **kw)
            if r.status_code != 429:
                break
            if attempt >= RATE_LIMIT_RETRIES:
                break  # exhausted retries, fall through to error path
            wait = _retry_after_seconds(r) or delay
            # Add small jitter so multiple workers don't sync on the same
            # retry instant and re-burst the moment the window resets.
            wait += random.uniform(0, min(0.5, wait * 0.1))
            log.warning(
                "API rate-limited (429) on %s %s; sleeping %.1fs before "
                "retry %d/%d",
                method, path, wait, attempt + 1, RATE_LIMIT_RETRIES,
            )
            time.sleep(wait)
            delay = min(delay * 2, RATE_LIMIT_MAX_DELAY_S)

        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise APIError(
                f"{method} {path} -> {r.status_code}: {detail}",
                status_code=r.status_code,
            )
        return r.json()

    # ---- prospects ----
    def upsert_prospect(self, prospect: dict) -> dict:
        p = self._req("POST", "/admin/prospects", json=prospect)["prospect"]
        self._prospect_cache[p["id"]] = p
        return p

    def list_prospects(self, limit: int = 100, offset: int = 0) -> list[dict]:
        page = self._req("GET", "/admin/prospects",
                         params={"limit": limit, "offset": offset})["prospects"]
        for p in page:
            self._prospect_cache[p["id"]] = p
        return page

    def iter_prospects(self, *, page_size: int = 500) -> Iterator[dict]:
        """Yield every prospect, transparently paging through the API.

        Use this in commands that need to act on the full prospect set —
        list_prospects with a fixed limit silently truncates once the DB
        outgrows the cap.
        """
        offset = 0
        while True:
            page = self.list_prospects(limit=page_size, offset=offset)
            if not page:
                return
            for p in page:
                yield p
            if len(page) < page_size:
                return
            offset += page_size

    def get_prospect(self, prospect_id: int) -> dict | None:
        """Fetch a single prospect by id. Returns None if it doesn't exist.

        Tries the direct ``GET /admin/prospects/{id}`` endpoint first (O(1)).
        If the backend responds 405 it means GET-by-id isn't implemented;
        we permanently fall back to scanning every prospect for the rest of
        this client's lifetime. Results are cached so repeated lookups in
        the same run don't re-hit the API.
        """
        cached = self._prospect_cache.get(prospect_id)
        if cached is not None:
            return cached

        if self._direct_get_supported:
            try:
                resp = self._req("GET", f"/admin/prospects/{prospect_id}")
            except APIError as exc:
                if exc.status_code == 404:
                    return None
                if exc.status_code == 405:
                    # Endpoint not implemented — disable and fall through.
                    self._direct_get_supported = False
                    log.debug(
                        "GET /admin/prospects/{id} not supported by backend; "
                        "falling back to scan."
                    )
                else:
                    raise
            else:
                prospect = resp.get("prospect", resp)
                self._prospect_cache[prospect_id] = prospect
                return prospect

        # Fallback: scan. iter_prospects populates _prospect_cache as it goes,
        # so subsequent get_prospect calls in this run are free.
        for p in self.iter_prospects():
            if p["id"] == prospect_id:
                return p
        return None

    def update_prospect(self, prospect_id: int, patch: dict) -> dict:
        p = self._req("PATCH", f"/admin/prospects/{prospect_id}", json=patch)["prospect"]
        self._prospect_cache[prospect_id] = p
        # Detect silent field drops — backend may strip fields not in its
        # allowlist while still returning 200. Warn loudly so we don't lose
        # writes to a misconfigured schema (the alternative is "everything
        # looks fine but cold_outreach_at never gets set" which is exactly
        # the bug we hit on first cold-blast).
        for k, expected in patch.items():
            if expected is None:
                continue  # explicit null write — server may not echo it
            actual = p.get(k)
            if actual != expected and not _values_equivalent(actual, expected):
                log.warning(
                    "update_prospect(%d): field %r was sent but not persisted "
                    "(expected %r, got %r). Check the backend's allowlist.",
                    prospect_id, k, _truncate(expected), _truncate(actual),
                )
        return p

    def delete_prospect(self, prospect_id: int) -> None:
        self._req("DELETE", f"/admin/prospects/{prospect_id}")
        self._prospect_cache.pop(prospect_id, None)

    # ---- discovery_searches ----
    def record_search(self, *, area: str, query: str | None, source: str = "foursquare",
                      raw_results: int | None = None, stored: int | None = None,
                      error: str | None = None) -> int:
        body = {"area": area, "query": query, "source": source,
                "raw_results": raw_results, "stored": stored, "error": error}
        return self._req("POST", "/admin/searches", json=body)["search"]["id"]

    def last_search(self, *, area: str, query: str | None, source: str = "foursquare") -> dict | None:
        params = {"area": area, "source": source}
        if query is not None:
            params["query"] = query
        return self._req("GET", "/admin/searches/last", params=params)["search"]

    def list_searches(self, *, source: str | None = None, limit: int = 100) -> list[dict]:
        params: dict = {"limit": limit}
        if source:
            params["source"] = source
        return self._req("GET", "/admin/searches", params=params)["searches"]

    # ---- demos ----
    def create_demo(
        self,
        prospect_id: int,
        data: dict,
        *,
        status: str = "pending_review",
        expires_in_days: Optional[int] = None,
    ) -> dict:
        body = {"prospect_id": prospect_id, "data": data, "status": status}
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        return self._req("POST", "/admin/demos", json=body)

    def list_demos(self, status: Optional[str] = None, limit: int = 100,
                   offset: int = 0) -> list[dict]:
        params: dict = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return self._req("GET", "/admin/demos", params=params)["demos"]

    def iter_demos(self, status: Optional[str] = None, *, page_size: int = 500) -> Iterator[dict]:
        """Yield every demo (optionally filtered by status), transparently paging."""
        offset = 0
        while True:
            page = self.list_demos(status=status, limit=page_size, offset=offset)
            if not page:
                return
            for d in page:
                yield d
            if len(page) < page_size:
                return
            offset += page_size

    def set_demo_status(self, demo_id: int, status: str) -> dict:
        return self._req("PATCH", f"/admin/demos/{demo_id}", json={"status": status})["demo"]

    def delete_demo(self, demo_id: int) -> None:
        self._req("DELETE", f"/admin/demos/{demo_id}")

    # ---- public (no auth) ----
    def get_demo_by_token(self, token: str) -> dict:
        # bypass the bearer header for the public route
        with httpx.Client(timeout=15) as anon:
            r = anon.get(f"{self.base}/p/{token}")
            r.raise_for_status()
            return r.json()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse the Retry-After header, returning seconds-to-wait or None.
    Accepts both the integer-seconds form and the HTTP-date form (RFC 7231).
    Express-rate-limit emits the integer form; this is mostly defensive
    against future server changes."""
    val = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if not val:
        return None
    val = val.strip()
    try:
        return max(0.0, float(val))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(val)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delta = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _values_equivalent(actual, expected) -> bool:
    """Tolerate the small round-trip differences a JSON API can introduce —
    booleans encoded as 0/1, lists/dicts re-serialized, trailing whitespace.
    Only used to decide whether to *warn* about a silent drop, so false
    positives are cheap; we err toward not warning for ambiguous cases."""
    if actual == expected:
        return True
    # bool / int round-trip (is_chain comes back as 0 or 1)
    if isinstance(expected, bool) and isinstance(actual, int):
        return int(expected) == actual
    # JSON-stringified containers
    import json
    if isinstance(expected, (dict, list)) and isinstance(actual, str):
        try:
            return json.loads(actual) == expected
        except Exception:
            return False
    if isinstance(actual, (dict, list)) and isinstance(expected, str):
        try:
            return json.loads(expected) == actual
        except Exception:
            return False
    return False


def _truncate(value, *, n: int = 80) -> str:
    s = repr(value)
    return s if len(s) <= n else s[: n - 3] + "..."
