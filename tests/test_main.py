"""Integration tests for src.main loop prevention, git workflow, and outputs."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.main import (
    PreflightError,
    count_autofix_attempts,
    run_autofix,
    validate_antigravity_auth,
)
from src.models import ActionConfig, ReviewThread, ThreadFixOutcome


def test_action_config_from_env_reads_event_payload(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text(
        json.dumps({"pull_request": {"number": 77, "head": {"ref": "feat/autofix"}}}),
        encoding="utf-8",
    )
    config = ActionConfig.from_env(
        {
            "GITHUB_TOKEN": "ghp_test",
            "GITHUB_REPOSITORY": "asabon/demo",
            "GITHUB_EVENT_PATH": str(event_file),
            "GITHUB_WORKSPACE": str(tmp_path),
            "INPUT_MAX_ATTEMPTS": "4",
            "INPUT_IGNORE_NITPICKS": "false",
            "INPUT_PROTECT_TESTS": "true",
        }
    )
    assert config.pr_number == 77
    assert config.head_ref == "feat/autofix"
    assert config.max_attempts == 4
    assert config.ignore_nitpicks is False
    assert config.protect_tests is True


def test_validate_antigravity_auth(tmp_path: Path) -> None:
    cfg_no_key = ActionConfig(
        github_token="ghp_test",
        github_repository="owner/repo",
        pr_number=1,
        antigravity_api_key="",
        workspace=tmp_path,
    )
    with pytest.raises(PreflightError):
        validate_antigravity_auth(cfg_no_key, env={}, home_dir=tmp_path)

    # Local .gemini session satisfies auth
    (tmp_path / ".gemini").mkdir()
    validate_antigravity_auth(cfg_no_key, env={}, home_dir=tmp_path)


def test_count_autofix_attempts(tmp_path: Path) -> None:
    msg = "fix: address CodeRabbit review feedback via Antigravity"
    fake_stdout = (
        f"{msg}\x1fdeveloper\n"
        f"chore: manual edit\x1fgithub-actions[bot]\n"
        f"feat: initial\x1fdeveloper\n"
    )
    with patch("src.main._run_subprocess") as mock_sub:
        mock_sub.return_value.returncode = 0
        mock_sub.return_value.stdout = fake_stdout
        assert count_autofix_attempts(tmp_path, msg) == 2


@pytest.mark.asyncio
async def test_run_autofix_stops_at_max_attempts(tmp_path: Path) -> None:
    output_file = tmp_path / "github_output.txt"
    config = ActionConfig(
        github_token="ghp_test",
        github_repository="owner/repo",
        pr_number=10,
        antigravity_api_key="test-key",
        max_attempts=2,
        workspace=tmp_path,
        github_output=output_file,
    )

    with (
        patch("src.main.count_autofix_attempts", return_value=2),
        patch("src.main.GitHubClient") as gh_cls,
        patch("src.main.run_agent_fixes") as mock_agent,
    ):
        gh = gh_cls.return_value
        gh.__aenter__ = AsyncMock(return_value=gh)
        gh.__aexit__ = AsyncMock(return_value=None)
        gh.post_pr_comment = AsyncMock()

        summary = await run_autofix(config)

    assert summary.fixed_count == 0
    assert summary.commit_sha == ""
    gh.post_pr_comment.assert_awaited_once()
    mock_agent.assert_not_called()
    assert "fixed-count=0" in output_file.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_autofix_happy_path_commits_replies_and_resolves(tmp_path: Path) -> None:
    output_file = tmp_path / "github_output.txt"
    config = ActionConfig(
        github_token="ghp_test",
        github_repository="owner/repo",
        pr_number=10,
        antigravity_api_key="test-key",
        test_command="pytest",
        auto_resolve=True,
        workspace=tmp_path,
        github_output=output_file,
    )
    threads = [
        ReviewThread(thread_id="T_1", path="src/a.py", line=5, body="Fix A", author="cr"),
        ReviewThread(thread_id="T_2", path="src/b.py", line=9, body="Fix B", author="cr"),
    ]
    outcomes = {
        "T_1": ThreadFixOutcome(thread_id="T_1", fixed=True, summary="Added null check."),
        "T_2": ThreadFixOutcome(thread_id="T_2", fixed=False, reason="Needs API redesign."),
    }

    with (
        patch("src.main.count_autofix_attempts", return_value=0),
        patch("src.main.run_agent_fixes", AsyncMock(return_value=outcomes)),
        patch("src.main.run_test_suite", return_value=(True, "ok")),
        patch("src.main.has_working_tree_changes", return_value=True),
        patch("src.main.commit_and_push_changes", return_value="abc1234567890"),
        patch("src.main.GitHubClient") as gh_cls,
    ):
        gh = gh_cls.return_value
        gh.__aenter__ = AsyncMock(return_value=gh)
        gh.__aexit__ = AsyncMock(return_value=None)
        gh.fetch_unresolved_coderabbit_threads = AsyncMock(return_value=(threads, 3))
        gh.reply_to_thread = AsyncMock()
        gh.resolve_thread = AsyncMock()

        summary = await run_autofix(config)

    assert summary.fixed_count == 1
    assert summary.skipped_count == 1
    assert summary.remaining_threads == 2
    assert summary.commit_sha == "abc1234567890"
    assert gh.reply_to_thread.await_count == 2
    gh.resolve_thread.assert_awaited_once_with("T_1")

    out_text = output_file.read_text(encoding="utf-8")
    assert "fixed-count=1" in out_text
    assert "skipped-count=1" in out_text
    assert "remaining-threads=2" in out_text
    assert "commit-sha=abc1234567890" in out_text


@pytest.mark.asyncio
async def test_run_autofix_self_repair_and_abort_on_persistent_failure(tmp_path: Path) -> None:
    config = ActionConfig(
        github_token="ghp_test",
        github_repository="owner/repo",
        pr_number=10,
        antigravity_api_key="test-key",
        test_command="pytest",
        workspace=tmp_path,
    )
    threads = [
        ReviewThread(thread_id="T_1", path="src/a.py", line=5, body="Fix A", author="cr"),
    ]
    outcomes = {
        "T_1": ThreadFixOutcome(thread_id="T_1", fixed=True, summary="Attempted fix."),
    }

    with (
        patch("src.main.count_autofix_attempts", return_value=0),
        patch("src.main.run_agent_fixes", AsyncMock(return_value=outcomes)) as mock_agent,
        patch("src.main.run_test_suite", side_effect=[(False, "fail 1"), (False, "fail 2")]),
        patch("src.main.commit_and_push_changes") as mock_commit,
        patch("src.main.GitHubClient") as gh_cls,
    ):
        gh = gh_cls.return_value
        gh.__aenter__ = AsyncMock(return_value=gh)
        gh.__aexit__ = AsyncMock(return_value=None)
        gh.fetch_unresolved_coderabbit_threads = AsyncMock(return_value=(threads, 1))

        with pytest.raises(RuntimeError, match="self-repair pass"):
            await run_autofix(config)

    assert mock_agent.await_count == 2
    mock_commit.assert_not_called()
