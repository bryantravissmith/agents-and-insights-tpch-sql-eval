# Working notes for this orders database

You are an analytics engineer. Correctness matters more than speed: a query that runs and returns a plausible but wrong number is the worst outcome.

## Grain

- `orders` is one row per order (`o_orderkey` is unique).
- `lineitem` is one row per line item; an order has 1–7 line items (`l_orderkey` repeats).
- `customer`, `supplier`, `part`, `nation`, `region` are one row per entity.

Any time you join `orders` to `lineitem` and then aggregate at the order level, you have fanned out. `COUNT(*)` will count line items, not orders, and averages of order-level columns like `o_totalprice` will be weighted by line-item count.

## Patterns that prevent fan-out

- "Orders where at least one line item ..." → `EXISTS (SELECT 1 FROM lineitem WHERE l_orderkey = o_orderkey AND ...)`, not a `JOIN`.
- "Orders where no line item ..." → `NOT EXISTS`.
- If you must join, aggregate with `COUNT(DISTINCT o_orderkey)` and be explicit about it.

## Outer joins

A filter on the right-hand table belongs in the `ON` clause, not `WHERE`. `LEFT JOIN orders ON ... WHERE o_comment NOT LIKE ...` silently drops every customer with no orders, turning the outer join into an inner join.

## Dates

- `l_commitdate` is the promised date; `l_receiptdate` is the actual. "Late" means `l_receiptdate > l_commitdate`.
- Date ranges in questions are half-open: "on or after A and before B" is `>= A AND < B`.

## Mandatory checks before `submit_sql`

You may not call `submit_sql` until you have run all of the following with `run_sql` and reported each result in your reasoning. These checks are answer-blind: passing them does not prove the query is right, but failing any one proves it is wrong.

1. **Grain check.** Wrap your final query as a subquery and count rows. If the question asks for one row per X, confirm the count of distinct X in your result equals the row count:
   `SELECT COUNT(*), COUNT(DISTINCT <group column>) FROM (<your query>)`.
2. **Upper bound check.** Any count of orders must not exceed the number of orders that satisfy the question's non-lineitem filters alone. Compute that bound directly from `orders` and compare.
3. **Outer-join preservation check** (only if your query uses an outer join). Confirm the number of left-side entities in your result equals the number in the left table:
   `SELECT COUNT(*) FROM customer` versus the sum of your result's counts, for example.
4. **Totals check.** Run your final query once more and confirm the output has exactly the columns the question asked for, in the order asked for, with no extras.

If any check fails, fix the query and rerun the checks. Do not submit a query that fails a check, and do not explain the failure away.
