"""The one guard for agent-supplied SQL. Shared by mcp_server.py (tools) and evalkit (grader).

extensions/duckdb-tools.ts carries a TypeScript copy of this file for pi, between its `sqlguard:begin` and
`sqlguard:end` markers; tests/test_sqlguard.py checks the two give the same answer on the same SQL.

The guard is a courtesy, not the security boundary: the engine is read-only, has external access disabled,
cannot autoload extensions and has its configuration locked (ENGINE_CONFIG below; the pi extension's duckdb
CLI flags do the same). What the guard adds is a clear, early refusal,
and it must never refuse legitimate SQL, because every refusal costs the agent a turn.

  1. Comments are removed, and string literals are blanked for the statement checks, so `replace(...)`,
     `LIKE '%set%'`, a `;` inside a string or a trailing `-- call it done` comment are never mistaken for SQL.
  2. One statement per call, and it must start with a read-only keyword (SELECT, WITH, SHOW, DESCRIBE, ...).
  3. The answer key and file readers are refused anywhere outside comments, string contents included,
     because query('...') can run a string as SQL.
"""

import re

# Every DuckDB connection that runs agent or target SQL. The lock comes last in effect: nothing the agent sends
# can turn external access or extension autoloading back on, or change any other setting.
ENGINE_CONFIG = {
    "enable_external_access": "false",
    "autoinstall_known_extensions": "false",
    "autoload_known_extensions": "false",
    "threads": "1",
    "lock_configuration": "true",
}

# A statement may start with one of these (or a parenthesis). All are read-only in DuckDB.
ALLOWED = (
    "select",
    "with",
    "from",
    "values",
    "table",
    "show",
    "describe",
    "desc",
    "summarize",
    "explain",
    "pivot",
    "unpivot",
)

# Refused anywhere outside comments. ASCII so \b and \w mean what they mean in the TypeScript copy.
BLOCKLIST = [
    re.compile(r"\btpch_?", re.IGNORECASE | re.ASCII),
    re.compile(r"\bdbgen\b", re.IGNORECASE | re.ASCII),
    re.compile(r"\bread_\w*\s*\(", re.IGNORECASE | re.ASCII),
    re.compile(r"\bquery(_table)?\s*\(", re.IGNORECASE | re.ASCII),
    re.compile(r"""(['"])[^'"]*\.(csv|parquet|json|sql|duckdb)\1""", re.IGNORECASE | re.ASCII),  # file paths
]

_LEAD = re.compile(r"\s*(\(|[A-Za-z_]+)")
_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z_0-9]*)?\$")
_IDENT = re.compile(r"[A-Za-z0-9_]")


def scan(sql: str) -> tuple[str, str]:
    """(code, text): both have comments replaced by a space; code also has every string literal reduced to ''.

    Follows DuckDB's lexer: block comments nest, '' escapes a quote, E'...' strings take backslash escapes,
    $$...$$ and $tag$...$tag$ are strings, and "quoted identifiers" stay as code.
    """
    code: list[str] = []
    text: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            code.append(" ")
            text.append(" ")
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            i = j
            code.append(" ")
            text.append(" ")
        elif c in "'\"":
            backslash = c == "'" and i > 0 and sql[i - 1] in "eE" and not (i > 1 and _IDENT.match(sql[i - 2]))
            j = i + 1
            while j < n:
                if (backslash and sql[j] == "\\") or sql.startswith(c + c, j):
                    j += 2
                elif sql[j] == c:
                    break
                else:
                    j += 1
            j = min(j + 1, n)
            code.append(sql[i:j] if c == '"' else "''")
            text.append(sql[i:j])
            i = j
        elif c == "$" and (m := _DOLLAR.match(sql, i)):
            end = sql.find(m.group(0), m.end())
            j = n if end < 0 else end + len(m.group(0))
            code.append("''")
            text.append(sql[i:j])
            i = j
        else:
            code.append(c)
            text.append(c)
            i += 1
    return "".join(code), "".join(text)


def refuse(sql: str) -> str | None:
    """Reason the statement is refused, or None if it may run."""
    code, text = scan(sql)
    for rx in BLOCKLIST:
        if rx.search(text):
            return f"Refused: statement matches {rx.pattern}. Only read-only SELECT/DESCRIBE/SHOW/EXPLAIN is allowed."
    statements = [s for s in code.split(";") if s.strip()]
    if len(statements) > 1:
        return "Refused: one statement per call."
    if statements:
        lead = _LEAD.match(statements[0])
        word = lead.group(1).lower() if lead else ""
        if word != "(" and word not in ALLOWED:
            shown = word.upper() if word else statements[0].strip()[:20]
            return f"Refused: {shown} is not a read-only statement. Only read-only SELECT/DESCRIBE/SHOW/EXPLAIN is allowed."
    return None
