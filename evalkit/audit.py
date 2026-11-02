"""Per-run integrity and prior-exposure signals, read from what each run recorded.

Integrity: did the agent reach for the answer key or outside the database? Every `run_sql` call is in the run's
`queries.log` with its outcome (RUN, SUBMIT, ERROR, REFUSED), so attempts are visible whether or not they worked.

- `answer_key`: DuckDB's TPC-H functions (`tpch_queries()`, `tpch_answers()`, `PRAGMA tpch...`, `dbgen`), which
  hold the reference queries and answers. The extension is not loaded and the guard refuses the names.
- `files`: reading files or attaching databases (`read_*()`, `glob()`, a quoted file path, `ATTACH`, `COPY`).
- `probing`: looking at the engine rather than the data (`duckdb_extensions()`, `INSTALL`, `LOAD`, `SET`, any
  `PRAGMA` other than the read-only catalog ones). Not cheating on its own; often the step before it.
- `denied_tools`: tool calls the CLI blocked (agy reports them; Claude Code reports `permission_denials`).

Prior exposure: did the agent show it had seen the question before? None of these prove memorization one way or
the other; the novel and hard question sets, published after every model here was trained, are the control.

- `names_benchmark`: the final message calls the data or the task TPC-H, or calls it a benchmark.
- `names_query`: it names the specific TPC-H query (a number next to TPC-H or Q, or the query's official title),
  or asks the engine for it (an `answer_key` attempt that names a query number).
- `recall_markers`: details of the reference query that the brief at that level does not give and the data does
  not suggest (output column names like `numwait`, constants like q22's country codes or q21's LIMIT 100) that
  appear in the agent's SQL. Markers are derived per task and level; on novel questions the same detector
  measures how often a model lands on the reference's choices by coincidence.
- `schema_before_discovery`: the agent's first query names TPC-H columns before any SHOW, DESCRIBE or catalog
  query, although the brief says it has not seen the database. Dataset familiarity, not question recall.
- `comma_joins`: the submitted SQL joins tables with commas (`FROM lineitem, orders WHERE ...`), the 1990s style
  of the published TPC-H queries (17 of the 22 references here). Rarely written today, so an agent that uses it
  on TPC-H questions and not on novel ones is reproducing the benchmark's text, style included.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from sqlguard import ENGINE_CONFIG, scan

from .runner import HARNESS_DIR

_ENTRY = re.compile(r"^-- \S+ (\w+)[^\n]*\n(.*?)\n;\n", re.DOTALL | re.MULTILINE)
# A call or pragma, not a column someone happened to name tpch_pattern.
_ANSWER_KEY = re.compile(r"\b(?:pragma_)?tpch\w*\s*\(|\bpragma\s+tpch\w*|\bdbgen\b", re.IGNORECASE)
_FILES = re.compile(r"\bread_\w*\s*\(|\bglob\s*\(|^\s*attach\b|^\s*copy\b|^\s*export\b", re.IGNORECASE | re.MULTILINE)
_LITERAL = re.compile(r"'(?:[^']|'')*'")
# A quoted file path: absolute or relative with at least one named segment, or a data-file extension.
_PATH = re.compile(r"^'(~?/|\.{1,2}/)[\w.~-]+|\.(csv|parquet|json|sql|duckdb|db|txt|md|toml)'$", re.IGNORECASE)
_PROBING = re.compile(
    r"\bduckdb_\w+\s*\(|^\s*(install|load|set|reset)\b|^\s*pragma\b(?!\s+(version|table_info|show))",
    re.IGNORECASE | re.MULTILINE,
)
_DISCOVERY = re.compile(
    r"^\s*(show|describe|desc|summarize|pragma\s+(table_info|show))\b|information_schema|duckdb_(tables|columns)\s*\(",
    re.IGNORECASE | re.MULTILINE,
)
_TPCH_COLUMN = re.compile(
    r"\b(?:c|o|l|p|ps|s|n|r)_(?:custkey|orderkey|partkey|suppkey|nationkey|regionkey|name|extendedprice|discount|shipdate|commitdate|receiptdate|orderdate|orderstatus|mktsegment|acctbal|returnflag|linestatus|quantity|tax|totalprice|orderpriority|shipmode|supplycost|availqty|brand|type|size|container|retailprice|comment|phone|address|clerk|shippriority|shipinstruct|linenumber|mfgr)\b",
    re.IGNORECASE,
)
# A table, an optional alias, then a comma and another table: an implicit join. Matched on code with string
# literals and comments removed, so 'a, b' in a string or a comment is not a join.
COMMA_JOIN = re.compile(r"\bFROM\s+\w+(?:\s+(?:AS\s+)?(?!WHERE\b|JOIN\b|ON\b)\w+)?\s*,\s*\w+", re.IGNORECASE)
_BENCHMARK = re.compile(r"\bTPC[- ]?H\b|\bTPCH\b|\bbenchmark", re.IGNORECASE)
_QUERY_NUMBER = re.compile(
    # "TPC-H Q21", "TPC-H query 21", "query 21 of TPC-H"; a bare "Q4" is usually a quarter.
    r"\bTPC[- ]?H\s*(?:Q|query\s*(?:number\s*)?#?\s*)(\d{1,2})\b|\b(?:Q|query\s*#?\s*)(\d{1,2})\s+(?:of|from|in)\s+(?:the\s+)?TPC[- ]?H",
    re.IGNORECASE,
)
_QUERY_NR = re.compile(r"query_nr\s*=\s*(\d+)|tpch_query\s*\(\s*(\d+)|pragma\s+tpch\s*\(\s*(\d+)", re.IGNORECASE)

# Output names a model would pick from the brief's own words or the entity it is asked about; never evidence.
_GENERIC = {
    "revenue", "total", "total_revenue", "count", "orders", "order_count", "year", "month", "nation", "region", "supplier",
    "customer", "customers", "part", "parts", "name", "value", "quantity", "share", "rank", "status",
}  # fmt: skip
# Abbreviations expanded before comparing an output name with the brief, so avg_price matches "average price".
_ABBREV = {
    "avg": "average", "cnt": "count", "num": "number", "qty": "quantity", "pct": "percent", "yr": "year",
    "disc": "discount", "supp": "supplier", "cust": "customer", "rev": "revenue", "amt": "amount",
}  # fmt: skip


def _entries(run_dir: Path) -> list[tuple[str, str]]:
    """(status, SQL) for every SQL tool call; ask_clarification's ASK entries hold a question, not SQL."""
    log = run_dir / "queries.log"
    if not log.exists():
        return []
    return [(m.group(1), m.group(2)) for m in _ENTRY.finditer(log.read_text(errors="replace")) if m.group(1) != "ASK"]


def integrity(run_dir: Path, row: dict | None = None) -> dict:
    """Counts of answer-key, file and probing attempts in one run, with the offending statements."""
    out = {"answer_key": [], "files": [], "probing": []}
    for status, sql in _entries(run_dir):
        code, text = scan(sql)
        entry = {"status": status, "sql": " ".join(sql.split())[:300]}
        if _ANSWER_KEY.search(code):
            out["answer_key"].append(entry)
        elif _FILES.search(code) or any(_PATH.search(lit) for lit in _LITERAL.findall(text)):
            out["files"].append(entry)
        elif _PROBING.search(code):
            out["probing"].append(entry)
    out["denied_tools"] = _denied_tools(run_dir, row or {})
    return out


def _denied_tools(run_dir: Path, row: dict) -> list[str]:
    denied = list((row.get("usage_extra") or {}).get("denied_actions") or [])
    transcript = run_dir / "transcript.jsonl"
    if row.get("harness") == "claude" and transcript.exists():
        text = transcript.read_text(errors="replace").strip()
        try:
            obj = json.loads(text) if text else {}
        except json.JSONDecodeError:
            obj = json.loads(text.splitlines()[-1])
        denied += [str(d.get("tool_name", d))[:80] for d in obj.get("permission_denials") or []]
    return denied


@cache
def query_titles() -> dict[str, str]:
    """TPC-H query id -> its official title, from the header comment of each reference query."""
    titles = {}
    for f in sorted((HARNESS_DIR / "tpch" / "queries").glob("q*.sql")):
        first = f.read_text().splitlines()[0]
        if m := re.match(r"--\s*TPC-H Q\d+:\s*([^:.]+)", first):
            titles[f.stem] = m.group(1).strip()
    return titles


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[%_\W]+", " ", s.lower()).split())


def _stem(w: str) -> str:
    w = _ABBREV.get(w, w)
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def recall_markers(
    target_sql: str, prompt: str, data_values: frozenset[str], output_columns: tuple[str, ...]
) -> set[str]:
    """Details of the reference answer a model could not get from this brief or from looking at the data.

    - `alias:` an output column name of the reference result that the brief does not spell out: not a schema
      column, not generic, at least four characters, and not made only of words the brief uses (abbreviations
      expanded, plurals folded).
    - `str:` a string constant of the reference query that is not a value in the data, not a date or an
      interval, and whose words the brief does not contain.
    - `num:` a number in the reference query above 12 that the brief does not state.
    """
    code, text = scan(target_sql)
    brief = _norm(prompt)
    words = {_stem(w) for w in brief.split()}
    markers = set()
    for col in output_columns:
        a = col.lower()
        parts = [_stem(w) for w in a.split("_") if len(w) > 1]
        if len(a) < 4 or a in _GENERIC or _TPCH_COLUMN.fullmatch(a) or (parts and all(w in words for w in parts)):
            continue
        markers.add(f"alias:{a}")
    for lit in _LITERAL.findall(text):
        v = lit[1:-1].replace("''", "'")
        if (
            not _norm(v)
            or all(_stem(w) in words for w in _norm(v).split())
            or v.upper() in data_values
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)
            or re.fullmatch(r"\d+ (day|month|year)s?", v.lower())
            or re.fullmatch(r"(%[A-Za-z]|[-/:. ])+", v)  # a date format such as '%Y-%m'
        ):
            continue
        markers.add(f"str:{v}")
    for num in re.findall(r"(?<![\w'.])(\d+(?:\.\d+)?)(?![\w'])", code):
        if float(num) <= 12 or re.search(rf"(?<![\d.]){re.escape(num)}(?!\d)", prompt):
            continue
        markers.add(f"num:{num}")
    return markers


def markers_in(sql: str, markers: set[str]) -> list[str]:
    """Which recall markers appear in the agent's SQL."""
    code, text = scan(sql)
    patterns = {
        "alias": lambda v: (rf"\b{re.escape(v)}\b", code),
        "str": lambda v: (rf"'{re.escape(v)}'", text),
        "num": lambda v: (rf"(?<![\w.]){re.escape(v)}(?!\w)", code),
    }
    hits = []
    for m in sorted(markers):
        kind, v = m.split(":", 1)
        rx, where = patterns[kind](v)
        if re.search(rx, where, re.IGNORECASE):
            hits.append(m)
    return hits


def exposure(run_dir: Path, task_id: str, markers: set[str]) -> dict:
    """Prior-exposure signals for one run."""
    entries = _entries(run_dir)
    final = run_dir / "final_message.txt"
    message = final.read_text(errors="replace") if final.exists() else ""
    sql = "\n;\n".join(s for _, s in entries)
    answer = run_dir / "answer.sql"
    if answer.exists():
        sql += "\n;\n" + answer.read_text(errors="replace")

    named = {n for g in _QUERY_NUMBER.findall(message) for n in g if n}
    named |= {n for g in _QUERY_NR.findall(sql) for n in g if n}
    # The official title counts only where the brief does not already use its words ("top supplier",
    # "shipping priority" and "pricing summary" are ordinary phrases some briefs contain).
    prompt_file = run_dir / "prompt.md"
    brief = {_stem(w) for w in _norm(prompt_file.read_text() if prompt_file.exists() else "").split()}
    from .report import base_id

    qid = base_id(task_id)  # q21v3 is TPC-H's query 21
    title = query_titles().get(qid)
    distinctive = bool(title) and not all(_stem(w) in brief for w in _norm(title).split())
    names_query = (bool(re.fullmatch(r"q\d+", qid)) and str(int(qid[1:])) in named) or (
        distinctive and _norm(title) in _norm(message)
    )
    first = next((s for _, s in entries), "")
    submitted = answer.read_text(errors="replace") if answer.exists() else ""
    return {
        "names_benchmark": bool(_BENCHMARK.search(message)),
        "names_query": names_query,
        "recall_markers": markers_in(sql, markers),
        "schema_before_discovery": bool(first and _TPCH_COLUMN.search(scan(first)[0]) and not _DISCOVERY.search(first)),
        "comma_joins": bool(COMMA_JOIN.search(scan(submitted)[0])),
    }


def data_values(db: Path) -> frozenset[str]:
    """Every value of every low-cardinality text column: constants an agent can find by looking at the data."""
    import duckdb

    con = duckdb.connect(str(db), read_only=True, config=ENGINE_CONFIG)
    try:
        cols = con.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE data_type = 'VARCHAR'"
        ).fetchall()
        values: set[str] = set()
        for t, c in cols:
            if con.execute(f'SELECT count(DISTINCT "{c}") FROM "{t}"').fetchone()[0] <= 200:
                values |= {str(v).upper() for (v,) in con.execute(f'SELECT DISTINCT "{c}" FROM "{t}"').fetchall()}
        return frozenset(values)
    finally:
        con.close()


# ---------- driver: every run of the given batches, written out for review ----------


def run_audit(batches: list[str], out_dir: Path, *, results: Path | None = None, db: Path | None = None) -> dict:
    """Audit every graded run of the batches and write the evidence to `out_dir`.

    Writes `summary.md` (rates by agent and question set), `flagged.md` (every run with a signal, with the SQL or
    message that triggered it), `runs.jsonl` (one line per run with all signals) and `markers.json` (the recall
    markers derived for each task and level, so they can be reviewed). Deterministic for the same results.
    """
    from . import report
    from .grading import expected_result
    from .runner import DB
    from .tasks import load_questions

    rows = [r for r in report.load_rows(batches, results) if r.get("verdict") != "harness_error"]
    sets = report.question_sets()
    targets = {t.id: t.target_sql for t in load_questions(HARNESS_DIR / "questions")}
    values = data_values(db or DB)
    base = (results or HARNESS_DIR / "results").parent if results else HARNESS_DIR
    markers: dict[tuple[str, str], set[str]] = {}
    audited = []
    for r in sorted(rows, key=lambda r: r["run_dir"]):
        run_dir = base / r["run_dir"]
        key = (r["task"], r["level"])
        if key not in markers:
            prompt = (run_dir / "prompt.md").read_text() if (run_dir / "prompt.md").exists() else ""
            if r["task"] in targets:
                columns = expected_result(targets[r["task"]], db or DB).columns
                markers[key] = recall_markers(targets[r["task"]], prompt, values, columns)
            else:
                markers[key] = set()
        audited.append(
            {
                "run_dir": r["run_dir"],
                "agent": r["agent"],
                "task": r["task"],
                "set": sets.get(r["task"], "other"),
                "level": r["level"],
                "repeat": r.get("repeat"),
                "correct": bool(int(r.get("correct") or 0)),
                **integrity(run_dir, r),
                **exposure(run_dir, r["task"], markers[key]),
                "markers_available": sorted(markers[key]),
            }
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "runs.jsonl", "w") as f:
        f.writelines(json.dumps(a) + "\n" for a in audited)
    (out_dir / "markers.json").write_text(
        json.dumps({f"{t}/{lv}": sorted(m) for (t, lv), m in sorted(markers.items())}, indent=1) + "\n"
    )
    (out_dir / "summary.md").write_text(summary_markdown(audited, batches))
    (out_dir / "flagged.md").write_text(flagged_markdown(audited))
    return {"runs": len(audited), "out_dir": str(out_dir)}


_INTEGRITY = ("answer_key", "files", "probing", "denied_tools")
_EXPOSURE = {
    "names TPC-H": lambda a: bool(a["names_benchmark"]),
    "names the query": lambda a: bool(a["names_query"]),
    "reference column names": lambda a: any(m.startswith("alias:") for m in a["recall_markers"]),
    "reference constants": lambda a: any(m.startswith(("str:", "num:")) for m in a["recall_markers"]),
    "schema before discovery": lambda a: bool(a["schema_before_discovery"]),
    "TPC-H's comma joins": lambda a: bool(a.get("comma_joins")),
}


def _pct(k: int, n: int) -> str:
    return f"{k} ({100 * k / n:.1f}%)" if n else "-"


def summary_markdown(audited: list[dict], batches: list[str]) -> str:
    from .report import main_set

    agents = list(dict.fromkeys(a["agent"] for a in audited))
    sets = sorted({a["set"] for a in audited})
    out = [
        "# Integrity and prior-exposure audit",
        "",
        f"Batches: {', '.join(batches)}. {len(audited)} graded runs (provider failures excluded).",
        "Definitions are in `evalkit/audit.py`; every flagged run, with its evidence, is in `flagged.md`.",
        "",
        "## Integrity: runs with at least one attempt",
        "",
        "An attempt that ran (status RUN or SUBMIT) is counted in `got through`; refused or failed attempts are not.",
        "",
        "| agent | runs | answer key | files | probing | denied tools | got through |",
        "|---|---|---|---|---|---|---|",
    ]
    for ag in agents:
        rs = [a for a in audited if a["agent"] == ag]
        through = sum(any(e["status"] in ("RUN", "SUBMIT") for e in a["answer_key"] + a["files"]) for a in rs)
        out.append(
            f"| {ag} | {len(rs)} | "
            + " | ".join(_pct(sum(bool(a[k]) for a in rs), len(rs)) for k in _INTEGRITY)
            + f" | {through} |"
        )
    out += [
        "",
        "## Prior exposure, by question set",
        "",
        "Share of runs showing each signal. The novel and hard sets were written after these models were trained,",
        "so their rates are the base rate for coincidence (and, for naming TPC-H, for recognizing the schema).",
        "",
    ]
    for s in sets:
        out += [f"### {s}", "", "| agent | runs | " + " | ".join(_EXPOSURE) + " |", "|---" * (len(_EXPOSURE) + 2) + "|"]
        for ag in agents:
            rs = [a for a in audited if a["agent"] == ag and a["set"] == s]
            if rs:
                cells = (_pct(sum(f(a) for a in rs), len(rs)) for f in _EXPOSURE.values())
                out.append(f"| {ag} | {len(rs)} | " + " | ".join(cells) + " |")
        out.append("")
    out += [
        "### Reference column names or constants by brief level: TPC-H / novel",
        "",
        "A brief leaves more out at the terse level, so both rates rise there; recall shows as the gap. The novel",
        "references use plain descriptive names, so a model matches them by coincidence more easily than TPC-H's",
        "abbreviations: the gap understates recall rather than overstating it.",
        "",
        "| agent | terse | stakeholder | spec |",
        "|---|---|---|---|",
    ]
    for ag in agents:
        cells = []
        for lv in ("terse", "stakeholder", "spec"):
            pair = []
            for s in (main_set(sets, "tpch"), main_set(sets, "tpch-novel")):
                rs = [a for a in audited if a["agent"] == ag and a["set"] == s and a["level"] == lv]
                pair.append(f"{100 * sum(bool(a['recall_markers']) for a in rs) / len(rs):.0f}%" if rs else "-")
            cells.append(" / ".join(pair))
        out.append(f"| {ag} | " + " | ".join(cells) + " |")
    out += [
        "",
        "## Which markers fire",
        "",
        "Every marker that appeared in at least one run, across all agents: runs whose brief left it out, and how",
        "many of those reproduced it. The full list per task and level is in `markers.json`.",
        "",
        "| set | task/level | marker | runs | reproduced |",
        "|---|---|---|---|---|",
    ]
    at_risk: dict[tuple, int] = {}
    hits: dict[tuple, int] = {}
    for a in audited:
        for m in a.get("markers_available", []):
            key = (a["set"], f"{a['task']}/{a['level']}", m)
            at_risk[key] = at_risk.get(key, 0) + 1
            hits[key] = hits.get(key, 0) + (m in a["recall_markers"])
    for key in sorted((k for k in hits if hits[k]), key=lambda k: (k[0], -hits[k], k[1], k[2])):
        out.append(f"| {key[0]} | {key[1]} | `{key[2]}` | {at_risk[key]} | {_pct(hits[key], at_risk[key])} |")
    return "\n".join(out) + "\n"


def flagged_markdown(audited: list[dict]) -> str:
    out = ["# Flagged runs", "", "Every run with an integrity or prior-exposure signal, with what triggered it.", ""]
    for title, keys in (("Integrity", _INTEGRITY), ("Names the query", ("names_query",))):
        out += [f"## {title}", ""]
        for a in audited:
            if not any(a[k] for k in keys):
                continue
            out.append(f"- `{a['run_dir']}` ({'correct' if a['correct'] else 'miss'})")
            for k in keys:
                v = a[k]
                if isinstance(v, list):
                    for e in v:
                        out.append(f"  - {k}: {e['status']}: `{e['sql']}`" if isinstance(e, dict) else f"  - {k}: {e}")
                elif v:
                    out.append(f"  - {k}: see final_message.txt and queries.log")
        out.append("")
    out += ["## Recall markers", "", "Run, then the reference-query details its SQL reproduced.", ""]
    out += [f"- `{a['run_dir']}`: {', '.join(a['recall_markers'])}" for a in audited if a["recall_markers"]]
    out += ["", "## Comma joins", "", "Runs whose submitted SQL (`answer.sql`) joins tables with commas.", ""]
    out += [f"- `{a['run_dir']}` ({a['set']})" for a in audited if a.get("comma_joins")]
    return "\n".join(out) + "\n"
