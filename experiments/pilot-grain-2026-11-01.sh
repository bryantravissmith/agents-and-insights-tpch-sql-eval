#!/usr/bin/env bash
# Pilot for declared grain (--declare-grain: answer columns plus the columns that identify a row), 2026-11-01.
#
# Do agents name the right grain, and how often is a miss a grain error? Same questions, agents and repeats as
# experiments/pilot-declared-2026-11-01.sh, so the two compare directly: terse briefs of the novel and hard v2
# sets, q02, q10, q15, q21 (answers are identities) and q04, q09 (answers with labels); Sonnet 5.5, GPT-6.1 Sol and
# Grok 4.7 (Gemini is out of quota until its reset); 3 repeats: 162 runs.
#
#   bash experiments/pilot-grain-2026-11-01.sh            # runs the pilot (spends: OpenAI and xAI APIs)
#   DRY_RUN=1 bash experiments/pilot-grain-2026-11-01.sh  # prints the plan only
#   uv run --with duckdb python experiments/compare_declared.py <batch id>   # afterwards
set -euo pipefail
cd "$(dirname "$0")/.."

QUESTIONS=(
  questions/tpch-novel-v2
  questions/tpch-hard-v2
  questions/tpch/q02.toml questions/tpch/q10.toml questions/tpch/q15.toml questions/tpch/q21.toml
  questions/tpch/q04.toml questions/tpch/q09.toml
)
uv run evals.py run "${QUESTIONS[@]}" -l terse --agents-json experiments/pilot-declared-agents.json \
  -r 3 -j 6 --timeout 600 --name pilot-grain --declare-grain ${DRY_RUN:+--dry-run}
