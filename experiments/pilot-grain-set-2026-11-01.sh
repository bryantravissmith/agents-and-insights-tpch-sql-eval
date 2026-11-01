#!/usr/bin/env bash
# Pilot of the grain question set (questions/tpch-grain), 2026-11-01.
#
# How often is a miss a grain mistake? Each question is built so the tempting first query has the wrong grain,
# and --declare-grain makes the agent name its answer columns and its grain, so a grain error gets its own
# verdict. All three levels; Sonnet 5.5, GPT-6.1 Sol and Grok 4.7 (Gemini is out of quota until its reset);
# 3 repeats: 162 runs.
#
#   bash experiments/pilot-grain-set-2026-11-01.sh            # runs the pilot (spends: OpenAI and xAI APIs)
#   DRY_RUN=1 bash experiments/pilot-grain-set-2026-11-01.sh  # prints the plan only
#   uv run --with duckdb python experiments/compare_declared.py <batch id>   # afterwards
set -euo pipefail
cd "$(dirname "$0")/.."

uv run evals.py run questions/tpch-grain --agents-json experiments/pilot-declared-agents.json \
  -r 3 -j 6 --timeout 600 --name pilot-grain-set --declare-grain ${DRY_RUN:+--dry-run}
