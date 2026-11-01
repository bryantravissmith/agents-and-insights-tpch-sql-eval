"""Run agents on tasks, grade every run, record it. run_one is a single attempt; run_grid fans out in parallel.

Layout of one run (results/<batch>/<task>-<level>-<condition>-<harness>-<model>-r<n>/):
  prompt.md  meta.json  transcript.jsonl  stderr.log  queries.log  answer.sql  final_message.txt  result.json
The agent's cwd is an anonymous scratch dir outside the repo (AAI_WORK_ROOT, default $TMPDIR/aai-evals).
This prevents ancestor context discovery; disabled tools and DuckDB restrictions enforce answer-key isolation.
It is deleted after the run unless keep_work=True.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from . import grading, harnesses
from .declared import grade_task, read_declaration
from .harnesses import HARNESS_DIR, RunContext
from .tasks import Task

# Added to every prompt in a batch run with declare_columns, after the brief.
DECLARE_INSTRUCTION = (
    "When you call `submit_sql`, also pass `answer_columns`: the names of the columns in your result that answer"
    " the request. Other columns are treated as context."
)


# ... with declare_grain instead (it implies declare_columns).
DECLARE_GRAIN_INSTRUCTION = (
    "When you call `submit_sql`, also pass `answer_columns`: the names of the columns in your result that answer"
    " the request (other columns are treated as context), and `grain_columns`: the names of the columns that"
    " identify one row of your result, or an empty list if your result is a single row."
)


def declaring(tasks: list[Task], grain: bool = False) -> list[Task]:
    """The tasks with the declare instruction added to each prompt (so it is part of prompt_sha)."""
    text = DECLARE_GRAIN_INSTRUCTION if grain else DECLARE_INSTRUCTION
    return [replace(t, prompt=f"{t.prompt}\n\n{text}") for t in tasks]


RESULTS = HARNESS_DIR / "results"
SCORECARD = RESULTS / "scorecard.csv"
CONDITIONS = HARNESS_DIR / "conditions"
DB = HARNESS_DIR / "tpch.duckdb"
# The enclosing git checkout (the harness may sit at its root or deeper); agents must work outside it.
REPO_ROOT = next((p for p in (HARNESS_DIR, *HARNESS_DIR.parents) if (p / ".git").exists()), HARNESS_DIR)
WORK_ROOT = Path(os.environ.get("AAI_WORK_ROOT") or tempfile.gettempdir()) / "aai-evals"

COLS = [
    "batch",
    "started_at",
    "task",
    "level",
    "rank",
    "condition",
    "harness",
    "model",
    "agent",
    "repeat",
    "verdict",
    "correct",
    "lenient",
    "sql_source",
    "rows_returned",
    "rows_expected",
    "queries_run",
    "submits",
    "wall_seconds",
    "exit_code",
    "turns",
    "tokens_in",
    "tokens_out",
    "tokens_cache_read",
    "tokens_cache_write",
    "cost_usd",
    "error",
    "prompt_sha",
    "run_dir",
]

_scorecard_lock = threading.Lock()
_LOG_HEAD = re.compile(r"^-- \d{4}-\d{2}-\d{2}T\S+ (\S+)")


@dataclass(frozen=True)
class Agent:
    """A model inside a harness. Spec strings are "harness:model", e.g. "codex:gpt-5.6-luna",
    "pi:openai/gpt-5.6-luna", "agy:claude-sonnet-5-5-medium". extra_args go to the CLI verbatim."""

    harness: str
    model: str
    extra_args: tuple[str, ...] = ()
    label: str | None = None

    @classmethod
    def parse(cls, spec: str | Agent) -> Agent:
        if isinstance(spec, Agent):
            return spec
        harness, sep, model = spec.partition(":")
        if not sep or not model:
            raise ValueError(f"agent spec {spec!r} must be harness:model, e.g. codex:gpt-5.6-luna")
        harnesses.get(harness)
        return cls(harness, model)

    def __str__(self):
        return self.label or f"{self.harness}:{self.model}"

    @property
    def slug(self) -> str:
        return "".join(c if c.isalnum() or c in ".-_" else "-" for c in str(self).replace(":", "-"))


@dataclass
class RunResult:
    batch: str
    task: Task
    agent: Agent
    condition: str
    repeat: int
    run_dir: Path
    started_at: str = ""
    verdict: str = ""
    correct: bool = False  # verdict in grading.CORRECT and the SQL came from submit_sql
    lenient: bool = False  # verdict in grading.LENIENT, wherever the SQL came from
    sql_source: str = ""  # submit | final_message | last_query | none
    sql: str | None = None
    rows_returned: int = 0
    rows_expected: int = 0
    reference: int = 0  # which reference the grade used: 0 = the target, n = the nth accepted alternative
    queries_run: int = 0
    submits: int = 0
    wall_seconds: float = 0.0
    exit_code: int | None = None
    usage: harnesses.Usage = field(default_factory=harnesses.Usage)
    detail: str = ""

    def to_row(self) -> dict:
        u = self.usage
        return {
            "batch": self.batch,
            "started_at": self.started_at,
            "task": self.task.id,
            "level": self.task.level,
            "rank": self.task.rank,
            "condition": self.condition,
            "harness": self.agent.harness,
            "model": self.agent.model,
            "agent": str(self.agent),
            "repeat": self.repeat,
            "verdict": self.verdict,
            "correct": int(self.correct),
            "lenient": int(self.lenient),
            "sql_source": self.sql_source,
            "rows_returned": self.rows_returned,
            "rows_expected": self.rows_expected,
            "queries_run": self.queries_run,
            "submits": self.submits,
            "wall_seconds": round(self.wall_seconds, 1),
            "exit_code": "" if self.exit_code is None else self.exit_code,
            "turns": "" if u.turns is None else u.turns,
            "tokens_in": u.tokens_in,
            "tokens_out": u.tokens_out,
            "tokens_cache_read": u.tokens_cache_read,
            "tokens_cache_write": u.tokens_cache_write,
            "cost_usd": "" if u.cost_usd is None else f"{u.cost_usd:.4f}",
            "error": u.error,
            "prompt_sha": self.task.prompt_sha,
            "run_dir": os.path.relpath(self.run_dir, HARNESS_DIR),
        }

    def line(self) -> str:
        cost = f"${self.usage.cost_usd:.4f}" if self.usage.cost_usd is not None else "cost n/a"
        src = "" if self.sql_source in ("submit", "none") else f" [not submitted; graded {self.sql_source}]"
        msg = (
            f"{self.task.name} {self.condition} {self.agent} r{self.repeat}: {self.verdict}{src} "
            f"({self.rows_returned}/{self.rows_expected} rows, {self.queries_run} queries, "
            f"{self.wall_seconds:.0f}s, {cost})"
        )
        if self.detail and not self.correct:
            msg += f"\n    {self.detail}"
        return msg


def new_batch_id(name: str | None = None) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{name}" if name else stamp


# ---------- one run ----------


def _log_entries(run_dir: Path) -> list[tuple[str, str]]:
    """(kind, sql) for each tool call. Entries are "-- <timestamp> <KIND> [note]\n<sql>\n;\n\n",
    KIND in RUN, ERROR, REFUSED, SUBMIT, SUBMIT-ERROR, REFUSED-SUBMIT."""
    path = run_dir / "queries.log"
    if not path.exists():
        return []
    entries, kind, body = [], None, []
    for line in path.read_text().splitlines():
        if m := _LOG_HEAD.match(line):  # agent SQL may contain "-- comments"; headers carry an ISO timestamp
            if kind:
                entries.append((kind, "\n".join(body).strip().removesuffix(";").strip()))
            kind, body = m.group(1), []
        else:
            body.append(line)
    if kind:
        entries.append((kind, "\n".join(body).strip().removesuffix(";").strip()))
    return entries


def _launch(cmd: list[str], ctx: RunContext, env: dict, timeout_s: int) -> tuple[int | None, bool]:
    with open(ctx.run_dir / "transcript.jsonl", "w") as out, open(ctx.run_dir / "stderr.log", "w") as err:
        proc = subprocess.Popen(
            cmd, cwd=ctx.work_dir, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True
        )
        try:
            return proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            # The CLIs spawn MCP servers and helpers; kill the whole process group.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            return proc.returncode, True


@contextmanager
def _locked_scorecard(path: Path):
    """Held while the scorecard is written: by threads here and by other evals.py processes (a resume of
    one batch can run beside a run of another)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _scorecard_lock, open(path.with_name(path.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _latest_per_run(rows: list[dict]) -> list[dict]:
    """One row per run directory, the last one written: a resumed run replaces its earlier attempt."""
    latest = {r["run_dir"] or f"row{i}": i for i, r in enumerate(rows)}
    return [rows[i] for i in sorted(latest.values())]


def compact_scorecard(path: Path | None = None, refresh: dict[str, dict] | None = None) -> int:
    """Rewrite the scorecard with one row per run; returns how many superseded rows it dropped.

    `refresh` maps run_dir to that run's current result.json row, whose values replace the scorecard's
    (a reparse changes usage and cost after the scorecard row was written).
    """
    path = path or SCORECARD
    refresh = refresh or {}
    with _locked_scorecard(path):
        if not path.exists():
            return 0
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        kept = [
            {k: refresh[r["run_dir"]].get(k, "") for k in COLS} if r["run_dir"] in refresh else r
            for r in _latest_per_run(rows)
        ]
        if len(kept) < len(rows) or refresh:
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=COLS)
                w.writeheader()
                w.writerows(kept)
            os.replace(tmp, path)
    return len(rows) - len(kept)


def append_scorecard(row: dict, path: Path | None = None) -> None:
    path = path or SCORECARD  # resolved per call so tests can redirect it
    with _locked_scorecard(path):
        new = not path.exists() or path.stat().st_size == 0
        if not new:
            with open(path) as f:
                header = f.readline().strip().split(",")
            if header != COLS:
                raise RuntimeError(f"{path} has columns {header}; expected {COLS}. Migrate it before appending.")
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            if new:
                w.writeheader()
            w.writerow(row)


def run_one(
    task: Task,
    agent: Agent | str,
    *,
    condition: str = "bare",
    repeat: int = 1,
    batch: str | None = None,
    db: Path | None = None,
    timeout_s: int = 900,
    max_turns: int = 40,
    keep_work: bool = False,
    record: bool = True,
    declare_columns: bool = False,
    declare_grain: bool = False,
) -> RunResult:
    """One headless agent session on one task, graded against task.target_sql.

    The database is the task's own (a question file's `db`), else `db`, else tpch.duckdb. With
    `declare_columns`, submit_sql also takes the answer's column names and the run is graded by them
    (`declared.grade_task`); `declare_grain` adds the grain. The task's prompt must already ask for them
    (see `declaring`).
    """
    agent = Agent.parse(agent)
    db = resolve_db(task, db)
    harness = harnesses.get(agent.harness)
    batch = batch or new_batch_id()
    name = f"{task.id}-{task.level}-{condition}-{agent.slug}-r{repeat}"
    run_dir = RESULTS / batch / name
    # CLIs include cwd in their system context. Neither the question id nor a
    # descriptive batch name should disclose the benchmark identity there.
    work_dir = WORK_ROOT / uuid.uuid4().hex
    cond_dir = CONDITIONS / condition
    if not cond_dir.is_dir():
        raise ValueError(
            f"no condition {condition!r}; have {sorted(p.name for p in CONDITIONS.iterdir() if p.is_dir())}"
        )
    run_dir.mkdir(parents=True, exist_ok=False)
    work_dir.mkdir(parents=True, exist_ok=False)
    # A git root stops CLIs that walk up for AGENTS.md / .agents/ from looking any further.
    subprocess.run(["git", "init", "-q", str(work_dir)], check=True)
    notes = None
    if (cond_dir / "AGENTS.md").exists():
        notes = work_dir / "AGENTS.md"
        shutil.copy(cond_dir / "AGENTS.md", notes)

    ctx = RunContext(
        prompt=task.prompt,
        model=agent.model,
        work_dir=work_dir,
        run_dir=run_dir,
        db=db,
        notes=notes,
        max_turns=max_turns,
        timeout_s=timeout_s,
        extra_args=agent.extra_args,
        declare=declare_columns,
        declare_grain=declare_grain,
    )
    res = RunResult(
        batch=batch,
        task=task,
        agent=agent,
        condition=condition,
        repeat=repeat,
        run_dir=run_dir,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    (run_dir / "prompt.md").write_text(task.prompt + "\n")

    timed_out = False
    try:
        harness.prepare(ctx)
        cmd = harness.command(ctx)
        (run_dir / "meta.json").write_text(
            json.dumps(
                {
                    "task": asdict(task) | {"prompt_sha": task.prompt_sha},
                    "agent": asdict(agent),
                    "condition": condition,
                    "repeat": repeat,
                    "batch": batch,
                    "work_dir": str(work_dir),
                    "argv": [a if a != task.prompt else "<prompt.md>" for a in cmd],
                },
                indent=2,
                default=str,
            )
        )
        t0 = time.monotonic()
        res.exit_code, timed_out = _launch(cmd, ctx, harness.env(ctx), timeout_s)
        res.wall_seconds = time.monotonic() - t0
        res.usage = harness.parse(ctx, run_dir / "transcript.jsonl")
    except Exception as e:  # noqa: BLE001 - a broken harness is a result, not a crash of the whole grid
        res.usage.error = f"{type(e).__name__}: {e}"[:300]
    finally:
        if not keep_work:
            shutil.rmtree(work_dir, ignore_errors=True)

    if res.usage.final_text and not (run_dir / "final_message.txt").exists():
        (run_dir / "final_message.txt").write_text(res.usage.final_text)

    # Only submitted SQL can be correct. Without a submission, grade what the agent most plausibly meant
    # (a ```sql block in its final message, else its last successful query) so the miss is diagnosable.
    entries = _log_entries(run_dir)
    last_run = next((sql for kind, sql in reversed(entries) if kind == "RUN"), None)
    answer = run_dir / "answer.sql"
    if answer.exists():
        res.sql, res.sql_source = answer.read_text(), "submit"
    elif sql := harnesses.sql_from_text(res.usage.final_text):
        res.sql, res.sql_source = sql, "final_message"
    elif last_run:
        res.sql, res.sql_source = last_run, "last_query"
    else:
        res.sql_source = "none"

    # A run that declared its answer columns is graded by them; otherwise by its whole result.
    declaring_run = declare_columns or declare_grain
    declared = read_declaration(run_dir) if declaring_run and res.sql_source == "submit" else None
    g, res.reference, declaration = grade_task(res.sql, declared, task, ctx.db)
    res.verdict, res.detail = g.verdict, g.detail
    res.correct = g.correct and res.sql_source == "submit"
    res.lenient = g.verdict in grading.LENIENT
    res.rows_returned, res.rows_expected = g.rows_got, g.rows_expected
    if res.sql is None:
        if timed_out:
            res.verdict = "timeout"
        elif res.usage.error or res.exit_code not in (0, None):
            res.verdict = "harness_error"
            res.detail = res.usage.error or f"exit {res.exit_code}; see stderr.log"
    elif res.sql_source != "submit" and not timed_out and PROVIDER_FAILURE.search(res.usage.error or ""):
        # The provider cut the session short before the agent submitted: its last exploratory
        # query is not its answer, so this is an infrastructure failure, not a model outcome.
        res.verdict, res.correct, res.lenient = "harness_error", False, False
        res.detail = res.usage.error
    kinds = Counter(kind for kind, _ in entries)
    res.queries_run, res.submits = kinds["RUN"], kinds["SUBMIT"]

    (run_dir / "result.json").write_text(
        json.dumps(
            res.to_row()
            | {
                "detail": res.detail,
                "column_map": g.column_map,
                "reference": res.reference,  # 0 = the target; n = the nth accepted alternative
                "declaration": declaration,  # declared answer columns and how they lined up ({} if none)
                "usage_extra": res.usage.extra,
            },
            indent=2,
        )
    )
    if record:
        append_scorecard(res.to_row())
    return res


# ---------- grid ----------


def resolve_db(task: Task, default: Path | None = None) -> Path:
    return Path(task.db or default or DB).resolve()


def preflight(
    tasks: list[Task], agents: list[Agent], conditions: list[str], db: Path | None = None, allow_leaky: bool = False
) -> None:
    """Fail before spending anything: missing CLIs, databases, conditions, broken targets, leaky prompts."""
    problems = []
    for d in sorted({resolve_db(t, db) for t in tasks}):
        if not d.exists():
            problems.append(f"database missing: {d} (for tpch: uv run setup_db.py)")
    if Path(WORK_ROOT).resolve().is_relative_to(REPO_ROOT):
        problems.append(f"work root {WORK_ROOT} is inside the repo; agents could read the answer key")
    for h in sorted({a.harness for a in agents}):
        if not harnesses.get(h).available():
            problems.append(f"{h}: {harnesses.get(h).binary} not on PATH")
    for c in conditions:
        if not (CONDITIONS / c).is_dir():
            problems.append(f"no condition {c!r} in {CONDITIONS}")
    for t in tasks:
        if not allow_leaky:
            try:
                t.check_leaks()
            except ValueError as e:
                problems.append(str(e))
    if not problems:
        for target, tdb in {(ref, resolve_db(t, db)) for t in tasks for ref in t.references}:
            owner = next(t.name for t in tasks if target in t.references and resolve_db(t, db) == tdb)
            try:
                if not grading.expected_result(target, tdb).rows:
                    print(
                        f"warning: target SQL for {owner} returns 0 rows; any empty answer will match", file=sys.stderr
                    )
            except grading.SQLError as e:
                problems.append(f"target SQL for {owner} fails: {e}")
        for t in tasks:
            if t.answer_columns:
                cols = {c.lower() for c in grading.expected_result(t.target_sql, resolve_db(t, db)).columns}
                named = [c for slot in t.answer_columns for c in slot] + list(t.label_columns or ())
                if unknown := [c for c in named if c.lower() not in cols]:
                    problems.append(f"{t.name}: answer or label columns {unknown} are not columns of its target")
    if problems:
        raise SystemExit("preflight failed:\n  " + "\n  ".join(problems))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cli_version(binary: str) -> str:
    """`binary --version`, or the executable's hash when the CLI has no usable version flag."""
    path = shutil.which(binary)
    if not path:
        return "missing"
    try:
        out = subprocess.run([binary, "--version"], check=False, capture_output=True, text=True, timeout=30)
        text = (out.stdout or out.stderr).strip()
        if out.returncode == 0 and text:
            return text.splitlines()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return f"{path} sha256:{_sha256(Path(path).read_bytes())}"


def _git_state() -> dict:
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(HARNESS_DIR), *args], check=False, capture_output=True, text=True
        ).stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD") or None,
        "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
    }


def batch_manifest(batch, tasks, agents, conditions, repeats, jobs, harness_jobs, db, run_kw, planned_runs) -> dict:
    """What a batch asked for and what it ran on, so a published number traces to exact inputs."""
    import duckdb

    code = [HARNESS_DIR / p for p in ("evals.py", "sqlguard.py", "mcp_server.py")]
    code += sorted((HARNESS_DIR / "evalkit").glob("*.py")) + sorted((HARNESS_DIR / "extensions").glob("*"))
    code += [p for c in conditions for p in sorted((CONDITIONS / c).rglob("*")) if p.is_file()]
    dbs = sorted({resolve_db(t, db or DB) for t in tasks})
    binaries = sorted({harnesses.get(a.harness).binary for a in agents})
    return {
        "batch": batch,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "argv": sys.argv,
        "git": _git_state(),
        "planned_runs": planned_runs,
        "repeats": repeats,
        "jobs": jobs,
        "harness_jobs": harness_jobs or {},
        "conditions": conditions,
        "run_options": {k: v for k, v in run_kw.items() if isinstance(v, (str, int, float, bool))},
        "agents": [asdict(a) | {"label": str(a)} for a in agents],
        "tasks": [
            {
                "id": t.id,
                "level": t.level,
                "prompt_sha": t.prompt_sha,
                "target_sha256": _sha256(t.target_sql.encode()),
                **({"accepted_sha256": [_sha256(a.encode()) for a in t.accepted_sql]} if t.accepted_sql else {}),
                "db": resolve_db(t, db or DB).name,
            }
            for t in tasks
        ],
        "versions": {b: _cli_version(b) for b in binaries}
        | {"python": sys.version.split()[0], "duckdb": duckdb.__version__},
        "sha256": {str(p.relative_to(HARNESS_DIR)): _sha256(p.read_bytes()) for p in code if p.is_file()}
        | {f"db:{p.name}": _sha256(p.read_bytes()) for p in dbs},
    }


def write_manifest(batch_dir: Path, manifest: dict):
    (batch_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n")


def _run_name(t: Task, a: Agent, c: str, r: int) -> str:
    return f"{t.id}-{t.level}-{c}-{a.slug}-r{r}"


def _execute(plan, batch, jobs, caps, db, quiet, run_kw) -> list[RunResult]:
    """Run (task, agent, condition, repeat) tuples `jobs` at a time; a crashed attempt is recorded, not dropped."""

    def go(t, a, c, r):
        cap = caps.get(a.harness)
        if cap:
            cap.acquire()
        try:
            return run_one(t, a, condition=c, repeat=r, batch=batch, db=db, **run_kw)
        finally:
            if cap:
                cap.release()

    results: list[RunResult] = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(go, *p): p for p in plan}
        for i, fut in enumerate(as_completed(futures), 1):
            t, a, c, r = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                # A crashed attempt belongs in the denominator, even when it
                # fails outside run_one's launch exception handler.
                run_dir = RESULTS / batch / _run_name(t, a, c, r)
                run_dir.mkdir(parents=True, exist_ok=True)
                res = RunResult(
                    batch=batch,
                    task=t,
                    agent=a,
                    condition=c,
                    repeat=r,
                    run_dir=run_dir,
                    verdict="harness_error",
                    sql_source="none",
                    detail=f"{type(e).__name__}: {e}"[:300],
                )
                res.usage.error = res.detail
                (run_dir / "prompt.md").write_text(t.prompt + "\n")
                (run_dir / "result.json").write_text(json.dumps(res.to_row() | {"detail": res.detail}, indent=2))
                if run_kw.get("record", True):
                    append_scorecard(res.to_row())
            results.append(res)
            if not quiet:
                print(f"[{i}/{len(plan)}] {res.line()}", flush=True)
    return results


# Provider-side failures: the model never answered, so the run says nothing about it. Only these are re-run.
PROVIDER_FAILURE = re.compile(
    r"rate limit|credit balance|overloaded|temporarily unavailable|service unavailable|internal server error"
    r"|bad gateway|gateway timeout|an error occurred while processing your request|usage limit|limit reached"
    r"|quota|resource_exhausted",
    re.IGNORECASE,
)


def _transcript_error(run_dir: Path, row: dict) -> str:
    """The run's error as the current parser reads it from the saved transcript (older parsers dropped detail)."""
    from types import SimpleNamespace

    transcript = run_dir / "transcript.jsonl"
    if not transcript.exists() or not row.get("harness"):
        return ""
    ctx = SimpleNamespace(run_dir=run_dir, model=row.get("model"))
    return harnesses.get(row["harness"]).parse(ctx, transcript).error or ""


def resume(
    batch: str,
    *,
    jobs: int = 4,
    harness_jobs: dict[str, int] | None = None,
    questions: list[Path] | None = None,
    agents: list[str] | None = None,
    quiet: bool = False,
) -> list[RunResult]:
    """Finish a batch: run the planned runs that never ran or that failed on the provider's side.

    `agents` limits the re-runs to those agent labels (e.g. one whose quota has reset); the rest wait.

    The plan comes from the batch's manifest, and every task's composed prompt must still hash to what the
    batch ran (questions edited since then are refused). A replaced attempt moves to `_replaced/` and the
    manifest's `resumes` list records what was re-run and why. Model outcomes, timeouts included, are never re-run.
    """
    from .tasks import load_questions

    batch_dir = RESULTS / batch
    manifest = json.loads((batch_dir / "manifest.json").read_text())
    loaded = load_questions(*(questions or [HARNESS_DIR / "questions"]))
    opts = manifest["run_options"]
    if opts.get("declare_columns") or opts.get("declare_grain"):
        loaded = declaring(loaded, grain=bool(opts.get("declare_grain")))
    by_key = {(t.id, t.level): t for t in loaded}
    tasks = []
    for spec in manifest["tasks"]:
        t = by_key.get((spec["id"], spec["level"]))
        if t is None or t.prompt_sha != spec["prompt_sha"]:
            raise ValueError(
                f"{spec['id']}/{spec['level']}: prompt changed or missing since the batch ran; start a new batch"
            )
        tasks.append(t)
    planned = [Agent(a["harness"], a["model"], tuple(a["extra_args"]), a.get("label")) for a in manifest["agents"]]
    unknown = set(agents or ()) - {str(a) for a in planned}
    if unknown:
        raise ValueError(f"not in batch {batch}: {sorted(unknown)}; it ran {[str(a) for a in planned]}")
    conditions, repeats = manifest["conditions"], manifest["repeats"]
    run_kw = {
        k: v
        for k, v in manifest["run_options"].items()
        if k in ("timeout_s", "max_turns", "keep_work", "record", "declare_columns", "declare_grain")
    }
    plan = [(t, a, c, r) for r in range(1, repeats + 1) for t in tasks for c in conditions for a in planned]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    todo, missing, provider = [], 0, Counter()
    for p in plan:
        if agents and str(p[1]) not in agents:
            continue
        run_dir = batch_dir / _run_name(*p)
        result = run_dir / "result.json"
        if result.exists():
            row = json.loads(result.read_text())
            failed = row.get("verdict") == "harness_error" and PROVIDER_FAILURE.search(
                f"{row.get('detail', '')} {_transcript_error(run_dir, row)}"
            )
            # Runs recorded before the provider-cut rule: graded on an exploratory query after the API failed.
            cut_short = row.get("sql_source") != "submit" and PROVIDER_FAILURE.search(row.get("error") or "")
            if not (failed or cut_short):
                continue
            provider[p[1].label or str(p[1])] += 1
        else:
            missing += 1
        if run_dir.exists():
            aside = batch_dir / "_replaced" / f"{run_dir.name}-{stamp}"
            aside.parent.mkdir(exist_ok=True)
            run_dir.rename(aside)
        todo.append(p)
    if not quiet:
        print(
            f"batch {batch}: {len(plan)} planned, re-running {len(todo)} ({missing} never finished, "
            f"{sum(provider.values())} provider failures), {jobs} parallel"
        )
    caps = {h: threading.Semaphore(n) for h, n in (harness_jobs or {}).items()}
    started = time.monotonic()
    # So `progress` shows the batch as running again until this resume finishes.
    manifest["resuming"] = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs_at_start": len(list(batch_dir.glob("*/result.json"))),
        "rerunning": len(todo),
    }
    write_manifest(batch_dir, manifest)
    results = _execute(todo, batch, jobs, caps, None, quiet, run_kw)
    del manifest["resuming"]

    rows = [
        json.loads((batch_dir / _run_name(*p) / "result.json").read_text())
        for p in plan
        if (batch_dir / _run_name(*p) / "result.json").exists()
    ]
    manifest.setdefault("resumes", []).append(
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "git": _git_state(),
            "rerun_never_finished": missing,
            "rerun_provider_failures": dict(provider),
            "agents": agents or "all",
            "jobs": jobs,
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "replaced_attempts_in": "_replaced/",
        }
    )
    manifest.update(completed_at=datetime.now(UTC).isoformat(timespec="seconds"), recorded_runs=len(rows))
    write_manifest(batch_dir, manifest)
    compact_scorecard()  # the re-run rows replace the attempts they re-ran
    with open(batch_dir / "results.jsonl", "w") as f:
        f.writelines(json.dumps({k: row.get(k, "") for k in COLS}) + "\n" for row in rows)
    report = summarize(rows)
    (batch_dir / "summary.txt").write_text(report + "\n")
    if not quiet:
        print("\n" + report)
    return results


def rescore(batch: str, *, quiet: bool = False) -> int:
    """Recompute `correct` and `lenient` from each run's recorded verdict under the current grading rule.

    For a change of rule, not of grader: verdicts, SQL and usage are untouched, nothing runs again. A run whose
    score changes keeps the previous one in `rescored` (when, and what it was); the manifest's `rescored` list
    says when, with which commit and how many changed. Returns the number of runs whose score changed.
    """
    batch_dir = RESULTS / batch
    now = datetime.now(UTC).isoformat(timespec="seconds")
    changed = Counter()
    for result in sorted(batch_dir.glob("*/result.json")):
        row = json.loads(result.read_text())
        correct = int(row.get("verdict") in grading.CORRECT and row.get("sql_source") == "submit")
        lenient = int(row.get("verdict") in grading.LENIENT)
        if (correct, lenient) == (int(row.get("correct") or 0), int(row.get("lenient") or 0)):
            continue
        row.setdefault("rescored", []).append({"at": now, "correct": row.get("correct"), "lenient": row.get("lenient")})
        changed[row.get("verdict")] += 1
        result.write_text(json.dumps(row | {"correct": correct, "lenient": lenient}, indent=2))
    rows = [json.loads(p.read_text()) for p in sorted(batch_dir.glob("*/result.json"))]
    if (batch_dir / "results.jsonl").exists():
        with open(batch_dir / "results.jsonl", "w") as f:
            f.writelines(json.dumps({k: row.get(k, "") for k in COLS}) + "\n" for row in rows)
    compact_scorecard(refresh={row["run_dir"]: row for row in rows if row.get("run_dir")})
    manifest = json.loads((batch_dir / "manifest.json").read_text())
    manifest.setdefault("rescored", []).append(
        {"at": now, "git": _git_state(), "correct": sorted(grading.CORRECT), "changed": dict(changed)}
    )
    write_manifest(batch_dir, manifest)
    if not quiet:
        print(f"batch {batch}: re-scored {sum(changed.values())} runs under the current rule: {dict(changed)}")
    return sum(changed.values())


def reparse(batch: str, *, quiet: bool = False) -> int:
    """Recompute each run's usage fields (turns, tokens, cost) from its saved transcript with the current parsers.

    For fixing a parser after the fact. Verdicts, grades and error text are left as recorded; the manifest's
    `reparsed` list says when and with which commit. Returns the number of runs updated.
    """
    from types import SimpleNamespace

    batch_dir = RESULTS / batch
    updated = 0
    for result in sorted(batch_dir.glob("*/result.json")):
        run_dir = result.parent
        transcript = run_dir / "transcript.jsonl"
        row = json.loads(result.read_text())
        if not transcript.exists() or not row.get("harness"):
            continue
        u = harnesses.get(row["harness"]).parse(SimpleNamespace(run_dir=run_dir, model=row.get("model")), transcript)
        row |= {
            "turns": "" if u.turns is None else u.turns,
            "tokens_in": u.tokens_in,
            "tokens_out": u.tokens_out,
            "tokens_cache_read": u.tokens_cache_read,
            "tokens_cache_write": u.tokens_cache_write,
            "cost_usd": "" if u.cost_usd is None else f"{u.cost_usd:.4f}",
            "usage_extra": u.extra,
        }
        result.write_text(json.dumps(row, indent=2))
        updated += 1
    rows = [json.loads(p.read_text()) for p in sorted(batch_dir.glob("*/result.json"))]
    if (batch_dir / "results.jsonl").exists():
        with open(batch_dir / "results.jsonl", "w") as f:
            f.writelines(json.dumps({k: row.get(k, "") for k in COLS}) + "\n" for row in rows)
    compact_scorecard(refresh={row["run_dir"]: row for row in rows if row.get("run_dir")})
    manifest = json.loads((batch_dir / "manifest.json").read_text())
    manifest.setdefault("reparsed", []).append(
        {"at": datetime.now(UTC).isoformat(timespec="seconds"), "git": _git_state(), "runs": updated}
    )
    write_manifest(batch_dir, manifest)
    if not quiet:
        print(f"batch {batch}: re-parsed usage for {updated} runs")
    return updated


def run_grid(
    tasks: list[Task],
    agents: list[Agent | str],
    *,
    conditions: list[str] = ("bare",),
    repeats: int = 1,
    jobs: int = 4,
    harness_jobs: dict[str, int] | None = None,
    batch: str | None = None,
    db: Path | None = None,
    allow_leaky: bool = False,
    quiet: bool = False,
    dry_run: bool = False,
    **run_kw,
) -> list[RunResult]:
    """Every task x agent x condition x repeat, `jobs` at a time (`harness_jobs` caps one CLI, e.g. {"agy": 2}).

    Runs are ordered repeat-major with agents innermost, so concurrent slots spread across providers
    and an interrupted grid still has complete early repeats. The batch directory gets manifest.json
    before the first run (what was asked for, versions, hashes) and again at the end (how it went).
    `dry_run` prints the plan and runs nothing.
    """
    agents = [Agent.parse(a) for a in agents]
    conditions = list(conditions)
    if run_kw.get("declare_columns") or run_kw.get("declare_grain"):
        tasks = declaring(tasks, grain=bool(run_kw.get("declare_grain")))
    preflight(tasks, agents, conditions, db, allow_leaky)
    batch = batch or new_batch_id()
    plan = [(t, a, c, r) for r in range(1, repeats + 1) for t in tasks for c in conditions for a in agents]
    caps = {h: threading.Semaphore(n) for h, n in (harness_jobs or {}).items()}
    if not quiet:
        print(
            f"batch {batch}: {len(plan)} runs ({len(tasks)} tasks x {len(agents)} agents x "
            f"{len(conditions)} conditions x {repeats} repeats), {jobs} parallel"
        )
    if dry_run:
        if not quiet:
            for a in agents:
                print(f"  agent {a}: {a.harness} {a.model} {' '.join(a.extra_args)}".rstrip())
            for t in tasks:
                print(f"  task {t.name} prompt {t.prompt_sha} db {resolve_db(t, db or DB).name}")
        return []
    batch_dir = RESULTS / batch
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest = batch_manifest(batch, tasks, agents, conditions, repeats, jobs, harness_jobs, db, run_kw, len(plan))
    write_manifest(batch_dir, manifest)
    started = time.monotonic()
    results = _execute(plan, batch, jobs, caps, db, quiet, run_kw)
    try:
        (WORK_ROOT / batch).rmdir()  # empty once every run has cleaned up after itself
    except OSError:
        pass
    manifest.update(
        completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        elapsed_seconds=round(time.monotonic() - started, 1),
        recorded_runs=len(results),
    )
    write_manifest(batch_dir, manifest)
    rows = [r.to_row() for r in results]
    with open(batch_dir / "results.jsonl", "w") as f:
        f.writelines(json.dumps(row) + "\n" for row in rows)
    report = summarize(rows)
    (batch_dir / "summary.txt").write_text(report + "\n")
    if not quiet:
        print("\n" + report)
    return results


# ---------- reporting ----------


def progress(batches: list[str] | None = None, results: Path | None = None, now: datetime | None = None) -> str:
    """One line per batch: runs finished of planned, correct so far, pace and time left.

    Reads only manifest.json and each run's result.json, so it works while the batch is running.
    With no `batches`, shows every batch that has a manifest and has not completed.
    """
    results = results or RESULTS
    now = now or datetime.now(UTC)
    lines = [f"{'batch':42} {'done':>11} {'pct':>6} {'correct':>8} {'per min':>8} {'left':>9}  state"]
    for manifest_path in sorted(results.glob("*/manifest.json")):
        m = json.loads(manifest_path.read_text())
        resuming = m.get("resuming")
        finished = "completed_at" in m and not resuming
        if batches and not any(b in m["batch"] for b in batches):
            continue
        if not batches and finished:
            continue
        runs = list(manifest_path.parent.glob("*/result.json"))
        correct = sum(json.loads(p.read_text()).get("correct") in (True, 1, "1") for p in runs)
        # A resume's pace counts only the runs it has finished, from when it started.
        since, base = (resuming["started_at"], resuming["runs_at_start"]) if resuming else (m["created_at"], 0)
        minutes = max((now - datetime.fromisoformat(since)).total_seconds() / 60, 1e-9)
        pace = (len(runs) - base) / minutes
        newest = max((p.stat().st_mtime for p in runs), default=manifest_path.stat().st_mtime)
        quiet_min = (now.timestamp() - newest) / 60
        if finished:
            left, state = "-", "done"
        else:
            left = f"{(m['planned_runs'] - len(runs)) / pace:.0f} min" if pace else "?"
            state = "resuming" if resuming else "running"
            state = state if quiet_min < 15 else f"no new run for {quiet_min:.0f} min"
        lines.append(
            f"{m['batch']:42} {len(runs):>5}/{m['planned_runs']:<5} {100 * len(runs) / m['planned_runs']:5.1f}% "
            f"{correct:>8} {pace:8.1f} {left:>9}  {state}"
        )
    return "\n".join(lines) if len(lines) > 1 else "no unfinished batches"


def load_scorecard(batch: str | None = None, path: Path | None = None) -> list[dict]:
    """Scorecard rows, one per run, optionally one batch ("latest" = most recent)."""
    path = path or SCORECARD
    if not path.exists():
        return []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    if rows and list(rows[0]) != COLS:
        raise RuntimeError(f"{path} has columns {list(rows[0])}; expected {COLS}")
    rows = _latest_per_run(rows)
    if batch == "latest" and rows:
        batch = max(r["batch"] for r in rows)
    return [r for r in rows if batch is None or r["batch"] == batch]


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(rows: list[dict]) -> str:
    """Ladder view: one line per task x condition x agent, k/n correct at each level, then cost and effort."""
    if not rows:
        return "no runs"
    # A harness error (quota, outage, crash) says nothing about the model, so it is left out of k/n, as in report.
    failed = sum(r["verdict"] == "harness_error" for r in rows)
    rows = [r for r in rows if r["verdict"] != "harness_error"]
    if not rows:
        return f"no graded runs; {failed} harness errors"
    # A level whose prompt was edited between batches is split by prompt hash, never pooled.
    shas: dict[tuple, set] = defaultdict(set)
    for r in rows:
        shas[(r["task"], r["level"])].add(r.get("prompt_sha") or "")
    rows = [
        r
        | {
            "level": r["level"]
            if len(shas[(r["task"], r["level"])]) == 1
            else f"{r['level']}@{r.get('prompt_sha') or 'legacy'}"[:12]
        }
        for r in rows
    ]
    ranks: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        rank = _f(r.get("rank"))
        ranks[r["task"]][r["level"]] = min(ranks[r["task"]].get(r["level"], 1e9), 1e9 if rank is None else rank)
    level_order = {t: sorted(lv, key=lambda name: (lv[name], name)) for t, lv in ranks.items()}
    cells = defaultdict(list)
    for r in rows:
        cells[(r["task"], r["condition"], r["agent"])].append(r)

    out = []
    for task in sorted(level_order):
        levels = level_order[task]
        keys = sorted(k for k in cells if k[0] == task)
        width = max(len(k[2]) for k in keys)
        out.append(
            f"{task:<{width + 9}s} " + "  ".join(f"{lv:>12s}" for lv in levels) + "   queries   wall_s   cost/run"
        )
        for key in keys:
            rs = cells[key]
            by_level = defaultdict(list)
            for r in rs:
                by_level[r["level"]].append(r)
            ladder = []
            for lv in levels:
                lr = by_level.get(lv, [])
                k = sum(int(r["correct"]) for r in lr)
                near = sum(int(r["lenient"]) for r in lr) - k
                ladder.append((f"{k}/{len(lr)}" + (f" (+{near})" if near > 0 else "")) if lr else "-")
            q = [_f(r["queries_run"]) for r in rs if _f(r["queries_run"]) is not None]
            w = [_f(r["wall_seconds"]) for r in rs if _f(r["wall_seconds"]) is not None]
            c = [_f(r["cost_usd"]) for r in rs if _f(r["cost_usd"]) is not None]
            out.append(
                f"  {key[1]:6s} {key[2]:{width}s} "
                + "  ".join(f"{x:>12s}" for x in ladder)
                + f"   {statistics.mean(q) if q else 0:7.1f}   {statistics.median(w) if w else 0:6.0f}"
                + f"   {f'${statistics.mean(c):.4f}' if c else 'n/a':>8s}"
            )
        out.append("")

    misses = Counter(r["verdict"] for r in rows if not int(r["correct"]))
    n_ok = sum(int(r["correct"]) for r in rows)
    out.append(f"{n_ok}/{len(rows)} correct" + (f"; misses by verdict: {dict(misses.most_common())}" if misses else ""))
    if failed:
        out.append(f"left out {failed} harness errors (provider failures, crashes); `resume` re-runs provider failures")
    if any(int(r["lenient"]) > int(r["correct"]) for r in rows):
        out.append("(+n): n more runs had the right rows but added extra columns or never called submit_sql")
    fallback = Counter(r["sql_source"] for r in rows if r["sql_source"] in ("final_message", "last_query"))
    if fallback:
        out.append(f"never called submit_sql, graded anyway for diagnosis (never counted correct): {dict(fallback)}")
    return "\n".join(out)
