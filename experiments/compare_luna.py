# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==2.0.0"]
# ///
"""Reproducible model/harness comparison, defaulting to Pi/Codex GPT-5.6 Luna."""

import argparse
import dataclasses
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    source = Path(__file__).resolve().parents[1]
    ap.add_argument(
        "--question", choices=sorted(p.stem for p in (source / "questions/tpch").glob("*.toml")), default="q01"
    )
    ap.add_argument("--repeats", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=12)
    ap.add_argument("--level", dest="levels", choices=("terse", "stakeholder", "spec"), nargs="+", default=["spec"])
    ap.add_argument("--name")
    ap.add_argument("--agents-json", type=Path, help="JSON list of explicit Agent configurations")
    args = ap.parse_args()
    if args.repeats < 1 or args.jobs < 1:
        ap.error("repeats and jobs must be positive")
    if len(set(args.levels)) != len(args.levels):
        ap.error("prompt levels must be unique")
    name = args.name or f"{args.question}-{'-'.join(args.levels)}-pi-codex-luna-medium"
    batch = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + name
    out = source / "results" / batch
    snapshot = out / "snapshot"
    snapshot.mkdir(parents=True)
    for name in ("evalkit", "extensions", "conditions"):
        shutil.copytree(source / name, snapshot / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("mcp_server.py", "sqlguard.py"):
        shutil.copy2(source / name, snapshot / name)
    question = source / f"questions/tpch/{args.question}.toml"
    shutil.copy2(question, snapshot / "question.toml")
    shutil.copy2(source / f"tpch/queries/{args.question}.sql", snapshot / "target.sql")
    shutil.copy2(source / "tpch.duckdb", snapshot / "orders.duckdb")
    shutil.copy2(__file__, snapshot / "experiment.py")

    sys.path.insert(0, str(snapshot))
    import duckdb

    from evalkit import Agent, load_question, runner

    tasks = [dataclasses.replace(task, db=snapshot / "orders.duckdb") for task in load_question(question, args.levels)]
    agents = [
        Agent("pi", "openai/gpt-5.6-luna", extra_args=("--thinking", "medium"), label="pi:luna-medium"),
        Agent("codex", "gpt-5.6-luna", extra_args=("-c", 'model_reasoning_effort="medium"'), label="codex:luna-medium"),
    ]
    if args.agents_json:
        agents = [
            Agent(**(a | {"extra_args": tuple(a.get("extra_args", []))}))
            for a in json.loads(args.agents_json.read_text())
        ]
        if not agents or len({a.slug for a in agents}) != len(agents):
            raise ValueError("Agent configurations must have unique names and cannot be empty")
        shutil.copy2(args.agents_json, snapshot / "agents.json")
    planned_runs = len(tasks) * len(agents) * args.repeats
    runner.RESULTS = source / "results"
    runner.SCORECARD = out / "scorecard.csv"  # isolate from unrelated concurrent experiments
    versions = {}
    for binary in sorted({a.harness for a in agents} | {"duckdb"}):
        executable = Path(shutil.which(binary)).resolve()
        if binary == "agy":
            versions[binary] = {"path": str(executable), "sha256": sha(executable)}
        else:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True, timeout=30)
            versions[binary] = result.stdout.strip()
    versions["python"] = sys.version
    versions["python_duckdb"] = duckdb.__version__
    manifest = {
        "batch": batch,
        "task": args.question,
        "levels": [task.level for task in tasks],
        "level": tasks[0].level if len(tasks) == 1 else "mixed",
        "condition": "bare",
        "prompt_shas": {task.level: task.prompt_sha for task in tasks},
        "repeats_per_harness_per_level": args.repeats,
        "planned_runs": planned_runs,
        "jobs": args.jobs,
        "reasoning_effort": "medium",
        "timeout_seconds": 300,
        "versions": versions,
        "agents": [dataclasses.asdict(a) for a in agents],
        "snapshot_sha256": {
            str(p.relative_to(snapshot)): sha(p)
            for p in snapshot.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        },
        "notes": [
            "No failed-run replacement or selective reruns.",
            "Requested model IDs are recorded per agent; API and subscription backends may differ.",
            "TPC-H schema can be recognized from training; this is a single-question repeatability test.",
            "Prompt levels and harnesses are interleaved within each repeat.",
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"EXPERIMENT_DIR={out}", flush=True)
    active = peak = 0
    lock = threading.Lock()
    launch = runner._launch

    def traced_launch(cmd, ctx, env, timeout_s):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            with (out / "concurrency.jsonl").open("a") as f:
                f.write(
                    json.dumps({"event": "start", "time": time.time(), "active": active, "run": ctx.run_dir.name})
                    + "\n"
                )
        try:
            return launch(cmd, ctx, env, timeout_s)
        finally:
            with lock:
                active -= 1
                with (out / "concurrency.jsonl").open("a") as f:
                    f.write(
                        json.dumps({"event": "end", "time": time.time(), "active": active, "run": ctx.run_dir.name})
                        + "\n"
                    )

    runner._launch = traced_launch
    t0 = time.monotonic()
    results = runner.run_grid(
        tasks, agents, conditions=["bare"], repeats=args.repeats, jobs=args.jobs, batch=batch, timeout_s=300
    )
    manifest.update(
        completed_at=datetime.now(UTC).isoformat(),
        elapsed_seconds=time.monotonic() - t0,
        observed_peak_concurrency=peak,
        recorded_runs=len(results),
    )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    if len(results) != planned_runs:
        raise RuntimeError("Incomplete experiment: attempts are missing")


if __name__ == "__main__":
    main()
