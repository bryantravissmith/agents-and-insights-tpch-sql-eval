"""Databases for the tests.

synthetic_db  built from tpch_synthetic.sql (plain SQL, a few hundred rows), so every test runs without tpch.duckdb
tpch_db       the real SF 0.1 database; skips with a reason when it has not been built
db            both, as two parametrizations of the same test: synthetic always runs, tpch when present

    uv run --with pytest --with duckdb --with 'mcp<2' pytest tests/ -q
"""

import re
import sys
from pathlib import Path

import duckdb
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from evalkit import runner

SYNTHETIC_SQL = Path(__file__).with_name("tpch_synthetic.sql")
QUESTIONS = HERE / "questions" / "tpch"
NO_TPCH = f"{runner.DB} missing: the real-data tier needs it (uv run setup_db.py, or symlink an existing copy in)"


@pytest.fixture(scope="session")
def synthetic_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("db") / "tpch_synthetic.duckdb"
    con = duckdb.connect(str(path))
    con.execute(SYNTHETIC_SQL.read_text())
    con.close()
    return path


@pytest.fixture(scope="session")
def tpch_db() -> Path:
    if not runner.DB.exists():
        pytest.skip(NO_TPCH)
    return runner.DB


@pytest.fixture(params=["synthetic", "tpch"])
def db(request) -> Path:
    return request.getfixturevalue(f"{request.param}_db")


@pytest.fixture
def questions_on(tmp_path):
    """Copies of the TPC-H question files whose `db` points at the given database (targets stay where they are)."""

    def copy(db: Path, *names: str) -> Path:
        out = tmp_path / "questions" / "tpch"
        out.mkdir(parents=True, exist_ok=True)
        for src in sorted(QUESTIONS.glob("*.toml")):
            if names and src.stem not in names:
                continue
            text = re.sub(r'(?m)^db = "[^"]*"', f'db = "{db.resolve()}"', src.read_text())
            text = re.sub(
                r'(?m)^target_sql_file = "([^"]*)"',
                lambda m: f'target_sql_file = "{(QUESTIONS / m.group(1)).resolve()}"',
                text,
            )
            (out / src.name).write_text(text)
        if (QUESTIONS / "preamble.md").exists():
            (out / "preamble.md").write_text((QUESTIONS / "preamble.md").read_text())
        return out

    return copy
