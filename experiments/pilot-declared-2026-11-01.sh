#!/usr/bin/env bash
# Pilot for declared answer columns (--declare-columns), 2026-11-01.
#
# Do agents name the right answer columns, and does grading by them change the result? Terse briefs only,
# on the questions where the saved-run check (regrade --declare-all) showed the declaration matters: the novel
# and hard v2 sets, the "which" TPC-H questions (q02, q10, q15, q21) and two with labels (q04, q09).
# One agent per maker with quota (Gemini is out until its reset), the grid's settings, 3 repeats: 162 runs.
# Compare with the same agents' terse runs in the 2026-10-31 grid, whose prompts lack only the declare sentence.
#
#   bash experiments/pilot-declared-2026-11-01.sh            # runs the pilot (spends: OpenAI and xAI APIs)
#   DRY_RUN=1 bash experiments/pilot-declared-2026-11-01.sh  # prints the plan only
set -euo pipefail
cd "$(dirname "$0")/.."

QUESTIONS=(
  questions/tpch-novel-v2
  questions/tpch-hard-v2
  questions/tpch/q02.toml questions/tpch/q10.toml questions/tpch/q15.toml questions/tpch/q21.toml
  questions/tpch/q04.toml questions/tpch/q09.toml
)
uv run evals.py run "${QUESTIONS[@]}" -l terse --agents-json experiments/pilot-declared-agents.json \
  -r 3 -j 6 --timeout 600 --name pilot-declared --declare-columns ${DRY_RUN:+--dry-run}
