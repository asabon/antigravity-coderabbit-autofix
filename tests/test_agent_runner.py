"""Unit tests for src.agent_runner policies, hooks, and response parsing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.antigravity import types
from google.antigravity.hooks.hooks import HookContext

from src.agent_runner import (
    build_policies,
    build_pre_tool_call_hook,
    is_forbidden_command,
    is_test_path,
    matches_glob_patterns,
    parse_agent_outcomes,
    run_agent_fixes,
)
from src.models import ActionConfig, AgentFixResponse, ReviewThread, ThreadFixOutcome


def _make_config(tmp_path: Path, *, protect_tests: bool = True) -> ActionConfig:
    return ActionConfig(
        github_token="ghp_test",
        github_repository="owner/repo",
        pr_number=1,
        antigravity_api_key="test-key",
        protect_tests=protect_tests,
        deny_write_paths=[".github/**", "*.lock", ".env*"],
        workspace=tmp_path,
    )


def test_path_and_command_predicates(tmp_path: Path) -> None:
    assert is_test_path("tests/test_main.py", workspace=tmp_path)
    assert is_test_path("src/__tests__/app.spec.ts", workspace=tmp_path)
    assert is_test_path("pkg/service_test.go", workspace=tmp_path)
    assert not is_test_path("src/main.py", workspace=tmp_path)

    deny = [".github/**", "*.lock", ".env*"]
    assert matches_glob_patterns(".github/workflows/ci.yml", deny, workspace=tmp_path)
    assert matches_glob_patterns("uv.lock", deny, workspace=tmp_path)
    assert matches_glob_patterns(".env.production", deny, workspace=tmp_path)
    assert not matches_glob_patterns("src/github_client.py", deny, workspace=tmp_path)

    assert is_forbidden_command("git push origin main")
    assert is_forbidden_command("rm -rf /tmp/something")
    assert is_forbidden_command("gh pr merge 12 --squash")
    assert not is_forbidden_command("pytest -q")


def test_build_policies_includes_workspace_deny_write_and_command(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    policies = build_policies(config)
    names = [p.name for p in policies]
    assert "deny-write-paths" in names
    assert "deny-destructive-commands" in names

    deny_write_policies = [p for p in policies if p.name == "deny-write-paths"]
    assert deny_write_policies[0].when is not None
    assert deny_write_policies[0].when({"path": ".github/workflows/ci.yml"}) is True
    assert deny_write_policies[0].when({"path": "src/main.py"}) is False

    deny_cmd_policy = next(p for p in policies if p.name == "deny-destructive-commands")
    assert deny_cmd_policy.when is not None
    assert deny_cmd_policy.when({"command": "git push"}) is True
    assert deny_cmd_policy.when({"command": "pytest"}) is False


@pytest.mark.asyncio
async def test_pre_tool_call_hook_blocks_tests_and_denied_paths(tmp_path: Path) -> None:
    config = _make_config(tmp_path, protect_tests=True)
    hook = build_pre_tool_call_hook(config)
    ctx = HookContext()

    # Blocks editing test files when protect_tests=True
    test_edit = types.ToolCall(
        id="1",
        name=types.BuiltinTools.EDIT_FILE.value,
        args={"path": "tests/test_main.py"},
    )
    res = await hook.run(ctx, test_edit)
    assert res.allow is False
    assert "protect-tests" in res.message

    # Blocks writing to denied paths
    env_write = types.ToolCall(
        id="2",
        name=types.BuiltinTools.CREATE_FILE.value,
        args={"path": ".env.local"},
    )
    res_env = await hook.run(ctx, env_write)
    assert res_env.allow is False
    assert "deny-write-paths" in res_env.message

    # Blocks forbidden shell commands
    push_cmd = types.ToolCall(
        id="3",
        name=types.BuiltinTools.RUN_COMMAND.value,
        args={"command": "git push origin feature"},
    )
    res_cmd = await hook.run(ctx, push_cmd)
    assert res_cmd.allow is False

    # Allows normal source file edits
    src_edit = types.ToolCall(
        id="4",
        name=types.BuiltinTools.EDIT_FILE.value,
        args={"path": "src/models.py"},
    )
    res_ok = await hook.run(ctx, src_edit)
    assert res_ok.allow is True


@pytest.mark.asyncio
async def test_pre_tool_call_hook_allows_tests_when_protection_disabled(tmp_path: Path) -> None:
    config = _make_config(tmp_path, protect_tests=False)
    hook = build_pre_tool_call_hook(config)
    ctx = HookContext()

    test_edit = types.ToolCall(
        id="1",
        name=types.BuiltinTools.EDIT_FILE.value,
        args={"path": "tests/test_main.py"},
    )
    res = await hook.run(ctx, test_edit)
    assert res.allow is True


def test_parse_agent_outcomes_variants() -> None:
    threads = [
        ReviewThread(thread_id="T_1", path="a.py", body="Fix A", author="coderabbitai[bot]"),
        ReviewThread(thread_id="T_2", path="b.py", body="Fix B", author="coderabbitai[bot]"),
    ]

    structured = AgentFixResponse(
        outcomes=[ThreadFixOutcome(thread_id="T_1", fixed=True, summary="Fixed A")]
    )
    parsed = parse_agent_outcomes(structured, "", threads)
    assert parsed["T_1"].fixed is True
    assert parsed["T_2"].fixed is False
    assert "did not return" in parsed["T_2"].reason

    dict_payload = {
        "T_1": {"fixed": True, "summary": "Done", "reason": ""},
        "T_2": {"fixed": False, "summary": "", "reason": "Needs human input"},
    }
    parsed_dict = parse_agent_outcomes(dict_payload, "", threads)
    assert parsed_dict["T_1"].fixed is True
    assert parsed_dict["T_2"].reason == "Needs human input"


@pytest.mark.asyncio
async def test_run_agent_fixes_invokes_sdk_agent(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    threads = [
        ReviewThread(thread_id="T_1", path="src/app.py", line=10, body="Fix bug", author="cr")
    ]

    mock_response = MagicMock()
    mock_response.structured_output = AsyncMock(
        return_value=AgentFixResponse(
            outcomes=[ThreadFixOutcome(thread_id="T_1", fixed=True, summary="Fixed bug")]
        )
    )
    mock_agent = MagicMock()
    mock_agent.chat = AsyncMock(return_value=mock_response)
    mock_agent.__aenter__ = AsyncMock(return_value=mock_agent)
    mock_agent.__aexit__ = AsyncMock(return_value=None)

    with patch("src.agent_runner.Agent", return_value=mock_agent) as agent_cls:
        outcomes = await run_agent_fixes(threads, config)

    agent_cls.assert_called_once()
    mock_agent.chat.assert_awaited_once()
    assert outcomes["T_1"].fixed is True
    assert outcomes["T_1"].summary == "Fixed bug"
