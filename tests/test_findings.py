"""The findings: grading rules, intervals over questions, the recall comparison, and the driver on a tiny batch."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evalkit import findings


def _row(agent, task, level, verdict, repeat=1, source="submit"):
    correct = verdict in ("exact", "exact_unordered", "columns_reordered")
    return {
        "agent": agent,
        "task": task,
        "level": level,
        "verdict": verdict,
        "correct": int(correct),
        "sql_source": source,
        "repeat": repeat,
        "run_dir": f"results/b/{task}-{level}-{agent}-r{repeat}",
    }


def test_rules_widen_in_order_and_need_a_submitted_answer():
    extra, missing = _row("a", "q", "terse", "extra_columns"), _row("a", "q", "terse", "missing_columns")
    assert [findings.passes(extra, rule) for rule in findings.RULES] == [False, True, True]
    assert [findings.passes(missing, rule) for rule in findings.RULES] == [False, False, True]
    assert findings.passes(_row("a", "q", "terse", "exact"), "strict")
    assert not findings.passes(_row("a", "q", "terse", "missing_columns", source="last_query"), "as-asked")
    rows = [extra, missing, _row("a", "q", "terse", "harness_error")]  # a provider failure is not graded
    assert findings.tally(rows, "as-asked")[("a", "q", "terse")] == (2, 2)


def test_gap_interval_resamples_questions_and_shows_one_question_carrying_it():
    # TPC-H: agent a gets t1 right ten times and t2 wrong ten times; novel: wrong everywhere.
    rows = [_row("a", "t1", "terse", "exact", r) for r in range(10)]
    rows += [_row("a", "t2", "terse", "rows_differ", r) for r in range(10)]
    rows += [_row("a", q, "terse", "rows_differ", r) for q in ("n1", "n2") for r in range(10)]
    t = findings.tally(rows)
    g = findings.gaps(t, ["a"], {"tpch": ["t1", "t2"], "tpch-novel": ["n1", "n2"]}, "terse")["a"]
    assert (g["a"], g["b"], g["gap"]) == (0.5, 0.0, 0.5)
    assert g["ci"][0] == 0.0 and g["ci"][1] == 1.0  # a draw of only t2 gives 0, of only t1 gives 1
    assert g["loo"] == (0.0, 1.0)  # dropping t1 erases the gap: one question carries it


def test_spearman_handles_ties_and_reversal():
    assert findings._spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert abs(findings._spearman([1, 1, 2, 3], [1, 1, 2, 3]) - 1.0) < 1e-9
    assert findings._spearman([1, 2], [1, 2]) is None  # too few agents to rank


def test_recall_comparison_is_stratified_by_question():
    # q1 is easy and always recalled, q2 hard and never: pooled the gap is huge, within questions there is none.
    rows, audit = [], {}
    for r in range(10):
        for task, verdict, markers in (("q1", "exact", ["alias:x"]), ("q2", "rows_differ", [])):
            row = _row("a", task, "terse", verdict, r)
            rows.append(row)
            audit[row["run_dir"]] = {"set": "tpch", "recall_markers": markers}
    v = findings.recall_vs_correct(rows, audit)["a"]
    assert v["with"] == (10, 10) and v["without"] == (0, 10)
    assert v["stratified"] is None and v["questions_with_both"] == 0
    # Mixed within one question: recalled runs right, the others wrong.
    rows, audit = [], {}
    for r in range(4):
        row = _row("a", "q1", "terse", "exact" if r < 2 else "rows_differ", r)
        rows.append(row)
        audit[row["run_dir"]] = {"set": "tpch", "recall_markers": ["num:24"] if r < 2 else []}
    assert findings.recall_vs_correct(rows, audit)["a"]["stratified"] == 1.0


def test_repeats_and_failures():
    rows = [_row("a", "q", "terse", v, r) for r, v in enumerate(["exact", "rows_differ", "exact"])]
    rows += [_row("a", "p", "terse", "exact", r) for r in range(3)]
    rows.append(_row("a", "p", "spec", "values_differ", source="last_query"))
    t = findings.tally(rows)
    rep = findings.repeat_agreement(t, {"q": "tpch", "p": "tpch"})
    assert rep[("tpch", "terse")] == {"split": 1, "all right": 1}
    assert ("tpch", "spec") not in rep  # a single run cannot split
    fail = findings.failure_modes(rows, {"q": "tpch", "p": "tpch"})
    assert fail[("tpch", "terse")] == {"rows_differ": 1}
    assert fail[("tpch", "spec")] == {"values_differ": 1, "(never submitted)": 1}


def test_run_findings_writes_deterministic_files(tmp_path):
    qdir = tmp_path / "questions"
    for s, ids in (("tpch", ["q1", "q2"]), ("tpch-novel", ["n1", "n2"])):
        (qdir / s).mkdir(parents=True)
        for i in ids:
            (qdir / s / f"{i}.toml").write_text(f'id = "{i}"\n')
    results = tmp_path / "results"
    for agent in ("pi:a", "pi:b", "pi:c"):
        for task in ("q1", "q2", "n1", "n2"):
            for level in ("terse", "spec"):
                for r in (1, 2):
                    verdict = "exact" if task.startswith("q") or level == "spec" else "missing_columns"
                    row = _row(agent, task, level, verdict, r)
                    d = results / "b" / Path(row["run_dir"]).name
                    d.mkdir(parents=True)
                    (d / "result.json").write_text(json.dumps(row))
                    (d / "prompt.md").write_text("preamble\n\nHow much?")
    audit = tmp_path / "runs.jsonl"
    audit.write_text("")
    out = tmp_path / "out"
    f = findings.run_findings(["b"], out, audit_runs=audit, results=results, questions=qdir, root=tmp_path)
    assert f["partial"] == {}
    strict, asked = (f["terse_rules"]["tpch-novel"][r]["gaps"]["pi:a"] for r in ("strict", "as-asked"))
    assert strict["gap"] == 1.0 and asked["gap"] == 0.0  # the whole gap is left-out columns
    md = (out / "findings.md").read_text()
    assert "## 2. TPC-H minus novel" in md and "### tpch-novel, as-asked" in md
    assert "n1 - pi:a" in (out / "terse-misses.md").read_text()
    before = {p.name: p.read_text() for p in out.iterdir()}
    findings.run_findings(["b"], out, audit_runs=audit, results=results, questions=qdir, root=tmp_path)
    assert {p.name: p.read_text() for p in out.iterdir()} == before
