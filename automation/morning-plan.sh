#!/bin/zsh

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="/Users/xiangyu/job-search-os"
CACHE_DIR="$PROJECT_DIR/.cache"
LOG_FILE="$PROJECT_DIR/logs/morning-plan.log"
PLAN_FILE="$CACHE_DIR/daily-plan.md"
SUCCESS_MARKER="$CACHE_DIR/morning-plan-last-success"
CODEX_BIN="${MORNING_PLAN_CODEX_BIN:-codex}"

TODAY=$(date +%Y-%m-%d)
LOCAL_TIME=$(date +%H%M)

if [[ -f "$SUCCESS_MARKER" ]] && [[ "$(tr -d '[:space:]' < "$SUCCESS_MARKER")" == "$TODAY" ]]; then
  exit 0
fi

if [[ "$LOCAL_TIME" < "0930" ]]; then
  exit 0
fi

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

Task selection:
- Prioritize, in order:
  1. Imminent interview or deadline work.
  2. High-value unfinished carry-over work.
  3. Major skill gaps that have not yet been practiced.
  4. Spaced reviews that are explicitly due today.
  5. Lower-priority reinforcement or ready topics.
- Do not let spaced-review work crowd out higher-value unfinished interview
  deliverables or major unpracticed gaps.

Spaced review rules:
- Use only explicit review dates / due information from the SPACED REVIEWS
  section. Do not infer that a topic is overdue solely because it is marked
  gap or weak.
- A topic practiced yesterday should not normally be reviewed again today
  unless the planner context explicitly marks it due or there is a strong
  interview-driven reason.
- Apply gap > weak > ready priority only among reviews that are already due.
- Prefer one focused spaced-review task per day unless multiple reviews are
  genuinely overdue and higher-priority work still fits.
- Do not combine multiple unrelated review topics into one large task merely
  to clear the review queue.
- Future reinforcement does not mean the previous daily task was incomplete.

Workload rules:
- Keep P0 work realistic for the current day rather than maximizing coverage.
- Prefer finishing a nearly-complete high-value artifact over repeatedly
  reopening already-practiced topics.
- The Minimum Viable Day should be genuinely minimal: normally one main P0
  task, optionally plus one short review, and should usually fit within
  about 60–90 minutes.
- Do not label work as "overdue" unless the prepared planner context explicitly
  shows that its review date has passed.

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

MARKER_TMP="$SUCCESS_MARKER.tmp.$$"
if ! print -r -- "$TODAY" > "$MARKER_TMP" || ! mv -f "$MARKER_TMP" "$SUCCESS_MARKER"; then
  rm -f "$MARKER_TMP"
  log "Daily Plan succeeded but success marker update failed"
  exit 1
fi
log "Daily Plan write completed and was verified; success marker updated date=$TODAY"
