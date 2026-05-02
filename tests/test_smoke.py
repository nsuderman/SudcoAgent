"""Lightweight smoke tests — run with `make test`. These exercise pure logic
that doesn't need network or LLM."""
from __future__ import annotations

from sudco_agent.llm import _extract_json


def test_extract_json_clean():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_prose():
    txt = "Sure, here's the JSON:\n\n{\"name\": \"x\", \"n\": 2}\n\nLet me know if you need more."
    assert _extract_json(txt) == {"name": "x", "n": 2}


def test_extract_json_with_markdown_fence():
    txt = "```json\n{\"ok\": true}\n```"
    assert _extract_json(txt) == {"ok": True}


def test_extract_json_array():
    assert _extract_json("[1,2,3]") == [1, 2, 3]
