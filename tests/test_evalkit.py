"""Grader, question loading and harness command lines. No agent CLI is launched.

Tests that take `db` run on the synthetic database and, when it exists, on tpch.duckdb (see conftest.py).

uv run --with pytest --with duckdb --with 'mcp<2' pytest tests/ -q
"""

import csv
import json
import sys
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from evalkit import (
    Agent,
    Task,
    grade_sql,
    harnesses,
    load_question,
    load_questions,
)
from evalkit.harnesses import RunContext

QUERIES = HERE / "tpch" / "queries"

Q04 = (QUERIES / "q04.sql").read_text()
Q04_BODY = """
SELECT o_orderpriority, count(*) AS order_count
FROM orders
WHERE o_orderdate >= DATE '1993-07-01' AND o_orderdate < DATE '1993-10-01'
  AND EXISTS (SELECT 1 FROM lineitem WHERE l_orderkey = o_orderkey AND l_commitdate < l_receiptdate)
GROUP BY o_orderpriority
"""


@pytest.mark.parametrize("q", sorted(p.stem for p in QUERIES.glob("q*.sql")))
def test_every_target_matches_itself(q, db):
    sql = (QUERIES / f"{q}.sql").read_text()
    assert grade_sql(sql, sql, db).verdict == "exact"


def test_rewrite_with_different_aliases_is_exact(db):
    assert grade_sql(Q04_BODY + "ORDER BY 1", Q04, db).verdict == "exact"


def test_wrong_order_is_unordered_match(db):
    assert grade_sql(Q04_BODY + "ORDER BY 2 DESC", Q04, db).verdict == "exact_unordered"


def test_swapped_columns(db):
    sql = Q04_BODY.replace("SELECT o_orderpriority, count(*) AS order_count", "SELECT count(*) AS n, o_orderpriority")
    g = grade_sql(sql + "ORDER BY 2", Q04, db)
    assert (g.verdict, g.column_map, g.correct) == ("columns_reordered", [1, 0], True)


def test_extra_column_is_correct_and_noted(db):
    # An extra column does not change the answer: correct, with the verdict kept so reports can note it.
    sql = Q04_BODY.replace("count(*) AS order_count", "count(*) AS order_count, 'x' AS note")
    g = grade_sql(sql + "ORDER BY 1", Q04, db)
    assert (g.verdict, g.correct) == ("extra_columns", True)
    # A missing column is not: part of the answer is absent.
    sql = Q04_BODY.replace(", count(*) AS order_count", "")
    assert grade_sql(sql + "ORDER BY 1", Q04, db).correct is False


def test_fan_out_join_is_values_differ(db):
    sql = """SELECT o_orderpriority, count(*) FROM orders JOIN lineitem ON l_orderkey = o_orderkey
             WHERE o_orderdate >= DATE '1993-07-01' AND o_orderdate < DATE '1993-10-01' AND l_commitdate < l_receiptdate
             GROUP BY 1 ORDER BY 1"""
    g = grade_sql(sql, Q04, db)
    assert g.verdict == "values_differ" and "row 0" in g.detail


def test_missing_rows(db):
    assert grade_sql(Q04_BODY + "ORDER BY 1 LIMIT 3", Q04, db).verdict == "rows_differ"


def test_rounding_within_tolerance(db):
    q14 = (QUERIES / "q14.sql").read_text()
    inner = q14.split("SELECT", 1)[1]
    rounded = (
        "SELECT round("
        + inner.split(" AS promo_revenue")[0]
        + ", 2) AS promo_revenue"
        + inner.split(" AS promo_revenue", 1)[1]
    )
    assert grade_sql(rounded, q14, db).verdict == "exact"


def test_sql_error_and_blocklist(db):
    assert grade_sql("SELEC 1", Q04, db).verdict == "sql_error"
    assert grade_sql("SELECT * FROM tpch_answers()", Q04, db).verdict == "sql_error"
    assert grade_sql("", Q04, db).verdict == "no_sql"


def test_question_files_load_in_ladder_order(db, questions_on):
    tasks = load_questions(questions_on(db))
    assert {t.id for t in tasks} >= {"q04", "q13", "q21"}
    q21 = [t for t in tasks if t.id == "q21"]
    assert [t.level for t in q21] == ["terse", "stakeholder", "spec"]
    assert [t.rank for t in q21] == [0, 1, 2]
    for t in tasks:
        t.check_leaks()
        assert grade_sql(t.target_sql, t.target_sql, db).verdict == "exact"


def test_level_filter_and_unknown_level(synthetic_db, questions_on):
    q04 = questions_on(synthetic_db, "q04") / "q04.toml"
    assert [t.level for t in load_question(q04, ["spec", "terse"])] == ["terse", "spec"]
    with pytest.raises(ValueError):
        load_question(q04, ["nope"])


def test_task_requires_target_and_flags_leaks():
    with pytest.raises(ValueError):
        Task.adhoc("How many orders?", target_sql=" ")
    with pytest.raises(ValueError):
        Task.adhoc("Run TPC-H Q04 for me", target_sql="SELECT 1").check_leaks()
    Task.adhoc("How many late orders in Q3 1993?", target_sql="SELECT 1").check_leaks()  # a quarter is fine


def test_agent_spec_parsing():
    a = Agent.parse("pi:openai/gpt-5.6-luna:high")
    assert (a.harness, a.model, a.slug) == ("pi", "openai/gpt-5.6-luna:high", "pi-openai-gpt-5.6-luna-high")
    with pytest.raises(ValueError):
        Agent.parse("gpt-5.6-luna")
    with pytest.raises(ValueError):
        Agent.parse("cursor:x")


def test_sql_from_final_message():
    text = "First try:\n```sql\nSELECT 1\n```\nFinal:\n```SQL\nSELECT 2;\n```\nAssumptions..."
    assert harnesses.sql_from_text(text) == "SELECT 2;"
    assert harnesses.sql_from_text("no code here") is None


@pytest.mark.parametrize("name", sorted(harnesses.HARNESSES))
def test_commands_lock_down_tools(name, tmp_path):
    work, run = tmp_path / "work", tmp_path / "run"
    work.mkdir(), run.mkdir()
    notes = work / "AGENTS.md"
    notes.write_text("notes")
    ctx = RunContext(
        prompt="-- a prompt that starts with dashes",
        model="m",
        work_dir=work,
        run_dir=run,
        db=runner.DB,
        notes=notes,
        max_turns=7,
        timeout_s=60,
    )
    h = harnesses.get(name)
    if name == "agy":
        h.ensure_project = lambda: None  # never touch ~/.gemini from a test
    h.prepare(ctx)
    cmd = h.command(ctx)
    assert cmd[0] == h.binary and ctx.prompt in cmd
    joined = " ".join(cmd)
    if name == "pi":
        assert "--no-builtin-tools" in cmd and "--no-context-files" in cmd and cmd[-2] == "--"
    if name == "claude":
        assert (
            cmd[cmd.index("--tools") + 1] == ""
            and "--strict-mcp-config" in cmd
            and "--append-system-prompt-file" in cmd
        )
    if name == "codex":
        assert "shell_tool" in cmd and "read-only" in cmd and 'default_tools_approval_mode="approve"' in joined
    if name == "agy":
        assert (work / ".agents" / "plugins" / "audit" / "mcp_config.json").exists() and "--project" in cmd


def test_missing_columns(db):
    sql = Q04_BODY.replace("SELECT o_orderpriority, count(*) AS order_count", "SELECT o_orderpriority")
    g = grade_sql(sql + "ORDER BY 1", Q04, db)
    assert (g.verdict, g.correct) == ("missing_columns", False) and "order_count" in g.detail


# ---------- runner, with a fake harness that "answers" by writing what the tools would write ----------

import evals
from evalkit import runner


class FakeHarness(harnesses.Harness):
    """Logs one query with a SQL comment in it, then submits (or only prints) the given SQL."""

    name = binary = "fake"

    def __init__(self, sql, submit=True):
        self.sql, self.submit = sql, submit

    def available(self):
        return True

    def command(self, ctx):
        script = (
            "import os, pathlib, sys\n"
            "out = pathlib.Path(os.environ['AUDIT_OUT'])\n"
            "(out / 'tool_db.txt').write_text(os.environ['AUDIT_DB'])\n"
            "sql = sys.argv[1]\n"
            "log = '-- 2026-10-25T00:00:00+00:00 RUN rows=1\\n-- a comment\\nSELECT 1\\n;\\n\\n'\n"
            "if sys.argv[2] == '1':\n"
            "    (out / 'answer.sql').write_text(sql)\n"
            "    log += '-- 2026-10-25T00:00:01+00:00 SUBMIT rows=5\\n' + sql + '\\n;\\n\\n'\n"
            "(out / 'queries.log').write_text(log)\n"
            "print('{\"result\": \"done\\\\n```sql\\\\n' + sql.replace('\\n', ' ') + '\\\\n```\"}')\n"
        )
        return [sys.executable, "-c", script, self.sql, "1" if self.submit else "0"]

    def parse(self, ctx, stdout):
        import json

        return harnesses.Usage(final_text=json.loads(stdout.read_text())["result"], cost_usd=0.001)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "SCORECARD", tmp_path / "results" / "scorecard.csv")
    monkeypatch.setattr(runner, "WORK_ROOT", tmp_path / "work")
    return tmp_path


def _with_fake(monkeypatch, harness):
    monkeypatch.setitem(harnesses.HARNESSES, "fake", harness)


def test_run_one_submitted_answer(sandbox, monkeypatch, db):
    _with_fake(monkeypatch, FakeHarness(Q04_BODY + "ORDER BY 1"))
    t = Task.adhoc("How many late orders by priority?", target_sql=Q04, db=db)
    r = runner.run_one(t, "fake:m", batch="b", record=True)
    assert (r.verdict, r.correct, r.sql_source, r.queries_run, r.submits) == ("exact", True, "submit", 1, 1)
    assert (r.run_dir / "result.json").exists() and not (sandbox / "work" / "b" / r.run_dir.name).exists()
    rows = runner.load_scorecard("b")
    assert len(rows) == 1 and rows[0]["correct"] == "1"


def test_unsubmitted_sql_is_graded_but_never_correct(sandbox, monkeypatch, db):
    _with_fake(monkeypatch, FakeHarness(Q04_BODY + "ORDER BY 1", submit=False))
    r = runner.run_one(
        Task.adhoc("How many late orders by priority?", target_sql=Q04, db=db), "fake:m", batch="b", record=False
    )
    assert (r.verdict, r.correct, r.lenient, r.sql_source) == ("exact", False, True, "final_message")


def test_run_grid_parallel_repeats_and_summary(sandbox, monkeypatch, db, questions_on):
    _with_fake(monkeypatch, FakeHarness(Q04_BODY + "ORDER BY 2 DESC"))
    tasks = load_question(questions_on(db, "q04") / "q04.toml")
    res = runner.run_grid(
        tasks, ["fake:a", "fake:b"], conditions=["bare", "notes"], repeats=3, jobs=8, batch="g", quiet=True
    )
    assert len(res) == 3 * 2 * 2 * 3 and all(r.verdict == "exact_unordered" and r.correct for r in res)
    assert len({r.run_dir for r in res}) == len(res)
    summary = (sandbox / "results" / "g" / "summary.txt").read_text()
    header = summary.splitlines()[0].split()
    assert header[1:4] == ["terse", "stakeholder", "spec"] and "3/3" in summary
    assert not (sandbox / "work" / "g").exists()
    manifest = json.loads((sandbox / "results" / "g" / "manifest.json").read_text())
    assert manifest["planned_runs"] == manifest["recorded_runs"] == len(res)
    assert [a["label"] for a in manifest["agents"]] == ["fake:a", "fake:b"]
    assert {(t["id"], t["level"]) for t in manifest["tasks"]} == {
        ("q04", lv) for lv in ("terse", "stakeholder", "spec")
    }
    assert all(t["prompt_sha"] for t in manifest["tasks"]) and "completed_at" in manifest
    assert any(k.startswith("db:") for k in manifest["sha256"]) and "evalkit/grading.py" in manifest["sha256"]


class FlakyHarness(FakeHarness):
    """Fails its first `failures` runs the way a rate-limited provider does, then answers normally."""

    def __init__(self, sql, failures):
        super().__init__(sql)
        self.failures = failures

    def command(self, ctx):
        if self.failures > 0:
            self.failures -= 1
            return [sys.executable, "-c", 'import sys; print(\'{"error": "Rate limit exceeded"}\'); sys.exit(1)']
        return super().command(ctx)

    def parse(self, ctx, stdout):
        body = json.loads(stdout.read_text())
        if "error" in body:
            return harnesses.Usage(error=f"OpenAI API error (429): {body['error']}")
        return super().parse(ctx, stdout)


def test_resume_reruns_provider_failures_and_unfinished_runs(sandbox, monkeypatch, db, questions_on):
    _with_fake(monkeypatch, FlakyHarness(Q04_BODY + "ORDER BY 1", failures=2))
    qdir = questions_on(db, "q04")
    tasks = load_question(qdir / "q04.toml", ["spec"])
    res = runner.run_grid(tasks, ["fake:a"], repeats=3, jobs=1, batch="flaky", quiet=True, timeout_s=60)
    assert sorted(r.verdict for r in res) == ["exact", "harness_error", "harness_error"]
    batch_dir = sandbox / "results" / "flaky"
    (ok,) = [r for r in res if r.verdict == "exact"]
    (ok.run_dir / "result.json").unlink()  # as if the batch was stopped mid-run
    runner.resume("flaky", jobs=1, questions=[qdir], quiet=True)
    rows = [json.loads(line) for line in (batch_dir / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and all(r["verdict"] == "exact" for r in rows)
    assert len(list((batch_dir / "_replaced").iterdir())) == 3
    (entry,) = json.loads((batch_dir / "manifest.json").read_text())["resumes"]
    assert entry["rerun_never_finished"] == 1 and entry["rerun_provider_failures"] == {"fake:a": 2}
    # The scorecard keeps one row per run: the re-run rows replace the failed attempts.
    with open(runner.SCORECARD) as f:
        card = [r for r in csv.DictReader(f) if r["batch"] == "flaky"]
    assert len(card) == 3 and all(r["verdict"] == "exact" for r in card)
    # A model outcome is never re-run: a second resume finds nothing to do.
    assert runner.resume("flaky", jobs=1, questions=[qdir], quiet=True) == []


def test_resume_can_wait_on_one_agent(sandbox, monkeypatch, db, questions_on):
    _with_fake(monkeypatch, FakeHarness(Q04_BODY + "ORDER BY 1"))
    qdir = questions_on(db, "q04")
    tasks = load_question(qdir / "q04.toml", ["spec"])
    res = runner.run_grid(tasks, ["fake:a", "fake:b"], batch="split", quiet=True)
    for r in res:
        (r.run_dir / "result.json").unlink()  # both agents unfinished; only a's quota has reset
    (done,) = runner.resume("split", agents=["fake:a"], questions=[qdir], quiet=True)
    assert str(done.agent) == "fake:a"
    (waiting,) = [r for r in res if str(r.agent) == "fake:b"]  # results come back in completion order
    assert not (waiting.run_dir / "result.json").exists()
    assert json.loads((sandbox / "results" / "split" / "manifest.json").read_text())["resumes"][0]["agents"] == [
        "fake:a"
    ]
    with pytest.raises(ValueError, match="not in batch"):
        runner.resume("split", agents=["fake:c"], questions=[qdir], quiet=True)


class CutShortHarness(FakeHarness):
    """Runs (and maybe submits) like FakeHarness, then reports the provider error a session ends on."""

    def parse(self, ctx, stdout):
        usage = super().parse(ctx, stdout)
        usage.error = 'OpenAI API error (429): 429 {"detail":"Rate limit exceeded"}'
        return usage


def test_provider_error_before_submit_is_a_harness_error(sandbox, monkeypatch, db):
    t = Task.adhoc("How many late orders by priority?", target_sql=Q04, db=db)
    _with_fake(monkeypatch, CutShortHarness("SELECT 1", submit=False))
    r = runner.run_one(t, "fake:m", batch="cut", repeat=1)
    assert (r.verdict, r.correct, r.lenient) == ("harness_error", False, False) and "Rate limit" in r.detail
    # An answer submitted before the provider failed is still the agent's answer.
    _with_fake(monkeypatch, CutShortHarness(Q04_BODY + "ORDER BY 1"))
    r = runner.run_one(t, "fake:m", batch="cut", repeat=2)
    assert (r.verdict, r.correct) == ("exact", True)


def test_resume_reruns_runs_graded_after_a_provider_cut(sandbox, monkeypatch, db, questions_on):
    _with_fake(monkeypatch, FakeHarness(Q04_BODY + "ORDER BY 1"))
    qdir = questions_on(db, "q04")
    (r,) = runner.run_grid(load_question(qdir / "q04.toml", ["spec"]), ["fake:a"], batch="old", quiet=True)
    # As recorded before the rule existed: graded on an exploratory query although the API had failed.
    row = json.loads((r.run_dir / "result.json").read_text())
    row |= {"verdict": "rows_differ", "correct": False, "sql_source": "last_query", "error": "Rate limit exceeded"}
    (r.run_dir / "result.json").write_text(json.dumps(row))
    runner.resume("old", questions=[qdir], quiet=True)
    assert json.loads((r.run_dir / "result.json").read_text())["verdict"] == "exact"


def test_resume_reruns_quota_failures_recorded_without_detail(sandbox, monkeypatch, db, questions_on):
    _with_fake(monkeypatch, FakeHarness(Q04_BODY + "ORDER BY 1"))
    qdir = questions_on(db, "q04")
    (r,) = runner.run_grid(load_question(qdir / "q04.toml", ["spec"]), ["fake:a"], batch="quota", quiet=True)
    # As the old agy parser recorded it: the quota message was in the transcript but not in the result.
    quota = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h6m."
    (r.run_dir / "transcript.jsonl").write_text(json.dumps({"status": "ERROR", "error": quota, "usage": {}}))
    row = json.loads((r.run_dir / "result.json").read_text())
    row |= {"verdict": "harness_error", "correct": False, "harness": "agy", "detail": "status=ERROR"}
    (r.run_dir / "result.json").write_text(json.dumps(row))
    assert harnesses.get("agy").parse(None, r.run_dir / "transcript.jsonl").error == f"status=ERROR: {quota}"
    runner.resume("quota", questions=[qdir], quiet=True)
    assert json.loads((r.run_dir / "result.json").read_text())["verdict"] == "exact"


def test_rescore_applies_the_current_rule_and_keeps_the_old_score(sandbox):
    batch = sandbox / "results" / "rs"
    rows = {
        "extra": {"verdict": "extra_columns", "sql_source": "submit", "correct": 0, "lenient": 1},
        "extra-unsubmitted": {"verdict": "extra_columns", "sql_source": "last_query", "correct": 0, "lenient": 1},
        "exact": {"verdict": "exact", "sql_source": "submit", "correct": 1, "lenient": 1},
    }
    for name, row in rows.items():
        (batch / name).mkdir(parents=True)
        full = {k: "" for k in runner.COLS} | row | {"batch": "rs", "run_dir": f"results/rs/{name}"}
        (batch / name / "result.json").write_text(json.dumps(full))
        runner.append_scorecard(full)
    (batch / "manifest.json").write_text(json.dumps({"batch": "rs"}))
    assert runner.rescore("rs", quiet=True) == 1  # only the submitted extra-column run changes
    extra = json.loads((batch / "extra" / "result.json").read_text())
    assert (extra["correct"], extra["rescored"][0]["correct"]) == (1, 0)
    assert json.loads((batch / "extra-unsubmitted" / "result.json").read_text())["correct"] == 0
    assert {r["run_dir"]: r["correct"] for r in runner.load_scorecard("rs")}["results/rs/extra"] == "1"
    (entry,) = json.loads((batch / "manifest.json").read_text())["rescored"]
    assert entry["changed"] == {"extra_columns": 1} and "extra_columns" in entry["correct"]
    assert runner.rescore("rs", quiet=True) == 0  # idempotent


def test_reparse_fixes_agy_usage_from_the_transcript(sandbox):
    run = sandbox / "results" / "agyb" / "q01-spec-bare-agy-gemini-4-argon-r1"
    run.mkdir(parents=True)
    usage = {"input_tokens": 20580, "cache_read_tokens": 36109, "output_tokens": 1479, "total_tokens": 22059}
    (run / "transcript.jsonl").write_text(
        json.dumps({"status": "SUCCESS", "response": "done", "num_turns": 1, "usage": usage})
    )
    # As recorded by the old parser: cache subtracted from an input count that never included it.
    (run / "result.json").write_text(
        json.dumps(
            {"harness": "agy", "model": "gemini-4-argon-medium", "verdict": "exact", "tokens_in": -15529, "turns": 1}
        )
    )
    (run.parent / "manifest.json").write_text(json.dumps({"batch": "agyb"}))
    # The scorecard row written at run time carries the old usage; another batch's row is left alone.
    rd = "results/agyb/q01-spec-bare-agy-gemini-4-argon-r1"
    other = {k: "" for k in runner.COLS} | {"batch": "other", "run_dir": "results/other/x", "cost_usd": "9.0"}
    runner.append_scorecard({k: "" for k in runner.COLS} | {"batch": "agyb", "run_dir": rd, "tokens_in": "-15529"})
    runner.append_scorecard(other)
    result = json.loads((run / "result.json").read_text()) | {"run_dir": rd}
    (run / "result.json").write_text(json.dumps(result))
    assert runner.reparse("agyb", quiet=True) == 1
    card = {r["run_dir"]: r for r in runner.load_scorecard()}
    assert card[rd]["tokens_in"] == "20580" and card[rd]["verdict"] == "exact"
    assert card["results/other/x"]["cost_usd"] == "9.0"
    row = json.loads((run / "result.json").read_text())
    assert (row["tokens_in"], row["tokens_cache_read"], row["turns"], row["verdict"]) == (20580, 36109, "", "exact")
    assert row["usage_extra"]["reported_total_tokens"] == 22059
    assert json.loads((run.parent / "manifest.json").read_text())["reparsed"][0]["runs"] == 1


def test_resume_refuses_edited_prompts(sandbox, monkeypatch, db, questions_on):
    _with_fake(monkeypatch, FakeHarness(Q04_BODY + "ORDER BY 1"))
    qdir = questions_on(db, "q04")
    runner.run_grid(load_question(qdir / "q04.toml", ["spec"]), ["fake:a"], batch="edited", quiet=True)
    f = qdir / "q04.toml"
    f.write_text(f.read_text().replace("Question from the operations team:", "Question from ops:"))
    with pytest.raises(ValueError, match="prompt changed"):
        runner.resume("edited", questions=[qdir], quiet=True)


def test_progress_reads_running_batches(tmp_path):
    import os
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

    def batch(name, planned, done, correct, started_min_ago, finished=False, quiet_min=0):
        d = tmp_path / name
        d.mkdir()
        m = {
            "batch": name,
            "planned_runs": planned,
            "created_at": (now - timedelta(minutes=started_min_ago)).isoformat(),
        }
        if finished:
            m["completed_at"] = now.isoformat()
        (d / "manifest.json").write_text(json.dumps(m))
        for i in range(done):
            r = d / f"run{i}"
            r.mkdir()
            (r / "result.json").write_text(json.dumps({"correct": i < correct}))
            t = (now - timedelta(minutes=quiet_min)).timestamp()
            os.utime(r / "result.json", (t, t))

    batch("b-live", planned=100, done=40, correct=30, started_min_ago=10)
    batch("b-done", planned=10, done=10, correct=9, started_min_ago=60, finished=True)
    batch("b-stuck", planned=50, done=5, correct=5, started_min_ago=90, quiet_min=60)
    out = runner.progress(results=tmp_path, now=now)
    live = next(line for line in out.splitlines() if line.startswith("b-live"))
    assert "40/100" in live and "40.0%" in live and " 30 " in live and "15 min" in live and live.endswith("running")
    assert "b-done" not in out  # finished batches only when named
    assert "no new run for 60 min" in out
    assert "done" in runner.progress(["b-done"], results=tmp_path, now=now).splitlines()[1]
    # A resume makes a finished batch show as running again, paced from when the resume started.
    batch("b-resume", planned=100, done=94, correct=80, started_min_ago=600, finished=True)
    m = json.loads((tmp_path / "b-resume" / "manifest.json").read_text())
    m["resuming"] = {"started_at": (now - timedelta(minutes=2)).isoformat(), "runs_at_start": 90, "rerunning": 10}
    (tmp_path / "b-resume" / "manifest.json").write_text(json.dumps(m))
    resume = next(line for line in runner.progress(results=tmp_path, now=now).splitlines() if "b-resume" in line)
    assert "94/100" in resume and " 2.0 " in resume and "3 min" in resume and resume.endswith("resuming")


def test_dry_run_and_agents_json(sandbox, monkeypatch, db, questions_on, tmp_path):
    fake = FakeHarness(Q04_BODY)
    _with_fake(monkeypatch, fake)
    agents = tmp_path / "agents.json"
    agents.write_text(
        json.dumps([{"harness": "fake", "model": "m", "extra_args": ["--thinking", "medium"], "label": "fake:m@med"}])
    )
    q04 = str(questions_on(db, "q04") / "q04.toml")
    assert evals.main(["run", q04, "--agents-json", str(agents), "--name", "dry", "--dry-run"]) == 0
    assert not list((sandbox / "results").glob("*dry*"))  # nothing ran, nothing written
    assert evals.main(["run", q04, "-l", "spec", "--agents-json", str(agents), "--name", "wet"]) == 0
    (batch,) = (sandbox / "results").glob("*wet")
    manifest = json.loads((batch / "manifest.json").read_text())
    assert manifest["agents"][0]["extra_args"] == ["--thinking", "medium"] and manifest["recorded_runs"] == 1
    with pytest.raises(SystemExit):
        evals.main(["run", q04, "--agents", "fake:m", "fake:m"])  # duplicate labels would share run dirs


def test_summary_splits_edited_prompts():
    base = {k: "" for k in runner.COLS} | {
        "task": "q",
        "condition": "bare",
        "agent": "x:y",
        "correct": "1",
        "lenient": "1",
        "verdict": "exact",
        "rank": "0",
        "level": "spec",
    }
    out = runner.summarize([base | {"prompt_sha": "aaaaaaaaaa"}, base | {"prompt_sha": "bbbbbbbbbb"}])
    assert "spec@aaaaaaa" in out and "spec@bbbbbbb" in out
    # A harness error is not a miss: it is left out of k/n and counted below the table.
    quota = base | {"correct": "0", "lenient": "0", "verdict": "harness_error"}
    out = runner.summarize([base, quota, quota])
    assert "1/1" in out and "1/1 correct" in out and "left out 2 harness errors" in out


# ---------- per-question databases ----------


@pytest.fixture
def tiny_db(tmp_path):
    import duckdb

    path = tmp_path / "tiny.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE pets AS SELECT * FROM (VALUES ('cat', 3), ('dog', 5)) t(kind, n)")
    con.close()
    return path


def test_question_file_db_resolves_relative_to_the_file(tmp_path, tiny_db):
    qdir = tmp_path / "questions" / "pets"
    qdir.mkdir(parents=True)
    (qdir / "p1.toml").write_text(
        'id = "p1"\ndb = "../../tiny.duckdb"\ntarget_sql = "SELECT kind, n FROM pets ORDER BY 1"\n'
        "[[levels]]\nname = \"terse\"\nprompt = '''How many of each pet?'''\n"
    )
    (t,) = load_questions(tmp_path / "questions")  # recursive: questions/<dataset>/*.toml
    assert t.db == tiny_db.resolve()
    assert runner.resolve_db(t, runner.DB) == tiny_db.resolve()  # the file's db beats the caller's default
    (qdir / "p1.toml").write_text((qdir / "p1.toml").read_text().replace("tiny.duckdb", "nope.duckdb"))
    with pytest.raises(ValueError, match="does not exist"):
        load_questions(qdir)


def test_every_level_shares_the_tool_preamble():
    # Levels may differ only in the business brief: tool and submission instructions come from preamble.md.
    preamble = (HERE / "questions" / "tpch" / "preamble.md").read_text().strip()
    assert "`run_sql`" in preamble and "`submit_sql`" in preamble
    for f in sorted((HERE / "questions" / "tpch").glob("*.toml")):
        for lv in tomllib.loads(f.read_text())["levels"]:
            assert "run_sql" not in lv["prompt"] and "submit_sql" not in lv["prompt"], f"{f.name}:{lv['name']}"


def test_novel_questions_share_the_preamble_and_catch_their_traps(db):
    if db.resolve() != runner.DB.resolve():
        pytest.skip("novel questions point at the real tpch.duckdb")
    tasks = {t.id: t for t in load_questions(HERE / "questions" / "tpch-novel", levels=["spec"])}
    assert sorted(tasks) == [f"n0{i}" for i in range(1, 7)]
    preamble = (HERE / "questions" / "tpch" / "preamble.md").read_text().strip()
    for t in tasks.values():
        t.check_leaks()
        assert t.prompt.startswith(preamble)
        assert grade_sql(t.target_sql, t.target_sql, db).verdict == "exact"
    # The tempting first attempts must grade wrong: a fan-out join, and an average over buyers only.
    fanout = (
        "SELECT year(o_orderdate), count(*), sum(o_totalprice) FROM orders JOIN lineitem ON l_orderkey = o_orderkey "
        "JOIN part ON p_partkey = l_partkey WHERE p_brand = 'Brand#13' AND o_orderdate >= DATE '1993-01-01' "
        "AND o_orderdate < DATE '1998-01-01' GROUP BY 1 ORDER BY 1"
    )
    buyers_only = (
        "SELECT c_mktsegment, count(*) / count(DISTINCT o_custkey) FROM orders JOIN customer ON c_custkey = o_custkey "
        "WHERE year(o_orderdate) = 1995 GROUP BY 1 ORDER BY 1"
    )
    assert not grade_sql(fanout, tasks["n01"].target_sql, db).correct
    assert not grade_sql(buyers_only, tasks["n05"].target_sql, db).correct


def test_hard_questions_reject_their_shortcuts(db):
    if db.resolve() != runner.DB.resolve():
        pytest.skip("hard questions point at the real tpch.duckdb")
    tasks = {t.id: t for t in load_questions(HERE / "questions" / "tpch-hard", levels=["spec"])}
    assert sorted(tasks) == [f"h0{i}" for i in range(1, 7)]
    for t in tasks.values():
        t.check_leaks()
        assert grade_sql(t.target_sql, t.target_sql, db).verdict == "exact"
    shortcuts = {
        "h02": tasks["h02"].target_sql.replace("o_orderdate - prev > 30", "o_orderdate - prev >= 30"),
        "h04": tasks["h04"].target_sql.replace(
            "  GROUP BY o_orderkey, o_custkey\n  HAVING count(*) FILTER (WHERE l_returnflag = 'R') = 0)",
            "    AND l_returnflag <> 'R'\n  GROUP BY o_orderkey, o_custkey)",
        ),
        "h05": tasks["h05"].target_sql.replace(
            "  WHERE (month(o_orderdate) < 8 OR (month(o_orderdate) = 8 AND day(o_orderdate) <= 2))\n", ""
        ),
        "h06": tasks["h06"].target_sql.replace("rank() OVER", "dense_rank() OVER"),
    }
    for qid, sql in shortcuts.items():
        assert sql != tasks[qid].target_sql, qid
        assert not grade_sql(sql, tasks[qid].target_sql, db).correct, qid


def test_grain_questions_reject_their_grain_mistakes(db):
    if db.resolve() != runner.DB.resolve():
        pytest.skip("grain questions point at the real tpch.duckdb")
    tasks = {t.id: t for t in load_questions(HERE / "questions" / "tpch-grain", levels=["spec"])}
    assert sorted(tasks) == [f"g0{i}" for i in range(1, 7)]
    for t in tasks.values():
        t.check_leaks()
        assert grade_sql(t.target_sql, t.target_sql, db).verdict == "exact"
    # Each question's tempting answer at the wrong grain: averaging over lines, over orders instead of customers,
    # counting line items, averaging shares, dividing by 52 weeks, multiplying stock through a join.
    mistakes = {
        "g01": """
SELECT o_orderpriority, avg(o_totalprice) FROM orders JOIN lineitem ON l_orderkey = o_orderkey
JOIN part ON p_partkey = l_partkey WHERE p_brand = 'Brand#21' AND year(o_orderdate) = 1996 GROUP BY 1 ORDER BY 1
""",
        "g02": """
SELECT r_name, avg(o_totalprice) FROM orders JOIN customer ON c_custkey = o_custkey
JOIN nation ON n_nationkey = c_nationkey JOIN region ON r_regionkey = n_regionkey
WHERE year(o_orderdate) = 1997 GROUP BY 1 ORDER BY 1
""",
        "g03": """
SELECT n_name, count(*) FROM orders JOIN lineitem ON l_orderkey = o_orderkey JOIN part ON p_partkey = l_partkey
JOIN customer ON c_custkey = o_custkey JOIN nation ON n_nationkey = c_nationkey
WHERE p_brand = 'Brand#45' AND year(o_orderdate) = 1996 GROUP BY 1 ORDER BY 1
""",
        "g04": """
SELECT r_name, count(*) FILTER (WHERE l_returnflag = 'R') / count(*) FROM lineitem JOIN orders ON o_orderkey = l_orderkey
JOIN customer ON c_custkey = o_custkey JOIN nation ON n_nationkey = c_nationkey JOIN region ON r_regionkey = n_regionkey
WHERE year(o_orderdate) = 1994 GROUP BY 1 ORDER BY 1
""",
        "g05": """
SELECT dayname(o_orderdate), count(*) / 52.0 FROM orders WHERE year(o_orderdate) = 1997
GROUP BY 1, isodow(o_orderdate) ORDER BY isodow(o_orderdate)
""",
        "g06": """
SELECT sum(ps_availqty) FROM partsupp JOIN supplier ON s_suppkey = ps_suppkey JOIN nation ON n_nationkey = s_nationkey
JOIN lineitem ON l_partkey = ps_partkey JOIN orders ON o_orderkey = l_orderkey
WHERE n_name = 'GERMANY' AND year(o_orderdate) = 1995
""",
    }
    for qid, sql in mistakes.items():
        assert not grade_sql(sql, tasks[qid].target_sql, db).correct, qid


def test_preamble_prefixes_each_level_unless_raw(tmp_path, tiny_db):
    qdir = tmp_path / "questions" / "pets"
    qdir.mkdir(parents=True)
    (qdir / "preamble.md").write_text("Use the tools.\n")
    (qdir / "p1.toml").write_text(
        f'id = "p1"\ndb = "{tiny_db}"\ntarget_sql = "SELECT kind, n FROM pets ORDER BY 1"\n'
        "[[levels]]\nname = \"terse\"\nprompt = '''How many of each pet?'''\n"
        "[[levels]]\nname = \"bare\"\nraw = true\nprompt = '''Just the brief.'''\n"
    )
    terse, bare = load_questions(qdir)
    assert terse.prompt == "Use the tools.\n\nHow many of each pet?"
    assert bare.prompt == "Just the brief."
    (qdir / "p1.toml").write_text('preamble_file = "missing.md"\n' + (qdir / "p1.toml").read_text())
    with pytest.raises(ValueError, match="preamble_file"):
        load_questions(qdir)


def test_tpch_questions_name_their_db():
    # Read the files directly: load_questions refuses a db that does not exist, and this must hold without one.
    files = sorted((HERE / "questions" / "tpch").glob("*.toml"))
    assert len(files) == 22
    for f in files:
        assert (f.parent / tomllib.loads(f.read_text())["db"]).resolve() == runner.DB.resolve()


def test_run_on_a_task_specific_db(sandbox, monkeypatch, tiny_db):
    _with_fake(monkeypatch, FakeHarness("SELECT kind, n FROM pets ORDER BY n DESC"))
    t = Task.adhoc("How many of each pet?", target_sql="SELECT kind, n FROM pets ORDER BY 1", db=tiny_db)
    (r,) = runner.run_grid([t], ["fake:m"], batch="pets", quiet=True)
    assert (r.verdict, r.correct) == ("exact_unordered", True)
    import json

    assert json.loads((r.run_dir / "meta.json").read_text())["task"]["db"] == str(tiny_db.resolve())
    assert (r.run_dir / "tool_db.txt").read_text() == str(tiny_db.resolve())  # the agent's tools see the same db


# ---------- numeric tolerance follows the candidate's precision ----------

Q08 = (QUERIES / "q08.sql").read_text()


def test_unrounded_wrong_small_value_fails(db):
    # q08 is a market share near 0.03; a fixed 0.01 tolerance would wave through a 10% error.
    wrong = f"SELECT o_year, mkt_share * 0.9 FROM ({Q08.strip().rstrip(';')})"
    assert grade_sql(wrong, Q08, db).verdict == "values_differ"


def test_rounded_share_matches_and_coarser_rounding_does_not(db):
    two = f"SELECT o_year, round(mkt_share, 4) FROM ({Q08.strip().rstrip(';')})"
    assert grade_sql(two, Q08, db).verdict == "exact"
    q17 = (QUERIES / "q17.sql").read_text().strip().rstrip(";")
    assert grade_sql(f"SELECT round(avg_yearly, 2) FROM ({q17})", q17, db).verdict == "exact"
    assert grade_sql(f"SELECT round(avg_yearly, 0) FROM ({q17})", q17, db).verdict == "values_differ"


def test_decimal_scale_sets_the_allowance():
    import decimal

    from evalkit.grading import Num, cell_eq

    assert Num(decimal.Decimal("5.00")).places == 2 and Num(0.25).places == 2 and Num(12).places == 0
    assert cell_eq(Num(decimal.Decimal("5.00")), Num(5.004)) and not cell_eq(Num(decimal.Decimal("5.00")), Num(5.006))


# ---------- cost: the evaluated model's own cost, at list price where the CLI has none ----------


def _claude_transcript(tmp_path, model, basis, cost):
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps(
            {
                "result": "done",
                "num_turns": 6,
                "total_cost_usd": cost + 0.001196,
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 48633,
                    "cache_creation_input_tokens": 8124,
                    "output_tokens": 1228,
                    "cache_creation": {"ephemeral_1h_input_tokens": 8124, "ephemeral_5m_input_tokens": 0},
                },
                "modelUsage": {
                    "claude-haiku-4-5-20251001": {"inputTokens": 1111, "outputTokens": 17, "costUSD": 0.001196},
                    model: {
                        "inputTokens": 10,
                        "outputTokens": 1228,
                        "cacheReadInputTokens": 48633,
                        "cacheCreationInputTokens": 8124,
                        "costUSD": cost,
                        "costBasis": basis,
                    },
                },
            }
        )
    )
    return path


def test_claude_cost_is_the_evaluated_model_at_list_price_when_unknown(tmp_path):
    from types import SimpleNamespace

    h = harnesses.get("claude")
    # As recorded on 2026-10-31: Claude Code had no price for Haiku 5.5 and reported $0.236 for this run.
    path = _claude_transcript(tmp_path, "claude-haiku-5-5", "unknown", 0.23613825)
    u = h.parse(SimpleNamespace(model="claude-haiku-5-5"), path)
    expected = (10 * 0.10 + 48633 * 0.01 + 1228 * 0.50 + 8124 * 0.20) / 1e6
    assert u.cost_usd == pytest.approx(expected) and u.extra["cost_source"] == "list price"
    assert u.extra["harness_overhead_usd"] == 0.001196 and u.extra["reported_model_cost_usd"] == 0.23613825
    # A model Claude Code prices itself keeps its reported cost, minus Claude Code's own side call.
    path = _claude_transcript(tmp_path, "claude-opus-5-5", "list", 0.0718174)
    u = h.parse(SimpleNamespace(model="claude-opus-5-5"), path)
    assert u.cost_usd == 0.0718174 and u.extra["cost_source"] == "reported"


# ---------- report ----------


def test_report_leaves_provider_failures_out_and_charges_misses(tmp_path):
    from evalkit import report

    def row(task, level, verdict, correct, cost):
        return {
            "agent": "pi:m",
            "harness": "pi",
            "model": "m",
            "task": task,
            "level": level,
            "verdict": verdict,
            "correct": int(correct),
            "lenient": int(correct),
            "sql_source": "submit",
            "queries_run": 4,
            "wall_seconds": 10,
            "tokens_in": 100,
            "tokens_cache_read": 0,
            "tokens_out": 50,
            "cost_usd": cost,
        }

    rows = [
        row("q01", "spec", "exact", True, 0.02),
        row("q01", "spec", "rows_differ", False, 0.04),  # a miss is paid for
        row("q01", "terse", "harness_error", False, ""),  # the provider failed: says nothing about the model
        row("n01", "spec", "exact", True, 0.03),
        row("n01", "spec", "exact", True, "") | {"agent": "agy:g", "harness": "agy", "model": "gemini-4-argon-medium"},
    ]
    rep = report.build(rows, {"q01": "tpch", "n01": "tpch-novel"})
    c = rep["cells"][("pi:m", "all", "all")]
    assert (c["runs"], c["excluded"], c["graded"], c["correct"]) == (4, 1, 3, 2)
    assert c["cost"] == pytest.approx(0.03) and c["cost_per_correct"] == pytest.approx(0.045)
    assert rep["cells"][("pi:m", "tpch", "spec")]["strict"] == 0.5
    assert rep["cells"][("pi:m", "tpch", "terse")]["strict"] is None
    # agy reports no cost: priced from Google's list price.
    assert rep["cells"][("agy:g", "all", "all")]["cost"] == pytest.approx((100 * 2.00 + 50 * 10.00) / 1e6)
    assert "list price" in rep["basis"]["agy:g"]
    md = report.markdown(rep)
    assert "## tpch-novel" in md and "| pi:m | 67% |" in md
    report.write_csv(rep, tmp_path / "r.csv")
    assert (tmp_path / "r.csv").read_text().count("\n") == len(rep["cells"]) + 1


def test_question_sets_cover_every_question_file():
    from evalkit import report

    sets = report.question_sets()
    assert sets["q04"] == "tpch" and sets["n01"] == "tpch-novel" and sets["h01"] == "tpch-hard"
