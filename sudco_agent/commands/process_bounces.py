"""`agent process-bounces` — IMAP-scan for MAILER-DAEMON bounce notifications
AND unsubscribe replies, mark the corresponding prospects, and (optionally)
clean the processed messages out of the inbox.

Why this exists: SMTP delivery failures and inbound unsubscribe requests
both land in nate@sudcosolutions.com's inbox as plain emails. Without this
command, both are visible to the human only — the agent has no idea the
recipient is unreachable / opted out, so ``send-cold`` / ``followup-cold``
would keep trying.

Two scan paths run in one IMAP session:

  Bounces — for each ``FROM "MAILER-DAEMON"`` (or postmaster) message:
    1. Parse the failed recipient — DSN ``Final-Recipient:`` is reliable;
       fall back to a body regex for non-DSN MTAs.
    2. Match prospect by ``contact_email``.
    3. PATCH ``bounced_at`` + null ``contact_email`` (let ``enrich`` re-find
       a different one if available).

  Unsubscribes — for each message with ``UNSUBSCRIBE-<id>`` in the subject
  (sent when a recipient clicks the List-Unsubscribe button — Gmail/Outlook
  surface this as a one-click prompt; the user's mail client emails us back
  via the mailto with the prefilled subject):
    1. Extract the prospect_id from the subject.
    2. PATCH ``unsubscribed_at`` (permanent skip in send-cold/followup-cold).
       Don't null ``contact_email`` — the address is fine, we just must not
       contact this person again.

In both paths, the IMAP message is optionally ``\\Deleted`` + EXPUNGEd so
it doesn't get reprocessed on the next run.

Default mode is ``--dry-run``. Pass ``--commit`` to actually write to the
API; pass ``--commit-imap`` to also delete processed messages from IMAP.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.message import Message

from rich.console import Console
from rich.table import Table

from ..api_client import APIError, SudcoAPI
from ..config import Config

log = logging.getLogger(__name__)

# DSN headers carry "Final-Recipient: rfc822;<email>" / "Original-Recipient:"
# in the report part. These are the most reliable signal across MTAs.
DSN_FINAL_RECIPIENT_RE = re.compile(
    r"^(?:Final-Recipient|Original-Recipient)\s*:\s*[^;]+;\s*<?([^>\s]+@[^>\s]+)>?",
    re.IGNORECASE | re.MULTILINE,
)

# Fallback for plain-text bounces that don't include a structured DSN.
# Three patterns observed in the wild, tried in order; first match wins:
#   1. Postfix/Exim w/ SMTP code: "<addr@host>: <SMTP error code> ..."
#   2. Gmail style:               "Your message wasn't delivered to addr@host because..."
#      (Gmail emits a multipart/report but its message/delivery-status part is
#      EMPTY — the recipient only appears in the plain-text body.)
#   3. Postfix DNS/host failure:  "<addr@host>: Host or domain name not found"
#      (no SMTP code because the remote MTA was never reached). Restricted to
#      the recognised failure keywords so this doesn't match arbitrary "<email>:"
#      occurrences in quoted message bodies.
PLAIN_BOUNCE_RECIPIENT_PATTERNS = [
    re.compile(
        r"<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?\s*:\s*"
        r"(?:host\s+\S+\s+said\s*:\s*)?\d{3}",
    ),
    re.compile(
        r"(?:wasn't|was not|couldn't be|could not be)\s+delivered\s+to\s+"
        r"<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?",
        re.IGNORECASE,
    ),
    re.compile(
        r"<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>\s*:\s*"
        r"(?:Host\b|User\s+unknown|Recipient\s+address\s+rejected|"
        r"Mailbox\s+(?:not\s+available|full|unavailable)|"
        r"unknown\s+user|domain\s+(?:not\s+found|invalid)|"
        r"name\s+service\s+error|"
        r"connect\s+to\s+\S+\s+(?:timed\s+out|refused)|"
        r"no\s+such\s+(?:user|recipient))",
        re.IGNORECASE,
    ),
]

# IMAP SEARCH criteria. SUBJECT-side filtering is unreliable across MTAs
# (some say "Undeliverable", some "Mail Delivery Failed", etc.); FROM is
# the only stable signal. We accept either MAILER-DAEMON or postmaster.
SEARCH_CRITERIA_BOUNCES = '(OR FROM "MAILER-DAEMON" FROM "postmaster")'

# Unsubscribe-reply detection: the List-Unsubscribe header carries a
# `mailto:nate@sudcosolutions.com?subject=UNSUBSCRIBE-<prospect_id>`.
# When the recipient hits Gmail/Outlook's one-click unsubscribe prompt,
# their client emails us back with that subject. Match the prospect_id
# anywhere in the subject (allows for "Re: UNSUBSCRIBE-12345" etc.).
SEARCH_CRITERIA_UNSUBSCRIBES = 'SUBJECT "UNSUBSCRIBE-"'
UNSUBSCRIBE_SUBJECT_RE = re.compile(r"UNSUBSCRIBE-(\d+)", re.IGNORECASE)


def run(
    cfg: Config,
    *,
    folders: list[str],
    dry_run: bool = True,
    commit_imap: bool = False,
    yes: bool = False,
) -> dict:
    """Scan IMAP folders for bounce notices and update prospects.

    Args:
      folders: list of IMAP folder names to scan (e.g. ["INBOX", "Trash"]).
      dry_run: if True (default), print what would happen without writing.
      commit_imap: if True, mark processed bounces ``\\Deleted`` + EXPUNGE.
        Independent of dry_run — you can write to the API but leave IMAP
        untouched, which is the safest default.
      yes: skip the interactive confirmation prompt.

    Returns: summary dict with counts.
    """
    summary = {
        "scanned_folders": 0,
        "bounce_messages": 0,
        "parsed_addresses": 0,
        "matched_prospects": 0,
        "unmatched_addresses": 0,
        "patched": 0,
        # Unsubscribe scan (parallel path, same IMAP session).
        "unsubscribe_messages": 0,
        "unsubscribe_matched": 0,
        "unsubscribe_unmatched": 0,
        "unsubscribe_patched": 0,
        "imap_deleted": 0,
        "errors": 0,
    }

    if not cfg.imap_host:
        raise RuntimeError("IMAP_HOST not configured — set it in .env")

    console = Console()
    parsed: list[tuple[str, str, dict]] = []  # (folder, uid, parse_dict)
    unsubs: list[tuple[str, str, dict]] = []  # (folder, uid, {"prospect_id", "subject", "from"})

    try:
        with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
            imap.login(cfg.smtp_user, cfg.smtp_pass)

            for folder in folders:
                summary["scanned_folders"] += 1
                # SELECT can fail if the folder doesn't exist (e.g. "Deleted
                # Items" vs "Trash" depending on the IMAP server). Log + skip.
                status, _ = imap.select(folder, readonly=False)
                if status != "OK":
                    log.warning("IMAP SELECT %r failed (status=%s) — skipping",
                                folder, status)
                    continue

                status, data = imap.uid("SEARCH", None, SEARCH_CRITERIA_BOUNCES)
                if status != "OK":
                    log.warning("IMAP SEARCH in %r failed: %s", folder, status)
                    continue

                uids = (data[0] or b"").split()
                log.info("Folder %r: %d bounce candidate(s)", folder, len(uids))

                for uid in uids:
                    summary["bounce_messages"] += 1
                    status, msg_data = imap.uid("FETCH", uid, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        summary["errors"] += 1
                        continue
                    raw_bytes = msg_data[0][1]
                    if not isinstance(raw_bytes, (bytes, bytearray)):
                        summary["errors"] += 1
                        continue
                    msg = email.message_from_bytes(raw_bytes)
                    info = _parse_bounce(msg)
                    if not info.get("recipient"):
                        summary["errors"] += 1
                        log.debug("No recipient parsed from bounce uid=%s in %s",
                                  uid.decode(), folder)
                        continue
                    summary["parsed_addresses"] += 1
                    parsed.append((folder, uid.decode(), info))

                # Second pass on the same folder: unsubscribe replies.
                status, data = imap.uid("SEARCH", None, SEARCH_CRITERIA_UNSUBSCRIBES)
                if status == "OK":
                    uids = (data[0] or b"").split()
                    log.info("Folder %r: %d unsubscribe candidate(s)", folder, len(uids))
                    for uid in uids:
                        summary["unsubscribe_messages"] += 1
                        # Lighter fetch — we only need headers for the subject + from.
                        status, msg_data = imap.uid("FETCH", uid, "(RFC822.HEADER)")
                        if status != "OK" or not msg_data or not msg_data[0]:
                            summary["errors"] += 1
                            continue
                        raw_bytes = msg_data[0][1]
                        if not isinstance(raw_bytes, (bytes, bytearray)):
                            summary["errors"] += 1
                            continue
                        msg = email.message_from_bytes(raw_bytes)
                        info = _parse_unsubscribe(msg)
                        if info.get("prospect_id") is None:
                            summary["errors"] += 1
                            log.debug("No prospect_id parsed from unsub uid=%s in %s "
                                      "(subject=%r)", uid.decode(), folder, info.get("subject"))
                            continue
                        unsubs.append((folder, uid.decode(), info))

            if not parsed and not unsubs:
                console.print("[yellow]No bounces or unsubscribes found in scanned folders.[/yellow]")
                return summary

            # Match parsed bounces + unsubscribes to prospects.
            with SudcoAPI.from_config(cfg) as api:
                # Index prospects by lowercased contact_email for O(1) lookup
                # (bounce match) and by id (unsubscribe match) — avoids one
                # GET per parsed item on big prospect tables.
                by_email: dict[str, dict] = {}
                by_id: dict[int, dict] = {}
                for p in api.iter_prospects():
                    em = (p.get("contact_email") or "").strip().lower()
                    if em:
                        by_email[em] = p
                    by_id[p["id"]] = p

                rows: list[dict] = []
                for folder, uid, info in parsed:
                    rcpt = info["recipient"].strip().lower()
                    p = by_email.get(rcpt)
                    rows.append({
                        "folder": folder, "uid": uid, "recipient": rcpt,
                        "subject": info.get("subject", ""),
                        "smtp_code": info.get("smtp_code", ""),
                        "prospect": p,
                    })
                    if p:
                        summary["matched_prospects"] += 1
                    else:
                        summary["unmatched_addresses"] += 1

                unsub_rows: list[dict] = []
                for folder, uid, info in unsubs:
                    pid = info["prospect_id"]
                    p = by_id.get(pid)
                    unsub_rows.append({
                        "folder": folder, "uid": uid, "prospect_id": pid,
                        "subject": info.get("subject", ""),
                        "from": info.get("from", ""),
                        "prospect": p,
                    })
                    if p:
                        summary["unsubscribe_matched"] += 1
                    else:
                        summary["unsubscribe_unmatched"] += 1

                _print_preview(console, rows)
                if unsub_rows:
                    _print_unsub_preview(console, unsub_rows)

                if dry_run:
                    console.print(
                        f"\n[bold yellow]DRY RUN[/bold yellow] — pass --commit to "
                        f"PATCH {summary['matched_prospects']} bounce(s) with "
                        f"bounced_at + null contact_email AND "
                        f"{summary['unsubscribe_matched']} unsubscribe(s) with "
                        f"unsubscribed_at."
                    )
                    if commit_imap:
                        console.print(
                            "[yellow](--commit-imap is ignored without --commit; "
                            "we don't delete messages unless we're also persisting them.)[/yellow]"
                        )
                    return summary

                if not yes and not _confirm(console, summary, commit_imap):
                    console.print("[yellow]cancelled[/yellow]")
                    return summary

                # Persist + optionally clean up.
                #
                # IMAP deletion is decoupled from the per-prospect match. A
                # bounce that the parser extracted a recipient from is
                # definitionally a real delivery failure — the email is dead
                # mail regardless of whether we currently track that recipient
                # as a prospect. This matters for two cases:
                #   1. Re-running --commit-imap after a previous --commit pass:
                #      contact_email is now null on the matched prospects, so
                #      they no longer appear in the email-indexed lookup.
                #      Without this decoupling, imap_deleted would always be 0.
                #   2. Bounces for prospects we've since deleted, or for emails
                #      that were never in our DB. Their bounce sits in IMAP
                #      forever otherwise.
                # Same logic applies to unsubscribes.
                ts = datetime.now(timezone.utc).isoformat()
                imap_to_delete: list[tuple[str, str]] = []  # (folder, uid)

                for row in rows:
                    p = row["prospect"]
                    if p is not None:
                        try:
                            api.update_prospect(p["id"], {
                                "bounced_at": ts,
                                "contact_email": None,
                            })
                            summary["patched"] += 1
                        except APIError as exc:
                            summary["errors"] += 1
                            log.error("Patch failed for prospect %d: %s",
                                      p["id"], exc)
                            # Don't delete the IMAP bounce if the patch failed —
                            # a transient API blip should leave the bounce in
                            # place so the next run can retry.
                            continue
                    if commit_imap:
                        imap_to_delete.append((row["folder"], row["uid"]))

                for row in unsub_rows:
                    p = row["prospect"]
                    if p is not None:
                        try:
                            # Don't null contact_email here — the address still
                            # works, we just must not mail it again. The
                            # unsubscribed_at flag handles that.
                            api.update_prospect(p["id"], {
                                "unsubscribed_at": ts,
                            })
                            summary["unsubscribe_patched"] += 1
                        except APIError as exc:
                            summary["errors"] += 1
                            log.error("Unsubscribe patch failed for prospect %d: %s",
                                      p["id"], exc)
                            continue
                    if commit_imap:
                        imap_to_delete.append((row["folder"], row["uid"]))

                # IMAP cleanup (best-effort): mark + expunge per folder.
                if commit_imap and imap_to_delete:
                    by_folder: dict[str, list[str]] = {}
                    for folder, uid in imap_to_delete:
                        by_folder.setdefault(folder, []).append(uid)
                    for folder, uids_in_folder in by_folder.items():
                        try:
                            status, _ = imap.select(folder, readonly=False)
                            if status != "OK":
                                continue
                            for uid in uids_in_folder:
                                imap.uid("STORE", uid.encode(), "+FLAGS", "(\\Deleted)")
                                summary["imap_deleted"] += 1
                            imap.expunge()
                        except Exception as exc:
                            log.warning("IMAP cleanup failed in %r: %s", folder, exc)
    except imaplib.IMAP4.error as exc:
        raise RuntimeError(f"IMAP error: {exc}") from exc

    return summary


def _parse_bounce(msg: Message) -> dict:
    """Pull the failed recipient + a few diagnostic fields out of a bounce
    message. Returns dict with keys: recipient, subject, smtp_code.
    Empty values where the parse couldn't extract them."""
    info: dict = {
        "recipient": None,
        "subject": (msg.get("Subject") or "").strip(),
        "smtp_code": "",
    }

    # Walk the message parts. DSN format: a multipart with a part of
    # content-type "message/delivery-status" carrying RFC 3464 headers.
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if ctype == "message/delivery-status":
            try:
                payload = part.get_payload(decode=True)
                text = payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace") if payload else ""
            except Exception:
                text = ""
            m = DSN_FINAL_RECIPIENT_RE.search(text)
            if m:
                info["recipient"] = m.group(1)
            # Status code from DSN: "Status: 5.1.1" — capture for the table.
            sm = re.search(r"^Status\s*:\s*([\d.]+)", text,
                           re.IGNORECASE | re.MULTILINE)
            if sm:
                info["smtp_code"] = sm.group(1)
            if info["recipient"]:
                return info

    # Fallback: scan the plaintext body for "<addr>: 5xx ..." patterns.
    body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode(part.get_content_charset() or "utf-8",
                                            errors="replace")
            except Exception:
                pass
    for pattern in PLAIN_BOUNCE_RECIPIENT_PATTERNS:
        m = pattern.search(body)
        if m:
            info["recipient"] = m.group(1)
            break

    return info


def _parse_unsubscribe(msg: Message) -> dict:
    """Pull the prospect_id from an unsubscribe reply's subject. The subject
    we care about is `UNSUBSCRIBE-<prospect_id>` (set in our List-Unsubscribe
    mailto). Some clients prepend "Re: " or wrap text — match anywhere."""
    subject = (msg.get("Subject") or "").strip()
    info = {
        "prospect_id": None,
        "subject": subject,
        "from": (msg.get("From") or "").strip(),
    }
    m = UNSUBSCRIBE_SUBJECT_RE.search(subject)
    if m:
        try:
            info["prospect_id"] = int(m.group(1))
        except ValueError:
            pass
    return info


def _print_preview(console: Console, rows: list[dict]) -> None:
    table = Table(title="Bounce processing preview", show_lines=False)
    table.add_column("Folder", style="dim")
    table.add_column("UID", style="dim", justify="right")
    table.add_column("Failed recipient", style="green")
    table.add_column("SMTP")
    table.add_column("Match → Prospect")
    matched = 0
    for row in rows:
        p = row["prospect"]
        if p:
            matched += 1
            match_str = f"[bold green]✓[/bold green] {p.get('business_name', '?')} (id={p['id']})"
        else:
            match_str = "[dim]— no prospect with this email —[/dim]"
        table.add_row(
            row["folder"],
            row["uid"],
            row["recipient"],
            row["smtp_code"] or "—",
            match_str,
        )
    console.print(table)
    console.print(
        f"\n{matched}/{len(rows)} bounces matched to prospects."
    )


def _print_unsub_preview(console: Console, rows: list[dict]) -> None:
    table = Table(title="Unsubscribe processing preview", show_lines=False)
    table.add_column("Folder", style="dim")
    table.add_column("UID", style="dim", justify="right")
    table.add_column("Prospect ID", style="green", justify="right")
    table.add_column("From")
    table.add_column("Match → Prospect")
    matched = 0
    for row in rows:
        p = row["prospect"]
        if p:
            matched += 1
            match_str = f"[bold green]✓[/bold green] {p.get('business_name', '?')}"
        else:
            match_str = "[dim]— no prospect with this id —[/dim]"
        table.add_row(
            row["folder"],
            row["uid"],
            str(row["prospect_id"]),
            (row.get("from") or "")[:40],
            match_str,
        )
    console.print(table)
    console.print(
        f"{matched}/{len(rows)} unsubscribes matched to prospects."
    )


def _confirm(console: Console, summary: dict, commit_imap: bool) -> bool:
    nb = summary["matched_prospects"]
    nu = summary["unsubscribe_matched"]
    extra = " AND mark messages \\Deleted in IMAP" if commit_imap else ""
    console.print(
        f"\n[bold]About to patch {nb} bounced prospect(s) (bounced_at + null "
        f"contact_email) and {nu} unsubscribed prospect(s) (unsubscribed_at)"
        f"{extra}.[/bold]"
    )
    resp = input("Proceed? [y/N] ").strip().lower()
    return resp == "y"
