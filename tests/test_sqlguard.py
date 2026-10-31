"""sqlguard.refuse: legitimate SQL passes, the answer key and writes do not, and pi's TypeScript copy agrees."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import sqlguard
from sqlguard import refuse

PI_EXTENSION = HERE / "extensions" / "duckdb-tools.ts"

# Each was refused by the harness v1 keyword blocklist.
LEGITIMATE = [
    "SELECT replace(s_name, '#', '') FROM supplier",
    "SELECT * FROM part WHERE p_name LIKE '%set%'",
    "SELECT count(*) FROM orders WHERE o_comment = 'a; b'",
    "SELECT 1 -- call it done",
    "SELECT o_orderkey FROM orders /* update: insert the delete later */ LIMIT 1",
    "SELECT 'drop table orders' AS note",
    "SELECT $$it's; -- not a comment$$ AS s",
    "SELECT E'it\\'s; fine' AS s",
    'SELECT 1 AS "create"',
    "WITH late AS (SELECT * FROM lineitem WHERE l_commitdate < l_receiptdate) SELECT count(*) FROM late",
    "  select 1;  ",
    "SHOW TABLES",
    "DESCRIBE orders",
    "DESC orders",
    "SUMMARIZE region",
    "EXPLAIN SELECT * FROM orders",
    "EXPLAIN ANALYZE SELECT count(*) FROM orders",
    "FROM region",
    "VALUES (1, 2)",
    "TABLE region",
    "(SELECT 1) UNION ALL (SELECT 2)",
    "PIVOT region ON r_name USING count(*)",
    "SELECT 1 /* nested /* comments */ close; twice */",
    "SELECT o_orderpriority, count(*) FROM orders GROUP BY ALL ORDER BY 1;\n-- set aside: retry with EXISTS",
    "",
    "-- only a comment",
]

REFUSED = {
    "SELECT * FROM tpch_answers()": "tpch",
    "CALL dbgen(sf = 1)": "dbgen",
    "SELECT * FROM read_csv('x.csv')": "read_",
    "SELECT * FROM read_csv_auto('/tmp/' || 'x')": "read_",
    "SELECT * FROM query('SELECT * FROM ' || 'tp' || 'ch_answers()')": "query",
    "SELECT * FROM query_table('orders')": "query",
    "SELECT * FROM 'answers.csv'": "csv",
    'SELECT * FROM "answers.parquet"': "parquet",
    "SELECT 'tpch_queries' AS s": "tpch",
    "INSTALL tpch": "tpch",
    "LOAD httpfs": "LOAD",
    "ATTACH 'x.duckdb' AS x": "duckdb",
    "ATTACH ':memory:' AS m": "ATTACH",
    "COPY orders TO 'out'": "COPY",
    "EXPORT DATABASE 'dir'": "EXPORT",
    "SET enable_external_access = true": "SET",
    "RESET threads": "RESET",
    "PRAGMA version": "PRAGMA",
    "CALL pragma_version()": "CALL",
    "CREATE TABLE t AS SELECT 1": "CREATE",
    "INSERT INTO region VALUES (9, 'X', 'y')": "INSERT",
    "UPDATE orders SET o_comment = ''": "UPDATE",
    "DELETE FROM orders": "DELETE",
    "DROP TABLE orders": "DROP",
    "SELECT 1; SELECT 2": "one statement",
    "SELECT 1; DROP TABLE orders": "one statement",
    "/* hide */ INSTALL httpfs": "INSTALL",
    "-- comment\nDETACH db": "DETACH",
    "SELECT E'\\'' ; LOAD httpfs --'": "one statement",
    "SELECT 1 /* not closed": None,  # a parse error, not a refusal: the engine reports it
    "$$ unterminated": "''",
}


@pytest.mark.parametrize("sql", LEGITIMATE)
def test_legitimate_sql_is_not_refused(sql):
    assert refuse(sql) is None


@pytest.mark.parametrize("sql,why", REFUSED.items())
def test_refused(sql, why):
    got = refuse(sql)
    if why is None:
        assert got is None
    else:
        assert got is not None and why.lower() in got.lower()


@pytest.mark.parametrize("sql", [s for s in LEGITIMATE if s.strip() and not s.startswith("--")])
def test_scanner_agrees_with_duckdb_parser(sql):
    """One statement by the guard's count is one statement to DuckDB, and DuckDB classifies it as read-only."""
    (statement,) = duckdb.extract_statements(sql)
    assert statement.type in {duckdb.StatementType.SELECT, duckdb.StatementType.EXPLAIN, duckdb.StatementType.MULTI}


def test_scanner_strips_comments_and_blanks_literals():
    code, text = sqlguard.scan("SELECT 'a;b', \"x\" -- c\n/* d /* e */ */ FROM t")
    assert code == "SELECT '', \"x\"  \n  FROM t"
    assert text == "SELECT 'a;b', \"x\"  \n  FROM t"


# ---------- pi's TypeScript copy ----------


def _ts_guard() -> str:
    src = PI_EXTENSION.read_text()
    return src[src.index("// sqlguard:begin") : src.index("// sqlguard:end")]


def test_typescript_lists_match_python():
    ts = _ts_guard()
    blocklist = ts[ts.index("const BLOCKLIST = [") : ts.index("];", ts.index("const BLOCKLIST = ["))]
    sources = re.findall(r"^\s*/(.+)/i,", blocklist, re.MULTILINE)
    assert sources == [rx.pattern for rx in sqlguard.BLOCKLIST]
    allowed = ts[ts.index("const ALLOWED = [") : ts.index("];", ts.index("const ALLOWED = ["))]
    assert tuple(re.findall(r'"(\w+)"', allowed)) == sqlguard.ALLOWED


def test_typescript_guard_gives_the_same_answers(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed; the TypeScript guard is only compared statically")
    corpus = LEGITIMATE + list(REFUSED)
    script = tmp_path / "guard.ts"
    script.write_text(
        _ts_guard() + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[2]).map((s: string) => refuse(s))));\n"
    )
    run = subprocess.run([node, str(script), json.dumps(corpus)], capture_output=True, text=True, check=False)
    if run.returncode and "Unknown file extension" in run.stderr:
        pytest.skip("this node cannot strip TypeScript types")
    assert run.returncode == 0, run.stderr
    assert dict(zip(corpus, json.loads(run.stdout))) == {sql: refuse(sql) for sql in corpus}
