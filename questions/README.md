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

Six questions written for this eval on 2026-10-31, on the same database with the same shared preamble, so no model can have memorized them before that date. They are public from that date: treat later runs as post-exposure and record the run date. Each target was checked against an independently written second query (they agree exactly), and each file's `note` names the trap and how far off the tempting answer is.

| Id | Business question | Trap |
| --- | --- | --- |
| n01 | Order value of orders containing Brand#13 parts, by year | Joining to line items fans out and double-counts order value |
| n02 | European customers with no 1997 orders, by nation | A date filter in WHERE turns the outer join inner; never-ordered customers count |
| n03 | Top customer per region by 1996 net revenue | Region via the customer's nation; revenue after discount |
| n04 | Monthly 1997 order counts with change from the prior month | January compares with December 1996, not NULL |
| n05 | Average 1995 orders per customer, by segment | Customers with zero orders count as zero (1.50, not 2.64) |
| n06 | 1994 first-order cohort returning within 90 days, by segment | "First order" means first ever; the window is per customer |

Run them alongside the TPC-H ladder (`uv run evals.py run questions/tpch questions/tpch-novel ...`). The gap between memorized and novel questions is itself a result.
