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

**Who wrote these, and what that did.** The novel and hard questions and their reference SQL were drafted by Claude Opus 5.5 in Claude Code and checked by hand. On the terse rung, four references carried that model's habits: a count or a denominator the brief never asks for (n01, n06), a month as a `'YYYY-MM'` string (n04), and a revenue column where the brief asks only who (n03). The grader compares columns, so the agents that share those habits, the Claude models, scored higher on the terse rung. Graded against the v2 references below, the same saved answers flip: on n01 and n06, Claude models go from 59 of 80 runs correct to 3, and the other agents from 5 of 192 to 186. With extra columns allowed, both groups are near 100%. Read v1 terse novel results as agreement with one author's conventions, not as skill.

## Novel questions, version 2 (`tpch-novel-v2/`)

The terse rung of the six novel questions, with references that return only what each terse brief asks and `accepted_sql` alternatives where a correct answer has more than one form (a month as a date, a timestamp, `'YYYY-MM'`, a number or a name; a year as a number or text; day 90 included or not). The prompts are identical to v1's, so every saved v1 terse run is also a valid v2 run: `uv run evals.py regrade <batches> --questions questions/tpch-novel-v2 --name novel-v2` grades them again without running an agent. n02 and n05 are unchanged; their re-grade reproduces v1's grades exactly. The v2 references were drafted by the same model and reviewed by GPT-6.1 Sol (a different model family). The review's data-dependent points were checked against the database; a human review is still owed.

Two readings are still decided by the author, not the brief: "biggest customer" (n03v2) is ranked by money spent, not by number of orders, and the n05 average counts customers with no 1995 orders as zero.

## Hard questions, version 2 (`tpch-hard-v2/`): provisional

The terse rung of the six hard questions, built the same way as the novel v2 (same prompts, so `evals.py regrade` grades the saved runs). On the terse rung, the v1 hard references did more than the brief asked:
- **Extra columns:** h02, h03, h04 and h06 return columns the brief never asks for.
- **Settled choices the brief leaves open:**
  - h04: "order size" as net line revenue, where 121 of 125 runs used the order's total price;
  - h06: the year assigned by ship date, where most runs used receipt date;
  - h05: 1993 compared with 1992;
  - h02: a 30-day spree gap that counts single orders as sprees.

v2 returns only what each brief asks and accepts alternatives for those choices. On the same 810 saved runs, graded with extra columns allowed, the terse rung goes from 65 runs passing to 651 (h01 is unchanged at 64).

**Read v2 as an upper bound, not a score.** A review by GPT-6.1 Sol, a different model family, found that a list of accepted answers cannot settle an open definition:
- h02 ("orders close together") has no right threshold.
- h05 and h06 leave several choices open at once (the comparison window, the ranking method, the date that assigns the year, ties at the cut-off).
- The alternatives were chosen partly by looking at what most agents did, which grades toward the agents' answers.

The hard terse rung asks questions an exact-match grader can't score. Use the stakeholder and spec rungs, whose briefs define the terms, to compare agents on the hard set. To make the terse rung gradable, the brief has to state the definition (a new prompt, so new runs).

## Accepted alternatives

A question file, or a level, may list `accepted_sql = ['''...''', ...]`: other queries whose results also count as correct. The grader tries the target first, then each alternative, and takes the first correct match (then the first lenient one); `result.json` records which in `reference` (0 is the target). Use it for representations of the same answer, not for different questions. An alternative belongs to the target it sits beside: a level with its own target does not inherit the file's alternatives.

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
