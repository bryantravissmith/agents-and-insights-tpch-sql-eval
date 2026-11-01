#!/usr/bin/env bash
# Analysis of the 2026-10-31 full grid: 34 questions (22 TPC-H, 6 novel, 6 hard) x 3 brief levels x 10 repeats.
# Re-runs nothing; it reads the batches under results/ and writes everything to results/analysis-2026-10-31/.
# Each batch was started with the matching experiments/full-grid-agents-*.json; manifest.json in each batch
# records the exact command, git commit, CLI versions and every resume and reparse since.
#
#   bash experiments/full-grid-2026-10-31.sh
set -euo pipefail
cd "$(dirname "$0")/.."

BATCHES=(
  20261031-102010-full-grid-openai       # pi, OpenAI models
  20261031-121139-full-grid-claude-code  # Claude Code on a subscription, Claude models
  20261031-102014-full-grid-agy          # Antigravity, Gemini models (quota-limited; resumed in passes)
  20261031-102013-full-grid-xai          # pi, Grok models
)
OUT=results/analysis-2026-10-31
mkdir -p "$OUT"

# Usage and cost fields come from each run's saved transcript with the current parsers (verdicts untouched).
for b in "${BATCHES[@]}"; do uv run evals.py reparse "$b"; done

# Accuracy and efficiency, with every cost labeled by its basis.
uv run evals.py report "${BATCHES[@]}" --csv "$OUT/report.csv" > "$OUT/report.md"

# Cheating attempts and signs of prior exposure, with the evidence for every flagged run.
uv run evals.py audit "${BATCHES[@]}" --out "$OUT/audit"

# How each model family writes SQL and how often DuckDB rejects it, sliced by question set.
uv run evals.py style "${BATCHES[@]}" --out "$OUT/style"

# The same novel terse answers graded by references that return only what each brief asks (questions/
# tpch-novel-v2, identical prompts). No agent runs; rebuilt from the saved runs every time.
uv run evals.py regrade "${BATCHES[@]}" --questions questions/tpch-novel-v2 --name novel-v2

# The claims, with intervals over questions; reads the audit's runs.jsonl, so it runs after the audit.
# The re-grade is a second grade of runs already counted, so it is left out of report and audit.
uv run evals.py findings "${BATCHES[@]}" regrade-novel-v2 --out "$OUT/findings" --audit "$OUT/audit"

echo "wrote $OUT/report.md, $OUT/report.csv, $OUT/audit/{summary.md,flagged.md,runs.jsonl,markers.json}"
echo "  and $OUT/findings/{findings.md,findings.json,terse-misses.md}, $OUT/style/{summary.md,runs.jsonl}"
