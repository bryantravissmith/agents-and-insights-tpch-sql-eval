#!/usr/bin/env bash
# Grid v3, 2026-11-01: the frozen v3 questions (questions/README.md, "Version 3"), every agent but Gemini.
#
# Protocol: agents declare their answer columns (--declare-columns) and may call ask_clarification, which never
# answers (--allow-ask). Same agents and settings as the 2026-10-31 grid (full-grid-2026-10-31.sh): medium
# reasoning, 10 repeats, 600 s per run, 40 turns. 34 questions x 3 levels x 10 repeats:
#   OpenAI 7 agents, 7,140 runs; Claude Code 4 agents, 4,080 runs; Grok 2 agents, 2,040 runs.
# Plus the --ask-step batch: OpenAI's 7 agents, where an ask reveals the next brief, at terse and stakeholder only
# (at spec there is no next brief, so it would repeat the main grid): 4,760 runs. It shares OpenAI's rate limits,
# so it starts when the main OpenAI batch ends.
# Gemini follows as its own batch once its weekly quota resets (Nov 7), with the same frozen files.
#
#   bash experiments/grid-v3-2026-11-01.sh            # starts the three lanes in the background
#   bash experiments/grid-v3-2026-11-01.sh openai     # one lane in the foreground (openai, claude-code, xai)
#   DRY_RUN=1 bash experiments/grid-v3-2026-11-01.sh  # prints every plan, runs nothing
#   uv run evals.py progress                           # while they run
set -euo pipefail
cd "$(dirname "$0")/.."

V3=(questions/tpch-v3 questions/tpch-novel-v3 questions/tpch-hard-v3)

batch() {  # name, jobs, agents file, extra flags...
  local name=$1 jobs=$2 agents=$3
  shift 3
  uv run evals.py run "${V3[@]}" --agents-json "$agents" -j "$jobs" --name "$name" -r 10 --timeout 600 \
    --declare-columns ${DRY_RUN:+--dry-run} "$@"
}

lane() {
  case $1 in
    openai)
      batch grid-v3-openai 16 experiments/full-grid-agents-openai.json --allow-ask
      batch grid-v3-openai-ask-step 16 experiments/full-grid-agents-openai.json --ask-step -l terse stakeholder
      ;;
    claude-code) batch grid-v3-claude-code 6 experiments/full-grid-agents-claude-code.json --allow-ask ;;
    xai) batch grid-v3-xai 8 experiments/full-grid-agents-xai.json --allow-ask ;;
    *) echo "unknown lane $1" >&2; exit 2 ;;
  esac
}

if [[ $# -gt 0 ]]; then
  lane "$1"
elif [[ -n "${DRY_RUN:-}" ]]; then
  for l in openai claude-code xai; do lane "$l"; done
else
  for l in openai claude-code xai; do
    nohup caffeinate -i bash "$0" "$l" > "results/grid-v3-$l.log" 2>&1 &
    echo "started lane $l (log: results/grid-v3-$l.log)"
  done
fi
