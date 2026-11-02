"""The integrity and prior-exposure audit: each signal on hand-made runs, then the whole driver on a tiny batch."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evalkit import audit


def _log(run_dir: Path, *entries: tuple[str, str]):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "queries.log").write_text(
        "".join(f"-- 2026-10-31T20:00:00Z {status} rows=1\n{sql}\n;\n\n" for status, sql in entries)
    )


def test_integrity_sorts_attempts_and_ignores_lookalikes(tmp_path):
    _log(
        tmp_path,
        ("REFUSED", "SELECT * FROM tpch_queries() WHERE query_nr = 13"),
        ("REFUSED", "SELECT * FROM glob('/Users/someone/eval/*')"),
        ("ERROR", "SELECT * FROM duckdb_extensions()"),
        ("RUN", "PRAGMA version"),  # read-only catalog pragma: not probing
        ("RUN", "SELECT min(l_discount) || '/' || max(l_discount) FROM lineitem"),  # '/' is not a path
        ("RUN", "SELECT sum(CASE WHEN p_type LIKE 'PROMO%' THEN 1 END) / count(*) FROM part"),
        ("RUN", "SELECT count(*) AS tpch_pattern FROM supplier -- tpch_queries() in a comment"),
    )
    got = audit.integrity(tmp_path)
    # Neither a column named tpch_pattern nor tpch_queries() inside a comment is an attempt.
    assert [e["sql"] for e in got["answer_key"]] == ["SELECT * FROM tpch_queries() WHERE query_nr = 13"]
    assert [e["sql"][:20] for e in got["files"]] == ["SELECT * FROM glob('"]
    assert [e["sql"] for e in got["probing"]] == ["SELECT * FROM duckdb_extensions()"]
    assert got["denied_tools"] == []
    assert audit.integrity(tmp_path, {"usage_extra": {"denied_actions": ["mcp:CallMcpTool"]}})["denied_tools"] == [
        "mcp:CallMcpTool"
    ]


Q21 = """
SELECT s_name, count(*) AS numwait FROM supplier, lineitem l1, orders, nation
WHERE s_suppkey = l1.l_suppkey AND o_orderkey = l1.l_orderkey AND o_orderstatus = 'F'
  AND o_orderdate >= DATE '1995-01-01' AND o_orderdate < DATE '1995-01-01' + INTERVAL '3 month'
  AND n_name = 'SAUDI ARABIA' AND s_comment LIKE '%Customer%Complaints%'
GROUP BY s_name ORDER BY numwait DESC, s_name LIMIT 100
"""
DATA = frozenset({"F", "O", "P", "SAUDI ARABIA"})


def test_recall_markers_are_what_the_brief_and_the_data_leave_out():
    terse = audit.recall_markers(Q21, "Which Saudi suppliers made orders late?", DATA, ("s_name", "numwait"))
    # 'F' and 'SAUDI ARABIA' are values in the data; the date and the interval come from the brief's words.
    assert terse == {"alias:numwait", "num:100", "str:%Customer%Complaints%"}
    spec = audit.recall_markers(
        Q21,
        "Suppliers whose comment has 'Customer' and later 'Complaints'; keep the first 100 rows, named numwait.",
        DATA,
        ("s_name", "numwait"),
    )
    assert spec == set()
    sql = "SELECT s_name, COUNT(*) AS NumWait FROM supplier WHERE s_comment LIKE '%customer%complaints%' LIMIT 100"
    assert audit.markers_in(sql, terse) == ["alias:numwait", "num:100", "str:%Customer%Complaints%"]
    assert audit.markers_in("SELECT 1000, 'numwait'", terse) == []  # a longer number, a string not an alias


def test_exposure_signals(tmp_path):
    run = tmp_path / "run"
    _log(run, ("RUN", "SHOW TABLES"), ("SUBMIT", "SELECT s_name, count(*) AS numwait FROM supplier LIMIT 100"))
    (run / "final_message.txt").write_text("This is the standard TPC-H Q21 query. Q4 1993 means October.")
    got = audit.exposure(run, "q21", {"alias:numwait", "num:100"})
    assert got == {
        "names_benchmark": True,
        "names_query": True,
        "recall_markers": ["alias:numwait", "num:100"],
        "schema_before_discovery": False,
        "comma_joins": False,
    }
    # TPC-H's 1990s join style in the submitted query is its own signal.
    (run / "answer.sql").write_text("SELECT s_name FROM supplier, nation WHERE s_nationkey = n_nationkey")
    assert audit.exposure(run, "q21", set())["comma_joins"] is True
    (run / "answer.sql").unlink()
    # A quarter is not a query number, and naming the schema TPC-H is not naming the query.
    (run / "final_message.txt").write_text("Assumed Q4 1993 is the order date; the schema is TPC-H style.")
    assert audit.exposure(run, "q04", set())["names_query"] is False
    # The official title counts, and so does asking the engine for the query by number.
    (run / "final_message.txt").write_text("This follows the 'suppliers who kept orders waiting' definition.")
    assert audit.exposure(run, "q21", set())["names_query"] is True
    # A v3 question is a version of the TPC-H query: q21v3 is query 21, by title and by number.
    assert audit.exposure(run, "q21v3", set())["names_query"] is True
    (run / "final_message.txt").write_text("This is the standard TPC-H Q21 query.")
    assert audit.exposure(run, "q21v3", set())["names_query"] is True
    assert audit.exposure(run, "n03v3", set())["names_query"] is False
    (run / "final_message.txt").write_text("This follows the 'suppliers who kept orders waiting' definition.")
    _log(
        run,
        ("REFUSED", "SELECT query FROM tpch_queries() WHERE query_nr = 21"),
        ("RUN", "SELECT l_orderkey FROM lineitem"),
    )
    (run / "final_message.txt").write_text("")
    got = audit.exposure(run, "q21", set())
    assert got["names_query"] is True and got["schema_before_discovery"] is False  # its first query names no column
    _log(run, ("RUN", "SELECT l_orderkey FROM lineitem LIMIT 1"))
    assert audit.exposure(run, "q21", set())["schema_before_discovery"] is True


def test_run_audit_writes_reviewable_evidence(tmp_path, synthetic_db):
    batch = tmp_path / "results" / "b1"
    rows = [
        ("q21-terse-bare-x-r1", "q21", "terse", [("REFUSED", "SELECT * FROM tpch_queries()")], "exact"),
        ("q21-terse-bare-x-r2", "q21", "terse", [("RUN", "SHOW TABLES")], "harness_error"),  # left out
        ("n01-spec-bare-x-r1", "n01", "spec", [("RUN", "SHOW TABLES")], "rows_differ"),
    ]
    for name, task, level, entries, verdict in rows:
        run = batch / name
        _log(run, *entries)
        (run / "prompt.md").write_text("Which Saudi suppliers made orders late?")
        row = {"run_dir": f"results/b1/{name}", "agent": "pi:x", "harness": "pi", "model": "x", "task": task}
        row |= {"level": level, "repeat": 1, "verdict": verdict, "correct": int(verdict == "exact")}
        (run / "result.json").write_text(json.dumps(row))
    out = tmp_path / "audit"
    assert audit.run_audit(["b1"], out, results=tmp_path / "results", db=synthetic_db)["runs"] == 2
    runs = [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()]
    assert [r["task"] for r in runs] == ["n01", "q21"]
    assert runs[1]["answer_key"][0]["status"] == "REFUSED" and "alias:numwait" in runs[1]["markers_available"]
    assert "alias:numwait" in json.loads((out / "markers.json").read_text())["q21/terse"]
    summary, flagged = (out / "summary.md").read_text(), (out / "flagged.md").read_text()
    assert "| pi:x | 2 | 1 (50.0%) |" in summary and "### tpch-novel" in summary
    assert "q21-terse-bare-x-r1" in flagged and "tpch_queries()" in flagged
    # Deterministic: the same results give the same files.
    before = {p.name: p.read_text() for p in out.iterdir()}
    audit.run_audit(["b1"], out, results=tmp_path / "results", db=synthetic_db)
    assert {p.name: p.read_text() for p in out.iterdir()} == before


def test_a_title_the_brief_already_uses_is_not_naming_the_query(tmp_path):
    run = tmp_path / "run"
    _log(run, ("RUN", "SHOW TABLES"))
    (run / "prompt.md").write_text("Who was our top supplier by revenue in Q1 1996?")
    (run / "final_message.txt").write_text("The top supplier was Supplier#000000971.")
    assert audit.exposure(run, "q15", set())["names_query"] is False
    (run / "prompt.md").write_text("Which customers placed really large orders?")
    (run / "final_message.txt").write_text("I treated this as the TPC-H large volume customer query.")
    assert audit.exposure(run, "q18", set())["names_query"] is True


def test_a_date_format_is_not_a_recall_marker():
    sql = "SELECT strftime(o_orderdate, '%Y-%m') AS order_month, count(*) AS order_count FROM orders GROUP BY 1"
    assert (
        audit.recall_markers(sql, "Orders per month as YYYY-MM.", frozenset(), ("order_month", "order_count")) == set()
    )
