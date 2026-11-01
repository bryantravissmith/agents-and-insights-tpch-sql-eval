# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "duckdb"]
# ///
"""MCP server exposing the audit tools to any MCP-capable CLI (Claude Code, Codex, Grok, Antigravity).

Same two tools, same blocklist (sqlguard.py), same logging as extensions/duckdb-tools.ts (which pi uses):
  run_sql(sql)     read-only, <=50 rows as CSV, every statement appended to $AUDIT_OUT/queries.log
  submit_sql(sql)  saves $AUDIT_OUT/answer.sql and runs it (evalkit re-executes answer.sql to grade)
                   with AUDIT_DECLARE=1 it also takes answer_columns, saved to answer_columns.json, and with
                   AUDIT_DECLARE_GRAIN=1 too, grain_columns (saved as {"answer": [...], "grain": [...]})

Env: AUDIT_DB (path to tpch.duckdb), AUDIT_OUT (run directory), AUDIT_DECLARE (1: declared answer columns).
Run: uv run mcp_server.py        (stdio transport; the CLIs launch it themselves)
"""

import csv
import io
import json
import os
from datetime import UTC, datetime

import duckdb
from mcp.server.fastmcp import FastMCP

from sqlguard import ENGINE_CONFIG, refuse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.abspath(os.environ.get("AUDIT_DB", os.path.join(HERE, "tpch.duckdb")))
OUT = os.path.abspath(os.environ.get("AUDIT_OUT", os.path.join(HERE, "results", "adhoc")))
MAX_ROWS = 50
# Batches run with declared answer columns: submit_sql also takes the names of the columns that answer.
DECLARE = os.environ.get("AUDIT_DECLARE") == "1"
# ... and also the grain: the names of the columns that identify one row of the result.
GRAIN = os.environ.get("AUDIT_DECLARE_GRAIN") == "1"

mcp = FastMCP("audit")


def _log(kind: str, sql: str, note: str = ""):
    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    with open(os.path.join(OUT, "queries.log"), "a") as f:
        f.write(f"-- {stamp} {kind}{(' ' + note) if note else ''}\n{sql.strip()}\n;\n\n")


def _run(sql: str):
    if not os.path.exists(DB):
        raise RuntimeError(f"Database not found at {DB}. Run setup_db.py first.")
    # A read-only database does not prohibit table functions from reading other files, loading extensions or
    # changing settings. Enforce this in the engine, rather than relying on the SQL spelling guard.
    con = duckdb.connect(DB, read_only=True, config=ENGINE_CONFIG)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    finally:
        con.close()
    return cols, rows


def _csv(cols, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(rows)
    return buf.getvalue()


@mcp.tool()
def run_sql(sql: str) -> str:
    """Execute ONE read-only SQL statement against the orders database (DuckDB).
    Returns up to 50 rows as CSV with a header. Use SHOW TABLES and DESCRIBE <table> to explore the schema.
    Writes, file access and extension loading are refused."""
    bad = refuse(sql)
    if bad:
        _log("REFUSED", sql, bad)
        raise ValueError(bad)
    t0 = datetime.now()
    try:
        cols, rows = _run(sql)
    except Exception as e:  # noqa: BLE001
        _log("ERROR", sql, str(e)[:300])
        raise ValueError(f"SQL error: {e}") from None
    ms = int((datetime.now() - t0).total_seconds() * 1000)
    _log("RUN", sql, f"rows={len(rows)} ms={ms}")
    shown = _csv(cols, rows[:MAX_ROWS])
    tail = f"\n({len(rows)} rows total; showing first {MAX_ROWS})" if len(rows) > MAX_ROWS else f"\n({len(rows)} rows)"
    return shown + tail


def _submit(sql: str, answer_columns: list[str] | None, grain_columns: list[str] | None = None) -> str:
    bad = refuse(sql)
    if bad:
        _log("REFUSED-SUBMIT", sql, bad)
        raise ValueError(bad)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "answer.sql"), "w") as f:
        f.write(sql.strip() + "\n")
    try:
        cols, rows = _run(sql)
    except Exception as e:  # noqa: BLE001
        _log("SUBMIT-ERROR", sql, str(e)[:300])
        raise ValueError(f"Submitted query failed: {e}") from None
    if answer_columns is not None:
        names = {c.lower() for c in cols}
        unknown = [c for c in answer_columns if c.strip().lower() not in names]
        if not answer_columns or unknown:
            note = f"answer_columns {unknown or 'empty'} not in the result columns {cols}"
            _log("SUBMIT-ERROR", sql, note)
            raise ValueError(f"Submission not recorded: {note}. Name columns of your query's result.")
        if grain_columns is not None and (unknown := [c for c in grain_columns if c.strip().lower() not in names]):
            note = f"grain_columns {unknown} not in the result columns {cols}"
            _log("SUBMIT-ERROR", sql, note)
            raise ValueError(f"Submission not recorded: {note}. Name columns of your query's result.")
        answer = [c.strip() for c in answer_columns]
        saved = answer if grain_columns is None else {"answer": answer, "grain": [c.strip() for c in grain_columns]}
        with open(os.path.join(OUT, "answer_columns.json"), "w") as f:
            json.dump(saved, f)
    note = f" answer_columns={answer_columns}" if answer_columns else ""
    note += f" grain_columns={grain_columns}" if grain_columns is not None else ""
    _log("SUBMIT", sql, f"rows={len(rows)}{note}")
    return f"Submitted. {len(rows)} rows recorded. Now state your assumptions and stop."


if DECLARE and GRAIN:

    @mcp.tool()
    def submit_sql(sql: str, answer_columns: list[str], grain_columns: list[str]) -> str:
        """Submit your FINAL answer. Saves the query and executes it to confirm it runs.
        answer_columns: the names of the result columns that answer the request; other columns are context.
        grain_columns: the names of the result columns that identify one row of your result (one row per what?);
        an empty list if the result is a single row.
        Call exactly once, when you are confident the query is correct. Then state your assumptions and stop."""
        return _submit(sql, answer_columns, grain_columns)

elif DECLARE:

    @mcp.tool()
    def submit_sql(sql: str, answer_columns: list[str]) -> str:
        """Submit your FINAL answer. Saves the query and executes it to confirm it runs.
        answer_columns: the names of the result columns that answer the request; other columns are context.
        Call exactly once, when you are confident the query is correct. Then state your assumptions and stop."""
        return _submit(sql, answer_columns)

else:

    @mcp.tool()
    def submit_sql(sql: str) -> str:
        """Submit your FINAL answer. Saves the query and executes it to confirm it runs.
        Call exactly once, when you are confident the query is correct. Then state your assumptions and stop."""
        return _submit(sql, None)


if __name__ == "__main__":
    mcp.run()
