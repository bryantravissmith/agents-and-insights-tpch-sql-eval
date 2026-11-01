# agents-and-insights-tpch-sql-eval

A text-to-SQL eval for AI agents, from Agents & Insights, a newsletter and video series by Bryan Smith on AI agents doing real data science: real tasks on real data, checked. It runs headless agent sessions (pi, Claude Code, Codex, Grok, Antigravity) against the TPC-H orders database, logs every query the agent executes, and grades the SQL it submits by **executing it next to a target query** and comparing results. The same question runs at several levels of disclosure (a one-line Slack ask, a stakeholder brief, a full spec), under several conditions (bare / notes / checks), many times, across models and harnesses, in parallel.

What is in the box:

- `questions/tpch/`: the 22 TPC-H questions rewritten in business English, each at three levels, with the shared tool instructions in `preamble.md`.
- `questions/tpch-novel/`: six questions written for this eval (first published 2026-10-31), each built around a common analytics trap. TPC-H is in every model's training data; these are the control.
- `questions/tpch-hard/`: six harder novel questions (first published 2026-10-31) meant to separate the strongest models: gaps and islands, sessionization, running thresholds, an exclusive percentile, a comparable partial-year window, tied ranks.
- `questions/tpch-grain/`: six questions (first published 2026-11-01) each built so the tempting first query has the wrong grain: averaging over lines instead of orders, over orders instead of customers, counting lines instead of customers or orders, dividing by 52 weeks, a join that multiplies stock. Their terse briefs pin every definition that is not about grain, so a miss is a grain mistake. Pair with `--declare-grain`.
- `tpch/`: the 22 reference queries and their validated answers at scale factor 0.1, extracted from DuckDB's `tpch` extension (`tpch/extract_tpch.py`).
- `evalkit/`, `evals.py`: the runner, the harness adapters and the execution grader. `sqlguard.py`, `mcp_server.py`, `extensions/duckdb-tools.ts`: the two read-only tools every agent gets.

Requires [uv](https://docs.astral.sh/uv/) and whichever agent CLIs you want to test. No model is called by the tests.

```bash
uv run setup_db.py                                   # once: build tpch.duckdb (SF 0.1)
uv run evals.py harnesses                            # which CLIs are on PATH

# a question file: every disclosure level x 3 agents x 5 repeats, 8 at a time
uv run evals.py run questions/tpch/q21.toml -a pi:openai/gpt-5.6-luna codex:gpt-5.6-luna agy:gemini-3.8-flash-low -r 5 -j 8

# only some levels, two conditions, every question file for a dataset, cap agy at 2 concurrent runs
uv run evals.py run questions/tpch -l terse spec -c bare notes -a agy:claude-sonnet-5-5-medium -r 3 --harness-jobs agy=2

# an arbitrary prompt; the target query is always required
uv run evals.py ask "How many customers are in each market segment?" \
    --target-sql "SELECT c_mktsegment, count(*) FROM customer GROUP BY 1 ORDER BY 1" -a codex:gpt-5.6-luna -r 5

uv run evals.py grade attempt.sql --target-sql-file tpch/queries/q21.sql   # grader only, no agent
uv run evals.py summary                     # latest batch; --batch <id> or --batch all
uv run evals.py progress                    # running batches: runs done, correct so far, time left
uv run evals.py resume <batch> -j 6         # finish a stopped batch; re-run rate-limit and other provider failures
uv run evals.py resume <batch> -a agy:gemini-3.8-flash   # only one agent, e.g. after its quota resets
uv run evals.py report <batch> [<batch> ...] --csv report.csv   # accuracy + efficiency across batches
uv run evals.py audit <batch> [<batch> ...] --out audit/        # cheating attempts and prior exposure
uv run evals.py findings <batch> [...] --out findings/ --audit audit/   # claims with intervals over questions
uv run evals.py regrade <batch> [...] --questions questions/tpch-novel-v2 --name novel-v2   # new key, same runs
uv run evals.py style <batch> [<batch> ...] --out style/        # SQL style and dialect stumbles by maker and set
uv run evals.py run questions/tpch -a pi:openai/gpt-5.6-luna --declare-columns   # agents name their answer columns
uv run evals.py run questions/tpch -a pi:openai/gpt-5.6-luna --allow-ask         # agents may ask; nobody answers
```

`report` reads every current run of the batches and prints accuracy (and lenient, which also counts right answers that were never submitted) by agent, question set and brief level, then efficiency per agent: queries, median wall time, tokens, cost per run, cost per correct answer and tokens per correct answer. Runs that failed on the provider's side (`harness_error`) are left out of every rate and counted in an `excluded` column; misses, timeouts and non-submissions stay in, so a model that spends and fails pays for it. Every cost is API-equivalent and labeled with its basis.

`audit` reads the same runs for two things a score does not show, and writes the evidence for review: `summary.md` (rates by agent and question set), `flagged.md` (every flagged run with the SQL or message that triggered it), `runs.jsonl` (every signal for every run) and `markers.json` (what counts as recall for each question and level). The rules are in `evalkit/audit.py`.

- **Cheating attempts**, from every `run_sql` call in `queries.log`, refused ones included: reaching for the answer key (DuckDB's `tpch_queries()`, `tpch_answers()`, `PRAGMA tpch`, `dbgen`), reading files or attaching databases, probing the engine (`duckdb_extensions()`, `SET`, `INSTALL`), and tool calls the CLI denied. A `got through` column counts attempts that ran.
- **Prior exposure**: naming TPC-H or the specific query in the final message (or asking the engine for it by number), reproducing details of the reference query that the brief at that level leaves out and the data does not reveal (TPC-H's output names such as `numwait` and `custdist`, constants such as q22's country codes or q21's `LIMIT 100`), and submitting SQL in the published queries' 1990s comma-join style (`FROM lineitem, orders WHERE ...`, used by 17 of the 22 references). The novel and hard questions, written after the models were trained, run through the same detectors as the control.

`findings` turns the batches into claims, each with a 95% interval from resampling questions (ten repeats of one question are not independent, and a set of six questions is a small sample): the spec-brief ceiling, the TPC-H minus novel gap at each level (as correct, with two notes: `exact columns`, nothing beyond the reference's columns, and `values only`, right values with some reference columns missing), whether runs that reproduce the reference score more often within a question, per-question terse tables, how misses fail, how often repeats disagree, cost per correct answer, and how often agents asked for clarification instead of assuming. It also writes `terse-misses.md`, a fixed sample of terse misses to read by hand. The rules are in `evalkit/findings.py`.

`style` describes how each model family writes SQL and how often DuckDB rejects it, every table sliced by question set (TPC-H, novel, hard): queries, schema lookups and failed queries per run, DuckDB's error types, calls to functions DuckDB does not have, the share of submitted queries using each construct (comma joins, CTEs, window functions, `QUALIFY`, `EXTRACT` versus `year()`, `GROUP BY 1`, `NULLIF`, `ROUND` and more) and query length. A construct common on TPC-H and absent from the other sets is the benchmark's style, not the model's. The rules are in `evalkit/style.py`.

`regrade` grades saved runs against new versions of their questions, matched by level and prompt hash, without running an agent: the reference is never shown to the agent, so a version that changes only the reference leaves every run as it was. It writes `results/regrade-<name>/`; each run keeps its prompt, graded SQL, query log and final message, and records the original grade and run. See `questions/README.md` for why the novel set has a v2. A run that declared its answer columns is re-graded by them; `--declare-all` treats every saved run as if it had declared all of its result columns.

**Declared answer columns** (`run --declare-columns`): `submit_sql` also takes the names of the result columns that answer the request. The grader pairs rows by the question's label columns, normalizing their form, and scores only the declared columns against the answer columns. See `questions/README.md`, "Answer, label and context columns", and `evalkit/declared.py`.

**Declared grain** (`run --declare-grain`, which implies `--declare-columns`): `submit_sql` also takes `grain_columns`, the columns that identify one row of the result (empty for a single-row answer). The grain must be unique in the agent's own result (else `grain_not_unique`), and only those columns may pair rows with the reference's labels; a different number of rows is `grain_differs`. Both are wrong answers that name the error, so grain mistakes, the classic analysis bug, can be counted apart from wrong values.

**Asking for clarification** (`run --allow-ask`, combinable with the declare flags): the agent also gets `ask_clarification(question)`, and the prompt says it may ask when the request is ambiguous. The tool always replies "No clarification is available. Use your best judgement, and state your assumptions in your final message.", so asking changes nothing but what we can observe. Each call is an `ASK` entry in `queries.log` (not counted as a query) and a line in `asks.jsonl`; `result.json` records `asks` (the number of calls) and `ask_questions` (the first 10). `findings` counts a tool call as asking, alongside questions in the final message.

`experiments/full-grid-2026-10-31.sh` runs `reparse`, `rescore`, `report`, `audit`, `style`, the novel and hard v2 `regrade` and `findings` over the 2026-10-31 batches in one go.

From Python (a notebook, a script):

```python
import sys; H = "."; sys.path.insert(0, H)   # run from the repo root; needs duckdb in the environment
from evalkit import Agent, Task, load_questions, run_grid, run_one, grade_sql

tasks = load_questions(f"{H}/questions/tpch/q21.toml", levels=["terse", "spec"])
tasks.append(Task.adhoc("Which nations have the most suppliers?", target_sql="SELECT n_name, count(*) ..."))
results = run_grid(tasks, ["pi:anthropic/claude-sonnet-5-5", "agy:claude-sonnet-5-5-medium"],
                   conditions=["bare", "notes"], repeats=5, jobs=8)
[r.verdict for r in results]
```

## Agents are `harness:model`

The model id is whatever that CLI calls it. Same model, different harness, e.g. `pi:anthropic/claude-sonnet-5-5` vs `claude:claude-sonnet-5-5` vs `agy:claude-sonnet-5-5-medium`. In Python, `Agent("codex", "gpt-5.6-luna", extra_args=("-c", "model_reasoning_effort=high"), label="codex:luna@high")` passes flags straight to the CLI.

| Harness | Invocation | Tools the agent has | Condition notes | Cost | Status |
| --- | --- | --- | --- | --- | --- |
| `pi` | `pi -p --mode json` | `extensions/duckdb-tools.ts`; `--no-builtin-tools --no-extensions --no-skills --no-context-files --no-mcp` | `--append-system-prompt` | yes (pi price table) | verified |
| `codex` | `codex exec --json --ephemeral --ignore-user-config` | MCP; shell, browser, apps, web search, image tools disabled; read-only sandbox | `AGENTS.md` in cwd | tokens only | verified |
| `agy` | `agy -p --output-format json --project aai-evals --sandbox` | MCP via a workspace plugin; project grants allow only the SQL tools (and `ask_clarification`, offered only with `--allow-ask`), deny shell/file/web | `AGENTS.md` in cwd | tokens, priced from Google's list price (`evalkit/pricing.py`) | verified |
| `claude` | `claude -p --output-format json --tools ""` | MCP, `--strict-mcp-config`, `dontAsk`; `--bare` when `ANTHROPIC_API_KEY` is set, else `--setting-sources ""` | `--append-system-prompt-file` | the evaluated model's `modelUsage` cost, without Claude Code's own side calls; list price where Claude Code marks it `unknown` (Haiku 5.5) | verified on Sonnet 5.5 and Haiku 5.5 with API authentication |
| `grok` | `grok -p --output-format streaming-messages-json` | MCP via trusted scratch config; built-ins removed; file/shell/web denied; compatibility discovery disabled | `AGENTS.md` in cwd | if reported | verified on 1.0.70 with Grok 4.7; native usage identifies `grok-4.7-build` |

agy has no per-run MCP flag, so each run's work dir carries `.agents/plugins/audit/` and agy names the server `audit_audit`. The permission grants live in `~/.gemini/config/projects/aai-evals.json`, which evalkit writes (and rewrites if it drifts) and never touches anything else in `~/.gemini`.

Known confound: global instruction files still reach some CLIs (`~/.codex/AGENTS.md` for codex, `~/.claude/` rules for grok). They are style rules, not data, but they are part of "the harness as installed on this machine".

## Questions: one file, ordered levels of disclosure

Questions live under `questions/<dataset>/`, one file per question. `questions/tpch/q21.toml`:

```toml
id = "q21"
db = "../../tpch.duckdb"                            # relative to this file: the agent's tools and the grader use it
target_sql_file = "../../tpch/queries/q21.sql"   # relative to this file, or target_sql = '''...'''
note = "Never shown to the agent."

[[levels]]                 # least specified first; the order is the ladder order in reports
name = "terse"
prompt = '''Identify suppliers based in Saudi Arabia who were the sole cause of a late order.'''

[[levels]]
name = "spec"
prompt = '''...exact definitions, columns, sort order...'''
```

**Shared instructions.** `questions/<dataset>/preamble.md` holds the tool and submission instructions. The loader puts it, a blank line, then the level's brief together, so every level, terse included, tells the agent about `run_sql` and `submit_sql` the same way and only the business wording varies. The composed text is what `prompt.md` stores and what `prompt_sha` hashes. A file can point elsewhere with `preamble_file = "..."`; a level with `raw = true` gets its prompt verbatim. Before 2026-10-31 (harness v1) only the stakeholder and spec briefs carried tool instructions, so v1 terse results mix "did not know to submit" with "did not know the definition".

Add levels freely (`misleading`, `with-schema-hint`). A level may carry its own `target_sql` / `target_sql_file` when a rung deliberately asks a different question. Write your own files for new questions: any `.toml` in this shape works, not just TPC-H.

**One database per question file.** A new dataset is a new folder (`questions/olist/`) whose files point `db` at that dataset's DuckDB file; a grid can mix datasets, and each run's tools and grader use its own question's database. A file (or `Task.adhoc(..., db=...)`) without `db` falls back to `--db`, then to `tpch.duckdb`. The `conditions/` notes describe the TPC-H schema; a new dataset wants its own notes.

**Never name the benchmark or a query number in a prompt.** Preflight refuses prompts matching `TPC-H` or `qNN` (a quarter like "Q3 1993" is fine); `--allow-leaky` overrides it for a deliberate contamination test.

## Grading

The grader executes the target SQL and the agent's SQL read-only on the same database (candidate SQL also passes the tool blocklist) and compares by column position, never by name. Integers and DECIMAL values retain their exact values; floating-point comparisons use 1e-12 relative tolerance for aggregation noise. A candidate with lower decimal precision can match a rounded target: `ROUND(x, 2)` gets at most +-0.005. Rounding allowances do not apply when both exact values have the same precision. This rejects incorrect large totals that the former 1e-6 relative tolerance accepted.

**What counts as correct.** Every column the reference asks for, with the right values. Extra columns, column order, row order and the form of a value (through `accepted_sql`, other representations of the same answer) do not change the answer, so they pass. The verdict and `reference` in `result.json` note them. On a terse brief, the question's intended definition is the rubric: a reasonable assumption that differs is graded wrong, by design (see `questions/README.md`, "How the terse rung is graded").

Until 2026-11-01, extra columns were not correct. `uv run evals.py rescore <batch>` recomputes `correct` from the recorded verdicts under the current rule, with no agent run again. It keeps each changed run's earlier score in `rescored` and logs the change in the manifest.

These verdicts measure result equivalence, not every instruction in a prompt. A prompt that forbids rounding or requires a particular row/column order needs a separate strict audit; `exact_unordered` and `columns_reordered` still count as equivalent results.

| Verdict | Meaning | Correct? |
| --- | --- | --- |
| `exact` | same rows, same order | yes |
| `exact_unordered` | same rows, different order | yes |
| `columns_reordered` | same rows once columns are permuted | yes |
| `extra_columns` | all target columns present, plus others | yes (noted; before 2026-11-01, no) |
| `missing_columns` | every column it returned lines up, but some target columns are absent | no |
| `bad_declaration` | a declared answer or grain column is not in the result (declaring batches only) | no |
| `grain_not_unique` | the declared grain repeats in the agent's own result, as when a join fans out | no |
| `grain_differs` | the declared grain gives a different number of rows than the reference's | no |
| `rows_differ` / `values_differ` / `columns_differ` | wrong answer | no |
| `sql_error` | candidate fails or is refused | no |
| `no_sql` / `timeout` / `harness_error` | nothing to grade | no |

**Only SQL passed to `submit_sql` can be correct.** If the agent never submits, the grader still grades the last ```` ```sql ```` block of its final message, or else its last successful `run_sql`. The result is marked `[not submitted; graded …]` for diagnosis, and only the `lenient` column (right answer, wherever the SQL came from) can credit it.

Summary cells read `k/n`, with `(+m)` when m more runs had the right rows but added columns or never submitted. A level whose prompt text changed between batches is split by prompt hash (`spec@ee42397`), never pooled.

## What a run leaves behind

```
results/<batch>/                       batch = YYYYMMDD-HHMMSS[-name]
├── summary.txt  results.jsonl
└── <task>-<level>-<condition>-<harness>-<model>-r<n>/
    ├── prompt.md  meta.json           exact prompt, argv, agent, condition
    ├── transcript.jsonl  stderr.log   the CLI's own output
    ├── queries.log                    every statement the agent ran, in order; read this before the answer
    ├── answer.sql                     what it submitted (the grader re-executes it)
    ├── asks.jsonl                     its ask_clarification questions (--allow-ask runs that asked)
    ├── final_message.txt              its closing message (assumptions)
    └── result.json                    verdict, detail, tokens, cost
results/scorecard.csv                  one row per run, every batch (local, gitignored)
```

The agent's working directory is an anonymous UUID scratch dir under `$TMPDIR/aai-evals/` (override with `AAI_WORK_ROOT`). It sits outside the repo and is a fresh git root, preventing ancestor-based context discovery. The cwd contains no question or batch name. This alone is not filesystem isolation: the disabled built-in tools and DuckDB external-access restrictions are also required. It is deleted after each run; `--keep-work` keeps it for debugging.

## Why the agent can't just look up the answer

With a shell and DuckDB, an agent could run `LOAD tpch; SELECT * FROM tpch_answers()` or `cat tpch/answers/sf0.1/q04.csv`. So every harness gets exactly two tools, and built-in shell/file/web tools are removed or denied:

| Tool | What it does | What it refuses (`sqlguard.py`) |
| --- | --- | --- |
| `run_sql` | executes one read-only statement, returns up to 50 rows as CSV, appends to `queries.log` | more than one statement; any statement that does not start with `SELECT`, `WITH`, `FROM`, `VALUES`, `TABLE`, `SHOW`, `DESCRIBE`, `SUMMARIZE`, `EXPLAIN` or `PIVOT` (so `INSTALL`, `LOAD`, `ATTACH`, `COPY`, `SET`, every write); `tpch_*`, `dbgen`, `read_*()`, `query()` and file paths anywhere outside comments |
| `submit_sql` | saves `answer.sql` and runs it to confirm it executes | same |
| `ask_clarification` | only with `--allow-ask`: records the question, always replies that no clarification is available | runs no SQL |

Since harness v2 the guard strips comments and blanks string literals before it looks at keywords, so `replace(...)`, `LIKE '%set%'`, a `;` inside a string or a trailing `-- call it done` comment are no longer refused. `extensions/duckdb-tools.ts` carries a copy of `sqlguard.py`; `tests/test_sqlguard.py` runs both on the same SQL.

TPC-H is still in every model's training data, and the schema (`l_orderkey`, `o_orderpriority`) is recognizable. Treat it as a warm-up golden set and expect optimistic scores.

The guard is advisory; the engine is the boundary. Python tools and grading connect read-only with `ENGINE_CONFIG` from `sqlguard.py`: external access off, extension autoinstall and autoload off, one thread, configuration locked. Pi's duckdb CLI sets one thread and turns autoinstall and autoload off, then enables safe mode, which disables external access and locks the configuration; it also skips user init files. So `SET enable_external_access = true` fails with "the configuration has been locked" and `tpch_queries()` fails as not in the catalog instead of loading the extension, whatever spelling gets past the guard (`tests/test_review_regressions.py` runs both against all three paths). Failed grid attempts are recorded as `harness_error` and remain in the denominator.

## The three conditions

| Condition | What the agent gets | What it tests |
| --- | --- | --- |
| `bare` | the question and the tools | baseline |
| `notes` | `bare` + `conditions/notes/AGENTS.md`: grain notes and known traps | does one page of context move the error rate? |
| `checks` | `notes` + mandatory answer-blind invariant checks before `submit_sql` | does forcing verification change behavior, or does the agent rationalize a failing check? |

## Tests

```bash
uv run --with pytest --with duckdb --with 'mcp<2' pytest tests/ -q
```

These cover the grader against all 22 targets plus known-wrong variants (fan-out join, swapped/extra/missing columns, rounding, blocklist), question loading, each harness's locked-down command line, and `run_one` / `run_grid` end to end through a fake harness. No model is called.

The suite needs no `tpch.duckdb`. `tests/conftest.py` builds a synthetic copy of the eight TPC-H tables (a few hundred rows, plain SQL in `tests/tpch_synthetic.sql`, no extension or download), and every grader, runner, isolation and MCP test runs on it. Tests that take the `db` fixture run a second time on the real `tpch.duckdb` when it exists and skip with the reason when it does not. Offline limitation: the synthetic numbers mean nothing, so the real-data tier (the 22 targets matching themselves, every question file's target, the numeric-tolerance checks on real totals) only runs where `uv run setup_db.py` has built the database, which needs the tpch extension download. Symlink an existing copy in to run it from a worktree.

## Fixed q01 harness comparison

```bash
uv run experiments/q01_pi_codex_luna.py --repeats 30 --jobs 12
uv run experiments/q01_pi_codex_luna.py --level terse --repeats 30 --jobs 12
```

This runs q01/bare with GPT-5.6 Luna at medium reasoning in both Pi and Codex, with a 300-second per-run timeout. The default prompt level is `spec`; use `--level terse` or `--level stakeholder` to select another level. Each batch snapshots the framework, question, reference SQL and database; records versions, hashes and observed concurrency; and writes a batch-local scorecard. Smoke tests should use a separate batch name and are not pooled into the 30 repeats. This is a repeatability comparison on one familiar question, not a general harness ranking.

For another question or several interleaved prompt levels:

```bash
uv run experiments/compare_luna.py --question q21 --level terse spec --repeats 30 --jobs 12
uv run experiments/analyze_batch.py results/<batch>
```

The first command makes 120 attempts: 30 per harness per prompt level. The audit makes no model calls, groups results by prompt level and harness, and counts failed SQL attempts as well as successful queries. Submission calls and refusals are reported separately.

### Explicit model comparisons

`experiments/compare_luna.py` keeps its Pi/Codex default and accepts `--agents-json`
for a list of `{harness, model, extra_args, label}` configurations. Each label must
have a unique filesystem slug. The manifest snapshots those configurations,
framework, prompts, target, database, and binary versions. Use separate batches
when authentication readiness requires phased execution; do not pool their timing
as a single simultaneous experiment.

`experiments/analyze_batch.py` groups by agent label and prompt level, re-executes
answers, and reparses raw usage. SQL attempts count successful and failed
`run_sql` executions; submissions and refusals are separate. Antigravity output
includes thinking; its input includes cache hits for Gemini 3.8 Flash but not for
Gemini 4 Argon, and the parser counts each only once. Native CLI token costs and API-equivalent estimates are not subscription
invoices. Grok's native model identifier may differ from the requested catalog ID.

## License and data

Code: MIT (see `LICENSE`). The 22 reference queries come from the TPC-H specification by way of DuckDB's MIT-licensed `tpch` extension; TPC-H is a trademark of the Transaction Processing Performance Council, and results from this harness are not TPC-H benchmark results. The database is generated locally by `setup_db.py` and is not distributed.
