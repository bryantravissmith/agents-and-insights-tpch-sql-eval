/**
 * pi extension: read-only DuckDB access for the SQL agent audit.
 *
 * Exposes two tools and nothing else (run pi with --no-builtin-tools):
 *   run_sql(sql)    -> executes read-only, returns up to MAX_ROWS rows as CSV, logs the query
 *   submit_sql(sql) -> saves the final query to answer.sql and runs it (evalkit re-executes it to grade)
 *
 * Both refuse statements that could write, load extensions, or peek at the TPC-H answer key.
 * The guard between the sqlguard markers mirrors ../sqlguard.py (used by mcp_server.py and the grader).
 *
 * Configuration via environment variables (set by evalkit for each run):
 *   AUDIT_DB       path to the .duckdb file            (default <sql-evals>/tpch.duckdb)
 *   AUDIT_OUT      directory for queries.log / answer.* (default <sql-evals>/results/adhoc)
 *   AUDIT_DECLARE  1: submit_sql also takes answer_columns, saved to answer_columns.json
 *   AUDIT_DECLARE_GRAIN  1 (with AUDIT_DECLARE): also grain_columns, saved as {"answer": [...], "grain": [...]}
 *
 * Requires the `duckdb` CLI on PATH (brew install duckdb).
 */
import { Type } from "typebox";
import { execFileSync } from "node:child_process";
import { appendFileSync, mkdirSync, writeFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const MAX_ROWS = 50;
// Defaults resolve relative to this file (sql-evals/extensions/), not the cwd,
// so interactive `pi` sessions started from any directory still find the database.
const HARNESS = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DB = resolve(process.env.AUDIT_DB ?? resolve(HARNESS, "tpch.duckdb"));
const OUT = resolve(process.env.AUDIT_OUT ?? resolve(HARNESS, "results", "adhoc"));
// Batches run with declared answer columns: submit_sql also takes the names of the columns that answer.
const DECLARE = process.env.AUDIT_DECLARE === "1";
// ... and also the grain: the names of the columns that identify one row of the result.
const GRAIN = process.env.AUDIT_DECLARE_GRAIN === "1";

/** The column names in the header line of the duckdb CLI's CSV output (quoted names unquoted). */
function csvHeader(out: string): string[] {
  const header = out.split("\n")[0] ?? "";
  const cols: string[] = [];
  let cur = "";
  let quoted = false;
  for (let i = 0; i < header.length; i++) {
    const ch = header[i];
    if (quoted) {
      if (ch === '"' && header[i + 1] === '"') {
        cur += '"';
        i++;
      } else if (ch === '"') quoted = false;
      else cur += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") {
      cols.push(cur);
      cur = "";
    } else cur += ch;
  }
  cols.push(cur.replace(/\r$/, ""));
  return cols;
}

// sqlguard:begin
// A line-for-line copy of ../sqlguard.py (read its docstring); tests/test_sqlguard.py runs both on the same SQL.
const ALLOWED = [
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
];

const BLOCKLIST = [
  /\btpch_?/i,
  /\bdbgen\b/i,
  /\bread_\w*\s*\(/i,
  /\bquery(_table)?\s*\(/i,
  /(['"])[^'"]*\.(csv|parquet|json|sql|duckdb)\1/i, // file paths
];

const LEAD = /^\s*(\(|[A-Za-z_]+)/;
const DOLLAR = /\$([A-Za-z_][A-Za-z_0-9]*)?\$/y;
const IDENT = /[A-Za-z0-9_]/;

function scan(sql: string): [string, string] {
  const code: string[] = [];
  const text: string[] = [];
  let i = 0;
  const n = sql.length;
  while (i < n) {
    const c = sql[i];
    let m: RegExpExecArray | null = null;
    if (sql.startsWith("--", i)) {
      const j = sql.indexOf("\n", i);
      i = j < 0 ? n : j;
      code.push(" ");
      text.push(" ");
    } else if (sql.startsWith("/*", i)) {
      let depth = 1;
      let j = i + 2;
      while (j < n && depth) {
        if (sql.startsWith("/*", j)) (depth += 1), (j += 2);
        else if (sql.startsWith("*/", j)) (depth -= 1), (j += 2);
        else j += 1;
      }
      i = j;
      code.push(" ");
      text.push(" ");
    } else if (c === "'" || c === '"') {
      const backslash = c === "'" && i > 0 && "eE".includes(sql[i - 1]) && !(i > 1 && IDENT.test(sql[i - 2]));
      let j = i + 1;
      while (j < n) {
        if ((backslash && sql[j] === "\\") || sql.startsWith(c + c, j)) j += 2;
        else if (sql[j] === c) break;
        else j += 1;
      }
      j = Math.min(j + 1, n);
      code.push(c === '"' ? sql.slice(i, j) : "''");
      text.push(sql.slice(i, j));
      i = j;
    } else if (c === "$" && ((DOLLAR.lastIndex = i), (m = DOLLAR.exec(sql)))) {
      const end = sql.indexOf(m[0], DOLLAR.lastIndex);
      const j = end < 0 ? n : end + m[0].length;
      code.push("''");
      text.push(sql.slice(i, j));
      i = j;
    } else {
      code.push(c);
      text.push(c);
      i += 1;
    }
  }
  return [code.join(""), text.join("")];
}

function refuse(sql: string): string | null {
  const [code, text] = scan(sql);
  for (const re of BLOCKLIST) {
    if (re.test(text)) {
      return `Refused: statement matches ${re.source}. Only read-only SELECT/DESCRIBE/SHOW/EXPLAIN is allowed.`;
    }
  }
  const statements = code.split(";").filter((s) => s.trim());
  if (statements.length > 1) return "Refused: one statement per call.";
  if (statements.length) {
    const lead = LEAD.exec(statements[0]);
    const word = lead ? lead[1].toLowerCase() : "";
    if (word !== "(" && !ALLOWED.includes(word)) {
      const shown = word ? word.toUpperCase() : statements[0].trim().slice(0, 20);
      return `Refused: ${shown} is not a read-only statement. Only read-only SELECT/DESCRIBE/SHOW/EXPLAIN is allowed.`;
    }
  }
  return null;
}
// sqlguard:end

function duckdb(sql: string): string {
  if (!existsSync(DB)) throw new Error(`Database not found at ${DB}. Run setup_db.py first.`);
  // Same engine settings as ENGINE_CONFIG in ../sqlguard.py. .safe_mode disables external access and locks the
  // configuration, so the settings it must not see changed come before it and nothing can change after it.
  const args = [
    "-no-init",
    "-readonly",
    "-csv",
    "-cmd",
    "SET threads=1",
    "-cmd",
    "SET autoinstall_known_extensions=false",
    "-cmd",
    "SET autoload_known_extensions=false",
    "-cmd",
    ".safe_mode",
    DB,
    sql,
  ];
  return execFileSync("duckdb", args, {
    encoding: "utf8",
    timeout: 60_000,
    maxBuffer: 16 * 1024 * 1024,
  });
}

function log(kind: string, sql: string, note = "") {
  mkdirSync(OUT, { recursive: true });
  const stamp = new Date().toISOString();
  appendFileSync(resolve(OUT, "queries.log"), `-- ${stamp} ${kind}${note ? " " + note : ""}\n${sql.trim()}\n;\n\n`);
}

export default function (pi: any) {
  pi.registerTool({
    name: "run_sql",
    label: "Run SQL (read-only)",
    description:
      `Execute a single read-only SQL statement against the orders database (DuckDB). ` +
      `Returns up to ${MAX_ROWS} rows as CSV with a header. Use SHOW TABLES and DESCRIBE <table> to explore the schema. ` +
      `Writes, file access, and extension loading are refused.`,
    parameters: Type.Object({
      sql: Type.String({ description: "One SELECT / DESCRIBE / SHOW / EXPLAIN statement." }),
    }),
    async execute(_id: string, params: { sql: string }) {
      const bad = refuse(params.sql);
      if (bad) {
        log("REFUSED", params.sql, bad);
        throw new Error(bad);
      }
      const t0 = Date.now();
      let out: string;
      try {
        out = duckdb(params.sql);
      } catch (e: any) {
        log("ERROR", params.sql, (e.stderr ?? e.message ?? "").toString().trim().slice(0, 300));
        throw new Error(`SQL error: ${(e.stderr ?? e.message ?? "").toString().trim()}`);
      }
      const lines = out.trimEnd().split("\n");
      const total = Math.max(lines.length - 1, 0);
      const shown = lines.slice(0, MAX_ROWS + 1).join("\n");
      log("RUN", params.sql, `rows=${total} ms=${Date.now() - t0}`);
      const note = total > MAX_ROWS ? `\n\n(${total} rows total; showing first ${MAX_ROWS})` : `\n\n(${total} rows)`;
      return { content: [{ type: "text", text: shown + note }], details: { rows: total } };
    },
  });

  pi.registerTool({
    name: "submit_sql",
    label: "Submit final SQL",
    description:
      "Submit your final answer. Saves the query and executes it to confirm it runs. " +
      "Call this exactly once, when you are confident the query is correct. After calling it, summarize your assumptions and stop.",
    parameters:
      DECLARE && GRAIN
        ? Type.Object({
            sql: Type.String({ description: "The final SELECT statement that answers the question." }),
            answer_columns: Type.Array(Type.String(), {
              description: "The names of the result columns that answer the request; other columns are context.",
            }),
            grain_columns: Type.Array(Type.String(), {
              description:
                "The names of the result columns that identify one row of your result (one row per what?); " +
                "an empty list if the result is a single row.",
            }),
          })
        : DECLARE
          ? Type.Object({
              sql: Type.String({ description: "The final SELECT statement that answers the question." }),
              answer_columns: Type.Array(Type.String(), {
                description: "The names of the result columns that answer the request; other columns are context.",
              }),
            })
          : Type.Object({
              sql: Type.String({ description: "The final SELECT statement that answers the question." }),
            }),
    async execute(_id: string, params: { sql: string; answer_columns?: string[]; grain_columns?: string[] }) {
      const bad = refuse(params.sql);
      if (bad) {
        log("REFUSED-SUBMIT", params.sql, bad);
        throw new Error(bad);
      }
      mkdirSync(OUT, { recursive: true });
      writeFileSync(resolve(OUT, "answer.sql"), params.sql.trim() + "\n");
      let out: string;
      try {
        out = duckdb(params.sql);
      } catch (e: any) {
        log("SUBMIT-ERROR", params.sql, (e.stderr ?? e.message ?? "").toString().trim().slice(0, 300));
        throw new Error(`Submitted query failed: ${(e.stderr ?? e.message ?? "").toString().trim()}`);
      }
      const rows = Math.max(out.trimEnd().split("\n").length - 1, 0);
      if (DECLARE) {
        const cols = csvHeader(out);
        const names = new Set(cols.map((c) => c.toLowerCase()));
        const declared = (params.answer_columns ?? []).map((c) => c.trim());
        const unknown = declared.filter((c) => !names.has(c.toLowerCase()));
        if (declared.length === 0 || unknown.length > 0) {
          const note = `answer_columns ${JSON.stringify(unknown.length ? unknown : "empty")} not in the result columns ${JSON.stringify(cols)}`;
          log("SUBMIT-ERROR", params.sql, note);
          throw new Error(`Submission not recorded: ${note}. Name columns of your query's result.`);
        }
        const grain = GRAIN ? (params.grain_columns ?? []).map((c) => c.trim()) : null;
        const badGrain = (grain ?? []).filter((c) => !names.has(c.toLowerCase()));
        if (badGrain.length > 0) {
          const note = `grain_columns ${JSON.stringify(badGrain)} not in the result columns ${JSON.stringify(cols)}`;
          log("SUBMIT-ERROR", params.sql, note);
          throw new Error(`Submission not recorded: ${note}. Name columns of your query's result.`);
        }
        const saved = grain === null ? declared : { answer: declared, grain };
        writeFileSync(resolve(OUT, "answer_columns.json"), JSON.stringify(saved));
        const grainNote = grain === null ? "" : ` grain_columns=${JSON.stringify(grain)}`;
        log("SUBMIT", params.sql, `rows=${rows} answer_columns=${JSON.stringify(declared)}${grainNote}`);
      } else {
        log("SUBMIT", params.sql, `rows=${rows}`);
      }
      return {
        content: [{ type: "text", text: `Submitted. ${rows} rows recorded. Now state your assumptions and stop.` }],
        details: { rows },
      };
    },
  });
}
