#!/bin/zsh

set -euo pipefail

PROJECT_DIR="/Users/xiangyu/job-search-os"

cd "$PROJECT_DIR"

PLANNER_CONTEXT=$(python3 scripts/build_planner_context.py)

PROMPT=$(cat <<EOF
Generate a DEBUG Morning Plan using only the prepared context below.

Do not access Lark, lark-cli, the browser, MCP, connectors, or any external
resource. Do not modify local files or Lark. Only print the proposed daily plan.

Planning requirements:
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

codex exec \
  --sandbox read-only \
  --cd "$PROJECT_DIR" \
  "$PROMPT"
