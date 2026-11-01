"""Accuracy and efficiency across batches: by agent, question set and brief level.

Runs that failed on the provider's side (`harness_error`) say nothing about the model, so they are left out of
every rate and counted separately; misses, timeouts and non-submissions stay in. Costs are per-run means over the
graded runs, each labeled with its basis (see `pricing`), and "per correct" divides the total by correct runs, so
a model that burns tokens and fails pays for it.
"""

from __future__ import annotations

import csv
import json
import statistics
import tomllib
from collections import defaultdict
from pathlib import Path

from . import pricing
from .runner import HARNESS_DIR, RESULTS

LEVELS = ["terse", "stakeholder", "spec"]


def question_sets(questions: Path | None = None) -> dict[str, str]:
    """Task id -> the question folder it lives in (tpch, tpch-novel, tpch-hard)."""
    out = {}
    for f in sorted((questions or HARNESS_DIR / "questions").glob("*/*.toml")):
        out[tomllib.loads(f.read_text()).get("id", f.stem)] = f.parent.name
    return out


def load_rows(batches: list[str], results: Path | None = None) -> list[dict]:
    """Every current run of the batches (replaced attempts under `_replaced/` are not read)."""
    rows = []
    for b in batches:
        found = sorted(((results or RESULTS) / b).glob("*/result.json"))
        if not found:
            raise FileNotFoundError(f"no runs under results/{b}")
        rows += [json.loads(p.read_text()) for p in found]
    return rows


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_cost(row: dict) -> float | None:
    if (c := _num(row.get("cost_usd"))) is not None:
        return c
    return pricing.api_cost(
        row.get("model", ""),
        _num(row.get("tokens_in")) or 0,
        _num(row.get("tokens_cache_read")) or 0,
        _num(row.get("tokens_out")) or 0,
    )


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def cell(rows: list[dict]) -> dict:
    """Aggregates for one group of runs."""
    graded = [r for r in rows if r.get("verdict") != "harness_error"]
    n, k = len(graded), sum(bool(int(r.get("correct") or 0)) for r in graded)
    lenient = sum(bool(int(r.get("lenient") or 0)) for r in graded)
    costs = [run_cost(r) for r in graded]
    known = [c for c in costs if c is not None]
    fields = ("tokens_in", "tokens_cache_read", "tokens_cache_write", "tokens_out")
    tok = {f: _mean(_num(r.get(f)) for r in graded) for f in fields}
    # Every token the model processed: cache reads and writes are cheaper per token but are still context it read.
    processed = sum(sum(_num(r.get(f)) or 0 for f in fields) for r in graded)
    return {
        "runs": len(rows),
        "excluded": len(rows) - n,
        "graded": n,
        "correct": k,
        "strict": k / n if n else None,
        "lenient": lenient / n if n else None,
        "submitted": sum(r.get("sql_source") == "submit" for r in graded) / n if n else None,
        "queries": _mean(_num(r.get("queries_run")) for r in graded),
        "wall_s": statistics.median(w)
        if (w := [_num(r.get("wall_seconds")) for r in graded if r.get("wall_seconds")])
        else None,
        **tok,
        "cost": statistics.mean(known) if known and len(known) == n else None,
        "cost_per_correct": sum(known) / k if known and len(known) == n and k else None,
        "tokens_per_correct": processed / k if k else None,
    }


def build(rows: list[dict], sets: dict[str, str]) -> dict:
    agents = list(dict.fromkeys(r["agent"] for r in rows))
    meta = {a: next(r for r in rows if r["agent"] == a) for a in agents}
    groups = defaultdict(list)
    for r in rows:
        s = sets.get(r["task"], "other")
        for key in (
            (r["agent"], s, r["level"]),
            (r["agent"], s, "all"),
            (r["agent"], "all", r["level"]),
            (r["agent"], "all", "all"),
        ):
            groups[key].append(r)
    return {
        "agents": agents,
        "sets": sorted({sets.get(r["task"], "other") for r in rows}),
        "basis": {a: pricing.cost_basis(meta[a]["harness"], meta[a]["model"]) for a in agents},
        "cells": {k: cell(v) for k, v in groups.items()},
    }


def _pct(x):
    return "-" if x is None else f"{100 * x:.0f}%"


def _f(x, fmt):
    return "-" if x is None else format(x, fmt)


def _usd(x):
    return "-" if x is None else f"${x:.4f}"


def markdown(rep: dict) -> str:
    cells, agents = rep["cells"], rep["agents"]
    out = [
        "# Accuracy",
        "",
        (
            "Correct over graded runs: every column the reference asks for, with the right values; extra columns"
            " pass and are noted in the verdicts. In parentheses, lenient: also right answers that were never"
            " submitted. n is graded runs per cell."
        ),
        "",
    ]
    for s in rep["sets"] + ["all"]:
        out += [
            f"## {s}",
            "",
            "| agent | " + " | ".join(LEVELS + ["all"]) + " | n | excluded |",
            "|---" * 7 + "|",
        ]
        for a in agents:
            if (a, s, "all") not in cells:
                continue
            row = [
                f"{_pct(c['strict'])} ({_pct(c['lenient'])})" if (c := cells.get((a, s, lv))) else "-"
                for lv in LEVELS + ["all"]
            ]
            tot = cells[(a, s, "all")]
            out.append(f"| {a} | " + " | ".join(row) + f" | {tot['graded']} | {tot['excluded']} |")
        out.append("")
    out += [
        "# Efficiency",
        "",
        (
            "Means per graded run, all question sets and levels. Wall time is the median. "
            "Tokens in exclude cache reads and writes; tokens/correct counts all four kinds."
        ),
        "",
        (
            "| agent | correct | queries | wall s | tokens in | cache read | cache write | tokens out | cost/run "
            "| cost/correct | tokens/correct |"
        ),
        "|---" * 11 + "|",
    ]
    for a in agents:
        c = cells[(a, "all", "all")]
        out.append(
            f"| {a} | {_pct(c['strict'])} | {_f(c['queries'], '.1f')} | {_f(c['wall_s'], '.0f')} "
            f"| {_f(c['tokens_in'], ',.0f')} | {_f(c['tokens_cache_read'], ',.0f')} | {_f(c['tokens_cache_write'], ',.0f')} | {_f(c['tokens_out'], ',.0f')} "
            f"| {_usd(c['cost'])} | {_usd(c['cost_per_correct'])} "
            f"| {_f(c['tokens_per_correct'], ',.0f')} |"
        )
    out += ["", "Cost basis:", ""] + [f"- {a}: {b}" for a, b in rep["basis"].items()]
    return "\n".join(out) + "\n"


FIELDS = [
    "agent",
    "set",
    "level",
    "runs",
    "excluded",
    "graded",
    "correct",
    "strict",
    "lenient",
    "submitted",
    "queries",
    "wall_s",
    "tokens_in",
    "tokens_cache_read",
    "tokens_cache_write",
    "tokens_out",
    "cost",
    "cost_per_correct",
    "tokens_per_correct",
    "cost_basis",
]


def write_csv(rep: dict, path: Path):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for (a, s, lv), c in sorted(rep["cells"].items()):
            w.writerow({"agent": a, "set": s, "level": lv, **c, "cost_basis": rep["basis"][a]})
