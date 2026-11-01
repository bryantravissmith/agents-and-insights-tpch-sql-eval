"""Findings: the claims the full grid supports, each with its numbers, an interval and how to check it.

Reads the same runs as `report` plus the audit's per-run signals (`audit/runs.jsonl`). Runs that failed on the
provider's side are left out, as everywhere else. An agent with any left out is marked partial.

Intervals resample questions, not runs: the ten repeats of one question share a prompt and a reference, so they
are not independent, and a set of six questions is a small sample however many repeats it has. Each interval is
the 2.5th to 97.5th percentile of a seeded bootstrap over the questions of each set, so the output is the same
on every run.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from .grading import CORRECT, EXACT_COLUMNS
from .report import LEVELS, cell, load_rows, question_sets

SEED, DRAWS = 20261031, 2000
SET_ORDER = ["tpch", "tpch-novel", "tpch-novel-v2", "tpch-hard", "tpch-hard-v2"]

# Grading rules, from each run's verdict (see evalkit/grading.py); a run passes only with a submitted query.
#   correct        the headline: every column the reference asks for, with the right values. Extra columns,
#                  column order and row order do not change the answer, so they pass (and are noted).
#   exact columns  a note, not a score: correct with nothing beyond the reference's columns.
#   values only    a note, not a score: every returned column right, but some of the reference's columns
#                  missing. Not correct (part of the answer is absent); it shows how many runs were close.
RULES = {
    "correct": CORRECT,
    "exact columns": EXACT_COLUMNS,
    "values only": CORRECT | {"missing_columns"},
}


def _key(r: dict) -> tuple:
    return r["agent"], r["task"], r["level"]


def passes(r: dict, rule: str = "correct") -> bool:
    return r.get("verdict") in RULES[rule] and r.get("sql_source") == "submit"


def tally(rows: list[dict], rule: str = "correct") -> dict[tuple, tuple[int, int]]:
    """(agent, task, level) -> (passing, graded)."""
    out: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        if r.get("verdict") == "harness_error":
            continue
        out[_key(r)][0] += int(passes(r, rule))
        out[_key(r)][1] += 1
    return {k: (v[0], v[1]) for k, v in out.items()}


def rate(t: dict, agent: str, tasks: list[str], level: str) -> float | None:
    k = sum(t.get((agent, q, level), (0, 0))[0] for q in tasks)
    n = sum(t.get((agent, q, level), (0, 0))[1] for q in tasks)
    return k / n if n else None


def _draws(tasks: list[str], rng: random.Random) -> list[list[str]]:
    return [[rng.choice(tasks) for _ in tasks] for _ in range(DRAWS)]


def _ci(xs: list[float]) -> tuple[float, float] | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[int(0.025 * (len(xs) - 1))], xs[int(0.975 * (len(xs) - 1))]


def _spearman(a: list[float], b: list[float]) -> float | None:
    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):  # ties share their mean rank
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            for m in range(i, j + 1):
                r[order[m]] = (i + j) / 2
            i = j + 1
        return r

    if len(a) < 3 or None in a or None in b:  # an agent without runs at this level cannot be ranked
        return None
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return cov / (va * vb) ** 0.5 if va and vb else None


def accuracy(t, agents, by_set) -> dict:
    """Rate and interval for every agent, set and level."""
    rng = random.Random(SEED)
    out = {}
    for s, tasks in by_set.items():
        draws = _draws(tasks, rng)
        for a in agents:
            for lv in LEVELS:
                if (r := rate(t, a, tasks, lv)) is not None:
                    out[(a, s, lv)] = (r, _ci([rate(t, a, d, lv) for d in draws]))
    return out


def gaps(t, agents, by_set, level: str, a_set="tpch", b_set="tpch-novel") -> dict:
    """Per agent: rate on a_set minus rate on b_set at one level, with an interval and a leave-one-question-out range."""
    rng = random.Random(SEED)
    da, db = _draws(by_set[a_set], rng), _draws(by_set[b_set], rng)
    out = {}
    for a in agents:
        ra, rb = rate(t, a, by_set[a_set], level), rate(t, a, by_set[b_set], level)
        if ra is None or rb is None:
            continue
        boot = [
            (x - y) if (x := rate(t, a, qa, level)) is not None and (y := rate(t, a, qb, level)) is not None else None
            for qa, qb in zip(da, db)
        ]
        loo = [rate(t, a, [q for q in by_set[a_set] if q != drop], level) - rb for drop in by_set[a_set]] + [
            ra - rate(t, a, [q for q in by_set[b_set] if q != drop], level) for drop in by_set[b_set]
        ]
        out[a] = {"a": ra, "b": rb, "gap": ra - rb, "ci": _ci(boot), "loo": (min(loo), max(loo))}
    return out


def rank_agreement(t, agents, by_set, level: str, a_set="tpch", b_set="tpch-novel") -> dict:
    """Spearman correlation between the agents' rates on two sets, with an interval from resampling questions."""
    rng = random.Random(SEED)
    da, db = _draws(by_set[a_set], rng), _draws(by_set[b_set], rng)

    def rho(qa, qb):
        return _spearman([rate(t, a, qa, level) for a in agents], [rate(t, a, qb, level) for a in agents])

    return {"rho": rho(by_set[a_set], by_set[b_set]), "ci": _ci([rho(x, y) for x, y in zip(da, db)])}


def recall_vs_correct(rows: list[dict], audit: dict[str, dict], set_name="tpch", level="terse") -> dict:
    """Correct rate when a run reproduced reference details the brief leaves out, and when it did not.

    Per agent and pooled. `stratified` is the Mantel-Haenszel risk difference across questions, so a question
    that is both easy and well remembered does not stand in for recall.
    """
    groups: dict[str, dict[str, list[list[int]]]] = defaultdict(lambda: defaultdict(lambda: [[0, 0], [0, 0]]))
    for r in rows:
        if r.get("verdict") == "harness_error" or r["level"] != level:
            continue
        sig = audit.get(r["run_dir"])
        if not sig or sig.get("set") != set_name:
            continue
        hit = int(bool(sig.get("recall_markers")))
        ok = int(bool(int(r.get("correct") or 0)))
        for who in (r["agent"], "all agents"):
            groups[who][r["task"]][hit][0] += ok
            groups[who][r["task"]][hit][1] += 1
    out = {}
    for who, by_task in groups.items():
        k1 = sum(v[1][0] for v in by_task.values())
        n1 = sum(v[1][1] for v in by_task.values())
        k0 = sum(v[0][0] for v in by_task.values())
        n0 = sum(v[0][1] for v in by_task.values())
        num = den = 0.0
        for (c0, m0), (c1, m1) in by_task.values():
            if m0 and m1:
                w = m0 * m1 / (m0 + m1)
                num += w * (c1 / m1 - c0 / m0)
                den += w
        out[who] = {
            "with": (k1, n1),
            "without": (k0, n0),
            "stratified": num / den if den else None,
            "questions_with_both": sum(1 for (_, m0), (_, m1) in by_task.values() if m0 and m1),
        }
    return out


def repeat_agreement(t: dict, sets: dict[str, str]) -> dict:
    """For each set and level: how many (agent, question) cells were right every time, wrong every time, or split."""
    out: dict[tuple, Counter] = defaultdict(Counter)
    for (a, q, lv), (k, n) in t.items():
        if n < 2:
            continue
        kind = "all right" if k == n else "all wrong" if k == 0 else "split"
        out[(sets.get(q, "other"), lv)][kind] += 1
    return out


def failure_modes(rows: list[dict], sets: dict[str, str]) -> dict:
    """Misses by verdict for each set and level, all agents together; plus runs that never submitted."""
    out: dict[tuple, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("verdict") == "harness_error" or int(r.get("correct") or 0):
            continue
        key = (sets.get(r["task"], "other"), r["level"])
        out[key][r["verdict"]] += 1
        if r.get("sql_source") != "submit":
            out[key]["(never submitted)"] += 1
    return out


_ASKS = re.compile(
    r"could you (?:please )?(?:clarify|confirm|specify|tell me)|can you (?:clarify|confirm|specify)|"
    r"please (?:clarify|confirm|specify|let me know which)|which (?:one|definition|do you mean|would you like)|"
    r"do you (?:mean|want)|should i (?:use|assume)|before i (?:proceed|answer)|need (?:more )?clarification",
    re.IGNORECASE,
)


def clarifications(rows: list[dict], root: Path) -> dict[tuple, Counter]:
    """(maker, level) -> runs, runs that asked instead of submitting, runs that asked and submitted anyway."""
    out: dict[tuple, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("verdict") == "harness_error" or "regraded_from" in r:
            continue
        cell = out[(maker(r["agent"]), r["level"])]
        cell["runs"] += 1
        final = root / r["run_dir"] / "final_message.txt"
        if final.exists() and _ASKS.search(final.read_text(errors="replace")):
            cell["and_submitted" if r.get("sql_source") == "submit" else "instead"] += 1
    return dict(out)


MAKERS = (("claude:", "Anthropic"), ("pi:gpt", "OpenAI"), ("pi:grok", "xAI"), ("agy:", "Google"))


def maker(agent: str) -> str:
    return next((m for prefix, m in MAKERS if agent.startswith(prefix)), agent)


def version_shift(rows: list[dict]) -> dict[tuple, dict]:
    """For re-graded runs (see `regrade`): the same answers graded by the old and the new reference, per maker.

    (new task, maker) -> counts of runs passing each rule under the original grade and under the new one.
    """
    out: dict[tuple, dict] = defaultdict(lambda: {"runs": 0, "old": Counter(), "new": Counter()})
    for r in rows:
        if "regraded_from" not in r:
            continue
        cell = out[(r["task"], maker(r["agent"]))]
        cell["runs"] += 1
        old = r | {"verdict": r["original"]["verdict"], "correct": r["original"]["correct"]}
        for rule in RULES:
            cell["old"][rule] += passes(old, rule)
            cell["new"][rule] += passes(r, rule)
    return dict(out)


def _pick(rows: list[dict], per_agent: int) -> list[dict]:
    """A fixed sample: the first runs per agent ordered by a hash of the run directory."""
    by_agent = defaultdict(list)
    for r in sorted(rows, key=lambda r: hashlib.sha256(r["run_dir"].encode()).hexdigest()):
        if len(by_agent[r["agent"]]) < per_agent:
            by_agent[r["agent"]].append(r)
    return [r for a in sorted(by_agent) for r in by_agent[a]]


def _read(path: Path, limit: int) -> str:
    if not path.exists():
        return "(none)"
    text = path.read_text(errors="replace").strip()
    return text if len(text) <= limit else text[:limit] + "\n... (cut)"


def samples_markdown(rows: list[dict], sets: dict[str, str], root: Path, per_agent=1) -> str:
    """Terse misses to read by hand: one per agent per set, fixed by a hash so the sample never moves."""
    out = [
        "# Terse misses to read by hand",
        "",
        "One miss per agent per question set at the terse level, chosen by a hash of the run directory.",
        "For each, one of three: a reasonable assumption that differs from the intended definition (wrong by",
        "design: the terse rung tests ambiguity); the right answer in a form the grader should accept (fix the",
        "key with accepted_sql); or an error.",
        "",
    ]
    for s in sorted({sets.get(r["task"], "other") for r in rows}):
        misses = [
            r
            for r in rows
            if r["level"] == "terse"
            and sets.get(r["task"]) == s
            and r.get("verdict") != "harness_error"
            and not passes(r)
        ]
        out += [f"## {s}", ""]
        for r in _pick(misses, per_agent):
            d = root / r["run_dir"]
            out += [
                f"### {r['task']} - {r['agent']} - r{r['repeat']}: {r['verdict']}",
                "",
                (
                    f"`{r['run_dir']}`; rows {r.get('rows_returned')} of {r.get('rows_expected')} expected;"
                    f" source {r.get('sql_source')}"
                ),
                "",
                "Brief:",
                "",
                "> " + _read(d / "prompt.md", 2000).split("\n\n")[-1].replace("\n", "\n> "),
                "",
                "Submitted SQL:",
                "",
                "```sql",
                _read(d / "answer.sql", 2500),
                "```",
                "",
                "Final message:",
                "",
                "> " + _read(d / "final_message.txt", 1200).replace("\n", "\n> "),
                "",
            ]
    return "\n".join(out) + "\n"


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.0f}%"


def _pp(x) -> str:
    return "-" if x is None else f"{100 * x:+.0f}"


def _iv(ci, fmt=_pct) -> str:
    return "-" if ci is None else f"{fmt(ci[0])} to {fmt(ci[1])}"


def build(
    batches: list[str],
    audit_runs: Path,
    *,
    results: Path | None = None,
    questions: Path | None = None,
    root: Path | None = None,
):
    rows = load_rows(batches, results)
    sets = question_sets(questions)
    audit = {}
    if audit_runs.exists():
        for line in audit_runs.read_text().splitlines():
            a = json.loads(line)
            audit[a["run_dir"]] = a
    found: dict[str, list[str]] = defaultdict(list)
    for q in sorted({r["task"] for r in rows}):
        found[sets.get(q, "other")].append(q)
    by_set = {s: found[s] for s in sorted(found, key=lambda s: (SET_ORDER + [s]).index(s))}
    agents = list(dict.fromkeys(r["agent"] for r in rows))
    left_out = Counter(r["agent"] for r in rows if r.get("verdict") == "harness_error")
    complete = [a for a in agents if not left_out[a]]
    t = tally(rows)
    by_rule = {rule: tally(rows, rule) for rule in RULES}
    novel = [s for s in by_set if s.startswith("tpch-novel") and "tpch" in by_set]
    return {
        "terse_rules": {
            s: {
                rule: {
                    "gaps": gaps(tr, agents, by_set, "terse", b_set=s),
                    "ranks": rank_agreement(tr, complete, by_set, "terse", b_set=s),
                }
                for rule, tr in by_rule.items()
            }
            for s in novel
        },
        "versions": version_shift(rows),
        "rows": rows,
        "sets": sets,
        "by_set": dict(by_set),
        "agents": agents,
        "partial": {a: left_out[a] for a in agents if left_out[a]},
        "complete": complete,
        "accuracy": accuracy(t, agents, by_set),
        "gaps": {lv: gaps(t, agents, by_set, lv) for lv in LEVELS},
        "ranks": {lv: rank_agreement(t, complete, by_set, lv) for lv in LEVELS},
        "recall": recall_vs_correct(rows, audit),
        "repeats": repeat_agreement(t, sets),
        "failures": failure_modes(rows, sets),
        # A re-graded run is a second grade of a run already counted, not a second run to pay for.
        "cost": {a: cell([r for r in rows if r["agent"] == a and "regraded_from" not in r]) for a in agents},
        "asks": clarifications(rows, root or (results.parent if results else Path(__file__).resolve().parent.parent)),
        "tally": t,
    }


def markdown(f: dict) -> str:
    agents, partial, by_set = f["agents"], f["partial"], f["by_set"]

    def name(a):
        return f"{a} (partial)" if a in partial else a

    out = [
        "# Findings: the 2026-10-31 full grid",
        "",
        (
            f"{len(f['rows'])} runs of {len(agents)} agents on {sum(len(v) for v in by_set.values())} questions "
            f"({', '.join(f'{len(v)} {k}' for k, v in sorted(by_set.items()))}), three brief levels, ten repeats."
        ),
        "Provider failures are left out of every rate. Partial agents (runs still owed to quota): "
        + (", ".join(f"{a} ({n} left out)" for a, n in partial.items()) or "none")
        + ".",
        "Intervals are 95% bootstrap intervals over questions (see `evalkit/findings.py`).",
        "",
        "## 1. A fully specified brief separates nothing",
        "",
        "Correct rate on spec briefs, with the interval over questions.",
        "",
        "| agent | " + " | ".join(by_set) + " |",
        "|---" * (len(by_set) + 1) + "|",
    ]
    for a in agents:
        cells = [f"{_pct(v[0])} ({_iv(v[1])})" if (v := f["accuracy"].get((a, s, "spec"))) else "-" for s in by_set]
        out.append(f"| {name(a)} | " + " | ".join(cells) + " |")
    out += [
        "",
        "## 2. TPC-H minus novel",
        "",
        (
            "Percentage points, TPC-H rate minus novel rate. The TPC-H questions are public and decades old; the"
            " novel ones were first published with this grid. `range` is the gap after leaving out any one question,"
            " so a gap that one question carries shows a range that crosses zero."
        ),
        "",
        (
            "Caveat on the novel and hard sets: their questions and reference SQL were drafted by Claude Opus 5.5"
            " (in Claude Code, then checked by hand). The v1 terse references carry that model's habits: columns"
            " the brief never asks for (a count, a denominator), and one form of a value (a month as 'YYYY-MM')."
            " The v2 sets (`tpch-novel-v2`, `tpch-hard-v2`) re-grade the same terse runs against references that"
            " return only what each brief asks, and accept the same value in other forms. Their definitions are"
            " unchanged: on a terse brief the definition is left open on purpose, and the intended one is the"
            " rubric, so a reasonable assumption that differs is graded wrong (see section 8)."
        ),
        "",
        "### 2a. Terse briefs: correct, with two notes",
        "",
        (
            "`correct` is the score: every column the reference asks for, with the right values; extra columns"
            " pass. `exact columns` (nothing beyond the reference's columns) and `values only` (every returned"
            " column right, some reference columns missing) are notes, not scores; see `RULES`."
        ),
        "",
    ]
    for s, by in f["terse_rules"].items():
        out += [
            f"TPC-H vs {s}:",
            "",
            "| agent | " + " | ".join(f"{rule}: TPC-H / novel / gap" for rule in RULES) + " |",
            "|---" * (len(RULES) + 1) + "|",
        ]
        for a in agents:
            cells = []
            for rule in RULES:
                g = by[rule]["gaps"].get(a)
                cells.append(
                    "-" if g is None else f"{_pct(g['a'])} / {_pct(g['b'])} / {_pp(g['gap'])} ({_iv(g['ci'], _pp)})"
                )
            out.append(f"| {name(a)} | " + " | ".join(cells) + " |")
        out += ["", f"Spearman correlation of the complete agents' terse rates, TPC-H vs {s}:", ""]
        out += ["| rule | rho | interval |", "|---|---|---|"]
        for rule in RULES:
            r = by[rule]["ranks"]
            rho = "-" if r["rho"] is None else f"{r['rho']:+.2f}"
            ci = "-" if r["ci"] is None else f"{r['ci'][0]:+.2f} to {r['ci'][1]:+.2f}"
            out.append(f"| {rule} | {rho} | {ci} |")
        out.append("")
    if f["versions"]:
        out += [
            "### 2c. The same answers, two answer keys",
            "",
            (
                "Every row is the same saved terse runs graded twice: by the original reference (v1) and by a"
                " version that returns only what the brief asks (v2), with accepted alternatives where the same"
                " answer has more than one form. The definitions are the same in both. Passing runs out of runs,"
                " by the maker of the agent's model. The v1 references were drafted by an Anthropic model."
            ),
            "",
            "| question | maker | runs | "
            + " | ".join(f"v1 {rule}" for rule in RULES)
            + " | "
            + " | ".join(f"v2 {rule}" for rule in RULES)
            + " |",
            "|---" * (3 + 2 * len(RULES)) + "|",
        ]
        for (q, m), c in sorted(f["versions"].items()):
            out.append(
                f"| {q} | {m} | {c['runs']} | "
                + " | ".join(str(c["old"][rule]) for rule in RULES)
                + " | "
                + " | ".join(str(c["new"][rule]) for rule in RULES)
                + " |"
            )
        out.append("")
    out += [
        "",
        "### 2b. Every level, correct",
        "",
        "| agent | level | TPC-H | novel | gap | interval | range |",
        "|---|---|---|---|---|---|---|",
    ]
    for lv in LEVELS:
        for a in agents:
            if (g := f["gaps"][lv].get(a)) is None:
                continue
            out.append(
                f"| {name(a)} | {lv} | {_pct(g['a'])} | {_pct(g['b'])} | {_pp(g['gap'])} "
                f"| {_iv(g['ci'], _pp)} | {_iv(g['loo'], _pp)} |"
            )
    out += [
        "",
        (
            "Do the two sets rank the agents the same way? Spearman correlation across the complete agents"
            f" ({len(f['complete'])}), with the interval over questions:"
        ),
        "",
        "| level | rho | interval |",
        "|---|---|---|",
    ]
    for lv in LEVELS:
        r = f["ranks"][lv]
        rho = "-" if r["rho"] is None else f"{r['rho']:+.2f}"
        ci = "-" if r["ci"] is None else f"{r['ci'][0]:+.2f} to {r['ci'][1]:+.2f}"
        out.append(f"| {lv} | {rho} | {ci} |")
    out += [
        "",
        "## 3. Do runs that reproduce the reference score more often?",
        "",
        (
            "TPC-H, terse briefs. `with` are runs whose SQL contains a reference detail the brief leaves out"
            " (see `audit/markers.json`). The stratified difference compares within each question,"
            " so it is not an easy-question effect. Some markers are constants the grader checks, so part of the"
            " link is mechanical: this shows that recall pays, not that it is the only route to a right answer."
        ),
        "",
        "| agent | correct with | correct without | difference | within-question difference | questions |",
        "|---|---|---|---|---|---|",
    ]
    for a in agents + ["all agents"]:
        if (v := f["recall"].get(a)) is None:
            continue
        (k1, n1), (k0, n0) = v["with"], v["without"]
        p1, p0 = (k1 / n1 if n1 else None), (k0 / n0 if n0 else None)
        diff = p1 - p0 if p1 is not None and p0 is not None else None
        out.append(
            f"| {name(a) if a in agents else a} | {_pct(p1)} of {n1} | {_pct(p0)} of {n0} | {_pp(diff)} "
            f"| {_pp(v['stratified']) if v['questions_with_both'] >= 3 else '- (too few)'} "
            f"| {v['questions_with_both']} |"
        )
    out += [
        "",
        "## 4. Per question, terse briefs",
        "",
        (
            "Correct out of graded runs. A column of zeros is a question whose terse brief no agent reads the way"
            " the intended definition does; a column split by agent is where the agents differ."
        ),
        "",
    ]
    for s, tasks in by_set.items():
        out += [f"### {s}", "", "| agent | " + " | ".join(tasks) + " |", "|---" * (len(tasks) + 1) + "|"]
        for a in agents:
            cells = [
                f"{k}/{n}" if (k, n) != (0, 0) else "-"
                for k, n in (f["tally"].get((a, q, "terse"), (0, 0)) for q in tasks)
            ]
            out.append(f"| {name(a)} | " + " | ".join(cells) + " |")
        out.append("")
    out += [
        "## 5. How the misses fail",
        "",
        "All agents together. `(never submitted)` overlaps the verdicts: those runs were graded on their last query.",
        "",
    ]
    kinds = sorted({k for c in f["failures"].values() for k in c}, key=lambda k: (k.startswith("("), k))
    out += ["| set | level | misses | " + " | ".join(kinds) + " |", "|---" * (len(kinds) + 3) + "|"]
    for (s, lv), c in sorted(f["failures"].items(), key=lambda kv: (kv[0][0], LEVELS.index(kv[0][1]))):
        misses = sum(v for k, v in c.items() if not k.startswith("("))
        out.append(f"| {s} | {lv} | {misses} | " + " | ".join(str(c.get(k, 0)) for k in kinds) + " |")
    out += [
        "",
        "## 6. Do repeats agree?",
        "",
        (
            "Each cell is one agent on one question at one level, usually ten repeats. A split cell is one where a"
            " single run would have given a different score than another single run."
        ),
        "",
        "| set | level | cells | all right | all wrong | split |",
        "|---|---|---|---|---|---|",
    ]
    for (s, lv), c in sorted(f["repeats"].items(), key=lambda kv: (kv[0][0], LEVELS.index(kv[0][1]))):
        n = sum(c.values())
        out.append(
            f"| {s} | {lv} | {n} | {c['all right']} | {c['all wrong']} | {c['split']} ({_pct(c['split'] / n)}) |"
        )
    out += [
        "",
        "## 7. What a correct answer costs",
        "",
        (
            "All sets and levels. Cost bases differ by harness; `report.md` labels each. Cache share is the part of"
            " the tokens processed that were cache reads or writes."
        ),
        "",
        "| agent | correct | cost/correct | tokens/correct | cache share |",
        "|---|---|---|---|---|",
    ]
    for a in sorted(agents, key=lambda a: f["cost"][a]["cost_per_correct"] or 0):
        c = f["cost"][a]
        parts = [c[k] or 0 for k in ("tokens_in", "tokens_cache_read", "tokens_cache_write", "tokens_out")]
        share = (parts[1] + parts[2]) / sum(parts) if sum(parts) else None
        cpc = "-" if c["cost_per_correct"] is None else f"${c['cost_per_correct']:.4f}"
        tpc = "-" if c["tokens_per_correct"] is None else f"{c['tokens_per_correct']:,.0f}"
        out.append(f"| {name(a)} | {_pct(c['strict'])} | {cpc} | {tpc} | {_pct(share)} |")
    out += [
        "",
        "## 8. The terse rung tests ambiguity: did anyone ask?",
        "",
        (
            "A terse brief leaves definitions open on purpose. The intended definition is the rubric, so an agent"
            " that silently assumes another reasonable one is graded wrong; asking is the move the rung rewards in"
            " principle. A run is headless, though: nobody answers, and the shared preamble tells the agent to"
            " submit and state its assumptions. `asked instead` ended with a question and no submitted query;"
            " `asked and submitted` asked a question but submitted anyway. Matched by phrases (`_ASKS`)."
        ),
        "",
        "| maker | level | runs | asked instead | asked and submitted |",
        "|---|---|---|---|---|",
    ]
    for (m, lv), c in sorted(f["asks"].items(), key=lambda kv: (kv[0][0], LEVELS.index(kv[0][1]))):
        out.append(f"| {m} | {lv} | {c['runs']} | {c['instead']} | {c['and_submitted']} |")
    return "\n".join(out) + "\n"


def _jsonable(f: dict) -> dict:
    def keys(d):
        return {"|".join(k) if isinstance(k, tuple) else k: v for k, v in d.items()}

    return {
        "agents": f["agents"],
        "partial": f["partial"],
        "by_set": f["by_set"],
        "accuracy": {k: {"rate": v[0], "ci": v[1]} for k, v in keys(f["accuracy"]).items()},
        "gaps": f["gaps"],
        "ranks": f["ranks"],
        "terse_rules": f["terse_rules"],
        "versions": {
            "|".join(k): {"runs": v["runs"], "old": dict(v["old"]), "new": dict(v["new"])}
            for k, v in f["versions"].items()
        },
        "asks": {"|".join(k): dict(v) for k, v in f["asks"].items()},
        "recall": f["recall"],
        "repeats": keys(f["repeats"]),
        "failures": keys(f["failures"]),
        "tally": {k: list(v) for k, v in keys(f["tally"]).items()},
    }


def run_findings(
    batches: list[str],
    out_dir: Path,
    *,
    audit_runs: Path,
    results: Path | None = None,
    questions: Path | None = None,
    root: Path | None = None,
) -> dict:
    """Write findings.md, findings.json and terse-misses.md to out_dir."""
    root = root or (results.parent if results else Path(__file__).resolve().parent.parent)
    f = build(batches, audit_runs, results=results, questions=questions, root=root)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "findings.md").write_text(markdown(f))
    (out_dir / "findings.json").write_text(json.dumps(_jsonable(f), indent=1, sort_keys=True) + "\n")
    (out_dir / "terse-misses.md").write_text(samples_markdown(f["rows"], f["sets"], root))
    return f
