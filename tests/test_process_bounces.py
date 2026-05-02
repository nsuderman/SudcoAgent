"""Pure-logic tests for the unsubscribe-subject parser.

The bounce-parsing path is exercised in production against real DSN messages
and isn't covered here (would require fixture corpora). What IS covered:
the new UNSUBSCRIBE-<id> subject regex — small, exact, easy to drift if
someone changes the List-Unsubscribe header format in mailer.send().
"""
from __future__ import annotations

from email.message import Message

from sudco_agent.commands import process_bounces as pb


def _msg(subject: str, sender: str = "user@example.com") -> Message:
    m = Message()
    m["Subject"] = subject
    m["From"] = sender
    return m


def test_clean_unsubscribe_subject():
    info = pb._parse_unsubscribe(_msg("UNSUBSCRIBE-12345"))
    assert info["prospect_id"] == 12345
    assert info["from"] == "user@example.com"


def test_lowercase_subject_still_matches():
    """Some MUAs lowercase subjects — match case-insensitively."""
    info = pb._parse_unsubscribe(_msg("unsubscribe-9999"))
    assert info["prospect_id"] == 9999


def test_re_prefix_still_matches():
    """If the recipient's mail client prepends 'Re:' — still extract the id."""
    info = pb._parse_unsubscribe(_msg("Re: UNSUBSCRIBE-42"))
    assert info["prospect_id"] == 42


def test_extra_text_around_subject():
    """User edits the subject before sending — still extract."""
    info = pb._parse_unsubscribe(_msg("Please remove me — UNSUBSCRIBE-7 — thanks"))
    assert info["prospect_id"] == 7


def test_no_id_in_subject_returns_none():
    """Plain 'UNSUBSCRIBE' with no id — the parser must not invent one."""
    info = pb._parse_unsubscribe(_msg("UNSUBSCRIBE"))
    assert info["prospect_id"] is None
    assert info["subject"] == "UNSUBSCRIBE"


def test_unrelated_subject_returns_none():
    info = pb._parse_unsubscribe(_msg("Quick question about pricing"))
    assert info["prospect_id"] is None


def test_id_must_be_digits_only():
    """`UNSUBSCRIBE-abc` shouldn't pull a prospect id — defends against
    accidental matches if someone sends a marketing list header."""
    info = pb._parse_unsubscribe(_msg("UNSUBSCRIBE-not-a-number"))
    assert info["prospect_id"] is None


def test_first_id_wins_when_multiple():
    """If somehow two UNSUBSCRIBE-<id> show up in the subject, take the first.
    (Shouldn't happen in practice; protects against ambiguous input.)"""
    info = pb._parse_unsubscribe(_msg("UNSUBSCRIBE-1 then UNSUBSCRIBE-2"))
    assert info["prospect_id"] == 1


def test_missing_subject_returns_none():
    """Defensive — message with no Subject header doesn't crash the parser."""
    m = Message()
    m["From"] = "x@y.com"
    info = pb._parse_unsubscribe(m)
    assert info["prospect_id"] is None
    assert info["subject"] == ""
