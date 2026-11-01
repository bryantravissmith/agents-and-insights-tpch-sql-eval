"""Compare a declared-columns batch with the same agents' terse runs in the 2026-10-31 grid, and grade the batch's
own runs both ways.

    uv run --with duckdb python experiments/compare_declared.py <batch id>

The grid's runs have no declaration, so they are compared as graded today: the original batches for TPC-H
questions, and the v2 re-grades (regrade-novel-v2, regrade-hard-v2) for v2 question ids. The batch's prompts
differ from the grid's by the declare sentence, so that comparison mixes behavior and grading. The second table
isolates grading: the batch's own submitted SQL graded by its declared columns and by its whole result (today's
rule, `grading.grade_any`). The third says how agents used the declaration.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from evalkit import grading, load_questions
from evalkit.declared import grade_task, read_declaration

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
    tasks = {(t.id, t.level): t for t in load_questions(HERE / "questions")}
    grid = defaultdict(lambda: [0, 0])
    for b in GRID:
        for r in rows(b):
            if r["level"] == "terse" and (r["agent"], r["task"]) in keys and r["verdict"] != "harness_error":
                grid[(r["agent"], r["task"])][0] += int(r["correct"])
                grid[(r["agent"], r["task"])][1] += 1
    pilot = defaultdict(lambda: [0, 0, 0])  # correct by declared columns, by whole result, runs
    use = defaultdict(Counter)
    for r in runs:
        k = (r["agent"], r["task"])
        run_dir = HERE / r["run_dir"]
        task = tasks[(r["task"], r["level"])]
        sql = (run_dir / "answer.sql").read_text() if (run_dir / "answer.sql").exists() else None
        whole = grading.grade_any(sql, task.target_sql, task.accepted_sql, task.db)[0]
        # Graded again with the current markings, not as stored at run time.
        declared = grade_task(sql, read_declaration(run_dir), task, task.db)[0]
        pilot[k][0] += int(declared.correct and r["sql_source"] == "submit")
        pilot[k][1] += int(whole.correct and r["sql_source"] == "submit")
        pilot[k][2] += 1
        d = r.get("declaration") or {}
        use[r["agent"]]["runs"] += 1
        use[r["agent"]]["declared"] += (run_dir / "answer_columns.json").exists()
        use[r["agent"]]["graded by declared columns"] += d.get("graded_by") == "declared columns"
        use[r["agent"]]["fell back (labels did not pair)"] += str(d.get("graded_by", "")).startswith("whole")
        use[r["agent"]]["bad declaration"] += r["verdict"] == "bad_declaration"
        labels = {c.lower() for c in (d.get("labels") or {}).values()}
        use[r["agent"]]["declared a label too"] += bool(labels & {c.lower() for c in d.get("declared", [])})
    print(f"# Declared-columns batch {batch}\n")
    print("Correct runs. grid: the same agents' terse runs on 2026-10-31 (prompt without the declare sentence).")
    print("declared / whole result: this batch's runs graded by their declared columns / by today's rule.\n")
    print("| agent | question | grid | declared | whole result |")
    print("|---|---|---|---|---|")
    tot = defaultdict(lambda: [0, 0, 0, 0, 0])
    for a, q in sorted(keys):
        g, p = grid[(a, q)], pilot[(a, q)]
        print(f"| {a} | {q} | {g[0]}/{g[1]} | {p[0]}/{p[2]} | {p[1]}/{p[2]} |")
        t = tot[a]
        for i, v in enumerate((g[0], g[1], p[0], p[1], p[2])):
            t[i] += v
    print("\n| agent | grid | declared | whole result |")
    print("|---|---|---|---|")
    for a, (g0, g1, d0, w0, n) in sorted(tot.items()):
        print(
            f"| {a} | {g0}/{g1} ({100 * g0 / g1:.0f}%) | {d0}/{n} ({100 * d0 / n:.0f}%) | {w0}/{n} ({100 * w0 / n:.0f}%) |"
        )
    cols = list(next(iter(use.values())).keys())
    print("\n| agent | " + " | ".join(cols) + " |")
    print("|---" * (len(cols) + 1) + "|")
    for a, c in sorted(use.items()):
        print(f"| {a} | " + " | ".join(str(c[k]) for k in cols) + " |")


if __name__ == "__main__":
    main(sys.argv[1])
