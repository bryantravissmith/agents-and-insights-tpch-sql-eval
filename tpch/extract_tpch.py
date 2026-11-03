# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Extract TPC-H reference queries + validated answers from DuckDB's tpch extension.

Writes:
  out/tpch-reference-queries.md   human-readable: each query, what it asks, and its SF=0.1 answer
  out/queries/q01.sql ... q22.sql  raw SQL
  out/answers/sf0.1/q01.csv ...    validated answers (pipe-delimited as DuckDB ships them)
Also runs each query at SF=0.1 and reports whether the live result matches the shipped answer.
"""

import os
import sys

import duckdb

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
SF = 0.1

# Plain-English intent of each TPC-H query (from the TPC-H spec, section 2.4).
INTENT = {
    1: "Pricing summary report: aggregate line items shipped by a date, grouped by return flag and line status.",
    2: "Minimum cost supplier: for a part size/type in a region, find suppliers offering the minimum supply cost.",
    3: "Shipping priority: top 10 unshipped orders by revenue for a market segment, ordered before a date.",
    4: "Order priority checking: count orders per priority where at least one line item arrived late.",
    5: "Local supplier volume: revenue by nation where customer and supplier are in the same nation, within a region and year.",
    6: "Forecasting revenue change: revenue increase if discounts in a range were eliminated for small-quantity items in a year.",
    7: "Volume shipping: revenue between two nations by year, for line items shipped in a two-year window.",
    8: "National market share: a nation's share of revenue in a region for a part type, by year.",
    9: "Product type profit measure: profit by nation and year for parts matching a name pattern.",
    10: "Returned item reporting: top 20 customers by revenue lost to returned items in a quarter.",
    11: "Important stock identification: parts whose stock value in a nation exceeds a fraction of the total.",
    12: "Shipping modes and order priority: count of high/low priority orders by ship mode for late-received items.",
    13: "Customer distribution: histogram of customers by number of orders, excluding certain comment patterns.",
    14: "Promotion effect: percentage of revenue from promotional parts in a month.",
    15: "Top supplier: supplier(s) with the maximum revenue in a quarter (uses a view/CTE).",
    16: "Parts/supplier relationship: count of suppliers per part brand/type/size, excluding complaint suppliers.",
    17: "Small-quantity-order revenue: average yearly revenue lost if small orders for a brand/container weren't taken.",
    18: "Large volume customer: top 100 customers with orders over a total quantity threshold.",
    19: "Discounted revenue: revenue from parts matching brand/container/size/quantity conditions shipped by air.",
    20: "Potential part promotion: suppliers in a nation with excess stock of parts matching a name pattern.",
    21: "Suppliers who kept orders waiting: suppliers whose late delivery was the only late item in a multi-supplier order.",
    22: "Global sales opportunity: count and balance of customers in given country codes with above-average balance and no orders.",
}

# Known-trap notes an analyst would flag when giving these to an agent.
TRAPS = {
    1: "Aggregates at lineitem grain; no join. Baseline sanity check.",
    2: "Correlated subquery for MIN(ps_supplycost). Agents often forget the subquery must share the region filter.",
    3: "Three-way join with LIMIT 10; order by revenue desc, then date. Tie handling matters.",
    4: "EXISTS semi-join. A plain JOIN to lineitem fans out and inflates the count.",
    5: "Customer AND supplier must be in the same nation (c_nationkey = s_nationkey). Easy to drop.",
    6: "Revenue = extendedprice * discount, not extendedprice * (1 - discount). Interval filter is half-open.",
    7: "Symmetric nation pair (either direction). Agents often only implement one direction.",
    8: "CASE inside SUM for market share; year from o_orderdate, not l_shipdate.",
    9: "Profit = extendedprice*(1-discount) - supplycost*quantity. Five-table join; part name LIKE pattern.",
    10: "Filter on l_returnflag = 'R' before aggregating; LIMIT 20.",
    11: "HAVING against a scalar subquery with a fraction (0.0001 * total). Nation filter must appear in both.",
    12: "Two CASE/SUM columns; three date predicates (commit < receipt, ship < commit, receipt in year).",
    13: "LEFT OUTER JOIN with the comment filter in the ON clause, not WHERE. Classic null-vs-inner-join trap.",
    14: "Percentage uses CASE in numerator; filter on l_shipdate month.",
    15: "Supplier(s) with MAX revenue; the view/CTE must be reused for both the MAX and the final select.",
    16: "COUNT(DISTINCT ps_suppkey); NOT IN subquery on complaint suppliers; NOT IN size list.",
    17: "Correlated subquery for 0.2 * AVG(l_quantity) per part. Divide by 7.0 for yearly average.",
    18: "GROUP BY with HAVING SUM(l_quantity) > 300 in a subquery; LIMIT 100.",
    19: "Three OR-ed condition blocks, each with its own quantity range and size range.",
    20: "Nested subqueries: 0.5 * SUM(l_quantity) threshold per part/supplier in a year.",
    21: "EXISTS and NOT EXISTS on the same order with different supplier conditions. Hardest to get right.",
    22: "SUBSTRING on phone for country code; scalar subquery for average balance; NOT EXISTS on orders.",
}


def main():
    os.makedirs(f"{OUT}/queries", exist_ok=True)
    os.makedirs(f"{OUT}/answers/sf{SF}", exist_ok=True)

    con = duckdb.connect()
    con.execute("INSTALL tpch; LOAD tpch;")
    con.execute(f"CALL dbgen(sf={SF});")

    queries = dict(con.execute("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall())
    answers = dict(
        con.execute(
            "SELECT query_nr, answer FROM tpch_answers() WHERE scale_factor = ? ORDER BY query_nr", [SF]
        ).fetchall()
    )

    md = []
    md.append("# TPC-H reference queries and validated answers\n")
    md.append(
        f"Extracted from DuckDB {duckdb.__version__}'s `tpch` extension (`tpch_queries()` / `tpch_answers()`). "
        f"Answers shown are at scale factor {SF}; the extension also ships SF 0.01 and 1.0.\n"
    )
    md.append(
        "Why this is the golden set: the queries and answers come from the TPC-H specification and are validated "
        "by every database vendor that publishes TPC-H results. Unlike research text-to-SQL benchmarks, the answer "
        "key here is not disputed.\n"
    )
    md.append("## Setup\n")
    md.append(
        "```sql\nINSTALL tpch; LOAD tpch;\nCALL dbgen(sf=0.1);           -- ~100MB; use sf=1 for ~1GB\n"
        "SELECT * FROM tpch_queries();  -- the 22 queries\n"
        "SELECT * FROM tpch_answers();  -- validated results at sf 0.01, 0.1, 1\nPRAGMA tpch(1);               -- run query 1\n```\n"
    )
    md.append("## Verification at SF " + str(SF) + "\n")
    md.append("Each query below was run live against `dbgen(sf=0.1)` and compared to the shipped answer.\n")

    results = []
    for n in range(1, 23):
        sql = queries[n].strip()
        with open(f"{OUT}/queries/q{n:02d}.sql", "w") as f:
            f.write(f"-- TPC-H Q{n}: {INTENT[n]}\n{sql}\n")
        with open(f"{OUT}/answers/sf{SF}/q{n:02d}.csv", "w") as f:
            f.write(answers[n])

        # Run live and compare to the shipped answer (DuckDB's own check is PRAGMA tpch(n), but we want a diff).
        live = con.execute(sql).fetchall()
        live_rows = [tuple(_norm(v) for v in row) for row in live]
        exp_rows = _parse_answer(answers[n])
        match = live_rows == exp_rows
        results.append((n, match, len(live_rows), len(exp_rows)))

    md.append("| Q | Rows | Matches shipped answer |\n| --- | --- | --- |")
    for n, match, lr, er in results:
        md.append(f"| {n} | {lr} | {'yes' if match else f'NO (live {lr} rows, expected {er})'} |")
    md.append("")

    md.append("## The 22 queries\n")
    for n in range(1, 23):
        md.append(f"### Q{n} — {INTENT[n]}\n")
        md.append(f"**Where agents slip:** {TRAPS[n]}\n")
        md.append("```sql\n" + queries[n].strip() + "\n```\n")
        ans = answers[n].strip()
        lines = ans.splitlines()
        shown = "\n".join(lines[:12])
        more = f"\n... ({len(lines) - 12} more rows)" if len(lines) > 12 else ""
        md.append(f"**Answer (SF {SF}, pipe-delimited):**\n\n```\n{shown}{more}\n```\n")

    with open(f"{OUT}/tpch-reference-queries.md", "w") as f:
        f.write("\n".join(md))

    ok = sum(1 for _, m, _, _ in results if m)
    print(f"{ok}/22 queries match shipped answers at SF {SF}")
    for n, m, lr, er in results:
        if not m:
            print(f"  Q{n}: live {lr} rows vs expected {er}")


def _norm(v):
    """Normalize a live cell for comparison: numbers -> rounded float, dates -> ISO, else str."""
    import datetime
    import decimal

    if v is None:
        return "NULL"
    if isinstance(v, (int, float, decimal.Decimal)):
        return round(float(v), 4)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.strftime("%Y-%m-%d")
    return _norm_answer_cell(str(v))  # numeric-looking strings compare as numbers on both sides


def _parse_answer(text):
    # Split on '|' directly: fields may contain quotes/commas, and csv quoting would mangle them.
    lines = text.strip().splitlines()[1:]  # drop header
    return [tuple(_norm_answer_cell(c) for c in line.split("|")) for line in lines]


def _norm_answer_cell(c):
    c = c.strip()
    if c == "NULL":
        return "NULL"
    try:
        return round(float(c), 4)
    except ValueError:
        return c


if __name__ == "__main__":
    main()
