#!/bin/zsh

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="/Users/xiangyu/job-search-os"
CACHE_DIR="$PROJECT_DIR/.cache"
LOG_FILE="$PROJECT_DIR/logs/morning-plan.log"
PLAN_FILE="$CACHE_DIR/daily-plan.md"
CODEX_BIN="${MORNING_PLAN_CODEX_BIN:-codex}"

cd "$PROJECT_DIR"
mkdir -p "$CACHE_DIR" "$PROJECT_DIR/logs"

log() {
  print -r -- "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG_FILE"
}

log "morning plan started"

if ! PLANNER_CONTEXT=$(python3 scripts/build_planner_context.py); then
  log "context build failed; Daily Plan unchanged"
  exit 1
fi

PROMPT=$(cat <<EOF
Generate today's Morning Plan using only the prepared planner context below.

Follow AGENTS.md planning rules, but do not read Lark or any other external
resource. Do not use lark-cli, MCP, browser automation, connectors, or search.
Do not modify local files or Lark. Only generate the final daily plan.

Requirements:
- Prefer 3–4 focused tasks.
- Maximum 5 tasks.
- Maximum 2 P0 tasks.
- Include estimated time for every task.
- Give every task a concrete completion criterion.
- Include a Minimum Viable Day.
- Briefly explain why each task was selected.

Prepared planner context:

$PLANNER_CONTEXT
EOF
)

rm -f "$PLAN_FILE"
CODEX_EVENTS=$(mktemp /private/tmp/morning-plan-events.XXXXXX)
trap 'rm -f "$CODEX_EVENTS"' EXIT
CODEX_STARTED=$(date +%s)

if ! print -r -- "$PROMPT" | "$CODEX_BIN" exec \
  --sandbox read-only \
  --ephemeral \
  --cd "$PROJECT_DIR" \
  --output-last-message "$PLAN_FILE" \
  --json \
  - > "$CODEX_EVENTS" 2>> "$LOG_FILE"; then
  log "Codex planner failed; Daily Plan unchanged"
  exit 1
fi

CODEX_ELAPSED=$(( $(date +%s) - CODEX_STARTED ))
CODEX_TOKENS=$(python3 - "$CODEX_EVENTS" <<'PY'
import json
import sys

best = None

def visit(value):
    global best
    if isinstance(value, dict):
        if any(key in value for key in ("total_tokens", "input_tokens", "output_tokens")):
            best = value
        for child in value.values():
            visit(child)
    elif isinstance(value, list):
        for child in value:
            visit(child)

for line in open(sys.argv[1], encoding="utf-8"):
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    visit(event)

if best is None:
    print("unknown")
else:
    total = best.get("total_tokens")
    if total is None and best.get("input_tokens") is not None and best.get("output_tokens") is not None:
        total = best["input_tokens"] + best["output_tokens"]
    print(total if total is not None else "unknown")
PY
)
log "Codex planner succeeded runtime_seconds=$CODEX_ELAPSED tokens=$CODEX_TOKENS"

if [[ ! -s "$PLAN_FILE" ]]; then
  log "Codex output was empty; Daily Plan unchanged"
  exit 1
fi

if ! python3 scripts/write_daily_plan.py "$PLAN_FILE" >> "$LOG_FILE" 2>&1; then
  log "Daily Plan writer or verification failed; inspect Daily Plan state"
  exit 1
fi

log "Daily Plan write completed and was verified"
