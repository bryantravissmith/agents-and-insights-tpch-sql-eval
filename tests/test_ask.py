"""ask_clarification: the tool on both servers, what it logs, the run option, the prompt, result.json and findings."""

import asyncio
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_evalkit import Q04_BODY, FakeHarness, _with_fake, sandbox  # noqa: F401 (sandbox is a fixture)

from evalkit import findings, harnesses, load_question, runner
from evalkit.audit import integrity
from evalkit.harnesses import RunContext
from evalkit.style import run_style
from evalkit.tasks import Task

HERE = Path(__file__).resolve().parent.parent
REPLY = "No clarification is available. Use your best judgement, and state your assumptions in your final message."
LOG = (
    "-- 2026-10-25T00:00:00+00:00 RUN rows=1 ms=2\nSELECT 1\n;\n\n"
    "-- 2026-10-25T00:00:01+00:00 ASK\nShould I count tpch_answers() rows from 'orders.csv'?\n;\n\n"
    "-- 2026-10-25T00:00:02+00:00 SUBMIT rows=1\nSELECT 2\n;\n\n"
)


def _server(monkeypatch, tmp_path, ask: bool):
    import importlib

    import mcp_server

    if ask:
        monkeypatch.setenv("AUDIT_ASK", "1")
    else:
        monkeypatch.delenv("AUDIT_ASK", raising=False)
    mod = importlib.reload(mcp_server)
    monkeypatch.setattr(mod, "OUT", str(tmp_path))
    return mod


def _tool_names(mod) -> set[str]:
    return {t.name for t in asyncio.run(mod.mcp.list_tools())}


def test_the_tool_is_offered_only_with_the_env_var(tmp_path, monkeypatch):
    import importlib

    import mcp_server

    assert _tool_names(_server(monkeypatch, tmp_path, ask=False)) == {"run_sql", "submit_sql"}
    mod = _server(monkeypatch, tmp_path, ask=True)
    assert _tool_names(mod) == {"run_sql", "submit_sql", "ask_clarification"}
    monkeypatch.delenv("AUDIT_ASK")
    importlib.reload(mcp_server)


def test_the_tool_never_answers_and_records_every_question(tmp_path, monkeypatch):
    import importlib

    import mcp_server

    mod = _server(monkeypatch, tmp_path, ask=True)
    assert mod.ASK_REPLY == REPLY
    assert mod.ask_clarification("Revenue gross or net\nof discount?") == REPLY
    assert mod.ask_clarification("Which year?") == REPLY
    asks = [json.loads(line) for line in (tmp_path / "asks.jsonl").read_text().splitlines()]
    assert [a["question"] for a in asks] == ["Revenue gross or net\nof discount?", "Which year?"]
    assert all(a["at"] for a in asks)
    # queries.log keeps the header format of RUN and SUBMIT, with the question on one line.
    assert [(k, q) for k, q in runner._log_entries(tmp_path)] == [
        ("ASK", "Revenue gross or net of discount?"),
        ("ASK", "Which year?"),
    ]
    assert runner.asked(tmp_path) == ["Revenue gross or net\nof discount?", "Which year?"]
    monkeypatch.delenv("AUDIT_ASK")
    importlib.reload(mcp_server)


def test_log_parsers_do_not_count_an_ask_as_a_query(tmp_path):
    (tmp_path / "queries.log").write_text(LOG)
    kinds = [k for k, _ in runner._log_entries(tmp_path)]
    assert kinds == ["RUN", "ASK", "SUBMIT"]
    # Without asks.jsonl, the log's ASK entries are the record.
    assert runner.asked(tmp_path) == ["Should I count tpch_answers() rows from 'orders.csv'?"]
    # A question that names the answer key or a file is not an attempt to read it.
    found = integrity(tmp_path)
    assert found["answer_key"] == [] and found["files"] == []
    style = run_style(tmp_path)
    assert style["queries"] == 1 and style["discovery"] == 0 and style["failed"] == 0


def test_run_context_offers_the_tool_to_every_harness(tmp_path, monkeypatch):
    ctx = RunContext("p", "m", tmp_path, tmp_path, Path("x.duckdb"), None, 1, 60, allow_ask=True)
    assert ctx.tool_env["AUDIT_ASK"] == "1" and "AUDIT_DECLARE" not in ctx.tool_env
    assert ctx.mcp_server()["env"]["AUDIT_ASK"] == "1"
    assert ctx.tools == ("run_sql", "submit_sql", "ask_clarification")
    off = RunContext("p", "m", tmp_path, tmp_path, Path("x.duckdb"), None, 1, 60)
    assert "AUDIT_ASK" not in off.tool_env and off.tools == harnesses.TOOLS

    def allowed(c):
        cmd = harnesses.get("claude").command(c)
        return cmd[cmd.index("--allowedTools") + 1].split(",")

    assert allowed(ctx) == ["mcp__audit__run_sql", "mcp__audit__submit_sql", "mcp__audit__ask_clarification"]
    assert "mcp__audit__ask_clarification" not in allowed(off)
    assert "MCPTool(audit__ask_clarification)" in harnesses.get("grok").command(ctx)
    assert "MCPTool(audit__ask_clarification)" not in harnesses.get("grok").command(off)
    # agy's project is shared by every run, so it always allows the tool; only allow_ask runs are offered it.
    agy = harnesses.Antigravity()
    monkeypatch.setattr(agy, "project_file", lambda: tmp_path / "aai-evals.json")
    agy.ensure_project()
    grants = json.loads((tmp_path / "aai-evals.json").read_text())["permissionGrants"]["permissionGrants"]
    assert "mcp(audit_audit/ask_clarification)" in grants["allow"]


def test_the_prompt_sentence_follows_the_declare_sentence():
    t = Task.adhoc("How many orders?", target_sql="SELECT 1")
    (ask,) = runner.instructed([t], allow_ask=True)
    assert ask.prompt == f"How many orders?\n\n{runner.ASK_INSTRUCTION}" and ask.prompt_sha != t.prompt_sha
    (both,) = runner.instructed([t], declare_columns=True, allow_ask=True)
    assert both.prompt == f"How many orders?\n\n{runner.DECLARE_INSTRUCTION} {runner.ASK_INSTRUCTION}"
    (grain,) = runner.instructed([t], declare_grain=True, allow_ask=True)
    assert grain.prompt.endswith(f"{runner.DECLARE_GRAIN_INSTRUCTION} {runner.ASK_INSTRUCTION}")
    # Batches without allow_ask keep the prompts (and hashes) they always had.
    assert runner.instructed([t]) == [t]
    assert runner.instructed([t], declare_columns=True) == runner.declaring([t])
    assert runner.ASK_INSTRUCTION == (
        "If the request is ambiguous, you may call `ask_clarification` with your question before you answer."
    )


class Asking(FakeHarness):
    """Asks `n` questions the way ask_clarification records them, when the run offers the tool, then submits."""

    def __init__(self, sql, n=2):
        super().__init__(sql)
        self.n = n

    def command(self, ctx):
        cmd = super().command(ctx)
        cmd[2] += (
            "import json\n"
            "if os.environ.get('AUDIT_ASK') == '1':\n"
            f"    for i in range({self.n}):\n"
            "        q = f'Question {i}?'\n"
            "        with open(out / 'queries.log', 'a') as f:\n"
            "            f.write(f'-- 2026-10-25T00:00:0{i % 10}+00:00 ASK\\n{q}\\n;\\n\\n')\n"
            "        with open(out / 'asks.jsonl', 'a') as f:\n"
            "            f.write(json.dumps({'at': 'now', 'question': q}) + '\\n')\n"
        )
        return cmd


@pytest.mark.usefixtures("sandbox")
def test_an_asking_batch_records_the_asks_and_resumes_with_the_same_prompts(monkeypatch, db, questions_on):
    _with_fake(monkeypatch, Asking(Q04_BODY + "ORDER BY 1", n=12))
    qdir = questions_on(db, "q04")
    tasks = load_question(qdir / "q04.toml", ["spec"])
    kw = {"declare_columns": True, "allow_ask": True}
    (res,) = runner.run_grid(tasks, ["fake:a"], batch="asking", quiet=True, **kw)
    row = json.loads((res.run_dir / "result.json").read_text())
    assert row["asks"] == 12 and row["ask_questions"] == [f"Question {i}?" for i in range(10)]
    assert (res.queries_run, res.submits) == (1, 1)  # asks are not queries
    prompt = (res.run_dir / "prompt.md").read_text()
    assert prompt.rstrip().endswith(f"{runner.DECLARE_INSTRUCTION} {runner.ASK_INSTRUCTION}")
    manifest = json.loads((runner.RESULTS / "asking" / "manifest.json").read_text())
    assert manifest["run_options"]["allow_ask"] is True
    assert manifest["tasks"][0]["prompt_sha"] == runner.instructed(tasks, **kw)[0].prompt_sha
    # As if the batch was stopped mid-run: resume rebuilds the same prompts and offers the tool again.
    (res.run_dir / "result.json").unlink()
    (again,) = runner.resume("asking", questions=[qdir], quiet=True)
    assert again.task.prompt_sha == manifest["tasks"][0]["prompt_sha"]
    assert json.loads((again.run_dir / "result.json").read_text())["asks"] == 12


@pytest.mark.usefixtures("sandbox")
def test_a_run_without_allow_ask_records_no_asks(monkeypatch, db):
    _with_fake(monkeypatch, Asking(Q04_BODY + "ORDER BY 1"))
    r = runner.run_one(Task.adhoc("How many late orders by priority?", target_sql=Q04_BODY, db=db), "fake:m")
    row = json.loads((r.run_dir / "result.json").read_text())
    assert (row["asks"], row["ask_questions"]) == (0, [])
    assert not (r.run_dir / "asks.jsonl").exists()


def test_findings_count_a_tool_ask_as_asking(tmp_path):
    def run(name, *, submitted, asks=0, message="Done. I assumed net revenue."):
        d = tmp_path / "results" / "b" / name
        d.mkdir(parents=True)
        (d / "final_message.txt").write_text(message)
        if asks:
            (d / "asks.jsonl").write_text(json.dumps({"at": "now", "question": "Which?"}) + "\n")
        return {
            "agent": "claude:m",
            "level": "terse",
            "verdict": "exact",
            "sql_source": "submit" if submitted else "none",
            "run_dir": f"results/b/{name}",
        }

    rows = [
        run("tool-and-submit", submitted=True, asks=1),
        run("tool-instead", submitted=False, asks=1),
        run("phrase", submitted=True, message="Could you clarify which year?"),
        run("both", submitted=True, asks=1, message="Could you clarify which year?"),
        run("neither", submitted=True),
    ]
    rows[0]["asks"] = 1  # a result.json row carries the count; a scorecard row does not, so asks.jsonl is read
    cell = findings.clarifications(rows, tmp_path)[("Anthropic", "terse")]
    assert cell["runs"] == 5 and cell["by_tool"] == 3
    assert cell["instead"] == 1 and cell["and_submitted"] == 3  # a run that asked both ways counts once


def _transpiled(tmp_path) -> Path:
    npx = shutil.which("npx")
    if not npx or not shutil.which("node"):
        pytest.skip("node / npx not installed")
    run = subprocess.run(
        [npx, "-y", "esbuild@0.24.0", str(HERE / "extensions" / "duckdb-tools.ts"), "--format=esm"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if run.returncode and ("ENOTFOUND" in run.stderr or "network" in run.stderr.lower()):
        pytest.skip("esbuild is not cached and the network is unavailable")
    assert run.returncode == 0, run.stderr  # the extension compiles
    # pi provides typebox; a stand-in that returns its arguments is enough to register the tools.
    stub = "const Type = new Proxy({}, { get: () => (...args) => args });\n"
    js = run.stdout.replace('import { Type } from "typebox";\n', stub)
    assert js != run.stdout
    out = tmp_path / "duckdb-tools.mjs"
    out.write_text(js)
    return out


def test_pi_extension_compiles_and_registers_the_tool_only_with_the_env_var(tmp_path):
    module = _transpiled(tmp_path)
    driver = tmp_path / "driver.mjs"
    driver.write_text(
        f"import ext from {json.dumps(str(module))};\n"
        "const tools = {};\n"
        "ext({ registerTool: (t) => { tools[t.name] = t; } });\n"
        "const out = { names: Object.keys(tools).sort() };\n"
        "if (tools.ask_clarification) {\n"
        "  const r = await tools.ask_clarification.execute('id', { question: 'Gross or net\\nrevenue?' });\n"
        "  out.reply = r.content[0].text;\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    for ask in (False, True):
        run_dir = tmp_path / f"out-{ask}"
        env = {"PATH": str(Path(shutil.which("node")).parent), "AUDIT_OUT": str(run_dir)}
        run = subprocess.run(
            ["node", str(driver)],
            capture_output=True,
            text=True,
            check=False,
            env=env | ({"AUDIT_ASK": "1"} if ask else {}),
        )
        assert run.returncode == 0, run.stderr
        got = json.loads(run.stdout)
        if not ask:
            assert got == {"names": ["run_sql", "submit_sql"]} and not run_dir.exists()
            continue
        assert got == {"names": ["ask_clarification", "run_sql", "submit_sql"], "reply": REPLY}
        (asked,) = [json.loads(line) for line in (run_dir / "asks.jsonl").read_text().splitlines()]
        assert asked["question"] == "Gross or net\nrevenue?" and asked["at"]
        assert runner._log_entries(run_dir) == [("ASK", "Gross or net revenue?")]


# --ask-step: an ask reveals the question's next brief.

STEP_HEAD = "The person who made the request replied with more detail:\n\n"


def test_each_level_carries_the_briefs_above_it(tmp_path):
    f = tmp_path / "x.toml"
    f.write_text(
        'id = "x"\ntarget_sql = "SELECT 1"\n'
        '[[levels]]\nname = "terse"\nprompt = "T"\n'
        '[[levels]]\nname = "stakeholder"\nprompt = "S"\n'
        '[[levels]]\nname = "spec"\nprompt = "P"\n'
        '[[levels]]\nname = "other"\nprompt = "O"\ntarget_sql = "SELECT 2"\n'  # another question: never revealed
    )
    assert {t.level: t.next_briefs for t in load_question(f)} == {
        "terse": ("S", "P"),
        "stakeholder": ("P",),
        "spec": (),
        "other": (),
    }
    # Filtering to one level keeps what it can reveal.
    assert [t.next_briefs for t in load_question(f, ["terse"])] == [("S", "P")]
    # The briefs an ask can reveal get the leak check too.
    f.write_text(f.read_text().replace('prompt = "P"', 'prompt = "As in TPC-H"'))
    with pytest.raises(ValueError, match="TPC-H"):
        load_question(f, ["terse"])[0].check_leaks()


def test_each_ask_reveals_the_next_brief_then_the_non_answer(tmp_path, monkeypatch):
    import importlib

    import mcp_server

    steps = tmp_path / "ask_steps.json"
    steps.write_text(json.dumps(["The stakeholder brief.", "The spec brief."]))
    monkeypatch.setenv("AUDIT_ASK_STEPS", str(steps))
    mod = _server(monkeypatch, tmp_path / "out", ask=True)
    replies = [mod.ask_clarification(q) for q in ("Gross or net?", "Which year?", "Anything else?")]
    assert replies == [STEP_HEAD + "The stakeholder brief.", STEP_HEAD + "The spec brief.", REPLY]
    asks = [json.loads(line) for line in (tmp_path / "out" / "asks.jsonl").read_text().splitlines()]
    assert [a.get("revealed") for a in asks] == [1, 2, None]
    assert runner.asked(tmp_path / "out") == ["Gross or net?", "Which year?", "Anything else?"]
    headers = [h for h in (tmp_path / "out" / "queries.log").read_text().splitlines() if h.startswith("-- ")]
    assert [h.split(" ", 2)[2] for h in headers] == ["ASK revealed=1", "ASK revealed=2", "ASK"]
    monkeypatch.delenv("AUDIT_ASK_STEPS")
    monkeypatch.delenv("AUDIT_ASK")
    importlib.reload(mcp_server)


def test_the_steps_file_reaches_the_tool_env_only_with_the_ask_tool(tmp_path):
    steps = tmp_path / "ask_steps.json"
    on = RunContext("p", "m", tmp_path, tmp_path, Path("x.duckdb"), None, 1, 60, allow_ask=True, ask_steps=steps)
    assert on.tool_env["AUDIT_ASK_STEPS"] == str(steps) and on.tool_env["AUDIT_ASK"] == "1"
    off = RunContext("p", "m", tmp_path, tmp_path, Path("x.duckdb"), None, 1, 60, ask_steps=steps)
    assert "AUDIT_ASK_STEPS" not in off.tool_env


@pytest.mark.usefixtures("sandbox")
def test_an_ask_step_batch_offers_the_next_briefs_and_counts_what_it_revealed(monkeypatch, db, questions_on):
    _with_fake(monkeypatch, Asking(Q04_BODY + "ORDER BY 1", n=3))
    qdir = questions_on(db, "q04")
    tasks = load_question(qdir / "q04.toml", ["terse", "spec"])
    terse, spec = sorted(
        runner.run_grid(tasks, ["fake:a"], batch="stepping", quiet=True, ask_step=True), key=lambda r: r.task.rank
    )
    briefs = {lv["name"]: lv["prompt"].strip() for lv in tomllib.loads((qdir / "q04.toml").read_text())["levels"]}
    assert json.loads((terse.run_dir / "ask_steps.json").read_text()) == [briefs["stakeholder"], briefs["spec"]]
    assert json.loads((spec.run_dir / "ask_steps.json").read_text()) == []
    rows = [json.loads((r.run_dir / "result.json").read_text()) for r in (terse, spec)]
    assert [(r["asks"], r["revealed"]) for r in rows] == [(3, 2), (3, 0)]
    # The prompt is --allow-ask's, so the two options differ only in the tool's reply.
    assert terse.task.prompt_sha == runner.instructed(tasks, allow_ask=True)[0].prompt_sha
    assert json.loads((runner.RESULTS / "stepping" / "manifest.json").read_text())["run_options"]["ask_step"] is True
    (terse.run_dir / "result.json").unlink()
    (again,) = runner.resume("stepping", questions=[qdir], quiet=True)
    assert json.loads((again.run_dir / "result.json").read_text())["revealed"] == 2


def test_pi_extension_reveals_the_next_brief_on_each_ask(tmp_path):
    module = _transpiled(tmp_path)
    steps = tmp_path / "ask_steps.json"
    steps.write_text(json.dumps(["The stakeholder brief.", "The spec brief."]))
    driver = tmp_path / "driver.mjs"
    driver.write_text(
        f"import ext from {json.dumps(str(module))};\n"
        "const tools = {};\n"
        "ext({ registerTool: (t) => { tools[t.name] = t; } });\n"
        "const replies = [];\n"
        "for (const q of ['Gross or net?', 'Which year?', 'Anything else?']) {\n"
        "  replies.push((await tools.ask_clarification.execute('id', { question: q })).content[0].text);\n"
        "}\n"
        "console.log(JSON.stringify(replies));\n"
    )
    run_dir = tmp_path / "out"
    env = {
        "PATH": str(Path(shutil.which("node")).parent),
        "AUDIT_OUT": str(run_dir),
        "AUDIT_ASK": "1",
        "AUDIT_ASK_STEPS": str(steps),
    }
    run = subprocess.run(["node", str(driver)], capture_output=True, text=True, check=False, env=env)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [STEP_HEAD + "The stakeholder brief.", STEP_HEAD + "The spec brief.", REPLY]
    asks = [json.loads(line) for line in (run_dir / "asks.jsonl").read_text().splitlines()]
    assert [a.get("revealed") for a in asks] == [1, 2, None]
