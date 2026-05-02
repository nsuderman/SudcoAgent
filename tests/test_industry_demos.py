"""Tests for the industry → demo-slug mapping logic."""
from __future__ import annotations

import json

import pytest

from sudco_agent.data import industry_demos


def test_load_returns_dict_with_known_slugs():
    mapping = industry_demos.load()
    assert isinstance(mapping, dict)
    # Sanity: at least the bakery demo we shipped first should be present.
    assert "bakery" in mapping
    assert isinstance(mapping["bakery"], list)
    assert all(isinstance(kw, str) for kw in mapping["bakery"])


def test_find_slug_matches_primary_industry():
    p = {"industry": "Bakery", "categories": []}
    assert industry_demos.find_slug(p) == "bakery"


def test_find_slug_matches_category_when_industry_unrelated():
    # Industry is generic but a category names a specific business type.
    p = {"industry": "Restaurant", "categories": ["Cafe", "Coffee Shop"]}
    assert industry_demos.find_slug(p) == "bakery"


def test_find_slug_handles_categories_as_json_string():
    # Backend returns categories as a JSON-encoded TEXT field; helper must
    # parse defensively rather than iterate the raw string character-by-char.
    p = {"industry": "Florist", "categories": json.dumps(["Flower Shop"])}
    assert industry_demos.find_slug(p) == "wild-stem"


def test_find_slug_returns_none_when_no_keyword_matches():
    p = {"industry": "Auto Repair", "categories": ["Mechanic"]}
    assert industry_demos.find_slug(p) is None


def test_find_slug_returns_none_for_empty_prospect():
    assert industry_demos.find_slug({}) is None
    assert industry_demos.find_slug({"industry": "", "categories": []}) is None


def test_matches_slug_uses_keyword_set_not_literal_substring():
    # A "Cafe" should match the bakery slug even though "bakery" doesn't
    # appear anywhere in the prospect's industry/categories — that's the
    # whole point of the one-to-many mapping.
    p = {"industry": "Cafe", "categories": []}
    assert industry_demos.matches_slug(p, "bakery") is True


def test_matches_slug_falls_back_to_literal_for_unknown_slug():
    # If someone passes --industry to a slug we haven't registered yet,
    # we should still let them blast by literal substring rather than
    # silently match nothing.
    p = {"industry": "Tattoo Parlor", "categories": []}
    assert industry_demos.matches_slug(p, "tattoo") is True
    assert industry_demos.matches_slug(p, "bakery") is False


def test_keywords_for_slug_returns_list():
    bakery_kw = industry_demos.keywords_for_slug("bakery")
    assert isinstance(bakery_kw, list)
    assert "bakery" in [kw.lower() for kw in bakery_kw]
    # Unknown slug returns empty list, not raises.
    assert industry_demos.keywords_for_slug("nonexistent") == []


def test_slugs_returns_priority_ordered_list():
    slugs = industry_demos.slugs()
    assert isinstance(slugs, list)
    # Bakery is the first demo we shipped; ensure it survives JSON ordering.
    assert "bakery" in slugs
