"""Accepted alternatives, question versions, and re-grading saved runs against a new version."""

import json
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evalkit import grading, load_question, load_questions, regrade

HERE = Path(__file__).resolve().parent.parent

STRING_MONTH = "SELECT '1997-01' AS month, 5 AS orders"
DATE_MONTH = "SELECT DATE '1997-01-01' AS month, 5 AS orders"


def test_grade_any_tries_alternatives_and_says_which_matched(synthetic_db):
    got, ref = grading.grade_any("SELECT DATE '1997-01-01', 5", STRING_MONTH, (DATE_MONTH,), synthetic_db)
    assert (got.verdict, ref) == ("exact", 1)
    # Correct beats lenient, and the target is tried first.
    assert grading.grade_any("SELECT '1997-01', 5", STRING_MONTH, (DATE_MONTH,), synthetic_db)[1] == 0
    got, ref = grading.grade_any("SELECT DATE '1997-01-01', 5, 9", STRING_MONTH, (DATE_MONTH,), synthetic_db)
    assert (got.verdict, ref) == ("extra_columns", 1)
    # With no match, the miss is diagnosed against the target.
    got, ref = grading.grade_any("SELECT '1997-01', 6", STRING_MONTH, (DATE_MONTH,), synthetic_db)
    assert (got.verdict, ref) == ("values_differ", 0)
    assert grading.grade_any("SELECT nope", STRING_MONTH, (DATE_MONTH,), synthetic_db)[0].verdict == "sql_error"


def _question(path: Path, db: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'db = "{db}"\n' + body)
    return path


def test_accepted_sql_follows_its_target(tmp_path, synthetic_db):
    q = _question(
        tmp_path / "q" / "x.toml",
        synthetic_db,
        f'id = "x"\ntarget_sql = "{STRING_MONTH}"\naccepted_sql = ["{DATE_MONTH}"]\n\n'
        '[[levels]]\nname = "terse"\nraw = true\nprompt = "Monthly orders?"\n\n'
        '[[levels]]\nname = "other"\nraw = true\nprompt = "A different question?"\ntarget_sql = "SELECT 1"\n',
    )
    terse, other = load_question(q)
    assert terse.accepted_sql == (DATE_MONTH,) and terse.references == (STRING_MONTH, DATE_MONTH)
    assert other.accepted_sql == ()  # a level with its own target does not inherit the file's alternatives
    bad = _question(
        tmp_path / "q" / "bad.toml",
        synthetic_db,
        'id = "bad"\ntarget_sql = "SELECT 1"\naccepted_sql = "SELECT 1"\n\n[[levels]]\nname = "t"\nprompt = "?"\n',
    )
    with pytest.raises(ValueError, match="accepted_sql must be a list"):
        load_question(bad)


@pytest.mark.skipif(not (HERE / "tpch.duckdb").exists(), reason="needs tpch.duckdb (uv run setup_db.py)")
def test_novel_v2_keeps_v1s_prompts_and_answers_and_drops_only_unasked_columns():
    v1 = {t.id: t for t in load_questions(HERE / "questions" / "tpch-novel", levels=["terse"])}
    v2 = {t.id: t for t in load_questions(HERE / "questions" / "tpch-novel-v2")}
    assert sorted(v2) == [f"{q}v2" for q in sorted(v1)]
    for qid, t in v2.items():
        old = v1[qid[:3]]
        assert t.prompt_sha == old.prompt_sha, qid  # same prompt, so saved v1 runs are valid v2 runs
        verdict = grading.grade_any(old.target_sql, t.target_sql, t.accepted_sql, t.db)[0].verdict
        # v1's own answer is still right, with the columns the terse brief never asked for as extras.
        assert verdict == {"n01v2": "extra_columns", "n03v2": "extra_columns", "n06v2": "extra_columns"}.get(
            qid, "exact"
        ), qid
    # Alternatives are other forms of the same answer, never other definitions: only n01 (the year) and n04
    # (the month) have them, and every alternative has the target's row count.
    assert sorted(q for q, t in v2.items() if t.accepted_sql) == ["n01v2", "n04v2"]
    for t in (v2["n01v2"], v2["n04v2"]):
        n = len(grading.expected_result(t.target_sql, t.db).rows)
        assert all(len(grading.expected_result(a, t.db).rows) == n for a in t.accepted_sql)


@pytest.mark.skipif(not (HERE / "tpch.duckdb").exists(), reason="needs tpch.duckdb (uv run setup_db.py)")
def test_hard_v2_keeps_v1s_prompts_and_definitions_and_drops_only_unasked_columns():
    v1 = {t.id: t for t in load_questions(HERE / "questions" / "tpch-hard", levels=["terse"])}
    v2 = {t.id: t for t in load_questions(HERE / "questions" / "tpch-hard-v2")}
    assert sorted(v2) == [f"{q}v2" for q in sorted(v1)]
    for qid, t in v2.items():
        old = v1[qid[:3]]
        assert t.prompt_sha == old.prompt_sha, qid
        # The intended definitions are the rubric: no alternative definitions, and v1's answer still passes,
        # with the columns the terse brief never asked for as extras.
        assert t.accepted_sql == (), qid
        assert grading.grade_sql(old.target_sql, t.target_sql, t.db).verdict in ("exact", "extra_columns"), qid
        assert (
            "graded by design"
            in tomllib.loads((HERE / "questions" / "tpch-hard-v2" / f"{qid}.toml").read_text())["note"]
        ), qid


def test_regrade_matches_runs_by_prompt_and_keeps_the_original(tmp_path, synthetic_db):
    qdir = tmp_path / "questions" / "set-v2"
    _question(
        qdir / "m1v2.toml",
        synthetic_db,
        f'id = "m1v2"\ntarget_sql = "{STRING_MONTH}"\naccepted_sql = ["{DATE_MONTH}"]\n\n'
        '[[levels]]\nname = "terse"\nraw = true\nprompt = "Monthly orders?"\n',
    )
    (task,) = load_questions(qdir)
    results = tmp_path / "results"

    def run(name, sql, verdict, sha=task.prompt_sha):
        d = results / "b" / name
        d.mkdir(parents=True)
        if sql:
            (d / "answer.sql").write_text(sql)
        (d / "prompt.md").write_text("Monthly orders?")
        row = {"task": "m1", "level": "terse", "prompt_sha": sha, "agent": "pi:x", "verdict": verdict}
        row |= {"correct": 0, "lenient": 0, "sql_source": "submit" if sql else "none", "run_dir": f"results/b/{name}"}
        (d / "result.json").write_text(json.dumps(row))

    run("m1-terse-bare-pi-x-r1", "SELECT DATE '1997-01-01' AS m, 5 AS n", "values_differ")
    run("m1-terse-bare-pi-x-r2", None, "harness_error")  # a provider failure has no answer to grade
    run("m1-terse-bare-pi-x-r3", "SELECT DATE '1997-01-01', 5", "values_differ", sha="0000000000")  # other prompt
    done = regrade.regrade(["b"], [qdir], "t", results=results, quiet=True)
    assert done["runs"] == 1 and done["mismatched"] == []
    (out,) = (results / "regrade-t").glob("*/result.json")
    row = json.loads(out.read_text())
    assert (row["task"], row["verdict"], row["correct"], row["reference"]) == ("m1v2", "exact", 1, 1)
    assert row["regraded_from"] == "results/b/m1-terse-bare-pi-x-r1" and row["original"]["verdict"] == "values_differ"
    assert (out.parent / "answer.sql").exists() and out.parent.name == "m1v2-terse-bare-pi-x-r1"
    manifest = json.loads((results / "regrade-t" / "manifest.json").read_text())
    assert manifest["kind"] == "regrade" and manifest["tasks"][0]["runs"] == 1
    # Rebuilding replaces the last re-grade; a real batch of that name is never replaced.
    assert regrade.regrade(["b"], [qdir], "t", results=results, quiet=True)["runs"] == 1
    (results / "regrade-u").mkdir()
    (results / "regrade-u" / "manifest.json").write_text(json.dumps({"batch": "regrade-u"}))
    with pytest.raises(FileExistsError):
        regrade.regrade(["b"], [qdir], "u", results=results, quiet=True)
