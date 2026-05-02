"""Crawl a prospect's existing website to find a contact email.

Strategy:
  1. Fetch the homepage. Look for `mailto:` links and visible email patterns.
  2. If nothing found, fetch up to 3 obvious contact pages (/contact, /about, /contact-us).
  3. Score every email found, prefer the one whose domain matches the site's domain.
  4. Filter out junk: noreply@, do-not-reply@, *@sentry.io, etc.

Politeness:
  - Custom User-Agent so we're identifiable.
  - 8s timeout per fetch; 3 max-pages cap; 1s delay between fetches.
  - Max 256 KB body — bigger pages are usually art-heavy and unlikely to have emails.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
import tldextract
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; SudcoAgent/0.1; +https://sudcosolutions.com) "
    "contact-discovery - requests homepage + /contact /about only"
)
MAX_BODY_BYTES = 256 * 1024
TIMEOUT = httpx.Timeout(8.0, connect=4.0)
MAX_PAGES = 3
INTER_FETCH_DELAY = 1.0

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Deobfuscation: accept ONLY explicitly-bracketed obfuscation markers
# ([at] / (at) / {at} / [dot] / (dot) / {dot}). These are self-delimiting
# and unambiguous — when a site uses them, they mean it.
#
# History note: an earlier version also accepted whitespace-only forms
# (" at " / " dot ") and a bare period. That collapsed normal English like
# "Registration. But..." into "Registr@ion.But" — a fake but EMAIL_RE-shaped
# string that polluted the DB with garbage like `registr@ion.but`. The lost
# coverage on whitespace-only obfuscations is worth the deliverability win.
OBFUSCATED_AT = re.compile(
    r"\s*[\(\[\{]\s*at\s*[\)\]\}]\s*",
    re.IGNORECASE,
)
OBFUSCATED_DOT = re.compile(
    r"\s*[\(\[\{]\s*dot\s*[\)\]\}]\s*",
    re.IGNORECASE,
)

JUNK_LOCAL_PARTS = {
    "noreply", "no-reply", "do-not-reply", "donotreply", "wordpress",
    "postmaster", "abuse", "webmaster",
}
JUNK_DOMAINS = {
    "sentry.io", "google.com", "googleapis.com", "gstatic.com", "wixpress.com",
    "wordpress.com", "godaddy.com", "facebook.com", "instagram.com",
    "schema.org", "example.com", "example.org", "domain.com", "yourdomain.com",
}

CONTACT_PATHS = ("/contact", "/contact-us", "/about", "/about-us", "/contact.html")


@dataclass
class EnrichmentResult:
    emails: list[str]
    pages_fetched: list[str]
    error: str | None = None
    # Emails whose domain has no MX/A records — i.e., would hard-bounce.
    # Kept on the result for diagnostics; best_email() filters them out.
    mx_rejected: list[str] = field(default_factory=list)

    def best_email(self) -> str | None:
        for e in self.emails:
            if e not in self.mx_rejected:
                return e
        return None


def crawl(website: str) -> EnrichmentResult:
    """Try to find a contact email for a website. Returns ranked emails."""
    if not website:
        return EnrichmentResult([], [], error="no website")

    try:
        target_url = _normalize_url(website)
    except Exception as exc:
        return EnrichmentResult([], [], error=f"bad url: {exc}")

    # SSRF guard: prospect URLs come from third-party data (Foursquare today,
    # potentially admin entry later). Refuse to fetch anything that points at
    # an internal address — loopback, RFC1918, link-local, etc.
    blocked = _ssrf_block_reason(target_url)
    if blocked:
        return EnrichmentResult([], [], error=f"refused: {blocked}")

    target_domain = _registered_domain(target_url)

    pages_visited: list[str] = []
    emails: dict[str, int] = {}  # email -> score

    state = {"insecure": False}  # flips to True after first SSL fallback so we don't keep retrying TLS

    def fetch(url: str) -> str | None:
        return _safe_get_with_tls_fallback(url, state)

    # Homepage first — most useful
    homepage_html = fetch(target_url)
    if homepage_html is None:
        return EnrichmentResult([], [target_url], error="homepage fetch failed")
    pages_visited.append(target_url)
    _harvest(homepage_html, emails, target_domain, source_url=target_url)

    # If we already have a domain-matched email, no need to crawl further
    if not _has_domain_match(emails, target_domain):
        for path in CONTACT_PATHS:
            if len(pages_visited) >= MAX_PAGES:
                break
            contact_url = urljoin(target_url, path)
            if contact_url == target_url or contact_url in pages_visited:
                continue
            time.sleep(INTER_FETCH_DELAY)
            html = fetch(contact_url)
            if html:
                pages_visited.append(contact_url)
                _harvest(html, emails, target_domain, source_url=contact_url)
                if _has_domain_match(emails, target_domain):
                    break

    ranked = [e for e, _ in sorted(emails.items(), key=lambda kv: kv[1], reverse=True)]

    # Drop emails on domains that definitely won't accept mail (NXDOMAIN, or
    # neither MX nor A/AAAA). DNS errors / dnspython missing are treated as
    # "uncertain — keep" so we don't lose addresses to transient lookup failures.
    # Cuts hard-bounce rate by catching dead/parked domains before send.
    mx_cache: dict[str, bool | None] = {}
    mx_rejected: list[str] = []
    for email in ranked:
        domain = email.split("@", 1)[1]
        if domain not in mx_cache:
            mx_cache[domain] = _domain_has_mail_dest(domain)
        if mx_cache[domain] is False:
            mx_rejected.append(email)
            log.info("dropping %s — domain %s has no mail destination", email, domain)

    return EnrichmentResult(
        emails=ranked,
        pages_fetched=pages_visited,
        mx_rejected=mx_rejected,
    )


def fetch_html(url: str) -> str | None:
    """Fetch HTML from a URL with SSRF guard, TLS-verify fallback, and size cap.
    Returns the decoded HTML or None on any failure (bad URL, blocked host,
    network error, non-HTML content type, oversized body).

    Public helper so other modules (site_review) can share the same
    well-tested fetch logic without duplicating the SSRF / TLS quirks.
    """
    if not url:
        return None
    try:
        target = _normalize_url(url)
    except Exception:
        return None
    if _ssrf_block_reason(target):
        return None
    return _safe_get_with_tls_fallback(target, {"insecure": False})


def _ssrf_block_reason(url: str) -> str | None:
    """Return a short reason if `url` resolves to a non-public address, else None.
    Best-effort — only validates the initial host. Redirect targets are not
    re-validated, but the alternative (custom transport) isn't worth the weight
    at solo-prospector volume."""
    try:
        host = urlparse(url).hostname
    except Exception:
        return "bad url"
    if not host:
        return "no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return f"dns: {exc}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_unspecified or ip.is_reserved or ip.is_multicast):
            return f"non-public host {host} → {ip_str}"
    return None


def _safe_get_with_tls_fallback(url: str, state: dict) -> str | None:
    """Fetch `url` with TLS verification on. If we hit a cert-verification
    error, log once and retry with verify=False — small businesses on
    DIY hosts often have expired/self-signed certs and we still need their
    HTML to harvest contact info. `state["insecure"]` short-circuits the
    first attempt once we've already fallen back during this crawl."""
    if not state["insecure"]:
        try:
            with httpx.Client(
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=TIMEOUT,
                follow_redirects=True,
                max_redirects=4,
                verify=True,
            ) as client:
                return _safe_get(client, url, reraise_tls=True)
        except Exception as exc:
            if not _is_tls_failure(exc):
                log.debug("fetch failed for %s: %s", url, exc)
                return None
            log.warning("TLS verification failed for %s (%s) — retrying without verify", url, exc)
            state["insecure"] = True

    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=4,
        verify=False,
    ) as client:
        return _safe_get(client, url)


def _is_tls_failure(exc: BaseException) -> bool:
    """Walk the exception chain looking for an SSL-related failure. httpx
    wraps these as ConnectError(cause=ssl.SSLCertVerificationError) typically."""
    seen = set()
    cur: BaseException | None = exc
    while cur and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__.lower()
        if "ssl" in name or "certificate" in name:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# ----- helpers ---------------------------------------------------------------

def _normalize_url(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    p = urlparse(url)
    if not p.netloc:
        raise ValueError("no host in url")
    return f"{p.scheme}://{p.netloc}{p.path or '/'}"


def _registered_domain(url: str) -> str:
    """e.g. https://www.bellas-bakery.com/about → bellas-bakery.com"""
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}".lower() if ext.suffix else ext.domain.lower()


def _safe_get(client: httpx.Client, url: str, *, reraise_tls: bool = False) -> str | None:
    try:
        with client.stream("GET", url) as r:
            if r.status_code >= 400:
                return None
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "xml" not in ctype:
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY_BYTES:
                    break
            try:
                return b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
            except Exception:
                return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception as exc:
        if reraise_tls and _is_tls_failure(exc):
            raise
        log.debug("fetch failed for %s: %s", url, exc)
        return None


def _harvest(html: str, found: dict[str, int], target_domain: str, *, source_url: str) -> None:
    soup = BeautifulSoup(html, "html.parser")

    # 1. mailto: links — strongest signal
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr:
                _maybe_add(addr, found, target_domain, weight=10, label="mailto")

    # 2. Visible text email patterns
    text = soup.get_text(separator=" ")
    for match in EMAIL_RE.finditer(text):
        _maybe_add(match.group(0), found, target_domain, weight=3, label="text")

    # 3. Deobfuscated patterns ("info [at] example [dot] com")
    deobs = OBFUSCATED_DOT.sub(".", OBFUSCATED_AT.sub("@", text))
    for match in EMAIL_RE.finditer(deobs):
        candidate = match.group(0)
        if candidate not in found:
            _maybe_add(candidate, found, target_domain, weight=2, label="obfuscated")


def _maybe_add(candidate: str, found: dict[str, int], target_domain: str, *, weight: int, label: str) -> None:
    candidate = candidate.lower().strip().strip(".")
    if not _looks_like_email(candidate):
        return
    if _is_junk(candidate):
        return
    score = weight
    # Strongly prefer emails on the prospect's own domain
    domain = candidate.split("@", 1)[1]
    if target_domain and (domain == target_domain or domain.endswith("." + target_domain)):
        score += 50
    # Slightly prefer "businessy" inboxes (info, contact, hello, sales, owner)
    local = candidate.split("@", 1)[0]
    if local in {"info", "contact", "hello", "sales", "owner", "manager", "office"}:
        score += 5
    found[candidate] = max(found.get(candidate, 0), score)


def _looks_like_email(s: str) -> bool:
    if "@" not in s:
        return False
    local, _, domain = s.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if len(s) > 254:
        return False
    # TLD must be a real registered public suffix. Catches false positives
    # like "ion.but" / "ion.thank" where the deobfuscation pass produced an
    # email-shaped string from prose. tldextract uses the Public Suffix List
    # so this also handles multi-part TLDs (e.g. "co.uk").
    ext = tldextract.extract(f"http://{domain}")
    if not ext.suffix:
        return False
    return True


def _is_junk(email: str) -> bool:
    local, _, domain = email.partition("@")
    if local in JUNK_LOCAL_PARTS:
        return True
    if domain in JUNK_DOMAINS:
        return True
    # CDN / asset hosts that sometimes appear in source text
    if domain.endswith(".sentry.io") or domain.endswith(".gstatic.com"):
        return True
    return False


def _domain_has_mail_dest(domain: str, *, timeout: float = 3.0) -> bool | None:
    """Returns True if `domain` has any plausible mail destination — an MX
    record, or A/AAAA fallback per RFC 5321 §5.1 (implicit MX). False if the
    domain definitely won't accept mail (NXDOMAIN, or no MX/A/AAAA at all).
    None on transient DNS failures or missing dnspython — caller treats this
    as "unknown, keep the email."
    """
    try:
        import dns.exception
        import dns.resolver
    except ImportError:
        return None

    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout

    try:
        answers = resolver.resolve(domain, "MX")
        if len(answers) > 0:
            return True
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        pass  # no MX → check A/AAAA fallback
    except dns.exception.DNSException:
        return None

    for rtype in ("A", "AAAA"):
        try:
            resolver.resolve(domain, rtype)
            return True
        except dns.resolver.NoAnswer:
            continue
        except dns.resolver.NXDOMAIN:
            return False
        except dns.exception.DNSException:
            return None
    return False


def _has_domain_match(found: Iterable, target_domain: str) -> bool:
    if not target_domain:
        return False
    for email in found:
        if "@" in email:
            d = email.split("@", 1)[1]
            if d == target_domain or d.endswith("." + target_domain):
                return True
    return False
