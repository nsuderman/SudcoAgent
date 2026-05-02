"""Pure-logic tests for the Google Maps rating parser."""
from __future__ import annotations

from sudco_agent.analysis.gmaps import parse_rating_label


def test_canonical_aria_label():
    # The shape we see most often
    assert parse_rating_label("Bella's Bakery, 4.5 stars 89 Reviews") == (4.5, 89)


def test_dot_separated():
    assert parse_rating_label("4.5 stars · 1,234 reviews") == (4.5, 1234)


def test_parenthesized_count():
    assert parse_rating_label("Apex Plumbing, 4.7 stars (245)") == (4.7, 245)


def test_comma_separated_thousands():
    assert parse_rating_label("Big Chain · 4.2 stars 12,500 reviews") == (4.2, 12500)


def test_no_review_count():
    # New listing with rating but no count yet — accept rating-only
    assert parse_rating_label("Some Place, 4.5 stars") == (4.5, None)


def test_no_rating_returns_none():
    assert parse_rating_label("Open · Closes 9 PM · phone (555) 123-4567") == (None, None)


def test_handles_capital_stars():
    assert parse_rating_label("4.6 Stars 12 Reviews") == (4.6, 12)


def test_integer_rating():
    assert parse_rating_label("Quick Stop, 5 stars 8 Reviews") == (5.0, 8)
