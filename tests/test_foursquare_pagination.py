"""Tests for Foursquare Link header parsing."""
from __future__ import annotations

from sudco_agent.discovery.foursquare import parse_next_url


def test_canonical_link_header():
    h = '<https://places-api.foursquare.com/places/search?cursor=c3I6Mg>; rel="next"'
    assert parse_next_url(h) == "https://places-api.foursquare.com/places/search?cursor=c3I6Mg"


def test_extra_whitespace():
    h = '<https://example.com/p?cursor=abc>  ;   rel="next"'
    assert parse_next_url(h) == "https://example.com/p?cursor=abc"


def test_case_insensitive_rel():
    h = '<https://example.com/p>; REL="next"'
    assert parse_next_url(h) == "https://example.com/p"


def test_no_next_returns_none():
    h = '<https://example.com/p>; rel="prev"'
    assert parse_next_url(h) is None


def test_empty_returns_none():
    assert parse_next_url("") is None
    assert parse_next_url(None or "") is None


def test_multi_link_picks_next():
    h = ('<https://example.com/prev>; rel="prev", '
         '<https://example.com/next?c=zzz>; rel="next"')
    assert parse_next_url(h) == "https://example.com/next?c=zzz"
