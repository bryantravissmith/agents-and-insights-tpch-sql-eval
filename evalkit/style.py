"""How each model family writes SQL, and how much it stumbles on the dialect, by question set.

Read from what every run recorded: `queries.log` (every `run_sql` call with its outcome; a failed call's header
carries DuckDB's error type) and `answer.sql` (the submitted query). Provider failures are left out. Every table is
sliced by question set (TPC-H, novel, hard): a habit that shows on TPC-H questions and not on the others is
the benchmark's habit, not the model's (see `audit.COMMA_JOIN`).

- Dialect: queries per run, schema lookups, failed queries, DuckDB's error types, and calls to functions DuckDB
  does not have (a function from another dialect, such as Postgres's `to_char`, fails as "does not exist").
  The tool description names the engine (DuckDB); nothing describes its dialect.
- Style: the share of submitted queries that use each construct in `CONSTRUCTS`, matched on code with string
  literals and comments removed, and the length of the submitted query.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from sqlguard import scan

from .audit import _DISCOVERY, COMMA_JOIN
from .findings import SET_ORDER, maker
from .report import load_rows, question_sets
from .runner import HARNESS_DIR

# SQL tool calls only: ask_clarification's ASK entries (a question, not SQL) never match. The kind must be whole,
# so REFUSED-SUBMIT and SUBMIT-ERROR (submit_sql calls) aren't read as a refused or a run_sql query.
_ENTRY = re.compile(
    r"^-- \S+ (RUN|ERROR|REFUSED|SUBMIT|SUBMIT-ERROR|REFUSED-SUBMIT)(?![\w-]) ?([^\n]*)\n(.*?)\n;\n",
    re.DOTALL | re.MULTILINE,
)
_MISSING_FUNCTION = re.compile(
    r"(?:Scalar|Aggregate|Table) Function with name \"?(\w+)\"? does not exist", re.IGNORECASE
)

CONSTRUCTS = {
    "comma joins (FROM a, b)": COMMA_JOIN,
    "CTE (WITH)": re.compile(r"^\s*WITH\b", re.IGNORECASE),
    "window function (OVER)": re.compile(r"\bOVER\s*\(", re.IGNORECASE),
    "QUALIFY": re.compile(r"\bQUALIFY\b", re.IGNORECASE),
    "FILTER (WHERE ...)": re.compile(r"\bFILTER\s*\(\s*WHERE\b", re.IGNORECASE),
    "EXISTS subquery": re.compile(r"\bEXISTS\s*\(", re.IGNORECASE),
    "LEFT JOIN": re.compile(r"\bLEFT\s+(?:OUTER\s+)?JOIN\b", re.IGNORECASE),
    "DATE '...' literal": re.compile(r"\bDATE\s+''", re.IGNORECASE),
    "EXTRACT(...)": re.compile(r"\bEXTRACT\s*\(", re.IGNORECASE),
    "year() / month()": re.compile(r"\b(?:year|month)\s*\(", re.IGNORECASE),
    "date_trunc": re.compile(r"\bdate_trunc\s*\(", re.IGNORECASE),
    "strftime": re.compile(r"\bstrftime\s*\(", re.IGNORECASE),
    ":: cast": re.compile(r"::"),
    "CAST(...)": re.compile(r"\bCAST\s*\(", re.IGNORECASE),
    "ROUND": re.compile(r"\bROUND\s*\(", re.IGNORECASE),
    "NULLIF": re.compile(r"\bNULLIF\s*\(", re.IGNORECASE),
    "GROUP BY position (GROUP BY 1)": re.compile(r"\bGROUP\s+BY\s+\d", re.IGNORECASE),
    "LIMIT": re.compile(r"\bLIMIT\b", re.IGNORECASE),
}
MAKER_ORDER = ["Anthropic", "OpenAI", "xAI", "Google"]


def entries(run_dir: Path) -> list[tuple[str, str, str]]:
    """(status, header detail, SQL or error text) for every call in the run's queries.log."""
    log = run_dir / "queries.log"
    if not log.exists():
        return []
    return [(m.group(1), m.group(2), m.group(3)) for m in _ENTRY.finditer(log.read_text(errors="replace"))]


def run_style(run_dir: Path) -> dict:
    """Dialect and style signals for one run."""
    calls = entries(run_dir)
    errors = [(detail, body) for status, detail, body in calls if status == "ERROR"]
    out = {
        "queries": sum(status in ("RUN", "ERROR", "REFUSED") for status, _, _ in calls),
        "discovery": sum(status == "RUN" and bool(_DISCOVERY.search(body)) for status, _, body in calls),
        "failed": len(errors),
        "refused": sum(status == "REFUSED" for status, _, _ in calls),
        "error_types": [detail.split(":", 1)[0].strip() or "unknown" for detail, _ in errors],
        "missing_functions": sorted(
            {m.group(1).lower() for detail, body in errors if (m := _MISSING_FUNCTION.search(f"{detail}\n{body}"))}
        ),
        "submitted": False,
        "constructs": [],
        "answer_lines": None,
    }
    answer = run_dir / "answer.sql"
    if answer.exists():
        sql = answer.read_text(errors="replace")
        code = scan(sql)[0]
        out |= {
            "submitted": True,
            "constructs": [name for name, rx in CONSTRUCTS.items() if rx.search(code)],
            "answer_lines": len([line for line in sql.strip().splitlines() if line.strip()]),
        }
    return out


def _order(values, order):
    return sorted(values, key=lambda v: (order.index(v) if v in order else len(order), v))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _pct(k, n):
    return f"{100 * k / n:.0f}%" if n else "-"


def summary_markdown(styled: list[dict], batches: list[str]) -> str:
    sets = _order({s["set"] for s in styled}, SET_ORDER)
    makers = _order({s["maker"] for s in styled}, MAKER_ORDER)
    agents = list(dict.fromkeys(s["agent"] for s in styled))
    group: dict[tuple, list[dict]] = defaultdict(list)
    for s in styled:
        group[(s["maker"], s["set"])].append(s)
        group[(s["agent"], s["set"])].append(s)
    out = [
        "# SQL style and dialect, by model maker and question set",
        "",
        f"Batches: {', '.join(batches)}. {len(styled)} graded runs (provider failures excluded).",
        "Definitions are in `evalkit/style.py`; one line per run is in `runs.jsonl`.",
        "",
        "## 1. Dialect: how much do agents stumble on DuckDB?",
        "",
        "Means per run. A failed query is one DuckDB rejected; schema lookups are SHOW, DESCRIBE and catalog queries.",
        "",
        "| maker | set | runs | queries | schema lookups | failed | runs with a failed query | refused |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in makers:
        for s in sets:
            rs = group.get((m, s), [])
            if rs:
                out.append(
                    f"| {m} | {s} | {len(rs)} | {_mean(r['queries'] for r in rs):.1f} "
                    f"| {_mean(r['discovery'] for r in rs):.1f} | {_mean(r['failed'] for r in rs):.2f} "
                    f"| {_pct(sum(r['failed'] > 0 for r in rs), len(rs))} | {_mean(r['refused'] for r in rs):.2f} |"
                )
    out += [
        "",
        "### Failed queries by DuckDB error type",
        "",
        "| maker | set | failed | error types |",
        "|---|---|---|---|",
    ]
    for m in makers:
        for s in sets:
            types = Counter(t for r in group.get((m, s), []) for t in r["error_types"])
            if types:
                n = sum(types.values())
                out.append(f"| {m} | {s} | {n} | " + ", ".join(f"{k} {v}" for k, v in types.most_common(5)) + " |")
    out += [
        "",
        "### Functions DuckDB does not have",
        "",
        "Calls that failed because the function does not exist: another dialect's function, or a probe of the engine.",
        "",
        "| maker | set | functions (failed calls) |",
        "|---|---|---|",
    ]
    for m in makers:
        for s in sets:
            fns = Counter(f for r in group.get((m, s), []) for f in r["missing_functions"])
            if fns:
                out.append(f"| {m} | {s} | " + ", ".join(f"`{k}` {v}" for k, v in fns.most_common(8)) + " |")
    out += [
        "",
        "## 2. Style: share of submitted queries using each construct",
        "",
        "Over runs that submitted a query. The TPC-H reference queries are 1990s SQL; a construct far more common",
        "on TPC-H than on the novel and hard sets, which no model could have seen, is the benchmark's style.",
        "",
    ]
    for s in sets:
        sub = {m: [r for r in group.get((m, s), []) if r["submitted"]] for m in makers}
        out += [f"### {s}", "", "| construct | " + " | ".join(makers) + " |", "|---" * (len(makers) + 1) + "|"]
        for name in CONSTRUCTS:
            out.append(
                f"| {name} | "
                + " | ".join(_pct(sum(name in r["constructs"] for r in sub[m]), len(sub[m])) for m in makers)
                + " |"
            )
        lines = (_mean(r["answer_lines"] for r in sub[m]) for m in makers)
        out.append("| lines per query (mean) | " + " | ".join("-" if x is None else f"{x:.1f}" for x in lines) + " |")
        out.append("| submitted queries | " + " | ".join(str(len(sub[m])) for m in makers) + " |")
        out.append("")
    headline = ["comma joins (FROM a, b)", "GROUP BY position (GROUP BY 1)", "year() / month()", "NULLIF", "ROUND"]
    out += [
        "## 3. By agent: the constructs that separate the makers",
        "",
        "Share of submitted queries, by question set (" + " / ".join(sets) + ").",
        "",
        "| agent | " + " | ".join(headline) + " | lines |",
        "|---" * (len(headline) + 2) + "|",
    ]
    for a in agents:
        cells = []
        for name in headline + ["lines"]:
            parts = []
            for s in sets:
                sub = [r for r in group.get((a, s), []) if r["submitted"]]
                if name == "lines":
                    x = _mean(r["answer_lines"] for r in sub)
                    parts.append("-" if x is None else f"{x:.0f}")
                else:
                    parts.append(_pct(sum(name in r["constructs"] for r in sub), len(sub)))
            cells.append(" / ".join(parts))
        out.append(f"| {a} | " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def run_style_report(batches: list[str], out_dir: Path, *, results: Path | None = None) -> dict:
    """Write `summary.md` and `runs.jsonl` for every graded run of the batches. Deterministic."""
    rows = [r for r in load_rows(batches, results) if r.get("verdict") != "harness_error"]
    sets = question_sets()
    base = results.parent if results else HARNESS_DIR
    styled = []
    for r in sorted(rows, key=lambda r: r["run_dir"]):
        styled.append(
            {
                "run_dir": r["run_dir"],
                "agent": r["agent"],
                "maker": maker(r["agent"]),
                "task": r["task"],
                "set": sets.get(r["task"], "other"),
                "level": r["level"],
                "correct": bool(int(r.get("correct") or 0)),
                **run_style(base / r["run_dir"]),
            }
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "runs.jsonl", "w") as f:
        f.writelines(json.dumps(s) + "\n" for s in styled)
    (out_dir / "summary.md").write_text(summary_markdown(styled, batches))
    return {"runs": len(styled), "out_dir": str(out_dir)}
