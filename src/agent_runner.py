"""Antigravity SDK wrapper with declarative Policy and PreToolCallDecide hooks."""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig, types
from google.antigravity.hooks import policy, pre_tool_call_decide
from google.antigravity.hooks.hooks import HookResult, PreToolCallDecideHook

from src.models import ActionConfig, AgentFixResponse, ReviewThread, ThreadFixOutcome

WRITE_TOOL_NAMES = frozenset(
    {
        types.BuiltinTools.EDIT_FILE.value,
        types.BuiltinTools.CREATE_FILE.value,
        "write_file",
    }
)

SHELL_TOOL_NAMES = frozenset({types.BuiltinTools.RUN_COMMAND.value})

_TEST_PATH_PATTERNS = (
    "*test*",
    "tests/**",
    "__tests__/**",
)

_FORBIDDEN_COMMAND_PATTERNS = (
    re.compile(r"\bgit\s+push\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[^\s]*r[^\s]*f\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[^\s]*f[^\s]*r\b", re.IGNORECASE),
    re.compile(r"\bgh\s+pr\s+merge\b", re.IGNORECASE),
)

_SYSTEM_INSTRUCTIONS = """\
You are an expert software engineer resolving CodeRabbit pull request review comments.

Rules:
- Inspect the referenced files and surrounding context before making edits.
- Apply minimal, accurate, idiomatic fixes that directly address each review comment.
- Do NOT modify test files if test protection is enabled, and do NOT modify denied paths.
- Do NOT run destructive or network-mutating commands (such as git push, rm -rf, or gh pr merge).
- If a review comment is ambiguous, requires architectural changes, or cannot be safely automated,
  leave it unchanged and mark `fixed: false` with a clear `reason`.
- Return structured JSON matching the requested schema with an outcome for every provided thread_id.
"""


def _normalize_rel_path(raw_path: str, workspace: Path | None = None) -> str:
    """Normalize a file path to a forward-slash relative path for glob matching."""
    cleaned = raw_path.strip().replace("\\", "/")
    if workspace is not None:
        try:
            rel = Path(raw_path).resolve().relative_to(workspace.resolve())
            cleaned = rel.as_posix()
        except (ValueError, OSError):
            pass
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def matches_glob_patterns(
    path: str,
    patterns: list[str] | tuple[str, ...],
    *,
    workspace: Path | None = None,
) -> bool:
    """Return True if path matches any glob pattern."""
    rel = _normalize_rel_path(path, workspace)
    posix = PurePosixPath(rel)
    for pattern in patterns:
        pat = pattern.strip().replace("\\", "/")
        if not pat:
            continue
        if pat.endswith("/**"):
            prefix = pat[:-3].rstrip("/")
            if rel == prefix or rel.startswith(f"{prefix}/"):
                return True
        if fnmatch(rel, pat) or fnmatch(posix.name, pat):
            return True
    return False


def is_test_path(path: str, *, workspace: Path | None = None) -> bool:
    """Return True if path targets a test file or test directory."""
    rel = _normalize_rel_path(path, workspace)
    if not rel:
        return False
    parts = PurePosixPath(rel).parts
    if any(part in {"tests", "__tests__"} for part in parts):
        return True
    return matches_glob_patterns(rel, _TEST_PATH_PATTERNS)


def is_forbidden_command(command: str) -> bool:
    """Return True if shell command matches destructive or network-mutating patterns."""
    return any(pattern.search(command) for pattern in _FORBIDDEN_COMMAND_PATTERNS)


def _extract_target_path(args: dict[str, Any]) -> str:
    for key in ("path", "file_path", "target_file", "Path", "TargetFile"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_command(args: dict[str, Any]) -> str:
    for key in ("command", "command_line", "CommandLine", "cmd"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def build_policies(config: ActionConfig) -> list[policy.Policy]:
    """Build declarative Antigravity policies for workspace, write, and command restrictions."""
    policies: list[policy.Policy] = list(policy.workspace_only([config.workspace]))

    if config.deny_write_paths:

        def _is_denied_write(args: dict[str, Any]) -> bool:
            target = _extract_target_path(args)
            if not target:
                return False
            return matches_glob_patterns(
                target,
                config.deny_write_paths,
                workspace=config.workspace,
            )

        for tool_name in (
            types.BuiltinTools.CREATE_FILE.value,
            types.BuiltinTools.EDIT_FILE.value,
        ):
            policies.append(
                policy.deny(
                    tool_name,
                    when=_is_denied_write,
                    name="deny-write-paths",
                    reason="Target path matches deny-write-paths policy.",
                )
            )

    def _is_denied_command(args: dict[str, Any]) -> bool:
        cmd = _extract_command(args)
        return bool(cmd and is_forbidden_command(cmd))

    policies.append(
        policy.deny(
            types.BuiltinTools.RUN_COMMAND.value,
            when=_is_denied_command,
            name="deny-destructive-commands",
            reason="Destructive or network-mutating shell commands are forbidden.",
        )
    )
    return policies


def build_pre_tool_call_hook(config: ActionConfig) -> PreToolCallDecideHook:
    """Create a @pre_tool_call_decide hook enforcing test protection and guardrails."""

    @pre_tool_call_decide
    def guard_tool_call(tool_call: types.ToolCall) -> HookResult:
        tool_name = tool_call.name
        args = tool_call.args if isinstance(tool_call.args, dict) else {}

        if tool_name in WRITE_TOOL_NAMES:
            target = _extract_target_path(args)
            if target and matches_glob_patterns(
                target,
                config.deny_write_paths,
                workspace=config.workspace,
            ):
                return HookResult(
                    allow=False,
                    message=f"Blocked write to '{target}': matches deny-write-paths.",
                )
            if config.protect_tests and target and is_test_path(target, workspace=config.workspace):
                return HookResult(
                    allow=False,
                    message=f"Blocked write to '{target}': protect-tests is enabled.",
                )

        if tool_name in SHELL_TOOL_NAMES:
            cmd = _extract_command(args)
            if cmd and is_forbidden_command(cmd):
                return HookResult(
                    allow=False,
                    message="Blocked forbidden shell command.",
                )

        return HookResult(allow=True)

    return guard_tool_call


def build_fix_prompt(
    threads: list[ReviewThread],
    *,
    repair_feedback: str | None = None,
) -> str:
    """Format unresolved CodeRabbit review threads into a structured agent prompt."""
    thread_blocks = []
    for idx, thread in enumerate(threads, start=1):
        line_info = str(thread.line) if thread.line is not None else "N/A"
        thread_blocks.append(
            f"### Thread {idx}\n"
            f"- thread_id: `{thread.thread_id}`\n"
            f"- path: `{thread.path}`\n"
            f"- line: {line_info}\n"
            f"- diff_hunk:\n```diff\n{thread.diff_hunk}\n```\n"
            f"- comment:\n{thread.body}\n"
        )

    prompt = (
        "Please resolve the following unresolved CodeRabbit review threads in the repository.\n\n"
        + "\n".join(thread_blocks)
    )
    if repair_feedback:
        prompt += (
            "\n\n## Test Failure Feedback (Self-Repair Pass)\n"
            "Your previous edits caused the test suite to fail. Fix the failing tests without "
            "modifying existing test files:\n"
            f"```\n{repair_feedback}\n```\n"
        )
    return prompt


def parse_agent_outcomes(
    structured: Any,
    raw_text: str,
    threads: list[ReviewThread],
) -> dict[str, ThreadFixOutcome]:
    """Normalize structured output or raw JSON text into a map of thread_id -> ThreadFixOutcome."""
    outcomes: dict[str, ThreadFixOutcome] = {}

    if isinstance(structured, AgentFixResponse):
        for item in structured.outcomes:
            outcomes[item.thread_id] = item
    elif isinstance(structured, dict):
        if "outcomes" in structured and isinstance(structured["outcomes"], list):
            for item in structured["outcomes"]:
                parsed = ThreadFixOutcome.model_validate(item)
                outcomes[parsed.thread_id] = parsed
        else:
            for tid, val in structured.items():
                if isinstance(val, dict):
                    outcomes[tid] = ThreadFixOutcome(
                        thread_id=tid,
                        fixed=bool(val.get("fixed", False)),
                        summary=str(val.get("summary", "")),
                        reason=str(val.get("reason", "")),
                    )
    elif raw_text.strip():
        try:
            data = json.loads(raw_text)
            return parse_agent_outcomes(data, "", threads)
        except json.JSONDecodeError:
            pass

    for thread in threads:
        if thread.thread_id not in outcomes:
            outcomes[thread.thread_id] = ThreadFixOutcome(
                thread_id=thread.thread_id,
                fixed=False,
                summary="",
                reason="Agent did not return an outcome for this thread.",
            )
    return outcomes


async def run_agent_fixes(
    threads: list[ReviewThread],
    config: ActionConfig,
    *,
    repair_feedback: str | None = None,
) -> dict[str, ThreadFixOutcome]:
    """Run the Antigravity Agent to resolve the given CodeRabbit review threads."""
    agent_config = LocalAgentConfig(
        system_instructions=_SYSTEM_INSTRUCTIONS,
        capabilities=CapabilitiesConfig(
            agent_behavior=types.AgentBehavior.AUTONOMOUS,
            enable_subagents=False,
        ),
        policies=build_policies(config),
        hooks=[build_pre_tool_call_hook(config)],
        workspaces=[str(config.workspace)],
        response_schema=AgentFixResponse,
        api_key=config.antigravity_api_key or None,
    )

    prompt = build_fix_prompt(threads, repair_feedback=repair_feedback)
    async with Agent(agent_config) as agent:
        response = await agent.chat(prompt)
        structured = await response.structured_output()
        raw_text = "" if structured is not None else await response.text()

    return parse_agent_outcomes(structured, raw_text, threads)
