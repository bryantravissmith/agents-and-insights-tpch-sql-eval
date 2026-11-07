#!/usr/bin/env bash
# Does naming the SQL dialect make agents more efficient? 2026-11-07.
#
# The dialect condition (conditions/dialect/AGENTS.md) adds one sentence, "The database engine is DuckDB 2.0.
# Write SQL in DuckDB's dialect.", and names no functions: naming quantile_cont would give away h04. pi puts a
# condition in the system prompt, so the question prompts and their hashes are the same as v3's.
#
# Where v3 shows dialect friction: the hard questions h02, h03, h04 and h06 (h04 is the only question where agents
# search the function catalog), and Grok 4.7 (23% of those runs had a query DuckDB rejected) and GPT-5.6 Luna (16%).
# GPT-6.1 Sol had none and is the control that should not move. Spec level only, so the definitions are fixed.
# bare and dialect run together, so drift at the providers since grid v3 is not read as an effect.
# 3 agents x 4 questions x 2 conditions x 10 repeats = 240 runs (20261107-070635-dialect): no measurable change.
#
# The terse lane: split by level, v3's friction on these questions is at terse (Grok 4.7: 42% of terse runs had a
# rejected query, 18% at spec; GPT-5.6 Luna: 22% and 2%), so the spec lane had little to remove. Sol, which had
# none at any level, is left out. 2 agents x 4 questions x 2 conditions x 10 repeats = 160 runs
# (20261107-073022-dialect-terse, resumed once; 14 Grok runs still failed on xAI capacity). Luna: no change. Grok
# 4.7: the same accuracy with more queries (18.1 to 23.0 per run) and cost, mostly on h04, about 8 runs per cell.
#
# What both lanes test is narrower than planned: run_sql's description already says "the orders database
# (DuckDB)" in every harness, so the condition names the engine a second time. Both lanes ran before pi's tools
# accepted a statement that starts with a comment (106e42d); that cost Grok retries in both conditions alike.
#
#   bash experiments/dialect-2026-11-07.sh          # spec lane
#   bash experiments/dialect-2026-11-07.sh terse    # terse lane
set -euo pipefail
cd "$(dirname "$0")/.."

HARD=(questions/tpch-hard-v3/h02.toml questions/tpch-hard-v3/h03.toml questions/tpch-hard-v3/h04.toml
      questions/tpch-hard-v3/h06.toml)

case ${1:-spec} in
  spec) LEVEL=spec AGENTS=experiments/dialect-agents.json ;;
  terse) LEVEL=terse AGENTS=experiments/dialect-terse-agents.json ;;
  *) echo "unknown lane $1" >&2; exit 2 ;;
esac
shift $(($# > 0 ? 1 : 0))

# 8 parallel: OpenAI drew 429s at 16 on 2026-11-01.
uv run evals.py run "${HARD[@]}" -l "$LEVEL" -c bare dialect --agents-json "$AGENTS" \
  -r 10 -j 8 --timeout 600 --declare-columns --allow-ask --name "dialect-$LEVEL" "$@"

# Then, with the batch id it prints:
#   uv run evals.py report <batch> > results/<batch>/report.md
#   uv run evals.py style <batch> --out results/<batch>/style
#   uv run evals.py audit <batch> --out results/<batch>/audit
