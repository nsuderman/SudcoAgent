"""Unit tests for captcha detection — pure logic, no Playwright needed."""
from __future__ import annotations

from sudco_agent.analysis.gmaps import looks_like_captcha


def test_no_signals_returns_none():
    assert looks_like_captcha(final_url="https://www.google.com/maps/search/bakery") is None


def test_sorry_url_detected():
    m = looks_like_captcha(final_url="https://www.google.com/sorry/index?continue=...")
    assert m is not None
    assert m.startswith("captcha:url:")


def test_recaptcha_url_detected():
    m = looks_like_captcha(final_url="https://www.google.com/recaptcha/api2/...")
    assert m is not None
    assert m.startswith("captcha:url:")


def test_unusual_traffic_in_title():
    m = looks_like_captcha(title="Sorry — unusual traffic from your computer network")
    assert m is not None
    assert m.startswith("captcha:title:")


def test_automated_requests_in_body():
    body = "We have detected unusual traffic. Our systems require you to verify automated requests."
    m = looks_like_captcha(body_snippet=body)
    assert m is not None
    assert m.startswith("captcha:body:")


def test_im_not_a_robot_in_body():
    m = looks_like_captcha(body_snippet="Please complete the I'm not a robot challenge below.")
    assert m is not None


def test_case_insensitive():
    m = looks_like_captcha(title="UNUSUAL TRAFFIC DETECTED")
    assert m is not None


def test_url_takes_precedence():
    m = looks_like_captcha(
        final_url="https://www.google.com/sorry/index",
        title="ignored — sorry-page already shorted out",
    )
    assert m is not None
    assert "url:" in m
