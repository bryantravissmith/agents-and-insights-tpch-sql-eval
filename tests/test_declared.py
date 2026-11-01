"""Declared answer columns: label normalization, row pairing, answer slots, fallbacks, the tool and a full run."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_evalkit import Q04_BODY, FakeHarness, _with_fake, sandbox  # noqa: F401 (sandbox is a fixture)

from evalkit import load_question, load_questions, runner
from evalkit.declared import (
    ALL,
    Declaration,
    grade_declared,
    grade_task,
    normalize_column,
    read_declaration,
)
from evalkit.tasks import Task

HERE = Path(__file__).resolve().parent.parent

SEGMENTS = "SELECT * FROM (VALUES ('AUTOMOBILE', 1.5), ('BUILDING', 2.25), ('MACHINERY', 3.0)) t(segment, avg_orders)"
MONTHS = "SELECT * FROM (VALUES ('1997-01', 1897, -20), ('1997-02', 1780, -117)) t(month, orders, change)"
WHO = (
    "SELECT * FROM (VALUES (10, 'Customer#000000010', 'addr a'), (20, 'Customer#000000020', 'addr b'))"
    " t(c_custkey, c_name, c_address)"
)


def test_normalize_column_reads_the_same_label_written_different_ways():
    assert normalize_column([" Automobile", "BUILDING "]) == ["automobile", "building"]
    assert normalize_column(["1997", 1998.0]) == [1997, 1998]
    assert normalize_column(["1997-01-01T00:00:00", "1997-02-01"]) == ["1997-01", "1997-02"]  # month starts
    assert normalize_column(["1993-01-01", "1994-01-01"]) == [1993, 1994]  # year starts
    assert normalize_column(["1997-03-15", None]) == ["1997-03-15", None]  # a plain day stays a day


def test_a_shown_checked_count_must_be_right(synthetic_db):
    # q21's shape: "which suppliers", with a count of late orders as checked context.
    ref = "SELECT * FROM (VALUES ('S1', 16), ('S2', 12), ('S3', 9)) t(s_name, numwait)"

    def grade(sql, checked=("numwait",), labels=()):
        return grade_declared(sql, None, ref, (("s_name",),), synthetic_db, labels, checked_columns=checked)[0]

    names = "SELECT * FROM (VALUES (1, 'S1'), (2, 'S2'), (3, 'S3')) t(s_suppkey, s_name)"
    assert grade(names).correct  # the count is optional, and a supplier key beside the names is not a count
    right = "SELECT * FROM (VALUES ('S3', 9), ('S1', 16), ('S2', 12)) t(supplier, late_orders)"
    assert grade(right).correct
    wrong = "SELECT * FROM (VALUES (1, 'S1', 43), (2, 'S2', 42), (3, 'S3', 7)) t(s_suppkey, supplier, late_orders)"
    g = grade(wrong)
    assert g.verdict == "values_differ" and "late_orders" in g.detail and "numwait" in g.detail
    assert grade(wrong, checked=()).correct  # unchecked context is never graded
    # With labels pairing the rows, the shown value is compared row by row.
    assert grade(wrong, labels=("s_name",)).verdict == "values_differ"
    assert grade(right, labels=("s_name",)).correct


def test_labels_pair_rows_so_right_values_on_wrong_labels_fail(synthetic_db):
    answers = (("avg_orders",),)
    right = "SELECT * FROM (VALUES ('machinery', 3.0, 7), ('automobile', 1.5, 7), ('building', 2.25, 7)) t(s, a, n)"
    g, notes = grade_declared(right, ["a"], SEGMENTS, answers, synthetic_db)
    assert g.verdict == "exact" and notes["labels"] == {"segment": "s"} and notes["labels_normalized"]
    # The same three averages under the wrong segments: correct as a set, wrong by row.
    swapped = "SELECT * FROM (VALUES ('AUTOMOBILE', 3.0), ('BUILDING', 1.5), ('MACHINERY', 2.25)) t(s, a)"
    assert grade_declared(swapped, ["a"], SEGMENTS, answers, synthetic_db)[0].verdict == "values_differ"
    # Rounded to the candidate's own two places: the grader's rounding rule applies.
    rounded = "SELECT * FROM (VALUES ('AUTOMOBILE', 1.50), ('BUILDING', 2.25), ('MACHINERY', 3.00)) t(s, a)"
    assert grade_declared(rounded, ["a"], SEGMENTS, answers, synthetic_db)[0].correct


def test_month_label_as_a_date_pairs_and_undeclared_columns_are_not_graded(synthetic_db):
    cand = (
        "SELECT * FROM (VALUES (DATE '1997-02-01', 1780, -117, 'x'), (DATE '1997-01-01', 1897, -20, 'y'))"
        " t(m, n, d, junk)"
    )
    g, _ = grade_declared(cand, ["n", "d"], MONTHS, (("orders",), ("change",)), synthetic_db)
    assert g.verdict == "exact"  # junk is not declared, so not graded
    g, _ = grade_declared(cand, ["n", "d", "junk"], MONTHS, (("orders",), ("change",)), synthetic_db)
    assert g.verdict == "extra_columns" and g.correct  # a declared extra is correct and noted
    g, _ = grade_declared(cand, ["n"], MONTHS, (("orders",), ("change",)), synthetic_db)
    assert g.verdict == "missing_columns" and not g.correct


def test_an_answer_slot_takes_a_key_or_a_name_and_context_is_ignored(synthetic_db):
    slot = (("c_custkey", "c_name"),)
    by_name = "SELECT * FROM (VALUES ('Customer#000000020'), ('Customer#000000010')) t(who)"
    assert grade_declared(by_name, ["who"], WHO, slot, synthetic_db, label_columns=())[0].correct  # any order
    by_key = "SELECT * FROM (VALUES (10, 1.0), (20, 2.0)) t(k, v)"
    assert grade_declared(by_key, ["k"], WHO, slot, synthetic_db, label_columns=())[0].verdict == "exact"
    wrong = "SELECT * FROM (VALUES (10), (30)) t(k)"
    assert not grade_declared(wrong, ["k"], WHO, slot, synthetic_db, label_columns=())[0].correct


def test_bad_declarations_and_fallbacks(synthetic_db):
    g, _ = grade_declared("SELECT 1 AS a", ["nope"], SEGMENTS, (("avg_orders",),), synthetic_db)
    assert g.verdict == "bad_declaration" and "nope" in g.detail
    # Other rows: the labels cannot pair, so the run is graded by its whole result.
    fewer = "SELECT * FROM (VALUES ('AUTOMOBILE', 1.5)) t(s, a)"
    assert grade_declared(fewer, ["a"], SEGMENTS, (("avg_orders",),), synthetic_db) is None
    task = Task.adhoc("?", target_sql=SEGMENTS, db=synthetic_db)
    task = Task(**{**task.__dict__, "answer_columns": (("avg_orders",),)})
    g, _, notes = grade_task(fewer, Declaration(("a",)), task, synthetic_db)
    assert g.verdict == "rows_differ" and notes["graded_by"].startswith("whole result")
    g, _, notes = grade_task(SEGMENTS, ALL, task, synthetic_db)  # every column declared
    assert g.verdict == "extra_columns" and notes["graded_by"] == "declared columns"
    assert grade_task(SEGMENTS, None, task, synthetic_db)[2] == {}  # nothing declared: today's grader


def test_loader_reads_answer_slots_and_labels(tmp_path, synthetic_db):
    q = tmp_path / "x.toml"
    q.write_text(
        f'db = "{synthetic_db}"\nid = "x"\ntarget_sql = "{WHO}"\n'
        'answer_columns = [["c_custkey", "c_name"]]\nlabel_columns = []\n\n'
        '[[levels]]\nname = "terse"\nraw = true\nprompt = "Which customers?"\n'
    )
    (t,) = load_question(q)
    assert t.answer_columns == (("c_custkey", "c_name"),) and t.label_columns == ()
    q.write_text(q.read_text().replace('answer_columns = [["c_custkey", "c_name"]]', "answer_columns = [1]"))
    with pytest.raises(ValueError, match="answer_columns"):
        load_question(q)


@pytest.mark.skipif(not (HERE / "tpch.duckdb").exists(), reason="needs tpch.duckdb (uv run setup_db.py)")
def test_every_reference_passes_its_own_declared_grading():
    for t in load_questions(HERE / "questions"):
        declared = [slot[0] for slot in t.answer_columns]
        hit = grade_declared(t.target_sql, declared, t.target_sql, t.answer_columns, t.db, t.label_columns)
        assert hit is not None and hit[0].verdict == "exact", t.name


def test_submit_tool_takes_answer_columns_only_when_the_batch_declares(tmp_path, synthetic_db, monkeypatch):
    import importlib

    import mcp_server

    for flag, expected in (("1", True), (None, False)):
        if flag:
            monkeypatch.setenv("AUDIT_DECLARE", flag)
        else:
            monkeypatch.delenv("AUDIT_DECLARE", raising=False)
        mod = importlib.reload(mcp_server)
        monkeypatch.setattr(mod, "DB", str(synthetic_db))
        monkeypatch.setattr(mod, "OUT", str(tmp_path / str(flag)))
        params = mod.submit_sql.__code__.co_varnames[: mod.submit_sql.__code__.co_argcount]
        assert ("answer_columns" in params) is expected
        if expected:
            with pytest.raises(ValueError, match="not in the result columns"):
                mod.submit_sql("SELECT 1 AS a", ["b"])
            mod.submit_sql("SELECT 1 AS a, 2 AS b", ["b"])
            assert json.loads((tmp_path / "1" / "answer_columns.json").read_text()) == ["b"]
    importlib.reload(mcp_server)


def test_pi_extension_reads_the_result_columns_from_the_duckdb_csv_header(tmp_path):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    ts = (HERE / "extensions" / "duckdb-tools.ts").read_text()
    start = ts.index("function csvHeader")
    body = ts[start : ts.index("\n}\n", start) + 3]
    script = tmp_path / "header.ts"
    script.write_text(body + "\nconsole.log(JSON.stringify(csvHeader(process.argv[2])));\n")
    out = 'region,"total, net","say ""hi"""\nAFRICA,1,2\n'
    run = subprocess.run([node, str(script), out], capture_output=True, text=True, check=False)
    if run.returncode and "Unknown file extension" in run.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == ["region", "total, net", 'say "hi"']


@pytest.mark.usefixtures("sandbox")
def test_a_declaring_batch_asks_for_columns_and_grades_by_them(monkeypatch, db, questions_on):
    class Declaring(FakeHarness):
        def command(self, ctx):
            cmd = super().command(ctx)
            # Declare the count column the way submit_sql would, when the batch asks for it.
            cmd[2] += (
                "\nif os.environ.get('AUDIT_DECLARE') == '1':\n"
                "    (out / 'answer_columns.json').write_text('[\"order_count\"]')\n"
            )
            return cmd

    _with_fake(monkeypatch, Declaring(Q04_BODY + "ORDER BY 1"))
    qdir = questions_on(db, "q04")
    tasks = load_question(qdir / "q04.toml", ["spec"])
    (res,) = runner.run_grid(tasks, ["fake:a"], batch="decl", quiet=True, declare_columns=True)
    row = json.loads((res.run_dir / "result.json").read_text())
    assert runner.DECLARE_INSTRUCTION in (res.run_dir / "prompt.md").read_text()
    assert row["declaration"]["graded_by"] == "declared columns" and row["correct"]
    manifest = json.loads((runner.RESULTS / "decl" / "manifest.json").read_text())
    assert manifest["run_options"]["declare_columns"] is True
    assert manifest["tasks"][0]["prompt_sha"] == runner.declaring(tasks)[0].prompt_sha


def test_a_declared_grain_must_be_unique_and_must_match_the_reference(synthetic_db):
    answers = (("avg_orders",),)
    # A fan-out repeats a segment: the agent's own grain claim is false.
    dup = "SELECT * FROM (VALUES ('AUTOMOBILE', 1.5), ('AUTOMOBILE', 1.5), ('BUILDING', 2.25)) t(s, a)"
    g, notes = grade_declared(dup, ["a"], SEGMENTS, answers, synthetic_db, grain=["s"])
    assert g.verdict == "grain_not_unique" and not g.correct and "automobile" in g.detail
    # One row per segment for two of three segments: the grain differs from the reference's.
    fewer = "SELECT * FROM (VALUES ('AUTOMOBILE', 1.5), ('BUILDING', 2.25)) t(s, a)"
    g, _ = grade_declared(fewer, ["a"], SEGMENTS, answers, synthetic_db, grain=["s"])
    # The same grain (segments) with a segment missing: other rows, not another grain.
    assert g.verdict == "rows_differ" and "2 rows" in g.detail and "missing ['machinery']" in g.detail
    # A different grain altogether (regions, where the reference has segments): grain_differs.
    regions = "SELECT * FROM (VALUES ('AFRICA', 1.5), ('ASIA', 2.25)) t(r, a)"
    g, _ = grade_declared(regions, ["a"], SEGMENTS, answers, synthetic_db, grain=["r"])
    assert g.verdict == "grain_differs" and "segment" in g.detail
    # The right grain, written in lower case: pairs and passes; the grain is recorded.
    right = "SELECT * FROM (VALUES ('machinery', 3.0), ('automobile', 1.5), ('building', 2.25)) t(s, a)"
    g, notes = grade_declared(right, ["a"], SEGMENTS, answers, synthetic_db, grain=["s"])
    assert g.verdict == "exact" and notes["grain"] == ["s"]
    # Only declared grain columns may pair rows: a grain that is not the segment cannot stand in for it.
    other = "SELECT * FROM (VALUES ('AUTOMOBILE', 1.5, 1), ('BUILDING', 2.25, 2), ('MACHINERY', 3.0, 3)) t(s, a, n)"
    assert grade_declared(other, ["a"], SEGMENTS, answers, synthetic_db, grain=["n"]) is None  # same rows: fall back
    # An empty grain claims one row.
    g, _ = grade_declared(right, ["a"], SEGMENTS, answers, synthetic_db, grain=[])
    assert g.verdict == "grain_not_unique"
    g, _ = grade_declared("SELECT 2 AS x", ["x"], "SELECT 2 AS total", (("total",),), synthetic_db, grain=[])
    assert g.verdict == "exact"
    assert grade_declared(right, ["a"], SEGMENTS, answers, synthetic_db, grain=["zz"])[0].verdict == "bad_declaration"


def test_declarations_read_both_saved_forms(tmp_path):
    (tmp_path / "answer_columns.json").write_text('["a", "b"]')
    assert read_declaration(tmp_path) == Declaration(("a", "b"))
    (tmp_path / "answer_columns.json").write_text('{"answer": ["a"], "grain": []}')
    assert read_declaration(tmp_path) == Declaration(("a",), ())
    (tmp_path / "answer_columns.json").unlink()
    assert read_declaration(tmp_path) is None


def test_submit_tool_takes_the_grain_when_the_batch_asks(tmp_path, synthetic_db, monkeypatch):
    import importlib

    import mcp_server

    monkeypatch.setenv("AUDIT_DECLARE", "1")
    monkeypatch.setenv("AUDIT_DECLARE_GRAIN", "1")
    mod = importlib.reload(mcp_server)
    monkeypatch.setattr(mod, "DB", str(synthetic_db))
    monkeypatch.setattr(mod, "OUT", str(tmp_path))
    with pytest.raises(ValueError, match="grain_columns"):
        mod.submit_sql("SELECT 1 AS a, 2 AS b", ["b"], ["zz"])
    mod.submit_sql("SELECT 1 AS a, 2 AS b", ["b"], ["a"])
    assert json.loads((tmp_path / "answer_columns.json").read_text()) == {"answer": ["b"], "grain": ["a"]}
    monkeypatch.delenv("AUDIT_DECLARE")
    monkeypatch.delenv("AUDIT_DECLARE_GRAIN")
    importlib.reload(mcp_server)


def test_a_grain_batch_asks_for_the_grain_and_sets_the_tool_env():
    from evalkit.harnesses import RunContext

    (t,) = runner.declaring([Task.adhoc("How many orders?", target_sql="SELECT 1")], grain=True)
    assert runner.DECLARE_GRAIN_INSTRUCTION in t.prompt and "grain_columns" in t.prompt
    ctx = RunContext("p", "m", Path("."), Path("."), Path("x.duckdb"), None, 1, 1, declare_grain=True)
    assert ctx.tool_env["AUDIT_DECLARE"] == "1" and ctx.tool_env["AUDIT_DECLARE_GRAIN"] == "1"
