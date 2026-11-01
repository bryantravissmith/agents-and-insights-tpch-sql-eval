"""Grading a run that declared its answer columns.

The agent submits a query and names the result columns that answer the question. The reference marks its own
answer columns (`answer_columns` in the question file) and its labels, which say which row is which (month,
segment, region): `label_columns`, or by default every other column. Columns in neither list are context, such
as a supplier's address beside "which suppliers": not graded and not used to pair rows. An answer slot may list
interchangeable columns, such as a customer's key or name: either one answers it. Grading:

1. Labels pair the rows. Each reference label column is matched to a candidate column whose values agree after
   normalizing their form (`normalize_column`): text ignores case and surrounding spaces, a year may be a number
   or text, a day may be a date, a midnight timestamp or 'YYYY-MM-DD', and a column of first-of-month days reads
   as months ('YYYY-MM'). Rows pair by those values, in any order. Pairing by label is what stops the right
   values on the wrong rows (five correct averages under the wrong segments) from passing.
2. Each answer slot needs a declared column whose values equal one of the slot's columns on every paired row,
   in any column order, with the grader's rounding rule (`grading.cell_eq`). Undeclared columns are not graded.
   With no labels (a "which suppliers" question), the declared values must match as a set of rows.
3. Verdicts are the grader's own: exact, extra_columns (a declared column the reference does not have), and
   missing_columns or values_differ when an answer is absent or wrong. bad_declaration means a declared name is
   not in the result.

When the labels cannot be paired (other rows, a label in a form not normalized, or a reference whose labels do
not identify its rows), `grade_declared` returns None and the caller grades the run as if nothing was declared.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from itertools import product
from pathlib import Path

from .grading import (
    EXACT_COLUMNS,
    Grade,
    Result,
    SQLError,
    cell_eq,
    compare,
    execute,
    expected_result,
    grade_any,
)

_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T]00:00:00(?:\.0+)?)?$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_INT = re.compile(r"^-?\d+$")


def _value(v):
    if v is None:
        return None
    if isinstance(v, float):  # grading.Num: integral numbers compare as ints, so 1997 == '1997'
        return int(v) if v.is_integer() else float(v)  # NaN and infinity are not integers
    s = str(v).strip()
    if m := _DAY.match(s):
        return m.group(1)
    if _MONTH.match(s):
        return s
    if _INT.match(s):
        return int(s)
    return s.lower()


def normalize_column(values) -> list:
    """One label column's values in a common form, so the same label written two ways compares equal."""
    out = [_value(v) for v in values]
    days = [v for v in out if v is not None]
    if days and all(isinstance(v, str) and _DAY.match(v) for v in days):
        if all(v.endswith("-01-01") for v in days):  # year starts: the year
            return [int(v[:4]) if v is not None else None for v in out]
        if all(v.endswith("-01") for v in days):  # month starts: the month
            return [v[:7] if v is not None else None for v in out]
    return out


def _project(res: Result, idx: list[int]) -> Result:
    return Result(tuple(res.columns[i] for i in idx), tuple(tuple(r[i] for i in idx) for r in res.rows))


def _pair_labels(got: Result, exp: Result, labels: list[int]) -> tuple[list[int], dict] | None:
    """Candidate column for each reference label column, and reference row -> candidate row; None if none fit."""
    exp_cols = {j: normalize_column(r[j] for r in exp.rows) for j in labels}
    got_cols = [normalize_column(r[i] for r in got.rows) for i in range(len(got.columns))]
    exp_keys = [tuple(exp_cols[j][k] for j in labels) for k in range(len(exp.rows))]
    if len(set(exp_keys)) < len(exp_keys):
        return None  # the reference's labels do not identify its rows
    cands = [[i for i, col in enumerate(got_cols) if Counter(col) == Counter(exp_cols[j])] for j in labels]

    def search(k, used, acc):
        if k == len(labels):
            keys = [tuple(got_cols[i][r] for i in acc) for r in range(len(got.rows))]
            if len(set(keys)) == len(keys) and set(keys) == set(exp_keys):
                where = {key: r for r, key in enumerate(keys)}
                return list(acc), {k2: where[key] for k2, key in enumerate(exp_keys)}
            return None
        for i in cands[k]:
            if i not in used and (hit := search(k + 1, used | {i}, acc + [i])) is not None:
                return hit
        return None

    return search(0, frozenset(), [])


def _assign(got_rows, exp_rows, n_got: int, slots: list[list[int]]) -> dict[int, int]:
    """The largest one-to-one matching of answer slots to declared columns whose values equal one of the slot's
    columns on every paired row."""
    fits = {
        s: [i for i in range(n_got) if any(all(cell_eq(g[i], e[j]) for g, e in zip(got_rows, exp_rows)) for j in slot)]
        for s, slot in enumerate(slots)
    }
    best: dict[int, int] = {}

    def search(s, used, acc):
        nonlocal best
        if len(acc) > len(best):
            best = dict(acc)
        if s == len(slots) or len(best) == len(slots):
            return
        for i in fits[s]:
            if i not in used:
                search(s + 1, used | {i}, acc | {s: i})
        search(s + 1, used, acc)  # leave this slot unmatched

    search(0, frozenset(), {})
    return best


def _slot_name(exp: Result, slot: list[int]) -> str:
    return " or ".join(exp.columns[j] for j in slot)


def answer_slots(exp_columns: tuple[str, ...], answer_columns: tuple[tuple[str, ...], ...]) -> list[list[int]]:
    """Reference column indices for each answer slot; with no marking, every column is its own slot."""
    index = {c.lower(): j for j, c in enumerate(exp_columns)}
    if not answer_columns:
        return [[j] for j in range(len(exp_columns))]
    return [[index[c.lower()] for c in slot] for slot in answer_columns]


DECLARATION = "answer_columns.json"


def read_declaration(run_dir: Path) -> list[str] | None:
    """The answer columns a run declared in submit_sql, or None when it declared none."""
    p = Path(run_dir) / DECLARATION
    if not p.exists():
        return None
    cols = json.loads(p.read_text())
    return [str(c) for c in cols] if isinstance(cols, list) else None


ALL = "*"  # as a declaration: every result column (checking runs that declared nothing)


def grade_task(candidate_sql: str | None, declared: list[str] | str | None, task, db: Path) -> tuple[Grade, int, dict]:
    """The grade for one run: by its declared answer columns when it declared them and its rows pair up,
    otherwise against the target and its accepted alternatives (`grading.grade_any`). Returns the grade, which
    reference graded it (0 is the target) and the declaration notes ({} when nothing was declared).
    `declared` is the list of names, ALL for every result column, or None for no declaration."""
    if declared is not None:
        names = None if declared == ALL else declared
        hit = grade_declared(candidate_sql, names, task.target_sql, task.answer_columns, db, task.label_columns)
        if hit is not None:
            return hit[0], 0, hit[1] | {"graded_by": "declared columns"}
    g, ref = grade_any(candidate_sql, task.target_sql, task.accepted_sql, db)
    return g, ref, ({"graded_by": "whole result (labels did not pair)"} if declared is not None else {})


def grade_declared(
    candidate_sql: str | None,
    declared: list[str] | None,
    target_sql: str,
    answer_columns: tuple[tuple[str, ...], ...],
    db: Path,
    label_columns: tuple[str, ...] | None = None,
) -> tuple[Grade, dict] | None:
    """Grade the declared columns against the reference's answer columns; None means fall back to grade_any.

    `declared` None means every result column, which is how saved runs that declared nothing can be checked.
    """
    exp = expected_result(target_sql, Path(db))
    if not candidate_sql or not candidate_sql.strip():
        return Grade("no_sql", rows_expected=len(exp.rows)), {}
    try:
        got = execute(candidate_sql, Path(db))
    except SQLError as e:
        return Grade("sql_error", rows_expected=len(exp.rows), detail=str(e)), {}
    names = {c.lower(): i for i, c in enumerate(got.columns)}
    if declared is None:
        declared = list(got.columns)
    unknown = [d for d in declared if d.strip().lower() not in names]
    if not declared or unknown:
        detail = f"declared {unknown or 'nothing'}; the result has {list(got.columns)}"
        return Grade("bad_declaration", rows_got=len(got.rows), rows_expected=len(exp.rows), detail=detail), {}
    dec = list(dict.fromkeys(names[d.strip().lower()] for d in declared))
    slots = answer_slots(exp.columns, answer_columns)
    in_slots = {j for slot in slots for j in slot}
    if label_columns is None:  # every column that is not an answer says which row is which
        labels = [j for j in range(len(exp.columns)) if j not in in_slots]
    else:  # the rest are context: neither graded nor used to pair rows
        wanted = {c.lower() for c in label_columns}
        labels = [j for j, c in enumerate(exp.columns) if c.lower() in wanted]
    notes = {"declared": [got.columns[i] for i in dec], "answers": [_slot_name(exp, s) for s in slots]}

    if not labels:  # nothing identifies the rows: the declared values must match as a set of rows
        best = None
        for chosen in product(*slots):  # one column per slot, for slots with interchangeable columns
            trial = compare(_project(got, dec), _project(exp, list(chosen)))
            rank = 0 if trial.verdict in EXACT_COLUMNS else 1 if trial.correct else 2
            if best is None or rank < best[0]:
                best = (rank, trial, chosen)
        _, g, chosen = best
        g.column_map = [dec[i] for i in g.column_map] if g.column_map else None
        return g, notes | {"matched": [exp.columns[j] for j in chosen]}
    paired = _pair_labels(got, exp, labels)
    if paired is None:
        return None
    label_cols, rows = paired
    notes["labels"] = {exp.columns[j]: got.columns[i] for j, i in zip(labels, label_cols)}
    notes["labels_normalized"] = any(
        [r[i] for r in got.rows] != [r[j] for r in exp.rows] or got.columns[i].lower() != exp.columns[j].lower()
        for j, i in zip(labels, label_cols)
    )
    got_rows = [tuple(got.rows[rows[k]][i] for i in dec) for k in range(len(exp.rows))]
    match = _assign(got_rows, list(exp.rows), len(dec), slots)
    g = Grade("", rows_got=len(got.rows), rows_expected=len(exp.rows))
    g.column_map = [dec[match[s]] for s in range(len(slots)) if s in match]
    lined_up = ", ".join(f"{got.columns[dec[i]]}->{_slot_name(exp, slots[s])}" for s, i in sorted(match.items()))
    if len(match) == len(slots):
        g.verdict = "exact" if len(dec) == len(slots) else "extra_columns"
        extra = [got.columns[dec[i]] for i in range(len(dec)) if i not in match.values()]
        g.detail = f"answers line up as {lined_up}" + (f"; extra declared {extra}" if extra else "")
    elif len(match) == len(dec):
        g.verdict = "missing_columns"
        missing = [_slot_name(exp, slots[s]) for s in range(len(slots)) if s not in match]
        g.detail = f"matches {lined_up}; missing {missing}"
    else:
        g.verdict = "values_differ"
        s = next(s for s in range(len(slots)) if s not in match)
        g.detail = f"no declared column matches {_slot_name(exp, slots[s])} on the paired rows"
    return g, notes
