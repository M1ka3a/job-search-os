#!/bin/zsh

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT_DIR="/Users/xiangyu/job-search-os"
CACHE_DIR="$PROJECT_DIR/.cache"
LOG_FILE="$PROJECT_DIR/logs/evening-review.log"
PROPOSAL_FILE="$CACHE_DIR/evening-review.json"
SCHEMA_FILE="$PROJECT_DIR/config/review-schema.json"
CODEX_BIN="${EVENING_REVIEW_CODEX_BIN:-codex}"
TODAY=$(date +%Y-%m-%d)

cd "$PROJECT_DIR"
mkdir -p "$CACHE_DIR" "$PROJECT_DIR/logs"

log() {
  print -r -- "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG_FILE"
}

log "evening review started"

if ! REVIEW_CONTEXT=$(python3 scripts/build_review_context.py); then
  log "review context build failed; no Lark writes"
  exit 1
fi

print "Enter your free-form daily review. Finish with a line containing END_REVIEW."
USER_REPORT=""
while IFS= read -r REVIEW_LINE; do
  [[ "$REVIEW_LINE" == "END_REVIEW" ]] && break
  USER_REPORT+="${REVIEW_LINE}"$'\n'
done

if [[ -z "${USER_REPORT//[[:space:]]/}" ]]; then
  log "empty user review; no Lark writes"
  print "No review entered. Nothing changed."
  exit 0
fi

PROMPT=$(cat <<EOF
Interpret the user's daily review and return ONLY a JSON object matching the
provided output schema.

Use AGENTS.md rules. Reason only from the prepared context and user report.
Do not access Lark, lark-cli, MCP, browser automation, connectors, or external
resources. Do not modify local files or Lark.

Rules:
- Set the proposal date to exactly $TODAY.
- Do not invent progress the user did not report.
- Treat explicit completion claims such as “完成”, “做完”, “finished”, or “done” as Completed; do not add unreported quality evidence.
- Judge each Daily Plan task against that task's original completion criterion.
  - Completed: its criterion was fully satisfied.
  - Partial: meaningful progress, but its criterion was not fully satisfied.
  - Blocked: a concrete blocker prevented progress.
  - Not done: no meaningful progress; do not put it in another status category,
    but carry it over when it still matters.
- Put each original Daily Plan task in at most one of completed, partial, or
  blocked. Never split the same underlying task across Completed and Partial,
  even if it has several subtopics.
- Daily task completion is separate from long-term skill readiness. Keep a task
  Completed when its criterion was met, even if later spaced reinforcement is useful.
- Preserve uncertainty when statements are ambiguous.
- New Gaps must be concrete weaknesses newly discovered in today's work. Do not
  restate known unfinished work or existing weaknesses as New Gaps; put those in
  Partial remaining work or Carry Over instead.
- Carry Over should contain actionable unfinished work and future reinforcement
  that still matters.
- Do not mark a skill ready from one exercise.
- Do not broadly downgrade a skill from one weak answer.
- Keep current_state_changes empty unless evidence materially changes readiness,
  a major bottleneck, priorities, or career strategy.

Prepared review context:

$REVIEW_CONTEXT

User's free-form review:

$USER_REPORT
EOF
)

rm -f "$PROPOSAL_FILE"
CODEX_EVENTS=$(mktemp /private/tmp/evening-review-events.XXXXXX)
trap 'rm -f "$CODEX_EVENTS"' EXIT
CODEX_STARTED=$(date +%s)

if ! print -r -- "$PROMPT" | "$CODEX_BIN" exec \
  --sandbox read-only \
  --ephemeral \
  --cd "$PROJECT_DIR" \
  --output-schema "$SCHEMA_FILE" \
  --output-last-message "$PROPOSAL_FILE" \
  --json \
  - > "$CODEX_EVENTS" 2>> "$LOG_FILE"; then
  log "Codex review failed; no Lark writes"
  print "Codex review failed. Nothing changed." >&2
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
        visit(json.loads(line))
    except json.JSONDecodeError:
        continue

if best is None:
    print("unknown")
else:
    total = best.get("total_tokens")
    if total is None and best.get("input_tokens") is not None and best.get("output_tokens") is not None:
        total = best["input_tokens"] + best["output_tokens"]
    print(total if total is not None else "unknown")
PY
)
log "Codex review succeeded runtime_seconds=$CODEX_ELAPSED tokens=$CODEX_TOKENS"

if [[ ! -s "$PROPOSAL_FILE" ]]; then
  log "Codex proposal was empty; no Lark writes"
  print "Codex returned an empty proposal. Nothing changed." >&2
  exit 1
fi

if VALIDATION_OUTPUT=$(python3 scripts/apply_review.py --validate-only "$PROPOSAL_FILE" 2>&1); then
  print -r -- "$VALIDATION_OUTPUT" >> "$LOG_FILE"
else
  print -r -- "$VALIDATION_OUTPUT" >&2
  log "structured review validation failed: ${VALIDATION_OUTPUT//$'\n'/ }"
  log "invalid structured review proposal; no Lark writes"
  print "Codex returned invalid structured review output. Nothing changed." >&2
  exit 1
fi

python3 scripts/apply_review.py --preview "$PROPOSAL_FILE"
print
print "Apply these changes? [y/N]"
read -r ANSWER
if [[ "$ANSWER" != "y" && "$ANSWER" != "Y" ]]; then
  log "review declined; no Lark writes"
  print "No changes applied."
  exit 0
fi

if ! APPLY_RESULT=$(python3 scripts/apply_review.py "$PROPOSAL_FILE" 2>&1); then
  print "$APPLY_RESULT" >&2
  log "review apply failed: ${APPLY_RESULT//$'\n'/ }"
  exit 1
fi
print "$APPLY_RESULT"
log "review apply succeeded: ${APPLY_RESULT//$'\n'/ }"
