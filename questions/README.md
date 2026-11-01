# Question ladders

Each dataset gets a folder (`tpch/`). Each `<dataset>/qNN.toml` holds one question at several levels of specification, least specified first, plus the target SQL that defines a correct answer and the `db` it runs on. Every level shares the target and the same tool instructions (`<dataset>/preamble.md`, put in front of every level's brief by the loader), so differences in outcome come from the business brief alone, and from how much the model fills in. Format: see `../README.md` ("Questions").

| Rung | Level | What it is | What it tests |
| --- | --- | --- | --- |
| 0 | `terse` | One line, the way it gets typed into Slack. No constants, ambiguous terms left ambiguous. | Does the model ask, assume, or guess? Does it find the right date range on its own? |
| 1 | `stakeholder` | How a thoughtful stakeholder would ask: plain English, the key definitions ("late" = after promised date), constants in natural form ("July through September"). | Can it translate a clear business definition into correct SQL without structural hints? |
| 2 | `spec` | Fully specified: exact dates, half-open ranges, column count, "count each order at most once." | Ceiling. If it fails here the model can't do the task; if it passes only here, the owner is doing the work. |

Add rungs freely (`misleading`, `with-schema-hint`), or skip files and pass free text with `uv run evals.py ask "..." --target-sql ...`.

The one rule: **never say "TPC-H" or "Q21" in a prompt.** The answer key is the target SQL, never the prompt, and preflight refuses prompts that name the benchmark or a query number.

## Novel questions (`tpch-novel/`)

Six questions written for this eval on 2026-10-31, on the same database with the same shared preamble, so no model can have memorized them before that date. They are public from that date: treat later runs as post-exposure and record the run date. Each target was checked against an independently written second query (they agree exactly; `tests/test_second_derivations.py`), and each file's `note` names the trap and how far off the tempting answer is.

| Id | Business question | Trap |
| --- | --- | --- |
| n01 | Order value of orders containing Brand#13 parts, by year | Joining to line items fans out and double-counts order value |
| n02 | European customers with no 1997 orders, by nation | A date filter in WHERE turns the outer join inner; never-ordered customers count |
| n03 | Top customer per region by 1996 net revenue | Region via the customer's nation; revenue after discount |
| n04 | Monthly 1997 order counts with change from the prior month | January compares with December 1996, not NULL |
| n05 | Average 1995 orders per customer, by segment | Customers with zero orders count as zero (1.50, not 2.64) |
| n06 | 1994 first-order cohort returning within 90 days, by segment | "First order" means first ever; the window is per customer |

Run them alongside the TPC-H ladder (`uv run evals.py run questions/tpch questions/tpch-novel ...`). The gap between memorized and novel questions is itself a result.

**Who wrote these, and what that did.** The novel and hard questions and their reference SQL were drafted by Claude Opus 5.5 in Claude Code and checked by hand. On the terse rung, several references carried that model's habits:
- columns the brief never asks for: a count or a denominator on n01 and n06, a revenue column on n03 where the brief asks only who, two statistics on h02, a share on h03;
- one form of a value: a month as a `'YYYY-MM'` string on n04.

The grader compares columns and values, so the agents that share those habits, the Claude models, scored higher. Graded against the v2 references below, the same saved answers show it. On n01 and n06, requiring exactly the reference's columns, Claude models go from 59 of 80 runs to 3 and the other agents from 5 of 192 to 186. Under the grading rule below, both groups are near 100%. Read v1 terse results on these sets as partly agreement with one author's habits.

## How the terse rung is graded: ambiguity, by design

A terse brief leaves definitions open on purpose: what "order size" means, which date puts an item in a year, how close orders must be to form a spree. That is what the rung tests. The question's intended definition, in the reference SQL and the file's `note`, is the rubric. **An agent that assumes another reasonable definition is graded wrong**, even when its reading is defensible, and the note says which readings are graded that way.

The rung rewards asking, or at least stating the assumption. A run is headless, though: nobody answers, and the shared preamble tells the agent to submit when confident and state its assumptions. In the 2026-10-31 grid, 4 of 4,623 graded terse runs asked instead of answering; the final message's assumptions are kept for review, not graded.

What is not part of the definition is not graded:
- **Columns:** the reference returns only what the brief asks for. Extra columns are correct, and the verdict (`extra_columns`) notes them. A column the brief asks for that is missing is wrong.
- **Order:** column order and row order are not graded (unless a brief asks for a sort).
- **Representation:** the same value in another form is correct, through `accepted_sql` (below). A month as a date, a timestamp, `'1997-01'`, a number or a name is the same month.

## Novel questions, version 2 (`tpch-novel-v2/`)

The terse rung of the six novel questions:
- the same prompts as v1, and the same definitions;
- references that return only what each terse brief asks;
- `accepted_sql` for other forms of the same value: n01's year as a number, text or a year-start timestamp; n04's month in six forms.

Every saved v1 terse run is also a valid v2 run, so `uv run evals.py regrade <batches> --questions questions/tpch-novel-v2 --name novel-v2` grades them again without running an agent. n02 and n05 are unchanged, and their re-grade reproduces v1's grades exactly. The v2 references were drafted by the same model and reviewed by GPT-6.1 Sol, a different model family. The review's data-dependent points were checked against the database; a human review is still owed.

## Hard questions, version 2 (`tpch-hard-v2/`)

The terse rung of the six hard questions, built the same way:
- the same prompts and definitions as v1;
- references that return only what each terse brief asks: h02's spree count, h03's number of customers, h04's percentile, h06's supplier names. h01 and h05 are unchanged.

Each note names the reasonable readings that are graded wrong:
- **h04:** "order size" as the order's total price (121 of 125 terse runs);
- **h06:** the year by receipt date (the most common choice);
- **h05:** a blank change for 1993;
- **h02:** a 7-day gap with at least two orders (the most common choice).

Re-graded, the 810 saved hard terse runs score 288 correct. The column fix alone moves h03 from 0 to 134 of 135; h04 stays at 0, all of it the order-size reading.

A cross-family review argued for accepting those other readings instead. Accepted alternatives were briefly used that way and were removed, because an alternative definition makes the rung stop testing ambiguity. Choosing them from what most agents did would also grade toward the agents.

## Answer, label and context columns

Each file marks what its target's columns are for, used when a run declares its answer columns (`--declare-columns`, below):
- `answer_columns`: the columns that answer the question. An entry is a name, or a list of names that each answer it alone, such as `["c_custkey", "c_name"]`: a customer's key or name.
- `label_columns`: the columns that say which row is which (a month, a segment). When the key is left out, every non-answer column is a label.
- **Context:** a column in neither list (a supplier's address beside "which suppliers") is not graded and not used to pair rows.

"Which ..." questions set `label_columns = []`: their answer is an identity, and the rest is context. The markings are judgment calls, drafted for all 46 files on 2026-11-01 and awaiting review.

With `uv run evals.py run ... --declare-columns`:
- **The tool:** `submit_sql` also takes `answer_columns`, the names of the agent's result columns that answer the request, and each prompt asks for them. That makes it a different prompt, so a different batch.
- **Pairing rows:** labels pair the agent's rows with the reference's, in any order, after normalizing their form. Text ignores case and spaces; a year can be a number or text; a day can be a date, a midnight timestamp or `'YYYY-MM-DD'`; a column of first-of-month days reads as months. Pairing by label stops the right values under the wrong labels from passing.
- **Scoring:** the declared columns are compared with the answer columns on the paired rows, with the grader's rounding rule. Undeclared columns are not graded.
- **Fallback:** when the labels can't pair (other rows, or a label form not normalized, such as a month number or name), the run is graded by its whole result, as without a declaration, with `accepted_sql` still applying.
- **Record:** `result.json`'s `declaration` says which way the run was graded and how the columns lined up.

`uv run evals.py regrade <batches> --questions questions/tpch questions/tpch-novel questions/tpch-hard --name declared-all --declare-all` checks this on saved runs, which declared nothing, by treating every result column as declared. On the 2026-10-31 grid:
- 13,487 of 13,864 runs paired by label;
- no run that is correct today became wrong;
- 220 became right: n04's month written as a date (95), "which" answers without the reference's context columns (q15 46, q21 36, q10 32, q02 9), and two more.

## Accepted alternatives

A question file, or a level, may list `accepted_sql = ['''...''', ...]`: other queries whose results are **the same answer in another form**. The grader tries the target first, then each alternative, and takes the first correct match, preferring one with exactly the reference's columns. `result.json` records which in `reference` (0 is the target).

Alternatives are for representations, never for other definitions: on a terse brief the definition is the rubric. An alternative belongs to the target it sits beside, so a level with its own target does not inherit the file's alternatives.

## Hard novel questions (`tpch-hard/`)

Six questions written on 2026-10-31 to separate the strongest models from strong ones. The difficulty is in exact multi-step reasoning, not in vagueness: the `spec` level defines everything, and the agent still has to get islands, sessions, running thresholds, an unusual percentile, a comparable partial-year window or tied ranks exactly right. Each target agrees with an independently written second query (`tests/test_second_derivations.py`), and `tests/test_evalkit.py` checks that the tempting shortcut grades wrong.

| Id | Business question | What makes it hard |
| --- | --- | --- |
| h01 | Distribution of customers' longest streak of consecutive ordering months | Gaps and islands over calendar months across year boundaries |
| h02 | Buying sprees per segment, 1996-1997 (new spree after a gap of more than 30 days) | Sessionization: gap from the previous order, not the spree start; window edges |
| h03 | Fewest customers making up at least half of each nation's 1995 revenue | Running sum with deterministic ties; which customers count |
| h04 | 90th percentile order revenue by region, 1994, orders with no returns, exclusive method | No built-in computes PERCENTILE.EXC; whole-order exclusion; returns only exist before mid-1995 |
| h05 | Year-over-year active customers by region on a Jan 1 to Aug 2 window | The data ends 1998-08-02; 1993's change needs 1992 |
| h06 | Five largest within-nation on-time rank improvements, 1995 to 1996 | Competition ranking with ties, per nation, per ship year |
