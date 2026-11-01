"""Re-grade saved runs against a new version of a question, without running any agent again.

A run is valid evidence for a new question version only if the agent saw the same prompt: the reference is
never shown to the agent, so a version that changes only the reference (or adds accepted alternatives) leaves
every run's behavior as it was. Runs are matched to the new version by level and prompt hash, never by name,
so a version whose brief changed matches nothing and has to be run.

The re-graded batch is a new folder under results/. Each run gets its own folder holding the small files a
reader needs (prompt, the graded SQL, the query log, the final message) and a result.json whose
`regraded_from` names the original run; transcripts stay with the original. Re-grades are not runs, so they
are not added to the scorecard.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from . import grading, harnesses
from .declared import ALL, grade_task
from .declared import read_declaration as recorded_declaration
from .runner import COLS, RESULTS, _git_state, _log_entries, _sha256, write_manifest
from .tasks import load_questions

COPIED = ("prompt.md", "answer.sql", "answer_columns.json", "queries.log", "final_message.txt", "meta.json")


def graded_sql(run_dir: Path) -> tuple[str | None, str]:
    """The SQL a run was graded on and where it came from, decided as run_one decides it."""
    if (answer := run_dir / "answer.sql").exists():
        return answer.read_text(), "submit"
    final = run_dir / "final_message.txt"
    if final.exists() and (sql := harnesses.sql_from_text(final.read_text())):
        return sql, "final_message"
    if last := next((sql for kind, sql in reversed(_log_entries(run_dir)) if kind == "RUN"), None):
        return last, "last_query"
    return None, "none"


def regrade(
    batches: list[str],
    questions: list[Path],
    name: str,
    *,
    results: Path | None = None,
    quiet: bool = False,
    now: datetime | None = None,
    declare_all: bool = False,
) -> dict:
    """Grade every matching run of `batches` against the question files in `questions`; write a new batch.

    A run that declared its answer columns (`answer_columns.json`) is graded by them (see `declared`).
    `declare_all` grades every run as if it had declared all of its result columns: the check of what pairing
    rows by label and ignoring context columns would change for runs that declared nothing.
    """
    results = results or RESULTS
    tasks = load_questions(*questions)
    by_prompt = {(t.level, t.prompt_sha): t for t in tasks}
    if len(by_prompt) < len(tasks):
        raise ValueError("two question versions share a level and prompt; a run could not say which it answers")
    now = now or datetime.now(UTC)
    # A fixed name, so a script can rebuild it: a re-grade is derived from saved runs and replaces its last build.
    batch = f"regrade-{name}"
    out = results / batch
    if out.exists():
        old = json.loads((out / "manifest.json").read_text()) if (out / "manifest.json").exists() else {}
        if old.get("kind") != "regrade":
            raise FileExistsError(f"{out} exists and is not a re-grade; refusing to replace it")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rows, matched, mismatched_source = [], {t.name: 0 for t in tasks}, []
    for b in batches:
        for result in sorted((results / b).glob("*/result.json")):
            row = json.loads(result.read_text())
            task = by_prompt.get((row.get("level"), row.get("prompt_sha")))
            if task is None or row.get("verdict") == "harness_error":
                continue  # a provider failure has no answer to grade; it stays owed to the original batch
            src = result.parent
            sql, source = graded_sql(src)
            if source != row.get("sql_source"):
                mismatched_source.append(row["run_dir"])
            declared = ALL if declare_all else recorded_declaration(src)
            g, ref, notes = (
                grade_task(sql, declared, task, task.db)
                if declared is not None
                else (
                    *grading.grade_any(sql, task.target_sql, task.accepted_sql, task.db),
                    {},
                )
            )
            dest = out / src.name.replace(f"{row['task']}-", f"{task.id}-", 1)
            dest.mkdir()
            for f in COPIED:
                if (src / f).exists():
                    shutil.copy2(src / f, dest / f)
            new = row | {
                "batch": batch,
                "task": task.id,
                "verdict": g.verdict,
                "correct": int(g.correct and source == "submit"),
                "lenient": int(g.verdict in grading.LENIENT),
                "rows_returned": g.rows_got,
                "rows_expected": g.rows_expected,
                "detail": g.detail,
                "column_map": g.column_map,
                "reference": ref,
                "declaration": notes,
                "run_dir": str(dest.relative_to(results.parent)),
                "regraded_from": row["run_dir"],
                "original": {k: row.get(k) for k in ("task", "verdict", "correct", "lenient")},
            }
            (dest / "result.json").write_text(json.dumps(new, indent=2))
            rows.append(new)
            matched[task.name] += 1
    manifest = {
        "batch": batch,
        "kind": "regrade",
        "declared": "every result column" if declare_all else "as recorded",
        "created_at": now.isoformat(timespec="seconds"),
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": _git_state(),
        "regraded_from": batches,
        "planned_runs": len(rows),
        "recorded_runs": len(rows),
        "tasks": [
            {
                "id": t.id,
                "level": t.level,
                "prompt_sha": t.prompt_sha,
                "target_sha256": _sha256(t.target_sql.encode()),
                **({"accepted_sha256": [_sha256(a.encode()) for a in t.accepted_sql]} if t.accepted_sql else {}),
                "runs": matched[t.name],
            }
            for t in tasks
        ],
        "sql_source_mismatches": mismatched_source,
    }
    write_manifest(out, manifest)
    with open(out / "results.jsonl", "w") as f:
        f.writelines(json.dumps({k: r.get(k, "") for k in COLS}) + "\n" for r in rows)
    if not quiet:
        print(f"batch {batch}: re-graded {len(rows)} runs; per question: {matched}")
        if mismatched_source:
            print(f"warning: {len(mismatched_source)} runs' graded SQL source differs from the record")
    return {"batch": batch, "runs": len(rows), "matched": matched, "mismatched": mismatched_source}
