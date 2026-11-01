"""Execution-based grading: run the candidate SQL and the target SQL on the same database, compare results.

Comparison is by position, never by column name (agents alias freely). A candidate number matches when it
is the target rounded to the candidate's own precision (ROUND(x, 2) gives +-0.005, never looser) or within REL_TOL,
so rounding is accepted but an unrounded wrong value is not, however small (a 0.025 market share is not 0.0286).

Verdicts, best to worst:
  exact              same rows, same order, same columns
  exact_unordered    same multiset of rows, different order
  columns_reordered  same rows once the candidate's columns are permuted to line up with the target's
  extra_columns      the target's columns are all present, plus more the question did not ask for
  missing_columns    every candidate column lines up with a target column, but some target columns are absent
  rows_differ        different row count
  values_differ      same shape, different values
  columns_differ     different column count and no projection lines up
  sql_error          candidate failed to execute (or was refused by the blocklist)
  no_sql             nothing to grade
The first four count as correct; the runner also requires the SQL to have come from submit_sql. Extra columns
are correct because they do not change the answer: every column the reference asks for is there with the right
values. The verdict still says extra_columns, so reports can note them. EXACT_COLUMNS is the stricter set,
kept for that note.
"""

from __future__ import annotations

import datetime as dt
import decimal
import math
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import duckdb

from sqlguard import ENGINE_CONFIG, refuse

EXACT_COLUMNS = frozenset({"exact", "exact_unordered", "columns_reordered"})
CORRECT = EXACT_COLUMNS | {"extra_columns"}
LENIENT = CORRECT  # correct wherever the SQL came from; see RunResult.lenient
REL_TOL = 1e-12  # floating-point aggregation noise, not a business-error allowance
QUERY_TIMEOUT_S = 120


@dataclass(frozen=True)
class Result:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]


@dataclass
class Grade:
    verdict: str
    rows_got: int = 0
    rows_expected: int = 0
    detail: str = ""
    column_map: list[int] | None = field(default=None)  # candidate column for each matched target column

    @property
    def correct(self) -> bool:
        return self.verdict in CORRECT


class SQLError(Exception):
    pass


# ---------- execution ----------


class Num(float):
    """A float that remembers how many decimal places the database gave it (DECIMAL scale, or the shortest repr)."""

    places: int

    def __new__(cls, v):
        n = super().__new__(cls, v)
        # Keep exact database numerics: float coercion loses integers above 2**53
        # and a relative tolerance can otherwise accept large errors in totals.
        n.exact = decimal.Decimal(v) if isinstance(v, (int, decimal.Decimal)) else None
        if isinstance(v, decimal.Decimal):
            n.places = max(-v.as_tuple().exponent, 0) if v.is_finite() else 0
        elif isinstance(v, int):
            n.places = 0
        else:
            r = repr(float(v))
            n.places = 17 if ("e" in r or "inf" in r or "nan" in r) else len(r.split(".")[1].rstrip("0"))
        return n


def _canon(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, decimal.Decimal)):
        return Num(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    if isinstance(v, str):
        return v.rstrip()
    return str(v)


def execute(sql: str, db: Path, *, guard: bool = True, timeout_s: int = QUERY_TIMEOUT_S) -> Result:
    """Run one statement read-only. guard=True applies the agent blocklist (use for candidate SQL)."""
    if guard and (why := refuse(sql)):
        raise SQLError(why)
    con = duckdb.connect(str(db), read_only=True, config=ENGINE_CONFIG)
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql.strip().rstrip(";"))
        cols = tuple(d[0] for d in cur.description or ())
        rows = tuple(tuple(_canon(v) for v in r) for r in cur.fetchall())
    except duckdb.Error as e:
        raise SQLError(str(e).splitlines()[0][:300]) from None
    finally:
        timer.cancel()
        con.close()
    return Result(cols, rows)


@lru_cache(maxsize=256)
def expected_result(target_sql: str, db: Path) -> Result:
    """Target results are cached per process: a grid grades the same target hundreds of times."""
    return execute(target_sql, db, guard=False)


# ---------- comparison ----------


def _num(v) -> bool:
    return isinstance(v, float)


def cell_eq(got, exp) -> bool:
    """Candidate cell vs target cell. Not symmetric: the candidate's precision sets the rounding allowance."""
    if _num(got) and _num(exp):
        got_exact, exp_exact = getattr(got, "exact", None), getattr(exp, "exact", None)
        got_places, exp_places = getattr(got, "places", 17), getattr(exp, "places", 17)
        if got_exact is not None and exp_exact is not None:
            if got_exact == exp_exact:
                return True
            if 2 <= got_places < exp_places:
                return abs(got_exact - exp_exact) <= decimal.Decimal(5).scaleb(-got_places - 1)
            return False
        # Rounding coarser than 2 places is not an answer, and a value like 25.5 may be exact rather than
        # rounded, so the allowance never exceeds +-0.005.
        places = max(getattr(got, "places", 17), 2)
        half_ulp = 0.5 * 10.0**-places if places < min(exp_places, 17) else 0.0
        return math.isclose(got, exp, rel_tol=REL_TOL) or abs(got - exp) <= half_ulp * (1 + 1e-9)
    return got == exp


def rows_eq(a: tuple, b: tuple) -> bool:
    return len(a) == len(b) and all(cell_eq(x, y) for x, y in zip(a, b))


def _key(row: tuple):
    # Rounding here collapses distinct small values into one sort key, so an
    # actually unordered match can fail depending on the input row order.
    def key(v):
        if v is None:
            return (0, "")
        if _num(v):
            exact = getattr(v, "exact", None)
            return (1, (1, 0) if math.isnan(v) else (0, exact if exact is not None else v))
        return (2, str(v))

    return tuple(key(v) for v in row)


def _same_ordered(got, exp) -> bool:
    return len(got) == len(exp) and all(rows_eq(g, e) for g, e in zip(got, exp))


def _same_unordered(got, exp) -> bool:
    return len(got) == len(exp) and all(rows_eq(g, e) for g, e in zip(sorted(got, key=_key), sorted(exp, key=_key)))


def _project(rows, idx):
    return tuple(tuple(r[i] for i in idx) for r in rows)


def _column_candidates(got: Result, exp: Result) -> list[list[int]]:
    """For each target column, the candidate columns whose sorted values match it."""

    def col(rows, j):
        return sorted(((r[j],) for r in rows), key=_key)

    out = []
    for j in range(len(exp.columns)):
        ej = col(exp.rows, j)
        out.append([i for i in range(len(got.columns)) if all(rows_eq(a, b) for a, b in zip(col(got.rows, i), ej))])
    return out


def _find_projection(got: Result, exp: Result, targets: list[int], cands: list[list[int]]) -> list[int] | None:
    """Candidate column for each target column in `targets` such that the projected rows match, or None."""
    want = _project(exp.rows, targets)

    # Small search: target queries have <= ~10 columns and candidates per column are few.
    def search(k, used, acc):
        if k == len(targets):
            proj = _project(got.rows, acc)
            return list(acc) if (_same_ordered(proj, want) or _same_unordered(proj, want)) else None
        for i in cands[targets[k]]:
            if i not in used and (hit := search(k + 1, used | {i}, acc + [i])) is not None:
                return hit
        return None

    return search(0, frozenset(), [])


def compare(got: Result, exp: Result) -> Grade:
    g = Grade(verdict="", rows_got=len(got.rows), rows_expected=len(exp.rows))
    same_width = len(got.columns) == len(exp.columns)
    if same_width and _same_ordered(got.rows, exp.rows):
        g.verdict = "exact"
        return g
    if same_width and _same_unordered(got.rows, exp.rows):
        g.verdict = "exact_unordered"
        return g
    if got.rows and len(got.rows) == len(exp.rows):
        cands = _column_candidates(got, exp)
        present = [j for j, c in enumerate(cands) if c]
        idx = _find_projection(got, exp, present, cands) if present else None
        # A partial match only means "missing" when every candidate column is accounted for; a leftover
        # candidate column is the wrong-valued version of a target column (values_differ).
        if idx is not None and (len(present) == len(exp.columns) or len(idx) == len(got.columns)):
            g.column_map = idx
            lined_up = ", ".join(f"{got.columns[i]}->{exp.columns[j]}" for i, j in zip(idx, present))
            if len(present) < len(exp.columns):
                g.verdict = "missing_columns"
                missing = [c for j, c in enumerate(exp.columns) if j not in present]
                g.detail = f"matches {lined_up}; missing {missing}"
            elif same_width:
                g.verdict = "columns_reordered"
                g.detail = f"columns line up as {lined_up}"
            else:
                g.verdict = "extra_columns"
                g.detail = f"matches {lined_up}; extra {[c for i, c in enumerate(got.columns) if i not in idx]}"
            return g
    if len(got.rows) != len(exp.rows):
        g.verdict = "rows_differ"
        g.detail = f"got {len(got.rows)} rows, expected {len(exp.rows)}"
    elif not same_width:
        g.verdict = "columns_differ"
        g.detail = (
            f"got {len(got.columns)} columns {list(got.columns)}, expected {len(exp.columns)} {list(exp.columns)}"
        )
    else:
        g.verdict = "values_differ"
        for i, (a, b) in enumerate(zip(got.rows, exp.rows)):
            if not rows_eq(a, b):
                g.detail = f"first diff at row {i}: got {a} expected {b}"
                break
    return g


def grade_sql(candidate_sql: str | None, target_sql: str, db: Path) -> Grade:
    """Grade one candidate query against the target query. The public entry point."""
    exp = expected_result(target_sql, Path(db))
    if not candidate_sql or not candidate_sql.strip():
        return Grade("no_sql", rows_expected=len(exp.rows))
    try:
        got = execute(candidate_sql, Path(db))
    except SQLError as e:
        return Grade("sql_error", rows_expected=len(exp.rows), detail=str(e))
    return compare(got, exp)


def grade_any(candidate_sql: str | None, target_sql: str, accepted: tuple[str, ...], db: Path) -> tuple[Grade, int]:
    """Grade against the target and any accepted alternatives; returns the grade and which reference it used.

    An alternative is the same answer in another form, such as a month given as a date or as 'YYYY-MM': one
    reference fixes one representation, and a representation is not part of the answer. Alternatives are not
    for other definitions; the target's definitions are the rubric. Reference 0 is the target. The first correct
    match wins, preferring one with exactly the reference's columns; with none, the grade is against the target,
    so the miss is diagnosed against it.
    """
    primary = grade_sql(candidate_sql, target_sql, db)
    if primary.verdict in EXACT_COLUMNS or not accepted or primary.verdict in ("no_sql", "sql_error"):
        return primary, 0
    grades = [primary] + [grade_sql(candidate_sql, alt, db) for alt in accepted]
    for ok in (lambda g: g.verdict in EXACT_COLUMNS, lambda g: g.correct):
        for i, g in enumerate(grades):
            if ok(g):
                return g, i
    return primary, 0
