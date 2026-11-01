"""SQL style and dialect signals, the comma-join exposure signal, and the style report on a tiny batch."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evalkit import audit, style
from sqlguard import scan


def joins(sql: str) -> bool:
    return bool(audit.COMMA_JOIN.search(scan(sql)[0]))


def test_comma_join_is_an_implicit_join_and_nothing_else():
    assert joins("SELECT * FROM lineitem, orders WHERE l_orderkey = o_orderkey")
    assert joins("select 1 from orders o, lineitem l where o.o_orderkey = l.l_orderkey")
    assert joins("SELECT 1 FROM orders AS o , lineitem AS l")
    assert not joins("SELECT a, b FROM t GROUP BY a, b ORDER BY a, b")  # commas after FROM t, but not tables
    assert not joins("SELECT 1 FROM orders o JOIN lineitem l ON o.o_orderkey = l.l_orderkey")
    assert not joins("SELECT 'FROM a, b' AS s FROM t")  # inside a string
    assert not joins("SELECT 1 FROM t -- FROM a, b\n")  # inside a comment
    assert not joins("SELECT 1 FROM t WHERE x IN (1, 2)")


def _run(run_dir: Path, log: str, answer: str | None):
    run_dir.mkdir(parents=True)
    (run_dir / "queries.log").write_text(log)
    if answer is not None:
        (run_dir / "answer.sql").write_text(answer)


LOG = (
    "-- 2026-10-31T20:00:00Z RUN rows=8\nSHOW TABLES\n;\n\n"
    "-- 2026-10-31T20:00:01Z RUN rows=9\nDESCRIBE orders\n;\n\n"
    "-- 2026-10-31T20:00:02Z ERROR Catalog Error: Scalar Function with name to_char does not exist!\n"
    "Did you mean \"strftime\"?\nSELECT to_char(o_orderdate, 'YYYY') FROM orders\n;\n\n"
    '-- 2026-10-31T20:00:03Z ERROR Binder Error: column "x" not found\nSELECT x FROM orders\n;\n\n'
    "-- 2026-10-31T20:00:04Z REFUSED blocked\nSELECT * FROM tpch_queries()\n;\n\n"
    "-- 2026-10-31T20:00:05Z SUBMIT rows=5\nSELECT year(o_orderdate), count(*) FROM orders, customer GROUP BY 1\n;\n\n"
)


def test_run_style_counts_dialect_stumbles_and_constructs(tmp_path):
    _run(tmp_path / "r", LOG, "SELECT year(o_orderdate), count(*)\nFROM orders, customer\nGROUP BY 1\n")
    s = style.run_style(tmp_path / "r")
    assert (s["queries"], s["discovery"], s["failed"], s["refused"]) == (5, 2, 2, 1)
    assert s["error_types"] == ["Catalog Error", "Binder Error"]
    assert s["missing_functions"] == ["to_char"]
    assert set(s["constructs"]) == {"comma joins (FROM a, b)", "year() / month()", "GROUP BY position (GROUP BY 1)"}
    assert s["answer_lines"] == 3 and s["submitted"]
    # A refused or failed submit_sql call is not a run_sql query.
    submits = "-- 2026-10-31T20:00:06Z REFUSED-SUBMIT blocked\nDROP TABLE x\n;\n\n"
    submits += "-- 2026-10-31T20:00:07Z SUBMIT-ERROR Parser Error\nSELEC 1\n;\n\n"
    _run(tmp_path / "s", LOG + submits, None)
    s = style.run_style(tmp_path / "s")
    assert (s["queries"], s["failed"], s["refused"]) == (5, 2, 1)
    _run(tmp_path / "none", "", None)  # no log, no submission
    assert style.run_style(tmp_path / "none") | {} == style.run_style(tmp_path / "none")
    assert style.run_style(tmp_path / "none")["submitted"] is False


def test_style_report_slices_by_set_and_is_deterministic(tmp_path, monkeypatch):
    results = tmp_path / "results"
    sets = {"q01": "tpch", "n01": "tpch-novel"}
    monkeypatch.setattr(style, "question_sets", lambda: sets)
    for agent, task, answer in (
        ("agy:gemini-x", "q01", "SELECT 1 FROM a, b"),
        ("agy:gemini-x", "n01", "SELECT 1 FROM a JOIN b ON true"),
        ("claude:y", "q01", "SELECT 1 FROM a JOIN b ON true"),
        ("claude:y", "n01", None),
    ):
        name = f"{task}-terse-{agent.replace(':', '-')}-r1"
        d = results / "b" / name
        _run(d, LOG, answer)
        row = {"agent": agent, "task": task, "level": "terse", "verdict": "exact", "correct": 1}
        (d / "result.json").write_text(json.dumps(row | {"run_dir": f"results/b/{name}"}))
    out = tmp_path / "style"
    assert style.run_style_report(["b"], out, results=results)["runs"] == 4
    md = (out / "summary.md").read_text()
    assert md.index("### tpch\n") < md.index("### tpch-novel\n")  # TPC-H first, then the controls
    tpch = md.split("### tpch\n")[1].split("###")[0]
    assert "| comma joins (FROM a, b) | 0% | 100% |" in tpch  # makers ordered Anthropic, then Google
    assert "| Google | tpch | 1 | 5.0 | 2.0 | 2.00 | 100% | 1.00 |" in md
    assert "`to_char` 1" in md
    before = {p.name: p.read_text() for p in out.iterdir()}
    style.run_style_report(["b"], out, results=results)
    assert {p.name: p.read_text() for p in out.iterdir()} == before
