# TPC-H reference queries and validated answers

Extracted from DuckDB 2.0.0's `tpch` extension (`tpch_queries()` / `tpch_answers()`). Answers shown are at scale factor 0.1; the extension also ships SF 0.01 and 1.0.

Why this is the golden set: the queries and answers come from the TPC-H specification and are validated by every database vendor that publishes TPC-H results. Unlike research text-to-SQL benchmarks, the answer key here is not disputed.

## Sources

- DuckDB `tpch` extension docs: https://duckdb.org/docs/2.0/core_extensions/tpch — documents `dbgen`, `tpch_queries()`, `tpch_answers()`
- Extension source code: https://github.com/duckdb/duckdb/tree/main/extension/tpch — the query text and answer files live here
- TPC-H specification (the origin of the 22 queries): https://www.tpc.org/tpch/

Caveat from the DuckDB docs: the generated data has small differences from the official TPC-H spec, and the queries use fixed substitution parameters rather than the spec's randomized ones. For your purposes that's fine — the answers are internally consistent with `dbgen` and verified below — but don't quote these numbers as official TPC-H results.

## Setup

```sql
INSTALL tpch; LOAD tpch;
CALL dbgen(sf=0.1);           -- ~100MB; use sf=1 for ~1GB
SELECT * FROM tpch_queries();  -- the 22 queries
SELECT * FROM tpch_answers();  -- validated results at sf 0.01, 0.1, 1
PRAGMA tpch(1);               -- run query 1
```

## Verification at SF 0.1

Each query below was run live against `dbgen(sf=0.1)` and compared to the shipped answer.

| Q | Rows | Matches shipped answer |
| --- | --- | --- |
| 1 | 4 | yes |
| 2 | 44 | yes |
| 3 | 10 | yes |
| 4 | 5 | yes |
| 5 | 5 | yes |
| 6 | 1 | yes |
| 7 | 4 | yes |
| 8 | 2 | yes |
| 9 | 175 | yes |
| 10 | 20 | yes |
| 11 | 2541 | yes |
| 12 | 2 | yes |
| 13 | 37 | yes |
| 14 | 1 | yes |
| 15 | 1 | yes |
| 16 | 2762 | yes |
| 17 | 1 | yes |
| 18 | 5 | yes |
| 19 | 1 | yes |
| 20 | 9 | yes |
| 21 | 47 | yes |
| 22 | 7 | yes |

## The 22 queries

### Q1 — Pricing summary report: aggregate line items shipped by a date, grouped by return flag and line status.

**Where agents slip:** Aggregates at lineitem grain; no join. Baseline sanity check.

```sql
SELECT
    l_returnflag,
    l_linestatus,
    sum(l_quantity) AS sum_qty,
    sum(l_extendedprice) AS sum_base_price,
    sum(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
    sum(l_extendedprice * (1 - l_discount) * (1 + l_tax)) AS sum_charge,
    avg(l_quantity) AS avg_qty,
    avg(l_extendedprice) AS avg_price,
    avg(l_discount) AS avg_disc,
    count(*) AS count_order
FROM
    lineitem
WHERE
    l_shipdate <= CAST('1998-09-02' AS date)
GROUP BY
    l_returnflag,
    l_linestatus
ORDER BY
    l_returnflag,
    l_linestatus;
```

**Answer (SF 0.1, pipe-delimited):**

```
l_returnflag|l_linestatus|sum_qty|sum_base_price|sum_disc_price|sum_charge|avg_qty|avg_price|avg_disc|count_order
A|F|3774200|5320753880.69|5054096266.6828|5256751331.449234|25.537587116854997|36002.12382901414|0.05014459706340077|147790
N|F|95257|133737795.84|127132372.6512|132286291.229445|25.30066401062417|35521.32691633466|0.04939442231075697|3765
N|O|7459297|10512270008.90|9986238338.3847|10385578376.585467|25.545537671232875|36000.9246880137|0.05009595890410959|292000
R|F|3785523|5337950526.47|5071818532.9420|5274405503.049367|25.5259438574251|35994.029214030925|0.04998927856184382|148301
```

### Q2 — Minimum cost supplier: for a part size/type in a region, find suppliers offering the minimum supply cost.

**Where agents slip:** Correlated subquery for MIN(ps_supplycost). Agents often forget the subquery must share the region filter.

```sql
SELECT
    s_acctbal,
    s_name,
    n_name,
    p_partkey,
    p_mfgr,
    s_address,
    s_phone,
    s_comment
FROM
    part,
    supplier,
    partsupp,
    nation,
    region
WHERE
    p_partkey = ps_partkey
    AND s_suppkey = ps_suppkey
    AND p_size = 15
    AND p_type LIKE '%BRASS'
    AND s_nationkey = n_nationkey
    AND n_regionkey = r_regionkey
    AND r_name = 'EUROPE'
    AND ps_supplycost = (
        SELECT
            min(ps_supplycost)
        FROM
            partsupp,
            supplier,
            nation,
            region
        WHERE
            p_partkey = ps_partkey
            AND s_suppkey = ps_suppkey
            AND s_nationkey = n_nationkey
            AND n_regionkey = r_regionkey
            AND r_name = 'EUROPE')
ORDER BY
    s_acctbal DESC,
    n_name,
    s_name,
    p_partkey
LIMIT 100;
```

**Answer (SF 0.1, pipe-delimited):**

```
s_acctbal|s_name|n_name|p_partkey|p_mfgr|s_address|s_phone|s_comment
9828.21|Supplier#000000647|UNITED KINGDOM|13120|Manufacturer#5|x5U7MBZmwfG9|33-258-202-4782|s the slyly even ideas poach fluffily 
9508.37|Supplier#000000070|FRANCE|3563|Manufacturer#1|INWNH2w,OOWgNDq0BRCcBwOMQc6PdFDc4|16-821-608-1166|ests sleep quickly express ideas. ironic ideas haggle about the final T
9508.37|Supplier#000000070|FRANCE|17268|Manufacturer#4|INWNH2w,OOWgNDq0BRCcBwOMQc6PdFDc4|16-821-608-1166|ests sleep quickly express ideas. ironic ideas haggle about the final T
9453.01|Supplier#000000802|ROMANIA|10021|Manufacturer#5|,6HYXb4uaHITmtMBj4Ak57Pd|29-342-882-6463|gular frets. permanently special multipliers believe blithely alongs
9453.01|Supplier#000000802|ROMANIA|13275|Manufacturer#4|,6HYXb4uaHITmtMBj4Ak57Pd|29-342-882-6463|gular frets. permanently special multipliers believe blithely alongs
9192.10|Supplier#000000115|UNITED KINGDOM|13325|Manufacturer#1|nJ 2t0f7Ve,wL1,6WzGBJLNBUCKlsV|33-597-248-1220|es across the carefully express accounts boost caref
9032.15|Supplier#000000959|GERMANY|4958|Manufacturer#4|8grA EHBnwOZhO|17-108-642-3106|nding dependencies nag furiou
8702.02|Supplier#000000333|RUSSIA|11810|Manufacturer#3|MaVf XgwPdkiX4nfJGOis8Uu2zKiIZH|32-508-202-6136|oss the deposits cajole carefully even pinto beans. regular foxes detect alo
8615.50|Supplier#000000812|FRANCE|10551|Manufacturer#2|8qh4tezyScl5bidLAysvutB,,ZI2dn6xP|16-585-724-6633|y quickly regular deposits? quickly pending packages after the caref
8615.50|Supplier#000000812|FRANCE|13811|Manufacturer#4|8qh4tezyScl5bidLAysvutB,,ZI2dn6xP|16-585-724-6633|y quickly regular deposits? quickly pending packages after the caref
8488.53|Supplier#000000367|RUSSIA|6854|Manufacturer#4|E Sv9brQVf43Mzz|32-458-198-9557|ages. carefully final excuses nag finally. carefully ironic deposits abov
... (33 more rows)
```

### Q3 — Shipping priority: top 10 unshipped orders by revenue for a market segment, ordered before a date.

**Where agents slip:** Three-way join with LIMIT 10; order by revenue desc, then date. Tie handling matters.

```sql
SELECT
    l_orderkey,
    sum(l_extendedprice * (1 - l_discount)) AS revenue,
    o_orderdate,
    o_shippriority
FROM
    customer,
    orders,
    lineitem
WHERE
    c_mktsegment = 'BUILDING'
    AND c_custkey = o_custkey
    AND l_orderkey = o_orderkey
    AND o_orderdate < CAST('1995-03-15' AS date)
    AND l_shipdate > CAST('1995-03-15' AS date)
GROUP BY
    l_orderkey,
    o_orderdate,
    o_shippriority
ORDER BY
    revenue DESC,
    o_orderdate
LIMIT 10;
```

**Answer (SF 0.1, pipe-delimited):**

```
l_orderkey|revenue|o_orderdate|o_shippriority
223140|355369.0698|1995-03-14|0
584291|354494.7318|1995-02-21|0
405063|353125.4577|1995-03-03|0
573861|351238.2770|1995-03-09|0
554757|349181.7426|1995-03-14|0
506021|321075.5810|1995-03-10|0
121604|318576.4154|1995-03-07|0
108514|314967.0754|1995-02-20|0
462502|312604.5420|1995-03-08|0
178727|309728.9306|1995-02-25|0
```

### Q4 — Order priority checking: count orders per priority where at least one line item arrived late.

**Where agents slip:** EXISTS semi-join. A plain JOIN to lineitem fans out and inflates the count.

```sql
SELECT
    o_orderpriority,
    count(*) AS order_count
FROM
    orders
WHERE
    o_orderdate >= CAST('1993-07-01' AS date)
    AND o_orderdate < CAST('1993-10-01' AS date)
    AND EXISTS (
        SELECT
            *
        FROM
            lineitem
        WHERE
            l_orderkey = o_orderkey
            AND l_commitdate < l_receiptdate)
GROUP BY
    o_orderpriority
ORDER BY
    o_orderpriority;
```

**Answer (SF 0.1, pipe-delimited):**

```
o_orderpriority|order_count
1-URGENT|999
2-HIGH|997
3-MEDIUM|1031
4-NOT SPECIFIED|989
5-LOW|1077
```

### Q5 — Local supplier volume: revenue by nation where customer and supplier are in the same nation, within a region and year.

**Where agents slip:** Customer AND supplier must be in the same nation (c_nationkey = s_nationkey). Easy to drop.

```sql
SELECT
    n_name,
    sum(l_extendedprice * (1 - l_discount)) AS revenue
FROM
    customer,
    orders,
    lineitem,
    supplier,
    nation,
    region
WHERE
    c_custkey = o_custkey
    AND l_orderkey = o_orderkey
    AND l_suppkey = s_suppkey
    AND c_nationkey = s_nationkey
    AND s_nationkey = n_nationkey
    AND n_regionkey = r_regionkey
    AND r_name = 'ASIA'
    AND o_orderdate >= CAST('1994-01-01' AS date)
    AND o_orderdate < CAST('1995-01-01' AS date)
GROUP BY
    n_name
ORDER BY
    revenue DESC;
```

**Answer (SF 0.1, pipe-delimited):**

```
n_name|revenue
CHINA|7822103.0000
INDIA|6376121.5085
JAPAN|6000077.2184
INDONESIA|5580475.4027
VIETNAM|4497840.5466
```

### Q6 — Forecasting revenue change: revenue increase if discounts in a range were eliminated for small-quantity items in a year.

**Where agents slip:** Revenue = extendedprice * discount, not extendedprice * (1 - discount). Interval filter is half-open.

```sql
SELECT
    sum(l_extendedprice * l_discount) AS revenue
FROM
    lineitem
WHERE
    l_shipdate >= CAST('1994-01-01' AS date)
    AND l_shipdate < CAST('1995-01-01' AS date)
    AND l_discount BETWEEN 0.05
    AND 0.07
    AND l_quantity < 24;
```

**Answer (SF 0.1, pipe-delimited):**

```
revenue
11803420.2534
```

### Q7 — Volume shipping: revenue between two nations by year, for line items shipped in a two-year window.

**Where agents slip:** Symmetric nation pair (either direction). Agents often only implement one direction.

```sql
SELECT
    supp_nation,
    cust_nation,
    l_year,
    sum(volume) AS revenue
FROM (
    SELECT
        n1.n_name AS supp_nation,
        n2.n_name AS cust_nation,
        extract(year FROM l_shipdate) AS l_year,
        l_extendedprice * (1 - l_discount) AS volume
    FROM
        supplier,
        lineitem,
        orders,
        customer,
        nation n1,
        nation n2
    WHERE
        s_suppkey = l_suppkey
        AND o_orderkey = l_orderkey
        AND c_custkey = o_custkey
        AND s_nationkey = n1.n_nationkey
        AND c_nationkey = n2.n_nationkey
        AND ((n1.n_name = 'FRANCE'
                AND n2.n_name = 'GERMANY')
            OR (n1.n_name = 'GERMANY'
                AND n2.n_name = 'FRANCE'))
        AND l_shipdate BETWEEN CAST('1995-01-01' AS date)
        AND CAST('1996-12-31' AS date)) AS shipping
GROUP BY
    supp_nation,
    cust_nation,
    l_year
ORDER BY
    supp_nation,
    cust_nation,
    l_year;
```

**Answer (SF 0.1, pipe-delimited):**

```
supp_nation|cust_nation|l_year|revenue
FRANCE|GERMANY|1995|4637235.1501
FRANCE|GERMANY|1996|5224779.5736
GERMANY|FRANCE|1995|6232818.7037
GERMANY|FRANCE|1996|5557312.1121
```

### Q8 — National market share: a nation's share of revenue in a region for a part type, by year.

**Where agents slip:** CASE inside SUM for market share; year from o_orderdate, not l_shipdate.

```sql
SELECT
    o_year,
    sum(
        CASE WHEN nation = 'BRAZIL' THEN
            volume
        ELSE
            0
        END) / sum(volume) AS mkt_share
FROM (
    SELECT
        extract(year FROM o_orderdate) AS o_year,
        l_extendedprice * (1 - l_discount) AS volume,
        n2.n_name AS nation
    FROM
        part,
        supplier,
        lineitem,
        orders,
        customer,
        nation n1,
        nation n2,
        region
    WHERE
        p_partkey = l_partkey
        AND s_suppkey = l_suppkey
        AND l_orderkey = o_orderkey
        AND o_custkey = c_custkey
        AND c_nationkey = n1.n_nationkey
        AND n1.n_regionkey = r_regionkey
        AND r_name = 'AMERICA'
        AND s_nationkey = n2.n_nationkey
        AND o_orderdate BETWEEN CAST('1995-01-01' AS date)
        AND CAST('1996-12-31' AS date)
        AND p_type = 'ECONOMY ANODIZED STEEL') AS all_nations
GROUP BY
    o_year
ORDER BY
    o_year;
```

**Answer (SF 0.1, pipe-delimited):**

```
o_year|mkt_share
1995|0.028648741305617557
1996|0.018250279107962147
```

### Q9 — Product type profit measure: profit by nation and year for parts matching a name pattern.

**Where agents slip:** Profit = extendedprice*(1-discount) - supplycost*quantity. Five-table join; part name LIKE pattern.

```sql
SELECT
    nation,
    o_year,
    sum(amount) AS sum_profit
FROM (
    SELECT
        n_name AS nation,
        extract(year FROM o_orderdate) AS o_year,
        l_extendedprice * (1 - l_discount) - ps_supplycost * l_quantity AS amount
    FROM
        part,
        supplier,
        lineitem,
        partsupp,
        orders,
        nation
    WHERE
        s_suppkey = l_suppkey
        AND ps_suppkey = l_suppkey
        AND ps_partkey = l_partkey
        AND p_partkey = l_partkey
        AND o_orderkey = l_orderkey
        AND s_nationkey = n_nationkey
        AND p_name LIKE '%green%') AS profit
GROUP BY
    nation,
    o_year
ORDER BY
    nation,
    o_year DESC;
```

**Answer (SF 0.1, pipe-delimited):**

```
nation|o_year|sum_profit
ALGERIA|1998|2321785.3682
ALGERIA|1997|3685016.8589
ALGERIA|1996|4276597.4253
ALGERIA|1995|4418370.4154
ALGERIA|1994|3864849.9521
ALGERIA|1993|3541051.3865
ALGERIA|1992|4310013.3482
ARGENTINA|1998|2685983.8005
ARGENTINA|1997|4242147.8124
ARGENTINA|1996|3907867.0103
ARGENTINA|1995|4605921.5011
... (164 more rows)
```

### Q10 — Returned item reporting: top 20 customers by revenue lost to returned items in a quarter.

**Where agents slip:** Filter on l_returnflag = 'R' before aggregating; LIMIT 20.

```sql
SELECT
    c_custkey,
    c_name,
    sum(l_extendedprice * (1 - l_discount)) AS revenue,
    c_acctbal,
    n_name,
    c_address,
    c_phone,
    c_comment
FROM
    customer,
    orders,
    lineitem,
    nation
WHERE
    c_custkey = o_custkey
    AND l_orderkey = o_orderkey
    AND o_orderdate >= CAST('1993-10-01' AS date)
    AND o_orderdate < CAST('1994-01-01' AS date)
    AND l_returnflag = 'R'
    AND c_nationkey = n_nationkey
GROUP BY
    c_custkey,
    c_name,
    c_acctbal,
    c_phone,
    n_name,
    c_address,
    c_comment
ORDER BY
    revenue DESC
LIMIT 20;
```

**Answer (SF 0.1, pipe-delimited):**

```
c_custkey|c_name|revenue|c_acctbal|n_name|c_address|c_phone|c_comment
8242|Customer#000008242|622786.7297|6322.09|ETHIOPIA|P2n4nJhy,UqSo2s43YfSvYJDZ6lk|15-792-676-1184|slyly regular packages haggle carefully ironic ideas. courts are furiously. furiously unusual theodolites cajole. i
7714|Customer#000007714|557400.3053|9799.98|IRAN|SnnIGB,SkmnWpX3|20-922-418-6024|arhorses according to the blithely express re
11032|Customer#000011032|512500.9641|8496.93|UNITED KINGDOM|WIKHC7K3Cn7156iNOyfVG3cZ7YqkgsR,Ly|33-102-772-3533|posits-- furiously ironic accounts are again
2455|Customer#000002455|481592.4053|2070.99|GERMANY|RVn1ZSRtLqPlJLIZxvpmsbgC02|17-946-225-9977|al asymptotes. finally ironic accounts cajole furiously. permanently unusual theodolites aro
12106|Customer#000012106|479414.2133|5342.11|UNITED STATES|wth3twOmu6vy|34-905-346-4472|ly after the blithely regular foxes. accounts haggle carefully alongside of the blithely even ideas.
8530|Customer#000008530|457855.9467|9734.95|MOROCCO|GMQyte94oDM7eD7exnkj 4hH9yq3|25-736-932-5850|slyly asymptotes. quickly final deposits in
13984|Customer#000013984|446316.5104|3482.28|IRAN|qZXwuapCHvxbX|20-981-264-2952|y unusual courts could wake furiously
1966|Customer#000001966|444059.0382|1937.72|ALGERIA|jPv1 UHra5JLALR5Isci5u0636RoAu7t vH|10-973-269-8886|the blithely even accounts. final deposits cajole around the blithely final packages. 
11026|Customer#000011026|417913.4142|7738.76|ALGERIA|XorIktoJOAEJkpNNMx|10-184-163-4632|ly even dolphins eat along the blithely even instructions. express attainments cajole slyly. busy dolphins in
8501|Customer#000008501|412797.5100|6906.70|ARGENTINA|776af4rOa mZ66hczs|11-317-552-5840|y final deposits after the fluffily even accounts are slyly final, regular
1565|Customer#000001565|412506.0062|1820.03|BRAZIL|EWQO5Ck,nMuHVQimqL8dLrixRP6QKveXcz9QgorW|12-402-178-2007|ously regular accounts wake slyly ironic idea
... (9 more rows)
```

### Q11 — Important stock identification: parts whose stock value in a nation exceeds a fraction of the total.

**Where agents slip:** HAVING against a scalar subquery with a fraction (0.0001 * total). Nation filter must appear in both.

```sql
SELECT
    ps_partkey,
    sum(ps_supplycost * ps_availqty) AS value
FROM
    partsupp,
    supplier,
    nation
WHERE
    ps_suppkey = s_suppkey
    AND s_nationkey = n_nationkey
    AND n_name = 'GERMANY'
GROUP BY
    ps_partkey
HAVING
    sum(ps_supplycost * ps_availqty) > (
        SELECT
            sum(ps_supplycost * ps_availqty) * 0.0001000000
        FROM
            partsupp,
            supplier,
            nation
        WHERE
            ps_suppkey = s_suppkey
            AND s_nationkey = n_nationkey
            AND n_name = 'GERMANY')
ORDER BY
    value DESC;
```

**Answer (SF 0.1, pipe-delimited):**

```
ps_partkey|value
12098|16227681.21
5134|15709338.52
13334|15023662.41
17052|14351644.20
3452|14070870.14
12552|13332469.18
1084|13170428.29
5797|13038622.72
12633|12892561.61
403|12856217.34
1833|12024581.72
... (2530 more rows)
```

### Q12 — Shipping modes and order priority: count of high/low priority orders by ship mode for late-received items.

**Where agents slip:** Two CASE/SUM columns; three date predicates (commit < receipt, ship < commit, receipt in year).

```sql
SELECT
    l_shipmode,
    sum(
        CASE WHEN o_orderpriority = '1-URGENT'
            OR o_orderpriority = '2-HIGH' THEN
            1
        ELSE
            0
        END) AS high_line_count,
    sum(
        CASE WHEN o_orderpriority <> '1-URGENT'
            AND o_orderpriority <> '2-HIGH' THEN
            1
        ELSE
            0
        END) AS low_line_count
FROM
    orders,
    lineitem
WHERE
    o_orderkey = l_orderkey
    AND l_shipmode IN ('MAIL', 'SHIP')
    AND l_commitdate < l_receiptdate
    AND l_shipdate < l_commitdate
    AND l_receiptdate >= CAST('1994-01-01' AS date)
    AND l_receiptdate < CAST('1995-01-01' AS date)
GROUP BY
    l_shipmode
ORDER BY
    l_shipmode;
```

**Answer (SF 0.1, pipe-delimited):**

```
l_shipmode|high_line_count|low_line_count
MAIL|647|945
SHIP|620|943
```

### Q13 — Customer distribution: histogram of customers by number of orders, excluding certain comment patterns.

**Where agents slip:** LEFT OUTER JOIN with the comment filter in the ON clause, not WHERE. Classic null-vs-inner-join trap.

```sql
SELECT
    c_count,
    count(*) AS custdist
FROM (
    SELECT
        c_custkey,
        count(o_orderkey)
    FROM
        customer
    LEFT OUTER JOIN orders ON c_custkey = o_custkey
    AND o_comment NOT LIKE '%special%requests%'
GROUP BY
    c_custkey) AS c_orders (c_custkey,
        c_count)
GROUP BY
    c_count
ORDER BY
    custdist DESC,
    c_count DESC;
```

**Answer (SF 0.1, pipe-delimited):**

```
c_count|custdist
0|5000
10|665
9|657
11|621
12|567
8|564
13|492
18|482
7|480
20|456
14|456
... (26 more rows)
```

### Q14 — Promotion effect: percentage of revenue from promotional parts in a month.

**Where agents slip:** Percentage uses CASE in numerator; filter on l_shipdate month.

```sql
SELECT
    100.00 * sum(
        CASE WHEN p_type LIKE 'PROMO%' THEN
            l_extendedprice * (1 - l_discount)
        ELSE
            0
        END) / sum(l_extendedprice * (1 - l_discount)) AS promo_revenue
FROM
    lineitem,
    part
WHERE
    l_partkey = p_partkey
    AND l_shipdate >= date '1995-09-01'
    AND l_shipdate < CAST('1995-10-01' AS date);
```

**Answer (SF 0.1, pipe-delimited):**

```
promo_revenue
16.283855689005982
```

### Q15 — Top supplier: supplier(s) with the maximum revenue in a quarter (uses a view/CTE).

**Where agents slip:** Supplier(s) with MAX revenue; the view/CTE must be reused for both the MAX and the final select.

```sql
WITH revenue AS (
    SELECT
        l_suppkey AS supplier_no,
        sum(l_extendedprice * (1 - l_discount)) AS total_revenue
    FROM
        lineitem
    WHERE
        l_shipdate >= CAST('1996-01-01' AS date)
      AND l_shipdate < CAST('1996-04-01' AS date)
    GROUP BY
        supplier_no
)
SELECT
    s_suppkey,
    s_name,
    s_address,
    s_phone,
    total_revenue
FROM
    supplier,
    revenue
WHERE
    s_suppkey = supplier_no
    AND total_revenue = (
        SELECT
            max(total_revenue)
        FROM revenue)
ORDER BY
    s_suppkey;
```

**Answer (SF 0.1, pipe-delimited):**

```
s_suppkey|s_name|s_address|s_phone|total_revenue
677|Supplier#000000677|8mhrffG7D2WJBSQbOGstQ|23-290-639-3315|1614410.2928
```

### Q16 — Parts/supplier relationship: count of suppliers per part brand/type/size, excluding complaint suppliers.

**Where agents slip:** COUNT(DISTINCT ps_suppkey); NOT IN subquery on complaint suppliers; NOT IN size list.

```sql
SELECT
    p_brand,
    p_type,
    p_size,
    count(DISTINCT ps_suppkey) AS supplier_cnt
FROM
    partsupp,
    part
WHERE
    p_partkey = ps_partkey
    AND p_brand <> 'Brand#45'
    AND p_type NOT LIKE 'MEDIUM POLISHED%'
    AND p_size IN (49, 14, 23, 45, 19, 3, 36, 9)
    AND ps_suppkey NOT IN (
        SELECT
            s_suppkey
        FROM
            supplier
        WHERE
            s_comment LIKE '%Customer%Complaints%')
GROUP BY
    p_brand,
    p_type,
    p_size
ORDER BY
    supplier_cnt DESC,
    p_brand,
    p_type,
    p_size;
```

**Answer (SF 0.1, pipe-delimited):**

```
p_brand|p_type|p_size|supplier_cnt
Brand#14|SMALL ANODIZED NICKEL|45|12
Brand#22|SMALL BURNISHED BRASS|19|12
Brand#25|PROMO POLISHED COPPER|14|12
Brand#35|LARGE ANODIZED STEEL|45|12
Brand#35|PROMO BRUSHED COPPER|9|12
Brand#51|ECONOMY ANODIZED STEEL|9|12
Brand#53|LARGE BRUSHED NICKEL|45|12
Brand#11|ECONOMY POLISHED COPPER|14|8
Brand#11|LARGE PLATED STEEL|23|8
Brand#11|PROMO POLISHED STEEL|23|8
Brand#11|STANDARD ANODIZED COPPER|9|8
... (2751 more rows)
```

### Q17 — Small-quantity-order revenue: average yearly revenue lost if small orders for a brand/container weren't taken.

**Where agents slip:** Correlated subquery for 0.2 * AVG(l_quantity) per part. Divide by 7.0 for yearly average.

```sql
SELECT
    sum(l_extendedprice) / 7.0 AS avg_yearly
FROM
    lineitem,
    part
WHERE
    p_partkey = l_partkey
    AND p_brand = 'Brand#23'
    AND p_container = 'MED BOX'
    AND l_quantity < (
        SELECT
            0.2 * avg(l_quantity)
        FROM
            lineitem
        WHERE
            l_partkey = p_partkey);
```

**Answer (SF 0.1, pipe-delimited):**

```
avg_yearly
23512.752857142856
```

### Q18 — Large volume customer: top 100 customers with orders over a total quantity threshold.

**Where agents slip:** GROUP BY with HAVING SUM(l_quantity) > 300 in a subquery; LIMIT 100.

```sql
SELECT
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice,
    sum(l_quantity)
FROM
    customer,
    orders,
    lineitem
WHERE
    o_orderkey IN (
        SELECT
            l_orderkey
        FROM
            lineitem
        GROUP BY
            l_orderkey
        HAVING
            sum(l_quantity) > 300)
    AND c_custkey = o_custkey
    AND o_orderkey = l_orderkey
GROUP BY
    c_name,
    c_custkey,
    o_orderkey,
    o_orderdate,
    o_totalprice
ORDER BY
    o_totalprice DESC,
    o_orderdate
LIMIT 100;
```

**Answer (SF 0.1, pipe-delimited):**

```
c_name|c_custkey|o_orderkey|o_orderdate|o_totalprice|sum
Customer#000001639|1639|502886|1994-04-12|456423.88|312
Customer#000006655|6655|29158|1995-10-21|452805.02|305
Customer#000014110|14110|565574|1995-09-24|425099.85|301
Customer#000001775|1775|6882|1997-04-09|408368.10|303
Customer#000011459|11459|551136|1993-05-19|386812.74|308
```

### Q19 — Discounted revenue: revenue from parts matching brand/container/size/quantity conditions shipped by air.

**Where agents slip:** Three OR-ed condition blocks, each with its own quantity range and size range.

```sql
SELECT
    sum(l_extendedprice * (1 - l_discount)) AS revenue
FROM
    lineitem,
    part
WHERE (p_partkey = l_partkey
    AND p_brand = 'Brand#12'
    AND p_container IN ('SM CASE', 'SM BOX', 'SM PACK', 'SM PKG')
    AND l_quantity >= 1
    AND l_quantity <= 1 + 10
    AND p_size BETWEEN 1 AND 5
    AND l_shipmode IN ('AIR', 'AIR REG')
    AND l_shipinstruct = 'DELIVER IN PERSON')
    OR (p_partkey = l_partkey
        AND p_brand = 'Brand#23'
        AND p_container IN ('MED BAG', 'MED BOX', 'MED PKG', 'MED PACK')
        AND l_quantity >= 10
        AND l_quantity <= 10 + 10
        AND p_size BETWEEN 1 AND 10
        AND l_shipmode IN ('AIR', 'AIR REG')
        AND l_shipinstruct = 'DELIVER IN PERSON')
    OR (p_partkey = l_partkey
        AND p_brand = 'Brand#34'
        AND p_container IN ('LG CASE', 'LG BOX', 'LG PACK', 'LG PKG')
        AND l_quantity >= 20
        AND l_quantity <= 20 + 10
        AND p_size BETWEEN 1 AND 15
        AND l_shipmode IN ('AIR', 'AIR REG')
        AND l_shipinstruct = 'DELIVER IN PERSON');
```

**Answer (SF 0.1, pipe-delimited):**

```
revenue
168597.2860
```

### Q20 — Potential part promotion: suppliers in a nation with excess stock of parts matching a name pattern.

**Where agents slip:** Nested subqueries: 0.5 * SUM(l_quantity) threshold per part/supplier in a year.

```sql
SELECT
    s_name,
    s_address
FROM
    supplier,
    nation
WHERE
    s_suppkey IN (
        SELECT
            ps_suppkey
        FROM
            partsupp
        WHERE
            ps_partkey IN (
                SELECT
                    p_partkey
                FROM
                    part
                WHERE
                    p_name LIKE 'forest%')
                AND ps_availqty > (
                    SELECT
                        0.5 * sum(l_quantity)
                    FROM
                        lineitem
                    WHERE
                        l_partkey = ps_partkey
                        AND l_suppkey = ps_suppkey
                        AND l_shipdate >= CAST('1994-01-01' AS date)
                        AND l_shipdate < CAST('1995-01-01' AS date)))
            AND s_nationkey = n_nationkey
            AND n_name = 'CANADA'
        ORDER BY
            s_name;
```

**Answer (SF 0.1, pipe-delimited):**

```
s_name|s_address
Supplier#000000157|,mEGorBfVIm
Supplier#000000197|YC2Acon6kjY3zj3Fbxs2k4Vdf7X0cd2F
Supplier#000000287|7a9SP7qW5Yku5PvSg
Supplier#000000378|FfbhyCxWvcPrO8ltp9
Supplier#000000530|0qwCMwobKY OcmLyfRXlagA8ukENJv,
Supplier#000000555|TfB,a5bfl3Ah 3Z 74GqnNs6zKVGM
Supplier#000000557|jj0wUYh9K3fG5Jhdhrkuy ,4
Supplier#000000729|pqck2ppy758TQpZCUAjPvlU55K3QjfL7Bi
Supplier#000000935|ij98czM 2KzWe7dDTOxB8sq0UfCdvrX
```

### Q21 — Suppliers who kept orders waiting: suppliers whose late delivery was the only late item in a multi-supplier order.

**Where agents slip:** EXISTS and NOT EXISTS on the same order with different supplier conditions. Hardest to get right.

```sql
SELECT
    s_name,
    count(*) AS numwait
FROM
    supplier,
    lineitem l1,
    orders,
    nation
WHERE
    s_suppkey = l1.l_suppkey
    AND o_orderkey = l1.l_orderkey
    AND o_orderstatus = 'F'
    AND l1.l_receiptdate > l1.l_commitdate
    AND EXISTS (
        SELECT
            *
        FROM
            lineitem l2
        WHERE
            l2.l_orderkey = l1.l_orderkey
            AND l2.l_suppkey <> l1.l_suppkey)
    AND NOT EXISTS (
        SELECT
            *
        FROM
            lineitem l3
        WHERE
            l3.l_orderkey = l1.l_orderkey
            AND l3.l_suppkey <> l1.l_suppkey
            AND l3.l_receiptdate > l3.l_commitdate)
    AND s_nationkey = n_nationkey
    AND n_name = 'SAUDI ARABIA'
GROUP BY
    s_name
ORDER BY
    numwait DESC,
    s_name
LIMIT 100;
```

**Answer (SF 0.1, pipe-delimited):**

```
s_name|numwait
Supplier#000000445|16
Supplier#000000825|16
Supplier#000000709|15
Supplier#000000762|15
Supplier#000000357|14
Supplier#000000399|14
Supplier#000000496|14
Supplier#000000977|13
Supplier#000000144|12
Supplier#000000188|12
Supplier#000000415|12
... (36 more rows)
```

### Q22 — Global sales opportunity: count and balance of customers in given country codes with above-average balance and no orders.

**Where agents slip:** SUBSTRING on phone for country code; scalar subquery for average balance; NOT EXISTS on orders.

```sql
SELECT
    cntrycode,
    count(*) AS numcust,
    sum(c_acctbal) AS totacctbal
FROM (
    SELECT
        substring(c_phone FROM 1 FOR 2) AS cntrycode,
        c_acctbal
    FROM
        customer
    WHERE
        substring(c_phone FROM 1 FOR 2) IN ('13', '31', '23', '29', '30', '18', '17')
        AND c_acctbal > (
            SELECT
                avg(c_acctbal)
            FROM
                customer
            WHERE
                c_acctbal > 0.00
                AND substring(c_phone FROM 1 FOR 2) IN ('13', '31', '23', '29', '30', '18', '17'))
            AND NOT EXISTS (
                SELECT
                    *
                FROM
                    orders
                WHERE
                    o_custkey = c_custkey)) AS custsale
GROUP BY
    cntrycode
ORDER BY
    cntrycode;
```

**Answer (SF 0.1, pipe-delimited):**

```
cntrycode|numcust|totacctbal
13|94|714035.05
17|96|722560.15
18|99|738012.52
23|93|708285.25
29|85|632693.46
30|87|646748.02
31|87|647372.50
```
