#!/usr/bin/env bash
# Run the full sweep -> analyze pipeline. Designed to be launched in tmux and
# left overnight. Both phases are idempotent so re-running picks up safely.

set -uo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="data/logs"
mkdir -p "$LOG_DIR"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }

QUERY="${QUERY:-bakery}"
SWEEP_LOG="$LOG_DIR/sweep-$(date +%Y%m%d-%H%M).log"
ANALYZE_LOG="$LOG_DIR/analyze-$(date +%Y%m%d-%H%M).log"

echo "[$(stamp)] === overnight run starting ==="
echo "[$(stamp)] query=$QUERY"
echo "[$(stamp)] sweep log:   $SWEEP_LOG"
echo "[$(stamp)] analyze log: $ANALYZE_LOG"
echo

echo "[$(stamp)] phase 1 — agent sweep --query $QUERY"
.venv/bin/agent sweep --query "$QUERY" 2>&1 | tee "$SWEEP_LOG"
SWEEP_RC=${PIPESTATUS[0]}
echo "[$(stamp)] sweep exit code: $SWEEP_RC"
echo

if [ "$SWEEP_RC" -ne 0 ]; then
    echo "[$(stamp)] sweep failed; not running analyze. Re-run manually after fixing."
    exit "$SWEEP_RC"
fi

echo "[$(stamp)] phase 2 — agent analyze (concurrency=4, delay=1.5)"
.venv/bin/agent analyze --concurrency 4 --delay 1.5 2>&1 | tee "$ANALYZE_LOG"
ANALYZE_RC=${PIPESTATUS[0]}
echo "[$(stamp)] analyze exit code: $ANALYZE_RC"

if [ "$ANALYZE_RC" -eq 2 ]; then
    echo
    echo "[$(stamp)] !! analyze aborted on captcha. Wait 1–4 hours, then run:"
    echo "    .venv/bin/agent analyze --concurrency 2 --delay 4"
    echo "    (it'll skip already-rated prospects automatically)"
elif [ "$ANALYZE_RC" -ne 0 ]; then
    echo "[$(stamp)] analyze failed unexpectedly. See $ANALYZE_LOG"
fi

echo
echo "[$(stamp)] === overnight run finished ==="
echo "[$(stamp)] press Ctrl-D / exit to close this tmux pane, or 'tmux detach' to leave it."
exec bash  # keep the pane interactive after completion
