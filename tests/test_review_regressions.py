"""Regression checks from the q01 Pi/Codex experiment review."""

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

from evalkit import Agent, Task, grade_sql, grading, harnesses, runner
from evalkit.grading import Num, cell_eq
from sqlguard import refuse


@pytest.fixture
def file_probe(tmp_path):
    path = tmp_path / "probe.csv"
    path.write_text("value\n12345\n")
    # The guard refuses this, so the engine checks below run it unguarded: the engine is the boundary.
    sql = f"SELECT * FROM read_csv_auto('{tmp_path}/' || 'probe' || chr(46) || 'csv')"
    assert refuse(sql) is not None
    return sql


def test_grader_blocks_external_file_read(file_probe, synthetic_db):
    assert grade_sql(file_probe, "SELECT 12345", synthetic_db).verdict == "sql_error"
    with pytest.raises(grading.SQLError, match="disabled"):
        grading.execute(file_probe, synthetic_db, guard=False)


def test_mcp_tool_blocks_external_file_read(file_probe, tmp_path, monkeypatch, synthetic_db):
    pytest.importorskip("mcp.server.fastmcp")
    import mcp_server

    monkeypatch.setattr(mcp_server, "DB", str(synthetic_db))
    monkeypatch.setattr(mcp_server, "OUT", str(tmp_path / "logs"))
    with pytest.raises(ValueError, match="Refused"):
        mcp_server.run_sql(file_probe)
    with pytest.raises(duckdb.Error, match="disabled"):
        mcp_server._run(file_probe)
    assert "12345" not in (tmp_path / "logs" / "queries.log").read_text()


def pi_duckdb(db, sql) -> subprocess.CompletedProcess:
    """Run the duckdb CLI with the argument list the pi extension passes, read from the extension itself."""
    src = (HERE / "extensions" / "duckdb-tools.ts").read_text()
    body = re.search(r"const args = \[(.*?)\];", src, re.DOTALL).group(1)
    tokens = re.findall(r'"([^"]*)"|\b(DB|sqlArg)\b', body)
    args = [lit or {"DB": str(db), "sqlArg": " " + sql}[name] for lit, name in tokens]
    return subprocess.run(["duckdb", *args], capture_output=True, text=True, check=False)


@pytest.mark.skipif(not shutil.which("duckdb"), reason="DuckDB CLI not installed")
def test_pi_duckdb_execution_blocks_external_file_read(file_probe, db):
    # Exercise the exact subprocess boundary used by the Pi extension.
    result = pi_duckdb(db, file_probe)
    assert result.returncode != 0 and "12345" not in result.stdout
    allowed = pi_duckdb(db, "SELECT count(*) FROM lineitem")
    assert allowed.returncode == 0 and allowed.stdout.strip().splitlines()[-1].isdigit()


# ---------- the engine, not the guard, refuses settings changes and extension autoloading ----------

# Without autoload_known_extensions=false, DuckDB would try to install and load tpch to run tpch_queries().
ENGINE_REFUSES = {
    "SET enable_external_access = true": "locked",
    "SET autoload_known_extensions = true": "locked",
    "SET threads = 4": "locked",
    "SELECT * FROM tpch_queries()": "is not in the catalog, but it exists in the tpch extension",
    "CALL dbgen(sf = 0.01)": "is not in the catalog, but it exists in the tpch extension",
}


@pytest.mark.parametrize("sql,error", ENGINE_REFUSES.items())
def test_grader_engine_refuses(sql, error, synthetic_db):
    assert refuse(sql) is not None  # the guard refuses first; the engine must refuse without it
    with pytest.raises(grading.SQLError, match=re.escape(error)):
        grading.execute(sql, synthetic_db, guard=False)


@pytest.mark.parametrize("sql,error", ENGINE_REFUSES.items())
def test_mcp_engine_refuses(sql, error, synthetic_db, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")
    import mcp_server

    monkeypatch.setattr(mcp_server, "DB", str(synthetic_db))
    with pytest.raises(duckdb.Error, match=re.escape(error)):
        mcp_server._run(sql)


@pytest.mark.skipif(not shutil.which("duckdb"), reason="DuckDB CLI not installed")
@pytest.mark.parametrize("sql,error", ENGINE_REFUSES.items())
def test_pi_engine_refuses(sql, error, synthetic_db):
    result = pi_duckdb(synthetic_db, sql)
    assert result.returncode != 0 and error in result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("duckdb"), reason="DuckDB CLI not installed")
def test_pi_engine_settings(synthetic_db):
    names = [
        "lock_configuration",
        "enable_external_access",
        "autoinstall_known_extensions",
        "autoload_known_extensions",
    ]
    sql = "SELECT " + ", ".join(f"current_setting('{n}')::VARCHAR" for n in names)
    result = pi_duckdb(synthetic_db, sql)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1].lower() == "true,false,false,false"


def test_q01_wrong_total_is_not_exact(db):
    target = (HERE / "tpch/queries/q01.sql").read_text()
    wrong = target.replace("sum(l_extendedprice) AS sum_base_price", "sum(l_extendedprice) + 100 AS sum_base_price")
    assert grade_sql(wrong, target, db).verdict == "values_differ"


def test_exact_integer_comparison_survives_float_precision_limit():
    assert not cell_eq(Num(2**53 + 1), Num(2**53))


def test_unordered_match_preserves_small_numeric_differences(synthetic_db):
    target = "SELECT x FROM (VALUES (0.001), (0.002)) t(x) ORDER BY x"
    candidate = target + " DESC"
    assert grade_sql(candidate, target, synthetic_db).verdict == "exact_unordered"


def test_work_dir_does_not_disclose_question_or_batch(tmp_path, monkeypatch, synthetic_db):
    monkeypatch.setattr(runner, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "WORK_ROOT", tmp_path / "work")

    class Probe(harnesses.Harness):
        name = binary = "probe"

        def command(self, ctx):
            assert "q01" not in str(ctx.work_dir) and "tpch" not in str(ctx.work_dir)
            return [sys.executable, "-c", "print('done')"]

        def parse(self, ctx, stdout):
            return harnesses.Usage()

    monkeypatch.setitem(harnesses.HARNESSES, "probe", Probe())
    result = runner.run_one(
        Task.adhoc("Count rows", "SELECT 1", id="q01", db=synthetic_db),
        Agent("probe", "test"),
        batch="tpch-q01-experiment",
        record=False,
    )
    assert not result.usage.error
    meta = json.loads((result.run_dir / "meta.json").read_text())
    assert not Path(meta["work_dir"]).exists()


def test_grid_retains_crashed_attempt_in_denominator(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "SCORECARD", tmp_path / "results/scorecard.csv")
    monkeypatch.setattr(runner, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)

    def crash(*args, **kwargs):
        raise RuntimeError("synthetic runner failure")

    monkeypatch.setattr(runner, "run_one", crash)
    results = runner.run_grid(
        [Task.adhoc("One", "SELECT 1")], ["pi:test"], repeats=2, jobs=2, batch="crash-check", quiet=True
    )
    assert len(results) == 2
    assert all(r.verdict == "harness_error" and not r.correct for r in results)
    assert len(runner.load_scorecard("crash-check")) == 2


def test_agy_usage_does_not_double_count_thinking_or_cache(tmp_path):
    # Public CLI example plus the real SELECT-7 smoke show input+output == total;
    # cache and thinking are subsets, not additional billable tokens.
    log = tmp_path / "transcript.jsonl"
    log.write_text(
        json.dumps(
            {
                "status": "SUCCESS",
                "usage": {
                    "input_tokens": 10415,
                    "output_tokens": 657,
                    "thinking_tokens": 616,
                    "cache_read_tokens": 8113,
                    "total_tokens": 11072,
                },
            }
        )
    )
    usage = harnesses.Antigravity().parse(None, log)
    assert usage.tokens_in == 2302
    assert usage.tokens_out == 657
    assert usage.tokens_in + usage.tokens_cache_read + usage.tokens_out == 11072


def test_grok_usage_counts_only_terminal_aggregate(tmp_path):
    log = tmp_path / "transcript.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(o)
            for o in [
                {"type": "assistant", "message": {"usage": {"input_tokens": 100}}},
                {
                    "type": "result",
                    "result": "Done",
                    "num_turns": 3,
                    "usage": {"input_tokens": 9178, "output_tokens": 311, "cache_read_input_tokens": 8320},
                    "total_cost_usd": 0.024382,
                    "modelUsage": {"grok-4.7-build": {"inputTokens": 9178}},
                },
            ]
        )
    )
    usage = harnesses.Grok().parse(None, log)
    assert usage.tokens_in == 9178 and usage.tokens_cache_read == 8320
    assert usage.tokens_out == 311 and usage.turns == 3
    assert usage.cost_usd == 0.024382


# ---------- pi's tools pass the SQL on stdin: a statement may start with a comment or a dash ----------

LEADING_COMMENT = "-- orders per customer\nSELECT count(*) AS n FROM orders"


@pytest.mark.skipif(not shutil.which("duckdb"), reason="DuckDB CLI not installed")
def test_pi_tools_run_sql_that_starts_with_a_comment(tmp_path, synthetic_db):
    # On 2026-11-07, the CLI read a statement passed as an argument and starting with "--" as an option
    # ("Unknown Option Error: Unrecognized option '- orders per customer ..."): 26 Grok runs in grid v3 hit it.
    from test_ask import _transpiled

    module = _transpiled(tmp_path)
    driver = tmp_path / "driver.mjs"
    driver.write_text(
        f"import ext from {json.dumps(str(module))};\n"
        "const tools = {};\n"
        "ext({ registerTool: (t) => { tools[t.name] = t; } });\n"
        f"const sql = {json.dumps(LEADING_COMMENT)};\n"
        "const out = {};\n"
        "for (const name of ['run_sql', 'submit_sql']) {\n"
        "  try { out[name] = (await tools[name].execute('id', { sql })).content[0].text; }\n"
        "  catch (e) { out[name] = 'ERROR ' + e.message; }\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    path = ":".join(str(Path(shutil.which(b)).parent) for b in ("node", "duckdb"))
    env = {"PATH": path, "AUDIT_DB": str(synthetic_db), "AUDIT_OUT": str(tmp_path / "run")}
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, check=False, env=env)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    expected = duckdb.connect(str(synthetic_db), read_only=True).execute("SELECT count(*) FROM orders").fetchone()[0]
    assert got["run_sql"].startswith(f"n\n{expected}\n"), got["run_sql"]
    assert not got["submit_sql"].startswith("ERROR"), got["submit_sql"]
    assert (tmp_path / "run" / "answer.sql").read_text() == LEADING_COMMENT + "\n"
