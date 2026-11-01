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

A batch can also ask for the grain (`run --declare-grain`): the columns that identify one row of the result, empty
for a one-row answer. A declared grain must be unique in the run's own result (else grain_not_unique, as when a
fan-out join repeats a row), and only its columns may pair rows with the reference's labels. With a different
number of rows than the reference, a grain whose values contain the reference's labels (or are contained in them)
is the same grain with other rows (rows_differ, naming the extra or missing values, such as a streak length of 0);
otherwise the grain itself differs (grain_differs). Both are wrong answers that name the error.

When the labels cannot be paired (other rows, a label in a form not normalized, or a reference whose labels do
not identify its rows), `grade_declared` returns None and the caller grades the run as if nothing was declared;
with a declared grain, only when the row counts agree.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
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


def _pair_labels(
    got: Result, exp: Result, labels: list[int], allowed: list[int] | None = None
) -> tuple[list[int], dict] | None:
    """Candidate column for each reference label column, and reference row -> candidate row; None if none fit.
    `allowed` limits the candidate columns that may serve as labels (a declared grain)."""
    exp_cols = {j: normalize_column(r[j] for r in exp.rows) for j in labels}
    got_cols = [normalize_column(r[i] for r in got.rows) for i in range(len(got.columns))]
    exp_keys = [tuple(exp_cols[j][k] for j in labels) for k in range(len(exp.rows))]
    if len(set(exp_keys)) < len(exp_keys):
        return None  # the reference's labels do not identify its rows
    pool = range(len(got.columns)) if allowed is None else allowed
    cands = [[i for i in pool if Counter(got_cols[i]) == Counter(exp_cols[j])] for j in labels]

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


def _same_grain(got: Result, exp: Result, labels: list[int], grain_idx: list[int]) -> str:
    """Whether a declared grain is the reference's grain with other rows: every reference label has a grain
    column whose values contain the label's or are contained in them. Returns which label values are extra or
    missing ("" when the grain itself differs)."""
    found = []
    for j in labels:
        exp_vals = set(normalize_column(r[j] for r in exp.rows))
        for i in grain_idx:
            got_vals = set(normalize_column(r[i] for r in got.rows))
            if got_vals <= exp_vals or got_vals >= exp_vals:
                found.append(
                    (exp.columns[j], sorted(map(str, got_vals - exp_vals)), sorted(map(str, exp_vals - got_vals)))
                )
                break
        else:
            return ""
    parts = [
        f"{name}: "
        + ", ".join(p for p in (f"extra {extra[:5]}" if extra else "", f"missing {miss[:5]}" if miss else "") if p)
        for name, extra, miss in found
        if extra or miss
    ]
    return "the same grain, other rows: " + ("; ".join(parts) or "repeated label values")


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


def _numeric(res: Result, i: int) -> bool:
    vals = [r[i] for r in res.rows if r[i] is not None]
    return bool(vals) and all(isinstance(v, (int, float)) for v in vals)


_KEY_NAME = re.compile(r"(key|id)$", re.IGNORECASE)


def _identifies(res: Result, i: int, j: int) -> bool:
    """Whether column i is a row identifier for column j: named like one (s_suppkey, supplier_id) and in one-to-one
    correspondence with it. The name matters: a count with no ties also maps one-to-one onto the names."""
    pairs = {(r[i], r[j]) for r in res.rows}
    one_to_one = len(pairs) == len({r[i] for r in res.rows}) == len({r[j] for r in res.rows})
    return one_to_one and bool(_KEY_NAME.search(res.columns[i]))


def _shown_context(got: Result, exp: Result, extra: list[int], got_used: list[int], context: list[int], same) -> str:
    """Why a shown context value is wrong ("" when none is). `context` holds the question's checked columns
    (`checked_columns`): context, so optional, but the number an answer would show beside it, like q21's count of
    late orders beside "which suppliers". A declared column that shows one must be right. Agents name columns their
    own way, so the check goes by values: a declared extra column i shows checked column j when `same(i, j)`, its
    values equal j's on the rows as already paired. A numeric checked column nobody matched is shown wrong when an
    unmatched extra column is numeric too; row identifiers, such as a key beside the matched name (`got_used`),
    never count. That guess is why the check is opt-in per question: on q10, agents add numbers of their own
    (returned quantity) that the reference's unrelated account balance would wrongly fail."""
    shown, unmatched = set(), []
    for j in context:
        hit = next((i for i in extra if i not in shown and same(i, j)), None)
        if hit is None:
            unmatched.append(j)
        else:
            shown.add(hit)
    rest = [i for i in extra if i not in shown and not any(_identifies(got, i, k) for k in got_used)]
    for j in (j for j in unmatched if _numeric(exp, j)):
        if wrong := [i for i in rest if _numeric(got, i)]:
            return f"shows {got.columns[wrong[0]]} where the reference's {exp.columns[j]} differs"
    return ""


def _slot_name(exp: Result, slot: list[int]) -> str:
    return " or ".join(exp.columns[j] for j in slot)


def answer_slots(exp_columns: tuple[str, ...], answer_columns: tuple[tuple[str, ...], ...]) -> list[list[int]]:
    """Reference column indices for each answer slot; with no marking, every column is its own slot."""
    index = {c.lower(): j for j, c in enumerate(exp_columns)}
    if not answer_columns:
        return [[j] for j in range(len(exp_columns))]
    return [[index[c.lower()] for c in slot] for slot in answer_columns]


DECLARATION = "answer_columns.json"


@dataclass(frozen=True)
class Declaration:
    """What a run declared in submit_sql: its answer columns and, when the batch asked, its grain (the columns
    that identify one row; empty for a one-row answer). None for answer means every result column."""

    answer: tuple[str, ...] | None
    grain: tuple[str, ...] | None = None


def read_declaration(run_dir: Path) -> Declaration | None:
    """The declaration a run saved with submit_sql, or None when it declared nothing.

    answer_columns.json holds a list of answer columns, or {"answer": [...], "grain": [...]} when the batch also
    asked for the grain."""
    p = Path(run_dir) / DECLARATION
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    if isinstance(data, list):
        return Declaration(tuple(str(c) for c in data))
    if isinstance(data, dict) and isinstance(data.get("answer"), list):
        grain = data.get("grain")
        return Declaration(
            tuple(str(c) for c in data["answer"]), tuple(str(c) for c in grain) if isinstance(grain, list) else None
        )
    return None


ALL = Declaration(None)  # every result column as the answer: how saved runs that declared nothing are checked


def grade_task(candidate_sql: str | None, declared: Declaration | None, task, db: Path) -> tuple[Grade, int, dict]:
    """The grade for one run: by its declaration when it made one and its rows pair up, otherwise against the
    target and its accepted alternatives (`grading.grade_any`). Returns the grade, which reference graded it
    (0 is the target) and the declaration notes ({} when nothing was declared).

    A declaration is graded against the target and then each accepted alternative (the same answer in another
    form, such as a share as a percent), as grade_any does for a whole result; the first that passes grades it.
    Alternatives carry the target's column names, so the markings apply to them unchanged."""
    if declared is not None:
        first = None
        for ref, reference in enumerate(task.references):
            hit = grade_declared(
                candidate_sql,
                None if declared.answer is None else list(declared.answer),
                reference,
                task.answer_columns,
                db,
                task.label_columns,
                grain=None if declared.grain is None else list(declared.grain),
                checked_columns=task.checked_columns,
            )
            if hit is None:
                continue
            first = first or (hit, ref)
            if hit[0].correct:
                return hit[0], ref, hit[1] | {"graded_by": "declared columns"}
        if first is not None:
            (g, notes), ref = first
            return g, ref, notes | {"graded_by": "declared columns"}
    g, ref = grade_any(candidate_sql, task.target_sql, task.accepted_sql, db)
    return g, ref, ({"graded_by": "whole result (labels did not pair)"} if declared is not None else {})


def grade_declared(
    candidate_sql: str | None,
    declared: list[str] | None,
    target_sql: str,
    answer_columns: tuple[tuple[str, ...], ...],
    db: Path,
    label_columns: tuple[str, ...] | None = None,
    grain: list[str] | None = None,
    checked_columns: tuple[str, ...] = (),
) -> tuple[Grade, dict] | None:
    """Grade the declared columns against the reference's answer columns; None means fall back to grade_any.

    `checked_columns` are context columns that must be right when a declared column shows them (`_shown_context`).

    `declared` None means every result column, which is how saved runs that declared nothing can be checked.
    `grain` is the run's declared grain, the columns that identify one of its rows. When given, it must be unique
    in the run's own result (else grain_not_unique), and rows pair only through it: a different number of rows
    than the reference is grain_differs. Without it, any result column may pair rows.
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
    checked_names = {c.lower() for c in checked_columns}
    context = [
        j for j, c in enumerate(exp.columns) if j not in in_slots and j not in labels and c.lower() in checked_names
    ]
    notes = {"declared": [got.columns[i] for i in dec], "answers": [_slot_name(exp, s) for s in slots]}
    grain_idx = None
    if grain is not None:
        if unknown := [c for c in grain if c.strip().lower() not in names]:
            detail = f"grain {unknown}; the result has {list(got.columns)}"
            return Grade("bad_declaration", rows_got=len(got.rows), rows_expected=len(exp.rows), detail=detail), notes
        grain_idx = list(dict.fromkeys(names[c.strip().lower()] for c in grain))
        notes["grain"] = [got.columns[i] for i in grain_idx]
        cols = [normalize_column(r[i] for r in got.rows) for i in grain_idx]
        keys = Counter(tuple(col[r] for col in cols) for r in range(len(got.rows)))
        if (repeated := [k for k, n in keys.items() if n > 1]) or (not grain_idx and len(got.rows) > 1):
            detail = f"{len(got.rows)} rows, {len(keys)} distinct values of the grain {notes['grain']}" + (
                f"; e.g. {repeated[0]} appears {keys[repeated[0]]} times" if repeated else ""
            )
            g = Grade("grain_not_unique", rows_got=len(got.rows), rows_expected=len(exp.rows), detail=detail)
            return g, notes

    if not labels:  # nothing identifies the rows: the declared values must match as a set of rows
        best = None
        for chosen in product(*slots):  # one column per slot, for slots with interchangeable columns
            trial = compare(_project(got, dec), _project(exp, list(chosen)))
            rank = 0 if trial.verdict in EXACT_COLUMNS else 1 if trial.correct else 2
            if best is None or rank < best[0]:
                best = (rank, trial, chosen)
        _, g, chosen = best
        g.column_map = [dec[i] for i in g.column_map] if g.column_map else None
        # An exact match leaves no column map: the declared columns are the answers, in order.
        used, chosen_exp = (list(g.column_map) if g.column_map else list(dec)), list(chosen)

        def same_rows(i, j):  # no labels: the rows pair as a set, with the matched answers alongside
            return compare(_project(got, used + [i]), _project(exp, chosen_exp + [j])).correct

        extra = [i for i in dec if i not in used]
        if g.correct and (why := _shown_context(got, exp, extra, used, context, same_rows)):
            g = Grade("values_differ", rows_got=len(got.rows), rows_expected=len(exp.rows), detail=why)
        return g, notes | {"matched": [exp.columns[j] for j in chosen]}
    paired = _pair_labels(got, exp, labels, allowed=grain_idx)
    if paired is None:
        if grain_idx is not None and len(got.rows) != len(exp.rows):
            ref = [exp.columns[j] for j in labels]
            detail = f"{len(got.rows)} rows at the grain {notes['grain']}; the reference has {len(exp.rows)} at {ref}"
            if same := _same_grain(got, exp, labels, grain_idx):  # the same grain, other rows
                return Grade(
                    "rows_differ", rows_got=len(got.rows), rows_expected=len(exp.rows), detail=f"{detail}; {same}"
                ), notes | {"grain_matches": True}
            return Grade("grain_differs", rows_got=len(got.rows), rows_expected=len(exp.rows), detail=detail), notes
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

        def same_paired(i, j):  # rows already paired by their labels
            return all(cell_eq(got.rows[rows[k]][i], exp.rows[k][j]) for k in range(len(exp.rows)))

        used = list(label_cols) + [dec[i] for i in match.values()]
        others = [dec[i] for i in range(len(dec)) if dec[i] not in used]
        if why := _shown_context(got, exp, others, used, context, same_paired):
            g = Grade("values_differ", rows_got=len(got.rows), rows_expected=len(exp.rows), detail=why)
            g.column_map = [dec[match[s]] for s in range(len(slots))]
    elif len(match) == len(dec):
        g.verdict = "missing_columns"
        missing = [_slot_name(exp, slots[s]) for s in range(len(slots)) if s not in match]
        g.detail = f"matches {lined_up}; missing {missing}"
    else:
        g.verdict = "values_differ"
        s = next(s for s in range(len(slots)) if s not in match)
        g.detail = f"no declared column matches {_slot_name(exp, slots[s])} on the paired rows"
    return g, notes
