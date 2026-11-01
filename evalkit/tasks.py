"""Tasks: one prompt plus the target SQL that defines a correct answer.

A question file (TOML) holds one question at several levels of disclosure, ordered from least to most
specified. Every level shares the question's target SQL, so differences in outcome come from the wording.

    id = "q21"
    db = "../../tpch.duckdb"                           # relative to this file; the database the target runs on
    target_sql_file = "../../tpch/queries/q21.sql"  # relative to this file; or target_sql = '''...'''
    note = "Never shown to the agent."

    [[levels]]
    name = "terse"
    prompt = '''Identify suppliers based in Saudi Arabia who were the sole cause of a late order.'''

    [[levels]]
    name = "spec"
    prompt = '''...'''

Ad hoc questions skip the file: Task.adhoc("Which customers ...?", target_sql="SELECT ...", db=...).
A task without a db runs on the default the caller passes (run_one / run_grid `db`, default tpch.duckdb).
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

# The models have memorized TPC-H. A prompt that names the benchmark or a query number measures recall.
LEAKY = re.compile(r"tpc[-_ ]?h|\bq\d{2}\b", re.IGNORECASE)


@dataclass(frozen=True)
class Task:
    id: str  # selects nothing on its own; the target SQL is the answer key
    level: str  # disclosure level name, or "adhoc"
    prompt: str
    target_sql: str
    rank: int = 0  # position in the ladder: 0 = least specified
    db: Path | None = None  # database for the agent and the grader; None = the caller's default
    accepted_sql: tuple[str, ...] = ()  # other forms of the same answer (see grading.grade_any)
    # The target's columns that answer the question, one tuple per answer (interchangeable columns, such as a
    # key and a name, share one); the target's other columns are labels. Empty: every column is an answer.
    answer_columns: tuple[tuple[str, ...], ...] = ()
    # The target's columns that say which row is which (a month, a segment). None: every non-answer column.
    # Columns in neither list are context: not graded and not used to pair rows.
    label_columns: tuple[str, ...] | None = None
    # The briefs of the more specified levels of the same question, in ladder order: what ask_clarification
    # reveals, one per ask, in --ask-step batches. Levels with their own target ask another question and are left out.
    next_briefs: tuple[str, ...] = ()

    @property
    def references(self) -> tuple[str, ...]:
        """The target, then the accepted alternatives: everything that defines a correct answer."""
        return (self.target_sql, *self.accepted_sql)

    def __post_init__(self):
        if not self.prompt.strip():
            raise ValueError(f"{self.id}/{self.level}: empty prompt")
        if not self.target_sql.strip():
            raise ValueError(f"{self.id}/{self.level}: a target SQL is required to grade the answer")

    @property
    def prompt_sha(self) -> str:
        return hashlib.sha256(self.prompt.encode()).hexdigest()[:10]

    @property
    def name(self) -> str:
        return f"{self.id}/{self.level}"

    def check_leaks(self):
        # The briefs an ask may reveal (--ask-step) reach the agent too.
        for text in (self.prompt, *self.next_briefs):
            if m := LEAKY.search(text):
                raise ValueError(
                    f"{self.name}: prompt contains {m.group(0)!r}; never name the benchmark or query number"
                )

    @classmethod
    def adhoc(
        cls, prompt: str, target_sql: str, id: str | None = None, level: str = "adhoc", db: str | Path | None = None
    ) -> Task:
        sha = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        return cls(
            id=id or f"adhoc-{sha}",
            level=level,
            prompt=prompt.strip(),
            target_sql=target_sql.strip(),
            db=Path(db).resolve() if db else None,
        )


def _target_sql(spec: dict, base: Path, where: str) -> str:
    if "target_sql" in spec and "target_sql_file" in spec:
        raise ValueError(f"{where}: give target_sql or target_sql_file, not both")
    if "target_sql_file" in spec:
        return (base / spec["target_sql_file"]).read_text()
    if "target_sql" in spec:
        return spec["target_sql"]
    raise ValueError(f"{where}: missing target_sql / target_sql_file")


def _preamble(spec: dict, base: Path, where: str) -> str:
    """The instructions every level shares: `preamble_file` if given, else `preamble.md` beside the file, else none."""
    if "preamble_file" in spec:
        f = base / spec["preamble_file"]
        if not f.exists():
            raise ValueError(f"{where}: preamble_file {f} does not exist")
        return f.read_text().strip()
    f = base / "preamble.md"
    return f.read_text().strip() if f.exists() else ""


def load_question(path: str | Path, levels: list[str] | None = None) -> list[Task]:
    """All levels of one question file, least specified first. `levels` filters by name, keeping ladder order.

    Each level's prompt is the shared preamble (tool and submission instructions), a blank line, then the
    level's business brief, so levels differ only in the brief. A level with `raw = true` gets its prompt verbatim.
    """
    path = Path(path)
    spec = tomllib.loads(path.read_text())
    qid = spec.get("id") or path.stem
    target = _target_sql(spec, path.parent, str(path))
    preamble = _preamble(spec, path.parent, str(path))
    db = (path.parent / spec["db"]).resolve() if "db" in spec else None
    if db and not db.exists():
        raise ValueError(f"{path}: db {db} does not exist")
    tasks = []
    briefs = []  # (brief, shares the question's target), per level
    for rank, lv in enumerate(spec.get("levels", [])):
        # A level may override the target when a rung deliberately asks a different question.
        lv_target = (
            _target_sql(lv, path.parent, f"{path}:{lv['name']}")
            if ("target_sql" in lv or "target_sql_file" in lv)
            else target
        )
        # Accepted alternatives follow the target they belong to: a level with its own target brings its own.
        accepted = lv.get("accepted_sql", [] if lv_target is not target else spec.get("accepted_sql", []))
        if not isinstance(accepted, list) or not all(isinstance(a, str) and a.strip() for a in accepted):
            raise ValueError(f"{path}:{lv['name']}: accepted_sql must be a list of SQL strings")
        # Each entry is one answer: a column name, or a list of interchangeable names (a key or a name).
        answers = lv.get("answer_columns", [] if lv_target is not target else spec.get("answer_columns", []))
        slots = [[a] if isinstance(a, str) else a for a in answers] if isinstance(answers, list) else None
        if slots is None or not all(
            isinstance(s, list) and s and all(isinstance(c, str) and c.strip() for c in s) for s in slots
        ):
            raise ValueError(f"{path}:{lv['name']}: answer_columns must list column names or lists of names")
        labels = lv.get("label_columns", None if lv_target is not target else spec.get("label_columns"))
        if labels is not None and not (isinstance(labels, list) and all(isinstance(c, str) for c in labels)):
            raise ValueError(f"{path}:{lv['name']}: label_columns must be a list of column names")
        brief = lv["prompt"].strip()
        briefs.append((brief, lv_target is target))
        prompt = brief if lv.get("raw") or not preamble else f"{preamble}\n\n{brief}"
        tasks.append(
            Task(
                id=qid,
                level=lv["name"],
                prompt=prompt,
                target_sql=lv_target,
                rank=rank,
                db=db,
                accepted_sql=tuple(a.strip() for a in accepted),
                answer_columns=tuple(tuple(c.strip() for c in s) for s in slots),
                label_columns=None if labels is None else tuple(c.strip() for c in labels),
            )
        )
    if not tasks:
        raise ValueError(f"{path}: no [[levels]]")
    tasks = [
        replace(t, next_briefs=tuple(b for b, shared in briefs[i + 1 :] if shared)) if briefs[i][1] else t
        for i, t in enumerate(tasks)
    ]
    if levels:
        unknown = set(levels) - {t.level for t in tasks}
        if unknown:
            raise ValueError(f"{path}: no level(s) {sorted(unknown)}; has {[t.level for t in tasks]}")
        tasks = [t for t in tasks if t.level in levels]
    return tasks


def load_questions(*paths: str | Path, levels: list[str] | None = None) -> list[Task]:
    """Question files and/or directories of them (every *.toml below, e.g. questions/ = all datasets)."""
    out: list[Task] = []
    for p in map(Path, paths):
        files = sorted(p.rglob("*.toml")) if p.is_dir() else [p]
        for f in files:
            out += load_question(f, levels)
    return out
