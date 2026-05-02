# CLAUDE.md — context for Claude Code in this project

This file is loaded into every Claude session in this directory. Keep it short and current.

## What this project is

`sudco-agent` is the **autonomous prospecting + outreach pipeline** for Sudco Solutions (`/home/nate/code/dev/SudcoSolutions/`). Single-operator (Nate), runs on this box. Pipeline:

1. **Discovery** — sweep top US/TX/Houston cities via Foursquare → store prospects in the SudcoSolutions admin API
2. **Enrichment** — crawl each prospect's site for a contact email
3. **Analysis** — fetch Google Maps rating + top-5 review excerpts via headless Playwright
4. **Cold-blast (`agent send-cold`)** — first-touch personalized email linking to a *shared* per-industry sample site at `sudcosolutions.com/demos/<industry>`. Pitches 4 service tiers (site refresh $800, GBP+SEO $300-500, review-funnel automation $200/mo, reputation monitoring $150/mo). Includes a reputation-framed text-LLM observation about the prospect's site + reviews (cached on the prospect)
5. **Follow-up (`agent followup-cold`)** — second-touch email after N days for non-repliers (reuses the cached observation; doesn't re-run the LLM)
6. **Warm path (`agent build` → `review` → `send`)** — only invoked manually after a reply; generates a custom per-prospect demo at `/p/<token>`

State lives entirely in the SudcoSolutions admin API. The agent itself is stateless — re-running any command is safe.

## Tech stack

- Python 3.12, async (`asyncio`) for parallel fetches in `analyze` / `enrich`
- httpx, Playwright (sync + async APIs), BeautifulSoup, tldextract
- Local Qwen via OpenAI-compatible client at `127.0.0.1:8011/v1`:
  - **text**: `qwen3.6-35b-a3b` (warm-path demo content, cold-path reputation-framed site/reviews observation)
  - **vision**: `qwen3-vl-30b-a3b-instruct` (currently unused; was the cold-path observer until the pivot to text+reviews)
- Rich for progress bars; logs via RichHandler + optional FileHandler (`--log-file PATH`)
- Mail via `mail.sudcosolutions.com:587` (STARTTLS); IMAPS on `:993` for Sent-folder save

## Architecture (data flow)

```
Foursquare → discover/sweep → SudcoSolutions API (prospects table)
                                       ↓
                         enrich (httpx) ← email_scraper.crawl
                                       ↓
                         analyze (Playwright) ← Google Maps
                                       ↓
                ┌──────────────────────┴──────────────────────┐
                ↓ cold lane (default)             ↓ warm lane (on reply)
        agent send-cold                    agent build → review → send
        ├─ JIT site_review (Qwen text):    ├─ Qwen text → demo JSON
        │  site HTML + cached reviews →    ├─ stored as demo row
        │  rating-tier-aware observation   ├─ unique /p/<token> URL
        ├─ HTML+text email w/ 4 tiers      └─ used for engaged leads only
        ├─ links to /demos/<industry>
        ├─ writes cold_outreach_at
        └─ saves copy to IMAP Sent
                ↓
        agent followup-cold (after 7d)
        └─ reuses cached site_observation, writes cold_followup_at
```

## Key commands

| Command | What it does |
|---|---|
| `agent sweep --query bakery --region hou` | Discover new prospects across Houston metro |
| `agent enrich --concurrency 4` | Concurrent contact-email scrape (idempotent, 30-day skip) |
| `agent analyze --concurrency 4` | Google Maps rating + top-5 review excerpts (captcha-aware, 4-way parallel) |
| `agent clean-emails --dry-run` | Re-validate `contact_email` and null junk (after scraper changes) |
| `agent send-cold --industry bakery --limit 25 --yes` | First-touch cold-blast targeting one demo's keyword set (cafe + patisserie + donut all routed to the bakery demo via `industry_demos.json`). Text-LLM reputation-framed. No `--min-rating` floor — low-rated prospects are intentionally included. |
| `agent send-cold --limit 200 --yes` | Same, but **all-industries**: every qualified prospect is matched against `sudco_agent/data/industry_demos.json` and gets a per-prospect demo URL. Prospects with no slug match are skipped. |
| `agent followup-cold [--industry SLUG] --yes` | Second-touch follow-up. Same `--industry` semantics as send-cold (omit for all-industries). Uses cached observation, no fresh LLM call. |
| `agent process-bounces` (dry-run) | Scan IMAP INBOX + Trash for two things: (1) MAILER-DAEMON bounces — marks `bounced_at` + nulls bad email; (2) inbound unsubscribe replies (subject `UNSUBSCRIBE-<id>` from List-Unsubscribe header) — marks `unsubscribed_at`. In `--commit` mode actually patches; `--commit-imap` also deletes processed messages. Single IMAP session for both scans. |
| `agent build --prospect-id N` | (Warm path) Generate per-prospect demo via Qwen |
| `agent review` | (Warm path) Human approval TUI |
| `agent send` | (Warm path) Send approved demos with unique `/p/<token>` URL |

## Config (`.env`)

Required vars: `SUDCO_ADMIN_API_KEY`, `SMTP_PASS`. Notable:

- `SMTP_HOST=mail.sudcosolutions.com` and `IMAP_HOST=mail.sudcosolutions.com` — **must NOT be `localhost`** (the docker-mailserver TLS cert is valid for `mail.sudcosolutions.com`/`mail.tradepointstudio.com`, not `localhost`)
- `IMAP_SENT_FOLDER=Sent` (Dovecot's default)
- `COLD_DEMO_BASE_URL=https://sudcosolutions.com/demos`

`load_dotenv` reads `.env` from the project root automatically. Cron jobs work without sourcing .env explicitly.

## Cold-outreach lifecycle (per prospect)

| Field on prospect | Set by | Meaning |
|---|---|---|
| `review_excerpts` | `agent analyze` after rating fetch | JSON-encoded list of top-5 Google review excerpts (`[{author, rating, text}, ...]`). Best-effort — empty if the place page lookup fails. Fed to `site_review` along with site HTML. |
| `cold_outreach_at` | `agent send-cold` after successful SMTP+IMAP | First cold email sent. **Permanently excludes** the prospect from future `send-cold` runs. |
| `site_observation` + `site_observation_at` | `site_review` (during `send-cold`) | Cached per-prospect text-LLM observation, reputation-framed using site HTML + `review_excerpts` + rating-tier guidance. Used by both `send-cold` and `followup-cold`. 90-day TTL. |
| `cold_followup_at` | `agent followup-cold` after successful send | Follow-up email sent. Permanently excludes from future `followup-cold` runs. |
| `cold_replied_at` | **Manual** (admin API) | Marks responder. Excludes from `followup-cold`. No automated reply detection yet. |
| `bounced_at` | `agent process-bounces --commit` | A MAILER-DAEMON delivery failure was observed for this prospect's `contact_email`. **Permanently excludes** from `send-cold` and `followup-cold`. The bad email is nulled at the same time so future enrichment can find a different one. |
| `unsubscribed_at` | `agent process-bounces --commit` | Recipient clicked the List-Unsubscribe button (Gmail/Outlook one-click), which sends a reply with subject `UNSUBSCRIBE-<prospect_id>`. The bounce processor parses the id from the subject and sets this field. **Permanently excludes** from `send-cold` and `followup-cold`. Unlike bounces, `contact_email` is NOT nulled — the address still works, we just legally/ethically must not contact this person again. |

Re-blasting a prospect intentionally requires manually nulling `cold_outreach_at` via PATCH. There is no `--force` knob — this is intentional, prevents accidents.

## Things to know

- **Backend `PATCHABLE_FIELDS` allowlist is strict.** SudcoSolutions's `server/db/index.js` silently drops fields not in the allowlist. The cold-outreach fields above were added there in the same session as the agent code; future new fields need to be added in both places. The agent's `update_prospect` now warns loudly if a sent field isn't echoed back — watch `data/logs/*.log` for `"silently dropped"` warnings.
- **`agent send-cold` is strict first-touch only.** Skips anyone with `cold_outreach_at` set, period. No `--max-age-days`. Followups go through `followup-cold`.
- **Pipeline is gated.** `send-cold` and `followup-cold` skip prospects that haven't been seen by `analyze` yet (rating null AND last_rating_attempt null). This is the `skipped_not_analyzed` counter in the dry-run output. After a fresh `agent sweep`, you must run `agent analyze` before send-cold/followup-cold will pick those prospects up. Without this gate, freshly-discovered prospects could be emailed before their reviews/ratings are scraped, which would cache a weaker site-only `site_observation` for 90 days.
- **API rate-limit:** backend caps admin endpoints at **8000 req/min** (in `SudcoSolutions/server/routes/prospects.js`). Agent transparently retries 429s with exponential backoff and Retry-After awareness in `api_client._req`, so parallel sweeps + enrich + analyze + cron can all run concurrently without dropping work. If you ever see "API rate-limited (429)" warnings in logs, it means the burst exceeded 8000/min momentarily — the retry handles it; if it becomes a sustained problem, bump the cap further in the backend.
- **Don't re-add `--min-rating` to the cron line.** Low-rated prospects are intentionally included — the recurring services (review-funnel automation, reputation monitoring) help them most. The LLM's rating-tier guidance in `site_review.PROMPT_TEMPLATE` produces softer, more empathetic framing for them; don't filter them out at the CLI layer.
- **Visible link text is `sudcosolutions.com`, hidden href has `?from=<prospect_id>`.** Frontend at `sudcosolutions.com/demos/<industry>` reads the `from` param to log clicks via `POST /api/showcase_views`.
- **`fetch_reviews` (in `analysis/gmaps.py`) is best-effort.** It navigates to the place page, clicks the Reviews tab, and scrapes top-N. If the place can't be auto-resolved (ambiguous match, no reviews, layout drift), it returns `[]` and `site_review` falls back to site-HTML-only framing. Don't treat empty `review_excerpts` as a bug.
- **`analyze` re-runs prospects with rating but no `review_excerpts`** to backfill reviews. After deploying review-aware analyze, run `agent analyze --concurrency 4` once to populate reviews on previously-rated prospects.
- **Qwen 3.x reasoning tokens are suppressed** via `disable_thinking=True` in `LLMClient.text_generate` / `vision_judge` — prepends `/no_think` AND sets `chat_template_kwargs.enable_thinking=False`. `site_review._validate_output` still strips `<think>` blocks defensively.
- **Email scraper rejects bare-period and bare-"at" obfuscation.** Only bracketed forms (`[at]` / `(at)` / `{at}`) are deobfuscated. This is what fixed the `registr@ion.but` false-positive bug — don't relax it without unit tests.
- **List-Unsubscribe header in every cold/follow-up.** `mailer.send()` adds `List-Unsubscribe: <mailto:nate@sudcosolutions.com?subject=UNSUBSCRIBE-{prospect_id}>` automatically when called from `send-cold` or `followup-cold`. Gmail/Outlook surface this as a one-click unsubscribe button. The reply lands in nate@'s inbox with the prospect_id-tagged subject; `agent process-bounces` parses the id and marks `unsubscribed_at`. **Don't** add the URI to rspamd's signed-headers — the value is normalized differently by relays and breaks DKIM. The fix lives in `/home/nate/docker-mailserver/docker-data/dms/config/rspamd/override.d/dkim_signing.conf` (explicit `sign_headers` excluding `list-*`); if you regenerate that file, preserve the override.
- **`enrich` MX-filters discovered emails.** After ranking, each candidate's domain is checked for MX records (or A/AAAA fallback per RFC 5321 §5.1). NXDOMAIN or "no records at all" → email is moved to `enrichment.mx_rejected` and `best_email()` skips it. Transient DNS errors (or a missing dnspython) are treated as "uncertain — keep" so we don't lose addresses to flaky lookups. This is the first line of defense against hard bounces; the second line is `process-bounces` reading MAILER-DAEMON. Don't tighten to "MX-only" (drop A-fallback) without measurement — would cost ~5-10% legit addresses.
- **`agent enrich --concurrency 8` is the default.** Bumped from 4 because enrich is httpx-based (light) and rarely rate-limited. Skips fast (~30/sec); only fetches sites for prospects whose `enriched_at` is null or older than 30 days. Don't bump above ~16 — risk of CDN-shared hosting providers (Squarespace, GoDaddy) rate-limiting cumulative request rate.
- **TLS cert hostname matters for SMTP and IMAP.** `localhost` fails verification; use `mail.sudcosolutions.com`. Same .env value covers both unless you override `IMAP_HOST` separately.
- **City list lives in `sudco_agent/data/top_us_cities.py`.** Three lists: 100 US, 100 TX, 37 Greater Houston. `agent sweep --region all` deduplicates to ~210. `--region hou` is Houston-metro only.
- **Query packs in `sudco_agent/data/queries.json`.** 6 packs (food / beauty / personal_services / trades / specialty_retail / health), 70 unique queries. `agent sweep --query-pack <name>` loops them.
- **Industry → demo-slug mapping in `sudco_agent/data/industry_demos.json`.** One-to-many: each demo slug carries a list of industry keywords that route to it. `send-cold` and `followup-cold` use this when `--industry` is omitted (all-industries mode), or when `--industry <slug>` is given (filters to that slug's keyword set rather than literal substring on `<slug>`). Edit the JSON to add new industry keywords or new demos. Insertion order = priority for ambiguous matches (a "fitness cafe" matches both `bakery` and `urban-fitness` — first one wins).
- **Hybrid demo/homepage routing.** When a prospect's industry doesn't map to any slug (e.g. restaurants, pet stores, retail), `send-cold` and `followup-cold` link to `sudcosolutions.com?from=<id>` instead of skipping. The frontend's `Home.js` reads `?from=<id>` and POSTs to `/api/showcase_views` with `demo_slug='homepage'` so click attribution still works. Per-slug breakdown in dry-run output shows `homepage=N` for these. The `no_demo_match` counter is informational, not a skip — those prospects DO get sent.

## Scheduled jobs (this user's crontab — `crontab -l`)

```cron
# Sudco cold-blast pacer: 10 prospects every 10 min (smaller bursts, smoother pacing), Mon–Sat 9 AM – 4 PM CDT
*/10 9-16 * * 1-6 flock -n /tmp/sudco-cold.lock /home/nate/code/dev/sudco-agent/.venv/bin/agent --log-file "/home/nate/code/dev/sudco-agent/data/logs/send-cold-$(date +\%Y\%m\%d-\%H\%M).log" send-cold --limit 10 --yes

# Sudco bounce cleanup: every 30 min at :05 / :35 (between send ticks). Marks prospects bounced_at, nulls
# bad emails, and deletes processed bounces from IMAP.
5,35 9-16 * * 1-6 flock -n /tmp/sudco-bounces.lock /home/nate/code/dev/sudco-agent/.venv/bin/agent --log-file "/home/nate/code/dev/sudco-agent/data/logs/process-bounces-$(date +\%Y\%m\%d).log" process-bounces --commit --commit-imap --yes
```

Cold-blast: ~480/day Mon–Sat (10 × 6 ticks × 8 hours), 2,880/week. Saturday is intentional — target industries (food / beauty / personal services / trades / specialty retail) skew owner-operator and email is welcome any day they're working. Sundays are intentionally excluded (perception risk outweighs incremental volume). Bounces: runs at :05 and :35 of each cold-blast hour (16 ticks/day) — staggered between sends so dead addresses get pruned within ~30 min of being discovered. `flock` prevents overlap per job. `cron` daemon is `systemctl is-active cron` → `active`. Pause cold-blast with `crontab -l | sed 's|^\*/10 9-16|#*/10 9-16|' | crontab -`; pause bounces with `crontab -l | sed 's|^5,35 9-16|#5,35 9-16|' | crontab -`.

**No `--min-rating` floor in the cold-blast line** — see "Things to know" above. Low-rated prospects are intentionally included; the LLM's tiered framing handles them with empathy.

## Conventions

- Default to writing no comments. Only add a comment when *why* is non-obvious. Multi-line module docstrings explaining the strategy of a module are fine.
- Tests in `tests/` are pure-logic only (no network, no DB). New regex / parser code must come with a test — see `test_email_scraper.py`'s regression suite for the pattern.
- Long-running commands (sweep, enrich, analyze, send-cold) all have Rich progress bars + `--log-file` support. New commands of this kind should match the pattern.
- Do not bypass the `PATCHABLE_FIELDS` design — the silent-drop warning was added to make schema mismatches loud.

## Common ops one-liners

```bash
crontab -l                                            # show scheduled jobs
ls -la data/logs/send-cold-*.log | tail -5            # recent cold-blast runs
grep "Sent " data/logs/send-cold-$(date +%Y%m%d)*.log # today's sends
grep "silently dropped" data/logs/*.log               # backend schema mismatch warnings
.venv/bin/pytest                                      # run unit tests
agent send-cold --industry bakery --dry-run           # preview without sending
```

## When testing

- **Cold-blast preview script:** `/tmp/send_test_email.py` is a manual-test scratch. Default mode is preview-only (renders HTML to `/tmp/cold_email_preview.html`). Pass `--send` to actually deliver to `natesuderman@gmail.com`. Each invocation runs the LLM, so don't grep its output without thinking — it'll re-run.
- **A real `--limit 1 --yes` run sends to the highest-id eligible prospect.** Their `cold_outreach_at` is updated permanently; manually backfill via API if you need to re-test against the same prospect.
- **Backend changes need `sudo systemctl restart sudco-api`.** Schema migrations in `server/db/index.js`'s `ensureColumn` calls are idempotent on startup.
