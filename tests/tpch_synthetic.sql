-- A small synthetic TPC-H database for the tests: the eight tables with the real column names and types,
-- a few hundred rows, built with plain SQL (no tpch extension, no download), so the suite runs anywhere.
--
-- Values come from modular arithmetic on the keys, so the data is identical on every DuckDB version.
-- The domains are narrow on purpose: the grader tests use the q01, q04, q08, q14 and q17 targets, and each
-- of those must return non-trivial rows here (late orders in 1993 Q3, Brazilian suppliers selling
-- ECONOMY ANODIZED STEEL in America in 1995-96, PROMO parts shipped in September 1995, small Brand#23
-- MED BOX orders). The numbers mean nothing; the 22-target self-match tier still needs the real tpch.duckdb.

CREATE TABLE region AS
SELECT r_regionkey::INTEGER AS r_regionkey, r_name, r_comment FROM (VALUES
    (0, 'AFRICA', 'synthetic'), (1, 'AMERICA', 'synthetic'), (2, 'ASIA', 'synthetic'),
    (3, 'EUROPE', 'synthetic'), (4, 'MIDDLE EAST', 'synthetic')
) t(r_regionkey, r_name, r_comment);

CREATE TABLE nation AS
SELECT n_nationkey::INTEGER AS n_nationkey, n_name, n_regionkey::INTEGER AS n_regionkey, 'synthetic' AS n_comment
FROM (VALUES
    (0, 'ALGERIA', 0), (1, 'ARGENTINA', 1), (2, 'BRAZIL', 1), (3, 'CANADA', 1), (4, 'EGYPT', 4),
    (5, 'ETHIOPIA', 0), (6, 'FRANCE', 3), (7, 'GERMANY', 3), (8, 'INDIA', 2), (9, 'INDONESIA', 2),
    (10, 'IRAN', 4), (11, 'IRAQ', 4), (12, 'JAPAN', 2), (13, 'JORDAN', 4), (14, 'KENYA', 0),
    (15, 'MOROCCO', 0), (16, 'MOZAMBIQUE', 0), (17, 'PERU', 1), (18, 'CHINA', 2), (19, 'ROMANIA', 3),
    (20, 'SAUDI ARABIA', 4), (21, 'VIETNAM', 2), (22, 'RUSSIA', 3), (23, 'UNITED KINGDOM', 3), (24, 'UNITED STATES', 1)
) t(n_nationkey, n_name, n_regionkey);

-- 20 suppliers; every fourth is Brazilian, the rest cycle through the other nations.
CREATE TABLE supplier AS
SELECT
    k::BIGINT AS s_suppkey,
    'Supplier#' || lpad(k::VARCHAR, 9, '0') AS s_name,
    'address ' || k AS s_address,
    (CASE WHEN k % 4 = 0 THEN 2 ELSE (k * 7) % 25 END)::INTEGER AS s_nationkey,
    '10-' || lpad(k::VARCHAR, 3, '0') || '-555-0100' AS s_phone,
    (((k * 3571) % 1100000) / 100.0 - 999.99)::DECIMAL(15, 2) AS s_acctbal,
    CASE WHEN k = 7 THEN 'Customer Complaints noted' ELSE 'synthetic' END AS s_comment
FROM range(1, 21) r(k);

-- 60 customers; half of them in America.
CREATE TABLE customer AS
SELECT
    k::BIGINT AS c_custkey,
    'Customer#' || lpad(k::VARCHAR, 9, '0') AS c_name,
    'address ' || k AS c_address,
    (CASE WHEN k % 2 = 0 THEN [1, 2, 3, 17, 24][1 + (k // 2) % 5] ELSE (k * 11) % 25 END)::INTEGER AS c_nationkey,
    lpad((10 + (k % 25))::VARCHAR, 2, '0') || '-' || lpad(k::VARCHAR, 3, '0') || '-555-0100' AS c_phone,
    (((k * 7919) % 1100000) / 100.0 - 999.99)::DECIMAL(15, 2) AS c_acctbal,
    ['AUTOMOBILE', 'BUILDING', 'FURNITURE', 'MACHINERY', 'HOUSEHOLD'][1 + k % 5] AS c_mktsegment,
    'synthetic' AS c_comment
FROM range(1, 61) r(k);

-- 40 parts. Brand#23 + MED BOX on every twelfth part.
CREATE TABLE part AS
SELECT
    k::BIGINT AS p_partkey,
    ['forest green', 'lavender blush', 'azure navy', 'ivory khaki', 'misty rose'][1 + k % 5]
        || ' ' || ['almond', 'chocolate', 'linen', 'peach', 'steel', 'wheat'][1 + k % 6] AS p_name,
    'Manufacturer#' || (1 + k % 5) AS p_mfgr,
    ['Brand#23', 'Brand#12', 'Brand#34'][1 + k % 3] AS p_brand,
    ['ECONOMY ANODIZED STEEL', 'PROMO BRUSHED COPPER', 'STANDARD POLISHED BRASS',
     'PROMO PLATED TIN', 'LARGE BRUSHED BRASS', 'MEDIUM POLISHED STEEL'][1 + k % 6] AS p_type,
    (1 + (k * 7) % 50)::INTEGER AS p_size,
    ['MED BOX', 'SM CASE', 'LG PACK', 'JUMBO JAR'][1 + k % 4] AS p_container,
    (900 + k * 1.5)::DECIMAL(15, 2) AS p_retailprice,
    'synthetic' AS p_comment
FROM range(1, 41) r(k);

-- Two suppliers per part.
CREATE TABLE partsupp AS
SELECT
    p::BIGINT AS ps_partkey,
    (1 + (p * 3 + i * 10) % 20)::BIGINT AS ps_suppkey,
    (1 + (p * 37 + i * 101) % 9999)::BIGINT AS ps_availqty,
    (1 + ((p * 113 + i * 17) % 100000) / 100.0)::DECIMAL(15, 2) AS ps_supplycost,
    'synthetic' AS ps_comment
FROM range(1, 41) a(p), range(0, 2) b(i);

-- 150 orders: a quarter in 1993 Q3, a quarter in 1995-96, the rest spread over 1992-1998.
CREATE TABLE orders AS
SELECT
    k::BIGINT AS o_orderkey,
    (1 + (k * 7) % 60)::BIGINT AS o_custkey,
    ['F', 'O', 'P'][1 + k % 3] AS o_orderstatus,
    (1000 + (k * 4567) % 400000 / 100.0)::DECIMAL(15, 2) AS o_totalprice,
    CASE k % 4
        WHEN 0 THEN DATE '1993-07-01' + ((k * 7) % 92)::INTEGER
        WHEN 1 THEN DATE '1995-01-01' + ((k * 13) % 730)::INTEGER
        ELSE DATE '1992-01-01' + ((k * 17) % 2400)::INTEGER
    END AS o_orderdate,
    ['1-URGENT', '2-HIGH', '3-MEDIUM', '4-NOT SPECIFIED', '5-LOW'][1 + (k * 3 + k // 4) % 5] AS o_orderpriority,
    'Clerk#' || lpad((1 + k % 10)::VARCHAR, 9, '0') AS o_clerk,
    0::INTEGER AS o_shippriority,
    'synthetic' AS o_comment
FROM range(1, 151) r(k);

-- One to four line items per order.
CREATE TABLE lineitem AS
SELECT
    o_orderkey AS l_orderkey,
    (1 + (o_orderkey * 7 + n * 13) % 40)::BIGINT AS l_partkey,
    (1 + (o_orderkey * 3 + n * 5) % 20)::BIGINT AS l_suppkey,
    n::BIGINT AS l_linenumber,
    (1 + (o_orderkey * 11 + n * 17) % 50)::DECIMAL(15, 2) AS l_quantity,
    ((1 + (o_orderkey * 11 + n * 17) % 50) * (900.17 + (1 + (o_orderkey * 7 + n * 13) % 40) * 1.5))::DECIMAL(15, 2)
        AS l_extendedprice,
    (((o_orderkey + n) % 11) / 100.0)::DECIMAL(15, 2) AS l_discount,
    (((o_orderkey * n) % 9) / 100.0)::DECIMAL(15, 2) AS l_tax,
    ['R', 'A', 'N'][1 + (o_orderkey + n) % 3] AS l_returnflag,
    ['O', 'F'][1 + (o_orderkey * n) % 2] AS l_linestatus,
    o_orderdate + (1 + (o_orderkey * n * 7) % 120)::INTEGER AS l_shipdate,
    o_orderdate + (30 + (o_orderkey + n * 3) % 60)::INTEGER AS l_commitdate,
    o_orderdate + (1 + (o_orderkey * n * 7) % 120)::INTEGER + (1 + (o_orderkey + n) % 30)::INTEGER AS l_receiptdate,
    ['DELIVER IN PERSON', 'COLLECT COD', 'NONE', 'TAKE BACK RETURN'][1 + (o_orderkey + n) % 4] AS l_shipinstruct,
    ['AIR', 'AIR REG', 'MAIL', 'SHIP', 'TRUCK', 'RAIL', 'FOB'][1 + (o_orderkey * 3 + n) % 7] AS l_shipmode,
    'synthetic' AS l_comment
FROM orders, range(1, 5) r(n)
WHERE n <= 1 + o_orderkey % 4;
