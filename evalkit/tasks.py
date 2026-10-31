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
from dataclasses import dataclass
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
        if m := LEAKY.search(self.prompt):
            raise ValueError(f"{self.name}: prompt contains {m.group(0)!r}; never name the benchmark or query number")

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
    for rank, lv in enumerate(spec.get("levels", [])):
        # A level may override the target when a rung deliberately asks a different question.
        lv_target = (
            _target_sql(lv, path.parent, f"{path}:{lv['name']}")
            if ("target_sql" in lv or "target_sql_file" in lv)
            else target
        )
        brief = lv["prompt"].strip()
        prompt = brief if lv.get("raw") or not preamble else f"{preamble}\n\n{brief}"
        tasks.append(Task(id=qid, level=lv["name"], prompt=prompt, target_sql=lv_target, rank=rank, db=db))
    if not tasks:
        raise ValueError(f"{path}: no [[levels]]")
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
