#!/usr/bin/env bash
# Control for the grain set (questions/tpch-grain), 2026-11-01.
#
# The grain pilot (pilot-grain-set-2026-11-01.sh) had 162 of 162 correct with three strong agents under
# --declare-grain, so the set separated nobody. Two explanations: current models don't fall for these traps when
# the question is clear, or the declare instruction makes them think about grain. This runs the three weakest
# agents twice: bare (no declare sentence) and under the v3 protocol (--declare-columns --allow-ask). If they
# fall for the traps in either, the set measures something; if not, it is hardened or dropped.
# 3 agents x 6 questions x 3 levels x 3 repeats x 2 modes = 324 runs.
#
#   bash experiments/pilot-grain-control-2026-11-01.sh            # runs it (spends: OpenAI API; Claude subscription)
#   DRY_RUN=1 bash experiments/pilot-grain-control-2026-11-01.sh  # prints the plans only
#   uv run --with duckdb python experiments/analyze_grain_set.py <batch id>   # per batch, afterwards
set -euo pipefail
cd "$(dirname "$0")/.."

COMMON=(questions/tpch-grain --agents-json experiments/pilot-grain-control-agents.json -r 3 -j 6 --timeout 600)
uv run evals.py run "${COMMON[@]}" --name pilot-grain-control-bare ${DRY_RUN:+--dry-run}
uv run evals.py run "${COMMON[@]}" --name pilot-grain-control-v3 --declare-columns --allow-ask ${DRY_RUN:+--dry-run}
