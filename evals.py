# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Command line for evalkit. Every run is graded by executing the agent's SQL and the target SQL.

  uv run evals.py run questions/tpch/q21.toml --agents pi:openai/gpt-5.6-luna codex:gpt-5.6-luna --repeats 3
  uv run evals.py run questions/tpch --levels terse spec --conditions bare notes --agents agy:gemini-3.8-flash-low -j 8
  uv run evals.py ask "Which Saudi suppliers made orders late?" --target-sql-file tpch/queries/q21.sql \
      --agents codex:gpt-5.6-luna --repeats 5
  uv run evals.py grade my_attempt.sql --target-sql-file tpch/queries/q21.sql    # no agent, just the grader
  uv run evals.py summary [--batch latest | <batch id>]
  uv run evals.py progress [<batch id or substring> ...]                              # while a batch runs
  uv run evals.py resume <batch id> -j 6                                             # re-run unfinished and provider-failed runs
  uv run evals.py resume <batch id> -a agy:gemini-3.8-flash                         # only one agent's runs
  uv run evals.py report <batch id> [<batch id> ...] --csv report.csv                   # accuracy + efficiency
  uv run evals.py audit <batch id> [<batch id> ...] --out audit/                        # cheating + prior exposure
  uv run evals.py findings <batch id> [<batch id> ...] --out findings/ --audit audit/   # claims, with intervals
  uv run evals.py regrade <batch id> [...] --questions questions/tpch-novel-v2 --name X  # new reference, same runs
  uv run evals.py harnesses                                                           # what is installed

Agents are harness:model. Models are named the way that CLI names them (pi: provider/model).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit import (
    HARNESSES,
    Agent,
    Task,
    audit,
    findings,
    grade_sql,
    load_questions,
    load_scorecard,
    progress,
    regrade,
    report,
    run_grid,
    summarize,
)
from evalkit.runner import DB, new_batch_id, reparse, resume


def _grid_args(p: argparse.ArgumentParser):
    p.add_argument("--agents", "-a", nargs="+", default=[], help="harness:model, e.g. codex:gpt-5.6-luna")
    p.add_argument(
        "--agents-json",
        type=Path,
        help='file with a list of {"harness", "model", "extra_args", "label"}; extra_args go to the CLI verbatim',
    )
    p.add_argument("--conditions", "-c", nargs="+", default=["bare"], help="conditions/<name> (default: bare)")
    p.add_argument("--repeats", "-r", type=int, default=1)
    p.add_argument("--jobs", "-j", type=int, default=4, help="parallel runs (default 4)")
    p.add_argument("--harness-jobs", nargs="*", default=[], metavar="HARNESS=N", help="per-CLI cap, e.g. agy=2")
    p.add_argument("--timeout", type=int, default=900, help="seconds per run (default 900)")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--name", help="suffix for the batch id")
    p.add_argument("--keep-work", action="store_true", help="keep the agent's scratch dir for debugging")
    p.add_argument("--allow-leaky", action="store_true", help="skip the TPC-H / query-number prompt check")
    p.add_argument("--dry-run", action="store_true", help="print the plan and each prompt's hash, run nothing")
    _db_arg(p)


def _db_arg(p: argparse.ArgumentParser):
    p.add_argument("--db", type=Path, help="database for tasks that do not name one (default: tpch.duckdb)")


def _target(args) -> str:
    if bool(args.target_sql) == bool(args.target_sql_file):
        raise SystemExit("give exactly one of --target-sql / --target-sql-file")
    return args.target_sql or Path(args.target_sql_file).read_text()


def _agents(args) -> list[Agent]:
    agents = [Agent.parse(a) for a in args.agents]
    if args.agents_json:
        for spec in json.loads(args.agents_json.read_text()):
            agents.append(Agent(spec["harness"], spec["model"], tuple(spec.get("extra_args", ())), spec.get("label")))
    if not agents:
        raise SystemExit("give --agents and/or --agents-json")
    slugs = [a.slug for a in agents]
    if len(set(slugs)) != len(slugs):
        raise SystemExit(f"agent labels must be unique: {slugs}")
    return agents


def _grid(tasks, args):
    caps = {k: int(v) for k, v in (s.split("=", 1) for s in args.harness_jobs)}
    results = run_grid(
        tasks,
        _agents(args),
        conditions=args.conditions,
        repeats=args.repeats,
        jobs=args.jobs,
        harness_jobs=caps,
        batch=new_batch_id(args.name),
        db=args.db,
        allow_leaky=args.allow_leaky,
        timeout_s=args.timeout,
        max_turns=args.max_turns,
        keep_work=args.keep_work,
        dry_run=args.dry_run,
    )
    return 0 if results or args.dry_run else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="question files (or dirs of them) x agents x conditions x repeats")
    p.add_argument("questions", nargs="+", help="question files or directories, e.g. questions/tpch/q21.toml")
    p.add_argument("--levels", "-l", nargs="+", help="only these disclosure levels")
    _grid_args(p)

    p = sub.add_parser("ask", help="an arbitrary prompt graded against a target query you supply")
    p.add_argument("prompt", help="the question exactly as the agent should see it (or @file.md)")
    p.add_argument("--target-sql")
    p.add_argument("--target-sql-file")
    p.add_argument("--id", help="task id for the scorecard (default adhoc-<hash>)")
    _grid_args(p)

    p = sub.add_parser("grade", help="grade a SQL file against a target query, no agent involved")
    p.add_argument("sql_file")
    p.add_argument("--target-sql")
    p.add_argument("--target-sql-file")
    _db_arg(p)

    p = sub.add_parser("summary", help="ladder table from results/scorecard.csv")
    p.add_argument("--batch", default="latest", help="batch id, 'latest' (default) or 'all'")

    sub.add_parser("harnesses", help="which agent CLIs are on PATH")

    p = sub.add_parser("progress", help="runs finished, correct so far and time left for unfinished batches")
    p.add_argument("batches", nargs="*", help="batch ids or substrings (default: every unfinished batch)")

    p = sub.add_parser("resume", help="finish a batch: re-run what never ran or failed on the provider's side")
    p.add_argument("batch", help="batch id (the folder name under results/)")
    p.add_argument("--jobs", "-j", type=int, default=4, help="parallel runs (default 4)")
    p.add_argument("--harness-jobs", nargs="*", default=[], metavar="HARNESS=N", help="per-CLI cap, e.g. agy=2")
    p.add_argument("--agents", "-a", nargs="+", help="only re-run these agents (labels as the batch recorded them)")

    p = sub.add_parser("report", help="accuracy and efficiency by agent, question set and level, across batches")
    p.add_argument("batches", nargs="+", help="batch ids (folder names under results/)")
    p.add_argument("--csv", type=Path, help="also write every cell as CSV")

    p = sub.add_parser("audit", help="cheating attempts and signs of prior exposure, with evidence, across batches")
    p.add_argument("batches", nargs="+", help="batch ids (folder names under results/)")
    p.add_argument("--out", type=Path, required=True, help="folder for summary.md, flagged.md, runs.jsonl, markers.json")
    _db_arg(p)

    p = sub.add_parser("findings", help="the claims the batches support, with intervals over questions")
    p.add_argument("batches", nargs="+", help="batch ids (folder names under results/)")
    p.add_argument("--out", type=Path, required=True, help="folder for findings.md, findings.json, terse-misses.md")
    p.add_argument("--audit", type=Path, required=True, help="the audit folder (its runs.jsonl) for these batches")

    p = sub.add_parser("regrade", help="grade saved runs against new question versions with the same prompts")
    p.add_argument("batches", nargs="+", help="batch ids whose runs to re-grade")
    p.add_argument("--questions", type=Path, nargs="+", required=True, help="the new question files or folders")
    p.add_argument("--name", required=True, help="name for the re-graded batch, e.g. novel-v2-regrade")

    p = sub.add_parser("reparse", help="recompute turns, tokens and cost from saved transcripts (after a parser fix)")
    p.add_argument("batch", help="batch id (the folder name under results/)")

    args = ap.parse_args(argv)
    if args.cmd == "report":
        rep = report.build(report.load_rows(args.batches), report.question_sets())
        print(report.markdown(rep))
        if args.csv:
            report.write_csv(rep, args.csv)
        return 0
    if args.cmd == "audit":
        done = audit.run_audit(args.batches, args.out, db=args.db)
        print(f"audited {done['runs']} runs; see {done['out_dir']}/summary.md and flagged.md")
        return 0
    if args.cmd == "findings":
        findings.run_findings(args.batches, args.out, audit_runs=args.audit / "runs.jsonl")
        print(f"wrote {args.out}/findings.md, findings.json and terse-misses.md")
        return 0
    if args.cmd == "regrade":
        regrade.regrade(args.batches, args.questions, args.name)
        return 0
    if args.cmd == "reparse":
        reparse(args.batch)
        return 0
    if args.cmd == "resume":
        caps = {k: int(v) for k, v in (s.split("=", 1) for s in args.harness_jobs)}
        resume(args.batch, jobs=args.jobs, harness_jobs=caps, agents=args.agents)
        return 0
    if args.cmd == "progress":
        print(progress(args.batches))
        return 0
    if args.cmd == "run":
        return _grid(load_questions(*args.questions, levels=args.levels), args)
    if args.cmd == "ask":
        prompt = Path(args.prompt[1:]).read_text() if args.prompt.startswith("@") else args.prompt
        return _grid([Task.adhoc(prompt, _target(args), id=args.id)], args)
    if args.cmd == "grade":
        g = grade_sql(Path(args.sql_file).read_text(), _target(args), args.db or DB)
        print(f"{g.verdict} ({g.rows_got}/{g.rows_expected} rows){': ' + g.detail if g.detail else ''}")
        return 0 if g.correct else 1
    if args.cmd == "summary":
        print(summarize(load_scorecard(None if args.batch == "all" else args.batch)))
        return 0
    if args.cmd == "harnesses":
        for name, h in HARNESSES.items():
            print(f"{name:7s} {'ok' if h.available() else 'missing':8s} {(h.__doc__ or '').split('. ')[0].strip()}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
