#!/bin/zsh

cd ~/job-search-os || exit 1

codex exec \
  --sandbox workspace-write \
  "Run the Morning Plan workflow defined in AGENTS.md.
   Read the latest Current State and relevant Lark pages first.
   Generate today's realistic preparation plan.
   Maximum 5 tasks, maximum 2 P0 tasks.
   Always include a Minimum Viable Day.
   Update today's Daily Plan in Lark.
   Do not change career strategy."