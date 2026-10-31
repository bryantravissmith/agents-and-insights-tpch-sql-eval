# /// script
# requires-python = ">=3.11"
# dependencies = ["duckdb"]
# ///
"""Build tpch.duckdb at SF 0.1 for the agent to query.

The agent never sees this script or the tpch extension; it gets the resulting file only.
Usage: python3 setup_db.py [sf]      (default 0.1)
"""
import os
import sys

import duckdb

sf = float(sys.argv[1]) if len(sys.argv) > 1 else 0.1
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tpch.duckdb")
if os.path.exists(path):
    os.remove(path)

con = duckdb.connect(path)
con.execute("INSTALL tpch; LOAD tpch;")
con.execute(f"CALL dbgen(sf={sf});")
tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
for t in tables:
    n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"{t:10s} {n:>10,}")
con.close()
print(f"\nWrote {path} (sf={sf})")
