# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb==2.0.0"]
# ///
"""Audit a saved comparison batch without making any model calls."""

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from evalkit import grading, harnesses, runner

# Recorded standard API prices, verified against this official page on this date.
# Estimates are a comparison basis, not subscription billing or an invoice.
LUNA_PRICING = {
    "model": "gpt-5.6-luna",
    "checked_on": "2026-10-25",
    "source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    "input_per_million": 0.20,
    "cached_input_per_million": 0.02,
    "output_per_million": 1.20,
}


PRICING = {
    "gpt-5.6-luna": LUNA_PRICING | {"context_threshold": 272001},
    "claude-sonnet-5-5": {
        "input_per_million": 2.0,
        "cached_input_per_million": 0.1,
        "output_per_million": 10.0,
        "source": "https://platform.claude.com/docs/en/models/sonnet-5-5/overview",
        "note": "Cache-write estimates withheld when TTL is unavailable; use CLI-reported cost.",
    },
    "grok-4.7": {
        "input_per_million": 2.0,
        "cached_input_per_million": 0.5,
        "output_per_million": 6.0,
        "context_threshold": 200000,
        "source": "https://docs.x.ai/developers/models/grok-4.7",
    },
    "gemini-3.8-flash-medium": {
        "input_per_million": 0.75,
        "cached_input_per_million": 0.075,
        "output_per_million": 3.75,
        "source": "https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash",
        "note": "Standard introductory prices through 2026-12-31; no explicit cache storage created.",
    },
}
PRICING["gemini-3.8-flash"] = PRICING["gemini-3.8-flash-medium"]


def strict_cell(got, expected):
    """Preserve exact numerics; permit floating noise but no rounding allowance."""
    if isinstance(got, grading.Num) and isinstance(expected, grading.Num):
        if got.exact is not None and expected.exact is not None:
            return got.exact == expected.exact
        return math.isclose(got, expected, rel_tol=1e-12, abs_tol=0.0)
    return got == expected


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("batch", type=Path)
    args = ap.parse_args()
    batch = args.batch.resolve()
    manifest = json.loads((batch / "manifest.json").read_text())
    rows = [json.loads(s) for s in (batch / "results.jsonl").read_text().splitlines() if s.strip()]
    if len(rows) != manifest["planned_runs"]:
        raise ValueError("Incomplete batch: result count does not match the manifest")
    db = batch / "snapshot/orders.duckdb"
    target = (batch / "snapshot/target.sql").read_text()
    expected = grading.execute(target, db, guard=False)
    audits, metrics = [], {}
    for level, agent in sorted({(r["level"], r["agent"]) for r in rows}):
        subset = [r for r in rows if r["level"] == level and r["agent"] == agent]
        harness = subset[0]["harness"]
        usages = []
        durations = [r["wall_seconds"] for r in subset]
        counts, models = Counter(), Counter()
        sql_texts, strict_passes, candidate_strict_passes = set(), 0, 0
        for row in subset:
            run = batch / Path(row["run_dir"]).name
            entries = runner._log_entries(run)
            calls = Counter(k for k, _ in entries)
            counts.update(calls)
            audit = {
                "run": run.name,
                "strict_ordered_unrounded_match": False,
                "query_log_counts": dict(calls),
                "candidate_columns": [],
                "sql_source": row["sql_source"],
            }
            answer = run / "answer.sql"
            submitted = answer.exists()
            sql = answer.read_text() if submitted else None
            if not submitted and row["sql_source"] == "last_query":
                sql = next((s for k, s in reversed(entries) if k == "RUN"), None)
            elif not submitted and row["sql_source"] == "final_message":
                sql = harnesses.sql_from_text((run / "final_message.txt").read_text())
            audit["submitted"] = submitted
            if sql:
                sql_texts.add(" ".join(sql.lower().split()))
                grade = grading.grade_sql(sql, target, db)
                audit.update(regraded_verdict=grade.verdict, detail=grade.detail)
                try:
                    got = grading.execute(sql, db)
                    strict = (
                        len(got.columns) == len(expected.columns)
                        and len(got.rows) == len(expected.rows)
                        and all(all(strict_cell(x, y) for x, y in zip(a, b)) for a, b in zip(got.rows, expected.rows))
                    )
                    audit.update(strict_ordered_unrounded_match=strict, candidate_columns=list(got.columns))
                    strict_passes += int(strict and submitted)
                    candidate_strict_passes += int(strict)
                except grading.SQLError as e:
                    audit["audit_error"] = str(e)
            else:
                audit["regraded_verdict"] = "no_sql"
            ctx = harnesses.RunContext(
                prompt="", model=row["model"], work_dir=run, run_dir=run, db=db, notes=None, max_turns=0, timeout_s=0
            )
            usage = harnesses.get(harness).parse(ctx, run / "transcript.jsonl")
            usages.append(usage)
            if harness == "pi":
                for ev in harnesses._jsonl(run / "transcript.jsonl"):
                    msg = ev.get("message", {})
                    if ev.get("type") == "message_end" and msg.get("role") == "assistant":
                        models[msg.get("model", "unknown")] += 1
            elif harness == "grok":
                models.update({k: v.get("modelCalls", 1) for k, v in usage.extra.get("modelUsage", {}).items()})
            audits.append(audit)
        p95 = statistics.quantiles(durations, n=100, method="inclusive")[94] if len(durations) > 1 else durations[0]
        metrics.setdefault(level, {})[agent] = {
            "harness": harness,
            "agent": agent,
            "model": subset[0]["model"],
            "n": len(subset),
            "correct": sum(int(r["correct"]) for r in subset),
            "strict_passes": strict_passes,
            "candidate_strict_passes": candidate_strict_passes,
            "verdicts": dict(Counter(r["verdict"] for r in subset)),
            "submission_sources": dict(Counter(r["sql_source"] for r in subset)),
            "process_errors": dict(Counter(r["error"] for r in subset if r.get("error"))),
            "exit_codes": dict(Counter(str(r["exit_code"]) for r in subset)),
            "median_seconds": statistics.median(durations),
            "p95_seconds": p95,
            "mean_seconds": statistics.mean(durations),
            "min_seconds": min(durations),
            "max_seconds": max(durations),
            "sql_attempts": counts["RUN"] + counts["ERROR"],
            "mean_queries": (counts["RUN"] + counts["ERROR"]) / len(subset),
            "successful_sql_calls": counts["RUN"],
            "failed_sql_calls": counts["ERROR"],
            "refused_sql_calls": counts["REFUSED"],
            "submit_calls": counts["SUBMIT"],
            "failed_submit_calls": counts["SUBMIT-ERROR"],
            "sum_run_seconds": sum(durations),
            "query_log_counts": dict(counts),
            "unique_sql_texts": len(sql_texts),
            "cost_usd": sum(u.cost_usd for u in usages) if all(u.cost_usd is not None for u in usages) else None,
            "model_messages": dict(models),
            "tokens_uncached_in": sum(u.tokens_in for u in usages),
            "tokens_cached_in": sum(u.tokens_cache_read for u in usages),
            "tokens_out": sum(u.tokens_out for u in usages),
        }
        group = metrics[level][agent]
        model = subset[0]["model"].split("/")[-1]
        price = PRICING.get(model)
        estimate = None
        full_input = [u.tokens_in + u.tokens_cache_read + u.tokens_cache_write for u in usages]
        if (
            price
            and all(n < price.get("context_threshold", float("inf")) for n in full_input)
            and all(u.tokens_cache_write == 0 or "cache_write_per_million" in price for u in usages)
        ):
            estimate = sum(
                (
                    u.tokens_in * price["input_per_million"]
                    + u.tokens_cache_read * price["cached_input_per_million"]
                    + u.tokens_cache_write * price.get("cache_write_per_million", 0)
                    + u.tokens_out * price["output_per_million"]
                )
                / 1_000_000
                for u in usages
            )
        cost = group["cost_usd"]
        display_cost = cost if cost is not None else estimate
        group.update(
            api_equivalent_cost_usd=estimate,
            display_cost_usd=display_cost,
            cost_per_run_usd=display_cost / len(subset) if display_cost is not None else None,
            cost_basis="CLI-reported"
            if cost is not None
            else "API-equivalent estimate"
            if estimate is not None
            else "unavailable",
            tokens_cache_write=sum(u.tokens_cache_write for u in usages),
            max_run_input_tokens=max(full_input),
            usage_reparsed_from_raw_transcripts=True,
        )
    analysis = {
        "batch": batch.name,
        "task": manifest["task"],
        "levels": sorted({r["level"] for r in rows}),
        "metrics": metrics,
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "audit_grader_sha256": hashlib.sha256(Path(grading.__file__).read_bytes()).hexdigest(),
        "strict_audit_note": "Ordered numeric result comparison; aliases and final prose are not scored.",
        "query_count_note": "SQL attempts include successful and failed run_sql executions; submit_sql and refusals are separate.",
        "api_equivalent_pricing": PRICING,
        "cost_note": "Standard API token-cost estimates are not actual subscription charges. No estimate is made when request-level pricing cannot be resolved from these logs.",
        "independent_audit": audits,
    }
    (batch / "analysis.json").write_text(json.dumps(analysis, indent=2) + "\n")
    print(json.dumps({"batch": batch.name, "metrics": metrics}, indent=2))


if __name__ == "__main__":
    main()
