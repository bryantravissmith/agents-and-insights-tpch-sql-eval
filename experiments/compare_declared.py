"""Compare a declared-columns batch with the same agents' terse runs in the 2026-10-31 grid.

    uv run --with duckdb python experiments/compare_declared.py <pilot batch id>

The grid's runs have no declaration, so they are compared as graded today: the original batches for TPC-H
questions, and the v2 re-grades (regrade-novel-v2, regrade-hard-v2) for v2 question ids. Prints, per agent and
question: correct in the grid, correct in the pilot, and how the pilot's declarations were used (declared at all,
graded by them or fallen back, labels declared too, bad names).
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
GRID = [
    "20261031-102010-full-grid-openai",
    "20261031-121139-full-grid-claude-code",
    "20261031-102013-full-grid-xai",
    "regrade-novel-v2",
    "regrade-hard-v2",
]


def rows(batch):
    return [json.loads(p.read_text()) for p in sorted((HERE / "results" / batch).glob("*/result.json"))]


def main(batch: str) -> None:
    runs = [r for r in rows(batch) if r["verdict"] != "harness_error"]
    keys = {(r["agent"], r["task"]) for r in runs}
    grid = defaultdict(lambda: [0, 0])
    for b in GRID:
        for r in rows(b):
            if r["level"] == "terse" and (r["agent"], r["task"]) in keys and r["verdict"] != "harness_error":
                grid[(r["agent"], r["task"])][0] += int(r["correct"])
                grid[(r["agent"], r["task"])][1] += 1
    pilot = defaultdict(lambda: [0, 0])
    use = defaultdict(Counter)
    for r in runs:
        k = (r["agent"], r["task"])
        pilot[k][0] += int(r["correct"])
        pilot[k][1] += 1
        d = r.get("declaration") or {}
        run_dir = HERE / r["run_dir"]
        declared = (run_dir / "answer_columns.json").exists()
        use[r["agent"]]["runs"] += 1
        use[r["agent"]]["declared"] += declared
        use[r["agent"]]["graded by declared columns"] += d.get("graded_by") == "declared columns"
        use[r["agent"]]["fell back (labels did not pair)"] += str(d.get("graded_by", "")).startswith("whole")
        use[r["agent"]]["bad declaration"] += r["verdict"] == "bad_declaration"
        labels = {c.lower() for c in (d.get("labels") or {}).values()}
        use[r["agent"]]["declared a label too"] += bool(labels & {c.lower() for c in d.get("declared", [])})
        use[r["agent"]]["correct with extra declared"] += r["verdict"] == "extra_columns" and bool(r["correct"])
    print(f"# Declared-columns batch {batch}\n")
    print("| agent | question | grid correct | pilot correct |")
    print("|---|---|---|---|")
    tot = defaultdict(lambda: [0, 0, 0, 0])
    for a, q in sorted(keys):
        g, p = grid[(a, q)], pilot[(a, q)]
        print(f"| {a} | {q} | {g[0]}/{g[1]} | {p[0]}/{p[1]} |")
        t = tot[a]
        t[0] += g[0]
        t[1] += g[1]
        t[2] += p[0]
        t[3] += p[1]
    print("\n| agent | grid correct | pilot correct |")
    print("|---|---|---|")
    for a, (g0, g1, p0, p1) in sorted(tot.items()):
        print(f"| {a} | {g0}/{g1} ({100 * g0 / g1:.0f}%) | {p0}/{p1} ({100 * p0 / p1:.0f}%) |")
    print("\n| agent | " + " | ".join(next(iter(use.values())).keys()) + " |")
    print("|---" * (len(next(iter(use.values()))) + 1) + "|")
    for a, c in sorted(use.items()):
        print(f"| {a} | " + " | ".join(str(v) for v in c.values()) + " |")


if __name__ == "__main__":
    main(sys.argv[1])
