"""Pure-logic tests for the email scraper — no network."""
from __future__ import annotations

from sudco_agent.enrichment import email_scraper as es


def _harvest(html: str, target_domain: str = "bellas.com") -> dict[str, int]:
    found: dict[str, int] = {}
    es._harvest(html, found, target_domain, source_url="https://bellas.com/")
    return found


def test_mailto_link_strongest_signal():
    html = '<a href="mailto:hello@bellas.com">Email us</a>'
    found = _harvest(html)
    assert "hello@bellas.com" in found
    # mailto + domain match → score >= 50
    assert found["hello@bellas.com"] >= 50


def test_visible_text_email():
    html = "<p>Contact us at info@bellas.com or stop by.</p>"
    found = _harvest(html)
    assert "info@bellas.com" in found


def test_obfuscated_email():
    html = "<p>orders [at] bellas [dot] com — please use the form below.</p>"
    found = _harvest(html)
    assert "orders@bellas.com" in found


def test_filters_junk_local_parts():
    html = '<a href="mailto:noreply@bellas.com">noreply</a>'
    assert "noreply@bellas.com" not in _harvest(html)


def test_filters_junk_domains():
    html = "<p>support@gstatic.com support@sentry.io contact@bellas.com</p>"
    found = _harvest(html)
    assert "support@gstatic.com" not in found
    assert "support@sentry.io" not in found
    assert "contact@bellas.com" in found


def test_domain_match_outranks_offdomain():
    html = "info@bellas.com someone@gmail.com"
    found = _harvest(html, target_domain="bellas.com")
    assert found["info@bellas.com"] > found["someone@gmail.com"]


def test_business_local_part_bumps_score():
    html = "info@bellas.com random@bellas.com"
    found = _harvest(html, target_domain="bellas.com")
    assert found["info@bellas.com"] > found["random@bellas.com"]


# ---- regression: false-positives the deobfuscator used to produce ----------

def test_prose_with_at_inside_word_does_not_become_email():
    """`Registration. But the menu...` used to become `registr@ion.but`
    because the deobfuscator matched bare 'at' inside English words AND
    collapsed whitespace around literal periods."""
    html = """
    <p>Registration. But the menu still rotates weekly.</p>
    <p>Generations of bakers. By appointment only.</p>
    <p>Confirmation. Thank you.</p>
    <p>Information. See website.</p>
    <p>Roommates. Upon request.</p>
    <p>Templates. Get started below.</p>
    <p>Scratch. Regarding the new oven.</p>
    """
    found = _harvest(html)
    for fake in (
        "registr@ion.but", "gener@ions.by", "confirm@ion.thank",
        "inform@ion.see", "roomm@es.upon", "templ@es.get",
        "scr@ch.regarding",
    ):
        assert fake not in found, f"deobfuscator regressed and produced {fake}"


def test_visit_us_at_does_not_become_email():
    """`Visit us at example.com` shouldn't deobfuscate into `us@example.com`."""
    html = "<p>Visit us at coolbakery.com for hours.</p>"
    found = _harvest(html)
    assert "us@coolbakery.com" not in found


def test_invalid_tld_rejected_even_if_email_shaped():
    """Even if some rogue substitution produces a thing-shaped-like-email,
    the TLD validation drops it."""
    assert es._looks_like_email("orders@bellas.com") is True
    assert es._looks_like_email("registr@ion.but") is False
    assert es._looks_like_email("ion.thank") is False  # no @
    assert es._looks_like_email("a@b.invalidtld") is False


def test_bracketed_obfuscation_still_works():
    """The supported obfuscation forms (square / round / curly brackets)
    must keep working — that's the whole point of the deobfuscator."""
    for marker_at, marker_dot in [("[at]", "[dot]"), ("(at)", "(dot)"),
                                   ("{at}", "{dot}"), ("[ at ]", "[ dot ]")]:
        html = f"<p>orders {marker_at} bellas {marker_dot} com</p>"
        found = _harvest(html)
        assert "orders@bellas.com" in found, (
            f"deobfuscator failed on legitimate marker {marker_at!r}/{marker_dot!r}: {found}"
        )


# ---- MX-filter on enrichment result ----------------------------------------

def _patch_resolver(monkeypatch, behavior):
    """Patch dns.resolver.Resolver.resolve with a fn(domain, rtype) → answers
    or raise. behavior takes (domain, rtype) and returns/raises."""
    import dns.resolver

    def fake_resolve(self, domain, rtype):
        return behavior(domain, rtype)

    monkeypatch.setattr(dns.resolver.Resolver, "resolve", fake_resolve)


class _FakeAnswers:
    def __init__(self, n=1): self._n = n
    def __len__(self): return self._n


def test_domain_has_mail_dest_with_mx(monkeypatch):
    import dns.resolver
    def behavior(domain, rtype):
        if rtype == "MX":
            return _FakeAnswers(2)
        raise dns.resolver.NoAnswer()
    _patch_resolver(monkeypatch, behavior)
    assert es._domain_has_mail_dest("example.com") is True


def test_domain_has_mail_dest_implicit_mx_via_a(monkeypatch):
    """No MX but has A → still mail-deliverable per RFC 5321 §5.1."""
    import dns.resolver
    def behavior(domain, rtype):
        if rtype == "MX":
            raise dns.resolver.NoAnswer()
        if rtype == "A":
            return _FakeAnswers()
        raise dns.resolver.NoAnswer()
    _patch_resolver(monkeypatch, behavior)
    assert es._domain_has_mail_dest("example.com") is True


def test_domain_has_mail_dest_nxdomain(monkeypatch):
    import dns.resolver
    def behavior(domain, rtype):
        raise dns.resolver.NXDOMAIN()
    _patch_resolver(monkeypatch, behavior)
    assert es._domain_has_mail_dest("nope.example") is False


def test_domain_has_mail_dest_no_mx_no_a(monkeypatch):
    import dns.resolver
    def behavior(domain, rtype):
        raise dns.resolver.NoAnswer()
    _patch_resolver(monkeypatch, behavior)
    assert es._domain_has_mail_dest("zombie.example") is False


def test_domain_has_mail_dest_transient_returns_none(monkeypatch):
    """Transient DNS errors → None (caller keeps the email)."""
    import dns.exception
    def behavior(domain, rtype):
        raise dns.exception.Timeout()
    _patch_resolver(monkeypatch, behavior)
    assert es._domain_has_mail_dest("flaky.example") is None


def test_best_email_skips_mx_rejected():
    """best_email() must skip rejected entries, even if they ranked first."""
    result = es.EnrichmentResult(
        emails=["info@deaddomain.com", "owner@gmail.com"],
        pages_fetched=[],
        mx_rejected=["info@deaddomain.com"],
    )
    assert result.best_email() == "owner@gmail.com"


def test_best_email_returns_none_when_all_rejected():
    result = es.EnrichmentResult(
        emails=["info@deaddomain.com"],
        pages_fetched=[],
        mx_rejected=["info@deaddomain.com"],
    )
    assert result.best_email() is None
