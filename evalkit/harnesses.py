"""One adapter per agent CLI: how to lock it down to the two SQL tools, launch it headless, and read its transcript.

Every harness gets the same prompt, the same database and the same run_sql / submit_sql tools (pi through
extensions/duckdb-tools.ts, everyone else through mcp_server.py). Built-in file, shell and web tools are
removed or denied, so a difference between harnesses is the model plus that CLI's system prompt and loop.

Condition notes (conditions/<name>/AGENTS.md) reach the agent the way each CLI expects:
system-prompt append for claude and pi, AGENTS.md in the working directory for codex, grok and agy.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent.parent
MCP_SERVER = HARNESS_DIR / "mcp_server.py"
PI_EXTENSION = HARNESS_DIR / "extensions" / "duckdb-tools.ts"
TOOLS = ("run_sql", "submit_sql")


@dataclass
class RunContext:
    prompt: str
    model: str
    work_dir: Path  # agent cwd: scratch dir outside the repo, holds only config + notes
    run_dir: Path  # inside results/: transcript, queries.log, answer.sql
    db: Path
    notes: Path | None  # AGENTS.md for the condition, already copied into work_dir
    max_turns: int
    timeout_s: int
    extra_args: tuple[str, ...] = ()

    @property
    def tool_env(self) -> dict[str, str]:
        return {"AUDIT_DB": str(self.db), "AUDIT_OUT": str(self.run_dir)}

    def mcp_server(self) -> dict:
        return {"command": "uv", "args": ["run", "--quiet", str(MCP_SERVER)], "env": self.tool_env}


@dataclass
class Usage:
    final_text: str = ""
    turns: int | None = None
    tokens_in: int = 0  # uncached input
    tokens_out: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    cost_usd: float | None = None  # only when the CLI reports it
    error: str = ""  # CLI-reported failure (auth, API error, denied tool)
    extra: dict = field(default_factory=dict)


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else ""


def _jsonl(path: Path):
    for line in _read(path).splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _json_obj(path: Path) -> dict | None:
    """A single JSON object, or the last {"type": "result"} line of a JSONL stream."""
    text = _read(path).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    last = None
    for ev in _jsonl(path):
        last = ev if ev.get("type") == "result" else last
    return last


class Harness:
    name = ""
    binary = ""

    def prepare(self, ctx: RunContext) -> None:
        """Write any config files the CLI needs into ctx.work_dir."""

    def command(self, ctx: RunContext) -> list[str]:
        raise NotImplementedError

    def env(self, ctx: RunContext) -> dict[str, str]:
        return {**os.environ, **ctx.tool_env}

    def parse(self, ctx: RunContext, stdout: Path) -> Usage:
        raise NotImplementedError

    def available(self) -> bool:
        return shutil.which(self.binary) is not None


class Pi(Harness):
    """pi (pi.dev). Model ids are provider/model, e.g. openai/gpt-5.6-luna."""

    name = binary = "pi"

    def command(self, ctx):
        cmd = [
            "pi",
            "-p",
            "--mode",
            "json",
            "--no-session",
            "--no-builtin-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-mcp",
            "--no-approve",
            "-e",
            str(PI_EXTENSION),
            "--model",
            ctx.model,
        ]
        if ctx.notes:
            cmd += ["--append-system-prompt", str(ctx.notes)]
        return cmd + list(ctx.extra_args) + ["--", ctx.prompt]

    def parse(self, ctx, stdout):
        u = Usage(turns=0, cost_usd=0.0)
        for ev in _jsonl(stdout):
            if ev.get("type") != "message_end":
                continue
            m = ev.get("message", {})
            if m.get("role") != "assistant":
                continue
            us = m.get("usage") or {}
            u.turns += 1
            u.tokens_in += us.get("input", 0) or 0
            u.tokens_out += us.get("output", 0) or 0
            u.tokens_cache_read += us.get("cacheRead", 0) or 0
            u.tokens_cache_write += us.get("cacheWrite", 0) or 0
            u.cost_usd += (us.get("cost") or {}).get("total", 0) or 0
            text = "".join(
                c.get("text", "") for c in m.get("content", []) if isinstance(c, dict) and c.get("type") == "text"
            )
            if text.strip():
                u.final_text = text
            if m.get("stopReason") == "error" or m.get("errorMessage"):
                u.error = str(m.get("errorMessage") or "error")[:300]
        return u


class Claude(Harness):
    """Claude Code. --bare (no CLAUDE.md, hooks, plugins) needs ANTHROPIC_API_KEY; with a subscription login
    it falls back to --setting-sources "" which skips user/project/local settings instead."""

    name = binary = "claude"

    def prepare(self, ctx):
        (ctx.work_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"audit": ctx.mcp_server()}}, indent=2))

    def command(self, ctx):
        isolation = ["--bare"] if os.environ.get("ANTHROPIC_API_KEY") else ["--setting-sources", ""]
        cmd = [
            "claude",
            "-p",
            ctx.prompt,
            *isolation,
            "--model",
            ctx.model,
            "--output-format",
            "json",
            "--tools",
            "",  # no built-in tools at all
            "--mcp-config",
            str(ctx.work_dir / "mcp.json"),
            "--strict-mcp-config",
            "--allowedTools",
            ",".join(f"mcp__audit__{t}" for t in TOOLS),
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--max-turns",
            str(ctx.max_turns),
        ]
        if ctx.notes:
            cmd += ["--append-system-prompt-file", str(ctx.notes)]
        return cmd + list(ctx.extra_args)

    def env(self, ctx):
        # A nested claude started from inside a Claude Code session must not inherit that session's identity.
        return {k: v for k, v in super().env(ctx).items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}

    def parse(self, ctx, stdout):
        obj = _json_obj(stdout) or {}
        us = obj.get("usage") or {}
        return Usage(
            final_text=obj.get("result") or "",
            turns=obj.get("num_turns"),
            tokens_in=us.get("input_tokens", 0) or 0,
            tokens_out=us.get("output_tokens", 0) or 0,
            tokens_cache_read=us.get("cache_read_input_tokens", 0) or 0,
            tokens_cache_write=us.get("cache_creation_input_tokens", 0) or 0,
            cost_usd=obj.get("total_cost_usd"),
            error=(obj.get("result") or "error")[:300] if obj.get("is_error") else "",
        )


class Codex(Harness):
    """Codex CLI. Shell, browser, apps and web search are disabled; the read-only sandbox covers apply_patch.
    ~/.codex/config.toml is ignored (--ignore-user-config) so local defaults do not leak into the comparison."""

    name = binary = "codex"
    DISABLE = (
        "shell_tool",
        "unified_exec",
        "browser_use",
        "in_app_browser",
        "computer_use",
        "image_generation",
        "apps",
        "plugins",
        "view_image",
        "tool_suggest",
        "skill_search",
        "goals",
        "sleep_tool",
        "multi_agent",
    )

    def command(self, ctx):
        srv = ctx.mcp_server()
        env_toml = "{" + ",".join(f"{k}={json.dumps(v)}" for k, v in srv["env"].items()) + "}"
        cmd = [
            "codex",
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "-m",
            ctx.model,
            "--sandbox",
            "read-only",
            "-C",
            str(ctx.work_dir),
            "-o",
            str(ctx.run_dir / "final_message.txt"),
        ]
        for feat in self.DISABLE:
            cmd += ["--disable", feat]
        cmd += [
            "-c",
            'web_search="disabled"',
            "-c",
            f"mcp_servers.audit.command={json.dumps(srv['command'])}",
            "-c",
            f"mcp_servers.audit.args={json.dumps(srv['args'])}",
            "-c",
            f"mcp_servers.audit.env={env_toml}",
            "-c",
            'mcp_servers.audit.default_tools_approval_mode="approve"',
        ]
        return cmd + list(ctx.extra_args) + ["--", ctx.prompt]

    def parse(self, ctx, stdout):
        u = Usage(final_text=_read(ctx.run_dir / "final_message.txt"), turns=0)
        last_message = ""
        for ev in _jsonl(stdout):
            t = ev.get("type")
            if t == "turn.completed":
                us = ev.get("usage") or {}
                cached = us.get("cached_input_tokens", 0) or 0
                u.tokens_in += (us.get("input_tokens", 0) or 0) - cached
                u.tokens_cache_read += cached
                u.tokens_cache_write += us.get("cache_write_input_tokens", 0) or 0
                u.tokens_out += us.get("output_tokens", 0) or 0
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message":
                    u.turns += 1
                    last_message = item.get("text", "")
                if item.get("type") == "mcp_tool_call" and item.get("error"):
                    msg = str((item["error"] or {}).get("message", ""))
                    if "approval" in msg:
                        u.error = msg[:300]
            elif t in ("turn.failed", "error"):
                u.error = json.dumps(ev.get("error") or ev.get("message") or ev)[:300]
        u.final_text = u.final_text or last_message
        return u


class Grok(Harness):
    """Grok CLI with built-ins removed and only the isolated SQL MCP server allowed."""

    name = binary = "grok"

    def prepare(self, ctx):
        (ctx.work_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"audit": ctx.mcp_server()}}, indent=2))

    def command(self, ctx):
        return [
            "grok",
            "-p",
            ctx.prompt,
            "-m",
            ctx.model,
            "--output-format",
            "streaming-messages-json",
            "--cwd",
            str(ctx.work_dir),
            "--tools",
            "run_terminal_cmd",
            "--disallowed-tools",
            "run_terminal_cmd,Agent",
            "--trust",
            "--no-plan",
            "--no-subagents",
            "--disable-web-search",
            "--permission-mode",
            "bypassPermissions",
            "--allow",
            "MCPTool(audit__run_sql)",
            "--allow",
            "MCPTool(audit__submit_sql)",
            "--deny",
            "Read",
            "--deny",
            "Bash",
            "--deny",
            "Write",
            "--deny",
            "Edit",
            "--deny",
            "Grep",
            "--deny",
            "WebFetch",
            "--max-turns",
            str(ctx.max_turns),
            *ctx.extra_args,
        ]

    def env(self, ctx):
        env = super().env(ctx)
        for vendor in ("CLAUDE", "CURSOR"):
            for feature in ("AGENTS", "HOOKS", "MCPS", "RULES", "SKILLS"):
                env[f"GROK_{vendor}_{feature}_ENABLED"] = "false"
        env["GROK_MEMORY"] = "false"
        env["GROK_DISABLE_AUTOUPDATER"] = "1"
        return env

    def parse(self, ctx, stdout):
        obj = _json_obj(stdout) or {}
        us = obj.get("usage") or {}
        return Usage(
            final_text=obj.get("text") or obj.get("result") or "",
            turns=obj.get("num_turns"),
            tokens_in=us.get("input_tokens", 0) or 0,
            tokens_out=us.get("output_tokens", 0) or 0,
            tokens_cache_read=us.get("cache_read_input_tokens", 0) or 0,
            tokens_cache_write=us.get("cache_creation_input_tokens", 0) or 0,
            cost_usd=None if obj.get("cost_is_partial") else obj.get("total_cost_usd"),
            error=(obj.get("message") or obj.get("result") or "error")[:300]
            if obj.get("type") == "error" or obj.get("is_error")
            else "",
            extra={k: obj[k] for k in ("modelUsage", "cost_is_partial", "usage_is_incomplete") if k in obj},
        )


class Antigravity(Harness):
    """Antigravity CLI (agy). It has no per-run MCP flag, so each work dir carries a workspace plugin
    (.agents/plugins/audit/) whose server agy names audit_audit. Permissions come from a dedicated agy
    project, ~/.gemini/config/projects/aai-evals.json: allow the two tools, deny shell, file, web."""

    name = "agy"
    binary = "agy"
    PROJECT = "aai-evals"
    SERVER = "audit_audit"  # <plugin>_<server>

    def project_file(self) -> Path:
        return Path.home() / ".gemini" / "config" / "projects" / f"{self.PROJECT}.json"

    def ensure_project(self):
        spec = {
            "id": self.PROJECT,
            "name": self.PROJECT,
            "projectResources": {},
            "permissionGrants": {
                "v2Migrated": True,
                "permissionGrants": {
                    "allow": [f"mcp({self.SERVER}/{t})" for t in TOOLS],
                    "deny": [
                        "command(*)",
                        "unsandboxed(*)",
                        "write_file(*)",
                        "read_file(*)",
                        "execute_url(*)",
                        "read_url(*)",
                        "search_web(*)",
                    ],
                },
            },
        }
        path = self.project_file()
        text = json.dumps(spec)
        if _read(path) != text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(0o600)

    def prepare(self, ctx):
        self.ensure_project()
        plugin = ctx.work_dir / ".agents" / "plugins" / "audit"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(
            json.dumps({"name": "audit", "description": "Read-only SQL tools for the orders database"})
        )
        (plugin / "mcp_config.json").write_text(json.dumps({"mcpServers": {"audit": ctx.mcp_server()}}, indent=2))

    def command(self, ctx):
        return [
            "agy",
            "-p",
            ctx.prompt,
            "--model",
            ctx.model,
            "--output-format",
            "json",
            "--project",
            self.PROJECT,
            "--sandbox",
            "--disable-slash-commands",
            "--print-timeout",
            f"{ctx.timeout_s}s",
            *ctx.extra_args,
        ]

    def parse(self, ctx, stdout):
        obj = _json_obj(stdout) or {}
        us = obj.get("usage") or {}
        denied = [d.get("action", "") + ":" + d.get("display_name", "") for d in obj.get("denied_actions") or []]
        # input + output == total in every observed run. Cache reads are usually a subset of input (the CLI's
        # documented example; Gemini 3.8 Flash), but Gemini 4 Argon reports more cache reads than input, so
        # there they are separate. Subtract only when the cache fits inside input; raw counts stay in extra.
        # agy reports num_turns = 1 for every run, so turns are not comparable; use queries_run for effort.
        raw_in, cache = us.get("input_tokens", 0) or 0, us.get("cache_read_tokens", 0) or 0
        u = Usage(
            final_text=obj.get("response") or "",
            turns=None,
            tokens_in=raw_in - cache if cache <= raw_in else raw_in,
            tokens_out=us.get("output_tokens", 0) or 0,
            tokens_cache_read=cache,
            extra={
                "denied_actions": denied,
                "thinking_tokens_in_output": us.get("thinking_tokens", 0),
                "reported_input_tokens": raw_in,
                "reported_total_tokens": us.get("total_tokens"),
                "reported_num_turns": obj.get("num_turns"),
            },
        )
        if obj and obj.get("status") != "SUCCESS":
            u.error = f"status={obj.get('status')}: {obj.get('error') or ''}".rstrip(": ")[:300]
        return u


HARNESSES: dict[str, Harness] = {h.name: h for h in (Pi(), Claude(), Codex(), Grok(), Antigravity())}


def get(name: str) -> Harness:
    try:
        return HARNESSES[name]
    except KeyError:
        raise ValueError(f"unknown harness {name!r}; one of {sorted(HARNESSES)}") from None


SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def sql_from_text(text: str) -> str | None:
    """Fallback when the agent never called submit_sql: the last ```sql block of its final message."""
    blocks = SQL_BLOCK.findall(text or "")
    return blocks[-1].strip() if blocks else None
