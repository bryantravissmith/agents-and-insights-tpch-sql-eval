"""Text-to-SQL evals for headless agent CLIs (pi, claude, codex, grok, agy) on the TPC-H orders database.

from evalkit import Agent, Task, load_questions, run_grid, run_one, grade_sql

tasks = load_questions("questions/tpch/q21.toml")                   # every disclosure level
tasks += [Task.adhoc("Which nations ...?", target_sql="SELECT ...")]  # any question, any target
results = run_grid(tasks, ["pi:openai/gpt-5.6-luna", "codex:gpt-5.6-luna", "agy:gemini-3.8-flash-low"],
                   conditions=["bare", "notes"], repeats=5, jobs=8)
"""

from .grading import Grade, grade_sql
from .harnesses import HARNESSES
from .runner import (
    Agent,
    RunResult,
    load_scorecard,
    progress,
    run_grid,
    run_one,
    summarize,
)
from .tasks import Task, load_question, load_questions

__all__ = [
    "HARNESSES",
    "Agent",
    "Grade",
    "RunResult",
    "Task",
    "grade_sql",
    "load_question",
    "load_questions",
    "load_scorecard",
    "progress",
    "run_grid",
    "run_one",
    "summarize",
]
