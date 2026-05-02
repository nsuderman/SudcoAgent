"""CLI dispatcher. `python -m sudco_agent <command>` or installed as `agent`."""
from __future__ import annotations

import argparse
import logging
import sys

from . import config as config_mod
from .commands import (analyze, build, clean_emails, discover, enrich,
                       followup, followup_cold, health, process_bounces,
                       send, send_cold, sweep)
from .review import tui as review_tui


def _setup_logging(verbose: bool, log_file: str | None = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # RichHandler pipes logs through the same Console used by progress bars,
    # so log lines render ABOVE in-flight progress bars instead of clobbering them.
    from rich.logging import RichHandler
    handlers: list[logging.Handler] = [
        RichHandler(show_time=True, show_level=True, show_path=False,
                    rich_tracebacks=True, markup=False),
    ]
    if log_file:
        # Plain FileHandler writes to disk in parallel — gives us persistence
        # without piping stdout through `tee`, which would kill the live bar
        # by hiding the TTY from Rich.
        from pathlib import Path
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(fh)
    logging.basicConfig(level=level, format="%(message)s", datefmt="%H:%M:%S",
                        handlers=handlers)
    # httpx + httpcore log every request at INFO. With hundreds of API PATCHes
    # during a sweep that floods the screen and races the progress bar.
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Sudco Solutions prospecting agent.")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--log-file",
                        help="Also append all log lines to this file. Use INSTEAD OF "
                             "shell `| tee` so the live progress bar can keep the TTY.")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="Sanity-check API + LLM + SMTP reachability.")

    p_disc = sub.add_parser("discover", help="Find prospects via Foursquare and store via the API.")
    p_disc.add_argument("--area", required=True, help='e.g. "Pasco, WA"')
    p_disc.add_argument("--query", help='e.g. "bakery"')
    p_disc.add_argument("--only-without-website", action="store_true",
                        help="Drop businesses that already have a website. Strong signal "
                             "they need one; lets `analyze` focus on the bad-site cases.")
    p_disc.add_argument("--include-chains", action="store_true",
                        help="Include chain locations (default: skip — small businesses only).")
    p_disc.add_argument("--limit", type=int, default=500,
                        help="Max prospects to upsert. Foursquare caps each request at 50; "
                             "values > 50 trigger automatic pagination (default 500).")
    p_disc.add_argument("--skip-if-recent-days", type=int, default=None,
                        help="If the same (area, query) was searched within this many days, skip.")

    p_build = sub.add_parser("build", help="Generate a demo for a single prospect via Qwen.")
    p_build_g = p_build.add_mutually_exclusive_group(required=True)
    p_build_g.add_argument("--prospect-id", type=int)
    p_build_g.add_argument("--all-pending", action="store_true",
                           help="Build for every prospect that doesn't have a demo yet.")
    p_build.add_argument("--min-rating", type=float, default=4.0,
                         help="With --all-pending: only build for prospects with rating "
                              ">= this value. Default 4.0. Ignored when targeting a single "
                              "prospect by --prospect-id.")

    sub.add_parser("review", help="Open the human approval TUI for pending_review demos.")

    p_analyze = sub.add_parser("analyze",
        help="Fetch Google Maps rating (and later, judge existing site with vision LLM).")
    p_analyze.add_argument("--prospect-id", type=int,
                           help="Analyze only one prospect (default: all without a rating).")
    p_analyze.add_argument("--force", action="store_true",
                           help="Re-analyze even prospects that already have a rating.")
    p_analyze.add_argument("--delay", type=float, default=1.5,
                           help="Per-worker delay between Google Maps requests (default 1.5s).")
    p_analyze.add_argument("--concurrency", type=int, default=4,
                           help="Number of parallel Playwright pages (default 4). "
                                "Aggregate rate ≈ concurrency/delay; bump if Google's not rate-limiting you.")
    p_analyze.add_argument("--limit", type=int, default=None,
                           help="Process at most N unrated prospects this run (default: all).")

    p_enrich = sub.add_parser("enrich",
        help="Fill in missing emails by crawling each prospect's existing website.")
    p_enrich.add_argument("--prospect-id", type=int,
                          help="Enrich a specific prospect (default: all that need it).")
    p_enrich.add_argument("--max-age-days", type=int, default=30,
                          help="Skip prospects enriched within this many days (default 30).")
    p_enrich.add_argument("--force", action="store_true",
                          help="Re-enrich even if recently attempted.")
    p_enrich.add_argument("--concurrency", type=int, default=8,
                          help="Number of parallel website fetches (default 8). "
                               "Each worker hits a different prospect's site, so "
                               "this stays polite — no single domain sees more "
                               "than one fetch at a time. Bumped from 4 because "
                               "enrich is httpx-based (light) and rarely "
                               "rate-limited.")

    p_sweep = sub.add_parser("sweep",
        help="Run `discover` across the top N cities (US, TX, or combined).")
    p_sweep.add_argument("--query", help='e.g. "bakery"')
    p_sweep.add_argument("--query-pack",
                         help='Sweep every query in a named pack (loops over it). '
                              'See sudco_agent/data/queries.json. '
                              'Mutually exclusive with --query. Use "all" for every query in every pack.')
    p_sweep.add_argument("--region", choices=("us", "tx", "hou", "all"), default="all",
                         help="us=top 100 US cities, tx=top 100 Texas cities, "
                              "hou=Greater Houston metro, all=all three deduped (default).")
    p_sweep.add_argument("--top", type=int, default=200,
                         help="Number of top cities to sweep (default 200, capped at list size).")
    p_sweep.add_argument("--limit-per-city", type=int, default=5000,
                         help="Max prospects per city — auto-paginates 50/page. Default 5000 = "
                              "effectively 'take everything Foursquare returns.'")
    p_sweep.add_argument("--only-without-website", action="store_true")
    p_sweep.add_argument("--include-chains", action="store_true")
    p_sweep.add_argument("--delay", type=float, default=3.0,
                         help="Seconds between cities (default 3).")
    p_sweep.add_argument("--skip-if-recent-days", type=int, default=7,
                         help="Skip cities last searched within this many days (default 7).")

    sub.add_parser("send", help="Email all demos with status=approved.")
    sub.add_parser("followup", help="(stub) Send follow-up emails after N days.")

    p_cold = sub.add_parser(
        "send-cold",
        help="Cold-blast per-industry shared sample-site URLs to many prospects. "
             "When --industry is omitted, every qualified prospect is matched "
             "against sudco_agent/data/industry_demos.json and gets a per-prospect "
             "demo URL.",
    )
    p_cold.add_argument("--industry", default=None,
                        help='Optional demo slug (e.g. "bakery", "apex-plumbing"). '
                             'When provided, only prospects matching that slug\'s '
                             'keyword set in industry_demos.json are emailed, all '
                             'linked to {COLD_DEMO_BASE_URL}/<slug>?from=<id>. '
                             'When omitted, all qualifying prospects are emailed '
                             'with per-prospect demo URLs from the mapping; '
                             'prospects with no slug match are skipped.')
    p_cold.add_argument("--limit", type=int, default=None,
                        help="Cap on emails this run (default no cap).")
    p_cold.add_argument("--min-rating", type=float, default=None,
                        help="Optional Google rating floor (e.g. 3.5). "
                             "Default None = include unrated.")
    p_cold.add_argument("--require-no-website", action="store_true",
                        help="Only send to prospects without a current_website.")
    p_cold.add_argument("--include-no-email", action="store_true",
                        help="Include prospects with no contact_email "
                             "(they'll be reported but not sent — useful for diagnostic).")
    p_cold.add_argument("--no-site-review", action="store_true",
                        help="Skip the JIT per-prospect site review (no LLM call). "
                             "Faster but the email loses its personalized opening line.")
    p_cold.add_argument("--dry-run", action="store_true",
                        help="Don't actually send; print preview + counts.")
    p_cold.add_argument("--yes", action="store_true",
                        help="Skip the interactive confirmation prompt.")

    p_fucold = sub.add_parser(
        "followup-cold",
        help="Send a follow-up to prospects who got the cold email but "
             "haven't replied (and haven't been followed up yet). Like "
             "send-cold, --industry is optional — omit to follow up across "
             "every demo in industry_demos.json.",
    )
    p_fucold.add_argument("--industry", default=None,
                          help='Optional demo slug. Same semantics as send-cold.')
    p_fucold.add_argument("--limit", type=int, default=None,
                          help="Cap on emails this run (default no cap).")
    p_fucold.add_argument("--days-since-outreach", type=int, default=7,
                          help="Only follow up if the cold email is at least N days "
                               "old (default 7).")
    p_fucold.add_argument("--min-rating", type=float, default=None,
                          help="Optional Google rating floor.")
    p_fucold.add_argument("--dry-run", action="store_true",
                          help="Don't actually send; print preview + counts.")
    p_fucold.add_argument("--yes", action="store_true",
                          help="Skip the interactive confirmation prompt.")

    p_rm = sub.add_parser("delete-prospect",
        help="Permanently delete a prospect and any of its demos.")
    p_rm.add_argument("--id", type=int, required=True, help="Prospect id to delete.")
    p_rm.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")

    p_clean = sub.add_parser(
        "clean-emails",
        help="Re-validate every prospect's contact_email and null out junk "
             "(useful after tightening the scraper).",
    )
    p_clean.add_argument("--dry-run", action="store_true",
                         help="Don't modify; print preview + counts.")
    p_clean.add_argument("--yes", action="store_true",
                         help="Skip the interactive confirmation prompt.")

    p_bounce = sub.add_parser(
        "process-bounces",
        help="Scan IMAP for MAILER-DAEMON bounces AND inbound unsubscribe replies "
             "(subject `UNSUBSCRIBE-<prospect_id>`); mark matching prospects "
             "as bounced_at / unsubscribed_at so send/follow-up runs skip them.",
    )
    p_bounce.add_argument("--folders", default="INBOX,Trash",
                          help="Comma-separated IMAP folders to scan "
                               "(default 'INBOX,Trash').")
    p_bounce.add_argument("--commit", action="store_true",
                          help="Actually PATCH prospects (default is dry-run).")
    p_bounce.add_argument("--commit-imap", action="store_true",
                          help="With --commit: also mark processed bounces "
                               "\\Deleted + EXPUNGE so they don't reprocess. "
                               "Off by default (safer).")
    p_bounce.add_argument("--yes", action="store_true",
                          help="Skip the interactive confirmation prompt.")

    args = parser.parse_args()
    _setup_logging(args.verbose, log_file=args.log_file)
    cfg = config_mod.load()

    try:
        if args.cmd == "health":
            ok = health.run(cfg)
            return 0 if ok else 1

        if args.cmd == "discover":
            result = discover.run(cfg, area=args.area, query=args.query,
                                  only_without_website=args.only_without_website,
                                  skip_chains=not args.include_chains,
                                  limit=args.limit,
                                  skip_if_recent_days=args.skip_if_recent_days)
            if result["skipped_recent"]:
                print("Skipped — searched recently.")
            else:
                print(f"Stored {result['stored']} prospects (from {result['raw']} raw).")
            return 0

        if args.cmd == "build":
            if args.all_pending:
                n = build.run_all_pending(cfg, min_rating=args.min_rating)
                print(f"Built {n} demos.")
            else:
                result = build.run(cfg, prospect_id=args.prospect_id)
                print(f"Built demo: {result['url']}")
            return 0

        if args.cmd == "review":
            review_tui.run(cfg)
            return 0

        if args.cmd == "analyze":
            summary = analyze.run(cfg, only_id=args.prospect_id, force=args.force,
                                  delay_seconds=args.delay, concurrency=args.concurrency,
                                  limit=args.limit)
            total = summary["rating_found"] + summary["no_rating"] + summary["errors"]
            print(f"Processed {total} | "
                  f"rating found {summary['rating_found']} | "
                  f"no rating {summary['no_rating']} | "
                  f"skipped {summary['skipped']} | "
                  f"errors {summary['errors']} | "
                  f"captchas {summary['captchas']} | "
                  f"recycles {summary.get('recycles', 0)}"
                  + (" | ABORTED" if summary["aborted"] else ""))
            return 2 if summary["aborted"] else 0

        if args.cmd == "enrich":
            summary = enrich.run(cfg, only_id=args.prospect_id,
                                 max_age_days=args.max_age_days,
                                 force=args.force,
                                 concurrency=args.concurrency)
            print(f"Processed {summary['processed']} | "
                  f"emails found {summary['found_email']} | "
                  f"no email {summary['no_email']} | "
                  f"skipped {summary['skipped']} | "
                  f"errors {summary['errors']}")
            return 0

        if args.cmd == "sweep":
            if args.query and args.query_pack:
                print("error: --query and --query-pack are mutually exclusive")
                return 2

            if args.query_pack:
                from .data.queries import all_queries, pack, pack_names
                if args.query_pack == "all":
                    queries = all_queries()
                else:
                    try:
                        queries = pack(args.query_pack)
                    except KeyError:
                        print(f"error: unknown query pack {args.query_pack!r}. "
                              f"Available: {', '.join(pack_names())}, all")
                        return 2
            else:
                queries = [args.query]  # may be [None] for unfiltered sweep

            totals = {"cities": 0, "searched": 0, "skipped_recent": 0,
                      "errors": 0, "total_stored": 0}
            for i, q in enumerate(queries, start=1):
                if len(queries) > 1:
                    print(f"\n=== sweep {i}/{len(queries)}: query={q!r} ===")
                summary = sweep.run(cfg,
                                    query=q,
                                    top_n=args.top,
                                    region=args.region,
                                    only_without_website=args.only_without_website,
                                    skip_chains=not args.include_chains,
                                    limit_per_city=args.limit_per_city,
                                    delay_seconds=args.delay,
                                    skip_if_recent_days=args.skip_if_recent_days)
                for k in totals:
                    totals[k] += summary.get(k, 0)
                print(f"Cities: {summary['cities']} | searched: {summary['searched']} | "
                      f"skipped (recent): {summary['skipped_recent']} | errors: {summary['errors']} | "
                      f"new prospects: {summary['total_stored']}")

            if len(queries) > 1:
                print(f"\n=== TOTAL ({len(queries)} queries) ===")
                print(f"Cities: {totals['cities']} | searched: {totals['searched']} | "
                      f"skipped (recent): {totals['skipped_recent']} | errors: {totals['errors']} | "
                      f"new prospects: {totals['total_stored']}")
            return 0

        if args.cmd == "send":
            n = send.run(cfg)
            print(f"Sent {n} emails.")
            return 0

        if args.cmd == "send-cold":
            summary = send_cold.run(
                cfg,
                industry=args.industry,
                limit=args.limit,
                min_rating=args.min_rating,
                require_no_website=args.require_no_website,
                require_email=not args.include_no_email,
                dry_run=args.dry_run,
                yes=args.yes,
                site_review_enabled=not args.no_site_review,
            )
            slug_summary = ""
            if summary.get("per_slug"):
                slug_summary = " | per_slug=" + ",".join(
                    f"{s}:{c}" for s, c in sorted(summary["per_slug"].items(),
                                                  key=lambda kv: -kv[1])
                )
            print(
                f"matched={summary['matched']} | "
                f"filtered={summary['filtered_out']} | "
                f"skipped_already_sent={summary['skipped_already_sent']} | "
                f"skipped_not_analyzed={summary.get('skipped_not_analyzed', 0)} | "
                f"no_demo_match={summary.get('no_demo_match', 0)} | "
                f"would_send={summary['would_send']} | "
                f"sent={summary['sent']} | "
                f"errors={summary['errors']}"
                f"{slug_summary}"
            )
            return 0

        if args.cmd == "followup":
            n = followup.run(cfg)
            print(f"Sent {n} follow-ups.")
            return 0

        if args.cmd == "followup-cold":
            summary = followup_cold.run(
                cfg,
                industry=args.industry,
                limit=args.limit,
                days_since_outreach=args.days_since_outreach,
                min_rating=args.min_rating,
                dry_run=args.dry_run,
                yes=args.yes,
            )
            slug_summary = ""
            if summary.get("per_slug"):
                slug_summary = " | per_slug=" + ",".join(
                    f"{s}:{c}" for s, c in sorted(summary["per_slug"].items(),
                                                  key=lambda kv: -kv[1])
                )
            print(
                f"matched={summary['matched']} | "
                f"no_outreach_yet={summary['no_outreach_yet']} | "
                f"already_followed_up={summary['already_followed_up']} | "
                f"replied={summary['replied']} | "
                f"too_recent={summary['too_recent']} | "
                f"no_demo_match={summary.get('no_demo_match', 0)} | "
                f"would_send={summary['would_send']} | "
                f"sent={summary['sent']} | "
                f"errors={summary['errors']}"
                f"{slug_summary}"
            )
            return 0

        if args.cmd == "clean-emails":
            summary = clean_emails.run(cfg, dry_run=args.dry_run, yes=args.yes)
            print(f"checked={summary['checked']} | "
                  f"valid={summary['valid']} | "
                  f"invalid={summary['invalid']} | "
                  f"cleaned={summary['cleaned']} | "
                  f"errors={summary['errors']}")
            return 0

        if args.cmd == "process-bounces":
            folders = [f.strip() for f in args.folders.split(",") if f.strip()]
            summary = process_bounces.run(
                cfg, folders=folders,
                dry_run=not args.commit,
                commit_imap=args.commit_imap,
                yes=args.yes,
            )
            print(
                f"folders={summary['scanned_folders']} | "
                f"bounces={summary['bounce_messages']} | "
                f"parsed={summary['parsed_addresses']} | "
                f"matched={summary['matched_prospects']} | "
                f"unmatched={summary['unmatched_addresses']} | "
                f"patched={summary['patched']} | "
                f"unsub_msgs={summary['unsubscribe_messages']} | "
                f"unsub_matched={summary['unsubscribe_matched']} | "
                f"unsub_patched={summary['unsubscribe_patched']} | "
                f"imap_deleted={summary['imap_deleted']} | "
                f"errors={summary['errors']}"
            )
            return 0

        if args.cmd == "delete-prospect":
            from .api_client import SudcoAPI
            with SudcoAPI.from_config(cfg) as api:
                p = api.get_prospect(args.id)
                if not p:
                    print(f"No prospect with id={args.id}")
                    return 1
                if not args.yes:
                    resp = input(f"Delete '{p['business_name']}' (id={p['id']}) and all its demos? [y/N] ")
                    if resp.strip().lower() != "y":
                        print("cancelled")
                        return 0
                api.delete_prospect(args.id)
                print(f"Deleted prospect {args.id} ({p['business_name']})")
            return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
