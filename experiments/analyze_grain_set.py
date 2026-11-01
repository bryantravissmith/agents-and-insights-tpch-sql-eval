"""How often is a miss on the grain questions a grain mistake? For a batch run on questions/tpch-grain.

    uv run --with duckdb python experiments/analyze_grain_set.py <batch id>

Every run is graded again with the current grader and markings (by its declaration when it made one). Each miss
is put in one class, in this order:
- designed trap: the submitted query's result equals the question's tempting_sql (the wrong grain it was built for);
- grain error: grain_not_unique or grain_differs (the agent's own declared grain is repeated, or is another grain);
- same grain, other rows: rows_differ with the declared grain matching the reference's;
- other wrong value: any other wrong answer (a different definition, a filter, arithmetic);
- no answer: nothing submitted, or the SQL failed.
"""

import json
import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from evalkit import grading, load_questions
from evalkit.declared import grade_task, read_declaration

CLASSES = ["designed trap", "grain error", "same grain, other rows", "other wrong value", "no answer"]
LEVELS = ["terse", "stakeholder", "spec"]


def classify(r: dict, run_dir: Path, task, trap_sql: str) -> tuple[bool, str, dict]:
    sql = (run_dir / "answer.sql").read_text() if (run_dir / "answer.sql").exists() else None
    if sql is None or r.get("sql_source") != "submit":
        return False, "no answer", {}
    g, _, notes = grade_task(sql, read_declaration(run_dir), task, task.db)
    if g.correct:
        return True, "", notes
    if g.verdict in ("no_sql", "sql_error", "bad_declaration"):
        return False, "no answer", notes
    # The trap alone: the question's accepted_sql are forms of the right answer, never of the trap.
    if grading.grade_any(sql, trap_sql, (), task.db)[0].correct:
        return False, "designed trap", notes
    if g.verdict in ("grain_not_unique", "grain_differs"):
        return False, "grain error", notes
    if notes.get("grain_matches"):
        return False, "same grain, other rows", notes
    return False, "other wrong value", notes


def main(batch: str) -> None:
    tasks = {(t.id, t.level): t for t in load_questions(HERE / "questions" / "tpch-grain")}
    traps = {
        p.stem: tomllib.loads(p.read_text())["tempting_sql"] for p in (HERE / "questions" / "tpch-grain").glob("*.toml")
    }
    cells = defaultdict(lambda: [0, 0])  # (task, level) -> correct, graded
    by_agent = defaultdict(lambda: [0, 0])  # (agent, level)
    misses = defaultdict(Counter)  # task -> class counts
    miss_level = defaultdict(Counter)  # level -> class counts
    grains = Counter()
    for p in sorted((HERE / "results" / batch).glob("*/result.json")):
        r = json.loads(p.read_text())
        if r["verdict"] == "harness_error":
            continue
        task = tasks[(r["task"], r["level"])]
        ok, cls, _notes = classify(r, p.parent, task, traps[r["task"]])
        for k in ((r["task"], r["level"]),):
            cells[k][0] += ok
            cells[k][1] += 1
        by_agent[(r["agent"], r["level"])][0] += ok
        by_agent[(r["agent"], r["level"])][1] += 1
        if not ok:
            misses[r["task"]][cls] += 1
            miss_level[r["level"]][cls] += 1
        decl = read_declaration(p.parent)
        grains["declared a grain"] += bool(decl and decl.grain is not None)
        grains["runs"] += 1
    qids = sorted({q for q, _ in cells})
    print(f"# Grain question set, batch {batch}\n")
    print(f"{grains['declared a grain']} of {grains['runs']} runs declared a grain.\n")
    print("## Correct by question and level\n")
    print("| question | " + " | ".join(LEVELS) + " |")
    print("|---" * (len(LEVELS) + 1) + "|")
    for q in qids:
        print(f"| {q} | " + " | ".join(f"{cells[(q, lv)][0]}/{cells[(q, lv)][1]}" for lv in LEVELS) + " |")
    print("\n## Correct by agent and level\n")
    print("| agent | " + " | ".join(LEVELS) + " |")
    print("|---" * (len(LEVELS) + 1) + "|")
    for a in sorted({a for a, _ in by_agent}):
        print(f"| {a} | " + " | ".join(f"{by_agent[(a, lv)][0]}/{by_agent[(a, lv)][1]}" for lv in LEVELS) + " |")
    print("\n## What the misses were\n")
    print("| question | " + " | ".join(CLASSES) + " |")
    print("|---" * (len(CLASSES) + 1) + "|")
    for q in qids:
        print(f"| {q} | " + " | ".join(str(misses[q][c]) for c in CLASSES) + " |")
    print("\n| level | " + " | ".join(CLASSES) + " |")
    print("|---" * (len(CLASSES) + 1) + "|")
    for lv in LEVELS:
        print(f"| {lv} | " + " | ".join(str(miss_level[lv][c]) for c in CLASSES) + " |")


if __name__ == "__main__":
    main(sys.argv[1])
