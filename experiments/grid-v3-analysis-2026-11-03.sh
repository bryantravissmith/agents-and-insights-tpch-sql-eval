#!/usr/bin/env bash
# Analysis of grid v3 (experiments/grid-v3-2026-11-01.sh): the frozen v3 questions, --declare-columns, --allow-ask.
# Re-runs nothing; it reads the batches under results/ and writes everything to results/analysis-v3-2026-11-03/.
# Gemini is left out (decided 2026-11-04): Flash's quota allowed 187 of its 1,020 runs in a week
# (20261101-172317-grid-v3-agy-flash, stopped) and Argon never ran. The --ask-step batch is analyzed on its own:
# its runs could see more of the brief.
#
#   bash experiments/grid-v3-analysis-2026-11-03.sh
set -euo pipefail
cd "$(dirname "$0")/.."

BATCHES=(
  20261101-161536-grid-v3-openai       # pi, OpenAI models
  20261101-161536-grid-v3-claude-code  # Claude Code on a subscription, Claude models
  20261101-161536-grid-v3-xai          # pi, Grok models
)
ASK_STEP=20261101-210805-grid-v3-openai-ask-step
OUT=results/analysis-v3-2026-11-03
mkdir -p "$OUT/ask-step"

# Accuracy and efficiency, with every cost labeled by its basis.
uv run evals.py report "${BATCHES[@]}" --csv "$OUT/report.csv" > "$OUT/report.md"

# Cheating attempts and signs of prior exposure, with the evidence for every flagged run.
uv run evals.py audit "${BATCHES[@]}" --out "$OUT/audit"

# How each model family writes SQL and how often DuckDB rejects it, sliced by question set.
uv run evals.py style "${BATCHES[@]}" --out "$OUT/style"

# The claims, with intervals over questions; reads the audit's runs.jsonl, so it runs after the audit.
uv run evals.py findings "${BATCHES[@]}" --out "$OUT/findings" --audit "$OUT/audit"

# The --ask-step batch: OpenAI's agents, where an ask reveals the next brief, at terse and stakeholder only.
uv run evals.py report "$ASK_STEP" --csv "$OUT/ask-step/report.csv" > "$OUT/ask-step/report.md"
uv run evals.py audit "$ASK_STEP" --out "$OUT/ask-step/audit"
# Against the main OpenAI batch: how often each agent asks, and what an answer to the ask is worth.
uv run experiments/compare_ask_step.py "${BATCHES[0]}" "$ASK_STEP" > "$OUT/ask-step/compare.md"

echo "wrote $OUT/report.md, $OUT/report.csv, $OUT/audit/, $OUT/style/, $OUT/findings/ and $OUT/ask-step/"
