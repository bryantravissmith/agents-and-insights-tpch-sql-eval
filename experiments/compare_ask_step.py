"""--ask-step against --allow-ask: the same agents, questions and prompts; only the answer to an ask differs.

With --allow-ask, ask_clarification always says no clarification is available. With --ask-step, an ask at terse
reveals the stakeholder brief and an ask at stakeholder reveals the spec brief. Spec runs are left out (the ask-step
batch has none). Prints markdown: by agent and level, then terse per question.

  uv run experiments/compare_ask_step.py <allow-ask batch> <ask-step batch>
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

RESULTS = Path(__file__).resolve().parents[1] / "results"
CORRECT = {"exact", "exact_unordered", "extra_columns"}


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def main(allow_ask: str, ask_step: str) -> None:
    batches = {"allow-ask": allow_ask, "ask-step": ask_step}
    cells = defaultdict(lambda: defaultdict(int))
    per_q = defaultdict(lambda: defaultdict(int))
    for name, batch in batches.items():
        for p in sorted((RESULTS / batch).glob("*/result.json")):
            r = json.loads(p.read_text())
            if r["verdict"] == "harness_error" or r["level"] == "spec":
                continue
            asked, ok = (r.get("asks") or 0) > 0, r["verdict"] in CORRECT
            for agent in (r["agent"], "all agents"):
                c = cells[(agent, r["level"], name)]
                c["n"] += 1
                c["ok"] += ok
                c["asked"] += asked
                c["asked_ok"] += asked and ok
                c["revealed"] += bool(r.get("revealed"))
            if r["level"] == "terse":
                q = per_q[(r["task"], name)]
                q["n"] += 1
                q["ok"] += ok
                q["asked"] += asked

    print(f"# --ask-step vs --allow-ask\n\nBatches: {allow_ask} (allow-ask), {ask_step} (ask-step).\n")
    print("## By agent and level\n")
    print("| agent | level | batch | runs | correct | asked | correct when asked | correct when not | revealed |")
    print("|---|---|---|---|---|---|---|---|---|")
    for k in sorted(cells, key=lambda k: (k[0] == "all agents", k)):
        c = cells[k]
        print(
            f"| {k[0]} | {k[1]} | {k[2]} | {c['n']} | {pct(c['ok'], c['n'])} | {pct(c['asked'], c['n'])} "
            f"| {pct(c['asked_ok'], c['asked'])} | {pct(c['ok'] - c['asked_ok'], c['n'] - c['asked'])} "
            f"| {c['revealed']} |"
        )
    print("\n## Terse, per question (all agents)\n")
    print("| question | correct, allow-ask | correct, ask-step | asked, allow-ask | asked, ask-step |")
    print("|---|---|---|---|---|")
    for t in sorted({t for t, _ in per_q}):
        a, s = per_q[(t, "allow-ask")], per_q[(t, "ask-step")]
        print(
            f"| {t} | {pct(a['ok'], a['n'])} | {pct(s['ok'], s['n'])} "
            f"| {pct(a['asked'], a['n'])} | {pct(s['asked'], s['n'])} |"
        )


if __name__ == "__main__":
    main(*sys.argv[1:3])
