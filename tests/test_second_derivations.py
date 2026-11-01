"""Each novel, hard and grain question's target agrees with an independently written second query.

The second queries take a different route on purpose (other join shapes, correlated subqueries, list
functions, calendar arithmetic), so a shared mistake is less likely. They run on the real tpch.duckdb.
"""

from pathlib import Path

import pytest

from evalkit import load_questions
from evalkit.grading import grade_sql
from evalkit.runner import DB

HERE = Path(__file__).resolve().parent.parent

SECOND = {
    "h01": """
WITH months AS (SELECT DISTINCT o_custkey c, date_trunc('month', o_orderdate)::DATE m FROM orders),
flag AS (
  SELECT c, m, CASE WHEN lag(m) OVER (PARTITION BY c ORDER BY m) = m - INTERVAL 1 MONTH THEN 0 ELSE 1 END AS brk FROM months),
isl AS (SELECT c, sum(brk) OVER (PARTITION BY c ORDER BY m ROWS UNBOUNDED PRECEDING) AS k FROM flag),
runs AS (SELECT c, k, count(*) n FROM isl GROUP BY 1, 2),
mx AS (SELECT c, max(n) n FROM runs GROUP BY 1)
SELECT n, count(*) FROM mx GROUP BY 1 ORDER BY 1
""",
    "h02": """
WITH o AS (
  SELECT o_custkey c, o_orderdate d, o_orderkey k FROM orders WHERE year(o_orderdate) IN (1996, 1997)),
gaps AS (
  SELECT c, d, k, date_diff('day', lag(d) OVER (PARTITION BY c ORDER BY d, k), d) gap FROM o),
starts AS (SELECT c, d, k FROM gaps WHERE gap IS NULL OR gap > 30),
sized AS (
  SELECT s.c, s.d, s.k,
    (SELECT count(*) FROM o WHERE o.c = s.c AND (o.d, o.k) >= (s.d, s.k)
       AND (o.d, o.k) < coalesce((SELECT min((s2.d, s2.k)) FROM starts s2 WHERE s2.c = s.c AND (s2.d, s2.k) > (s.d, s.k)), (DATE '9999-01-01', 0))) n
  FROM starts s)
SELECT cu.c_mktsegment, count(*), sum(CASE WHEN n >= 3 THEN 1 ELSE 0 END), sum(n) / count(*)
FROM sized JOIN customer cu ON cu.c_custkey = sized.c GROUP BY 1 ORDER BY 1
""",
    "h03": """
WITH rev AS (
  SELECT n.n_name nat, c.c_custkey ck, sum(l.l_extendedprice - l.l_extendedprice * l.l_discount) v
  FROM nation n, customer c, orders o, lineitem l
  WHERE c.c_nationkey = n.n_nationkey AND o.o_custkey = c.c_custkey AND l.l_orderkey = o.o_orderkey
    AND year(o.o_orderdate) = 1995 GROUP BY 1, 2),
tot AS (SELECT nat, sum(v) t, count(*) n FROM rev GROUP BY 1),
ranked AS (
  SELECT a.nat, a.ck, a.v,
    (SELECT sum(b.v) FROM rev b WHERE b.nat = a.nat AND (b.v > a.v OR (b.v = a.v AND b.ck <= a.ck))) cum,
    (SELECT count(*) FROM rev b WHERE b.nat = a.nat AND (b.v > a.v OR (b.v = a.v AND b.ck <= a.ck))) k
  FROM rev a)
SELECT r.nat, t.n, min(r.k), arg_min(r.cum, r.k) / t.t
FROM ranked r JOIN tot t USING (nat) WHERE r.cum >= t.t / 2 GROUP BY r.nat, t.n, t.t ORDER BY 1
""",
    "h04": """
WITH bad AS (SELECT DISTINCT l_orderkey FROM lineitem WHERE l_returnflag = 'R'),
ord AS (
  SELECT r.r_name reg, o.o_orderkey, sum(l.l_extendedprice * (1 - l.l_discount))::DOUBLE v
  FROM region r JOIN nation n ON n.n_regionkey = r.r_regionkey JOIN customer c ON c.c_nationkey = n.n_nationkey
  JOIN orders o ON o.o_custkey = c.c_custkey JOIN lineitem l ON l.l_orderkey = o.o_orderkey
  WHERE year(o.o_orderdate) = 1994 AND o.o_orderkey NOT IN (SELECT l_orderkey FROM bad)
  GROUP BY 1, 2),
lst AS (SELECT reg, list_sort(list(v)) xs, count(*) n FROM ord GROUP BY 1)
SELECT reg, n,
  xs[CAST(floor(0.9 * (n + 1)) AS BIGINT)]
  + (0.9 * (n + 1) - floor(0.9 * (n + 1))) * (xs[CAST(floor(0.9 * (n + 1)) AS BIGINT) + 1] - xs[CAST(floor(0.9 * (n + 1)) AS BIGINT)])
FROM lst ORDER BY 1
""",
    "h05": """
WITH w AS (SELECT generate_series AS y FROM generate_series(1992, 1998)),
a AS (
  SELECT r.r_name reg, w.y, count(DISTINCT o.o_custkey) n
  FROM w JOIN orders o ON o.o_orderdate BETWEEN make_date(w.y, 1, 1) AND make_date(w.y, 8, 2)
  JOIN customer c ON c.c_custkey = o.o_custkey JOIN nation n ON n.n_nationkey = c.c_nationkey
  JOIN region r ON r.r_regionkey = n.n_regionkey GROUP BY 1, 2)
SELECT cur.reg, cur.y, cur.n, cur.n - prv.n FROM a cur JOIN a prv ON prv.reg = cur.reg AND prv.y = cur.y - 1
ORDER BY 1, 2
""",
    "h06": """
WITH li AS (
  SELECT l_suppkey sk, year(l_shipdate) y, CASE WHEN l_receiptdate <= l_commitdate THEN 1.0 ELSE 0.0 END ok
  FROM lineitem WHERE year(l_shipdate) IN (1995, 1996)),
rate AS (SELECT sk, y, sum(ok) / count(*) r FROM li GROUP BY 1, 2),
rk AS (
  SELECT a.sk, a.y, 1 + (SELECT count(*) FROM rate b JOIN supplier sb ON sb.s_suppkey = b.sk
                         WHERE b.y = a.y AND sb.s_nationkey = sa.s_nationkey AND b.r > a.r) rnk
  FROM rate a JOIN supplier sa ON sa.s_suppkey = a.sk)
SELECT s.s_name, n.n_name, x.rnk, z.rnk, x.rnk - z.rnk
FROM rk x JOIN rk z ON z.sk = x.sk AND x.y = 1995 AND z.y = 1996
JOIN supplier s ON s.s_suppkey = x.sk JOIN nation n ON n.n_nationkey = s.s_nationkey
ORDER BY 5 DESC, x.sk LIMIT 5
""",
    "n01": """
WITH o AS (SELECT DISTINCT l_orderkey FROM lineitem, part WHERE p_partkey = l_partkey AND p_brand = 'Brand#13')
SELECT extract(year FROM o_orderdate)::BIGINT, count(o_orderkey), sum(o_totalprice)
FROM orders JOIN o ON o.l_orderkey = o_orderkey
WHERE o_orderdate BETWEEN DATE '1993-01-01' AND DATE '1997-12-31'
GROUP BY 1 ORDER BY 1
""",
    "n02": """
SELECT n.n_name, count(*) FILTER (WHERE x.c IS NULL)
FROM customer c JOIN nation n ON n.n_nationkey = c.c_nationkey
JOIN region r ON r.r_regionkey = n.n_regionkey AND r.r_name = 'EUROPE'
LEFT JOIN (SELECT o_custkey, count(*) c FROM orders WHERE year(o_orderdate) = 1997 GROUP BY 1) x ON x.o_custkey = c.c_custkey
GROUP BY 1 ORDER BY 1
""",
    "n03": """
WITH rev AS (
  SELECT r.r_name, c.c_name, sum(l.l_extendedprice - l.l_extendedprice * l.l_discount) revenue
  FROM region r, nation n, customer c, orders o, lineitem l
  WHERE n.n_regionkey = r.r_regionkey AND c.c_nationkey = n.n_nationkey AND o.o_custkey = c.c_custkey
    AND l.l_orderkey = o.o_orderkey AND year(o.o_orderdate) = 1996
  GROUP BY 1, 2), m AS (SELECT r_name, max(revenue) mx FROM rev GROUP BY 1)
SELECT rev.r_name, c_name, revenue FROM rev JOIN m ON m.r_name = rev.r_name AND revenue = mx ORDER BY 1
""",
    "n04": """
WITH m AS (SELECT year(o_orderdate) * 12 + month(o_orderdate) k, count(*) n FROM orders GROUP BY 1)
SELECT printf('%04d-%02d', (a.k - 1) // 12, (a.k - 1) % 12 + 1), a.n, a.n - b.n
FROM m a JOIN m b ON b.k = a.k - 1 WHERE (a.k - 1) // 12 = 1997 ORDER BY a.k
""",
    "n05": """
WITH n AS (
  SELECT c_custkey, c_mktsegment,
         (SELECT count(*) FROM orders WHERE o_custkey = c_custkey AND year(o_orderdate) = 1995) AS k
  FROM customer)
SELECT c_mktsegment, avg(k) FROM n GROUP BY 1 ORDER BY 1
""",
    "n06": """
WITH r AS (
  SELECT o_custkey, o_orderdate d, row_number() OVER (PARTITION BY o_custkey ORDER BY o_orderdate, o_orderkey) rn,
         min(o_orderdate) OVER (PARTITION BY o_custkey) f FROM orders),
c AS (SELECT o_custkey, f FROM r WHERE rn = 1 AND year(f) = 1994),
back AS (SELECT DISTINCT r.o_custkey FROM r JOIN c USING (o_custkey) WHERE r.d > c.f AND r.d <= c.f + 90)
SELECT cu.c_mktsegment, count(*), count(back.o_custkey)
FROM c JOIN customer cu ON cu.c_custkey = c.o_custkey LEFT JOIN back ON back.o_custkey = c.o_custkey
GROUP BY 1 ORDER BY 1
""",
    "g01": """
WITH q AS (SELECT DISTINCT l_orderkey FROM lineitem, part WHERE p_partkey = l_partkey AND p_brand = 'Brand#21')
SELECT o_orderpriority, sum(o_totalprice) / count(*) FROM orders JOIN q ON q.l_orderkey = o_orderkey
WHERE year(o_orderdate) = 1996 GROUP BY 1 ORDER BY 1
""",
    "g02": """
SELECT r.r_name, sum(o.o_totalprice) / count(DISTINCT o.o_custkey)
FROM orders o, customer c, nation n, region r
WHERE c.c_custkey = o.o_custkey AND n.n_nationkey = c.c_nationkey AND r.r_regionkey = n.n_regionkey
  AND year(o.o_orderdate) = 1997
GROUP BY 1 ORDER BY 1
""",
    "g03": """
SELECT n.n_name, count(*) FROM customer c JOIN nation n ON n.n_nationkey = c.c_nationkey
WHERE EXISTS (SELECT 1 FROM orders o JOIN lineitem l ON l.l_orderkey = o.o_orderkey JOIN part p ON p.p_partkey = l.l_partkey
              WHERE o.o_custkey = c.c_custkey AND p.p_brand = 'Brand#45' AND year(o.o_orderdate) = 1996)
GROUP BY 1 ORDER BY 1
""",
    "g04": """
WITH o AS (SELECT o_orderkey k, o_custkey c, max(CASE WHEN l_returnflag = 'R' THEN 1 ELSE 0 END) ret
  FROM orders JOIN lineitem ON l_orderkey = o_orderkey WHERE year(o_orderdate) = 1994 GROUP BY 1, 2)
SELECT r_name, avg(ret) FROM o JOIN customer ON c_custkey = o.c JOIN nation ON n_nationkey = c_nationkey
JOIN region ON r_regionkey = n_regionkey GROUP BY 1 ORDER BY 1
""",
    "g05": """
SELECT dayname(d), count(o_orderkey) / count(DISTINCT d)
FROM (SELECT o_orderdate d, o_orderkey FROM orders WHERE year(o_orderdate) = 1997) GROUP BY 1, isodow(d) ORDER BY isodow(d)
""",
    "g06": """
WITH sold AS (SELECT DISTINCT l_partkey FROM lineitem, orders WHERE o_orderkey = l_orderkey AND year(o_orderdate) = 1995)
SELECT sum(ps.ps_availqty) FROM partsupp ps
WHERE ps.ps_suppkey IN (SELECT s_suppkey FROM supplier, nation WHERE n_nationkey = s_nationkey AND n_name = 'GERMANY')
  AND ps.ps_partkey IN (SELECT l_partkey FROM sold)
""",
}


@pytest.mark.skipif(not DB.exists(), reason="needs tpch.duckdb (uv run setup_db.py)")
@pytest.mark.parametrize("qid", sorted(SECOND))
def test_target_matches_second_derivation(qid):
    folder = {"n": "tpch-novel", "h": "tpch-hard", "g": "tpch-grain"}[qid[0]]
    (task,) = load_questions(HERE / "questions" / folder / f"{qid}.toml", levels=["spec"])
    g = grade_sql(SECOND[qid], task.target_sql, DB)
    assert g.correct, f"{qid}: {g.verdict} {g.detail}"
