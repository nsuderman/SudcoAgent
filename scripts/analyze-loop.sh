#!/usr/bin/env bash
# Auto-restart wrapper for `agent analyze`. Long Playwright sessions sometimes
# die when the Python ↔ Node driver pipe snaps; the agent often returns rc=0
# anyway because workers log warnings and the queue drains, so we can't trust
# rc to signal progress. Instead, this loop checks the API after each run to
# see how many prospects still need analyze. We exit only when work is truly
# done (or MAX_RESTARTS is hit, defensive).
#
# Skip logic in `_should_skip` excludes prospects with rating + review_excerpts
# already populated, so each restart resumes from where the previous run died
# without duplicate work.
#
# Env vars:
#   CONCURRENCY    — workers per run (default 4)
#   MAX_RESTARTS   — defensive cap (default 30)
#   RESTART_DELAY  — seconds between runs (default 30)
#   DONE_THRESHOLD — exit when remaining work drops below this (default 50)
#
# Usage (in tmux):
#   tmux new-session -d -s analyze-loop \
#     'CONCURRENCY=12 /home/nate/code/dev/sudco-agent/scripts/analyze-loop.sh; read -n1'

set -u
cd "$(dirname "$0")/.."

CONCURRENCY="${CONCURRENCY:-4}"
DELAY="${DELAY:-1.5}"
MAX_RESTARTS="${MAX_RESTARTS:-30}"
RESTART_DELAY="${RESTART_DELAY:-30}"
DONE_THRESHOLD="${DONE_THRESHOLD:-50}"

# Query the API for remaining analyze work. Delegates to `_should_skip` from
# commands/analyze.py so this stays in sync with the actual skip logic
# (including the SKIP_WINDOW_BY_STATUS time gates — without that, prospects
# whose previous lookup landed at status=found-but-no-reviews would be counted
# as remaining work, but the analyze command itself would skip them, leaving
# the loop spinning until MAX_RESTARTS).
remaining_work() {
    .venv/bin/python -c '
from datetime import datetime, timezone
from sudco_agent.api_client import SudcoAPI
from sudco_agent.commands.analyze import _should_skip
from sudco_agent.config import load
need = 0
now = datetime.now(timezone.utc)
with SudcoAPI.from_config(load()) as api:
    for p in api.iter_prospects():
        if not _should_skip(p, force=False, now=now):
            need += 1
print(need)
' 2>/dev/null
}

count=0
while [ "$count" -lt "$MAX_RESTARTS" ]; do
    remaining=$(remaining_work)
    if [ -z "$remaining" ] || [ "$remaining" -lt "$DONE_THRESHOLD" ]; then
        echo "=== Remaining work: ${remaining:-unknown} (< $DONE_THRESHOLD) — done ==="
        exit 0
    fi

    count=$((count + 1))
    log_file="data/logs/analyze-all-$(date +%Y%m%d-%H%M)-run${count}.log"
    echo "=== Run $count/$MAX_RESTARTS — remaining=$remaining — log: $log_file ==="
    .venv/bin/agent --log-file "$log_file" analyze --concurrency "$CONCURRENCY" --delay "$DELAY"
    rc=$?
    echo "=== Run $count exited rc=$rc — sleeping ${RESTART_DELAY}s before next iteration ==="
    sleep "$RESTART_DELAY"
done

echo "=== Hit MAX_RESTARTS=$MAX_RESTARTS without finishing — bailing out ==="
exit 1
