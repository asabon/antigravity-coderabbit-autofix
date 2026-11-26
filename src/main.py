"""Entrypoint for Antigravity CodeRabbit Autofix GitHub Action."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from src.agent_runner import run_agent_fixes
from src.github_client import GitHubClient
from src.models import ActionConfig, AutofixSummary, ReviewThread, ThreadFixOutcome

BOT_GIT_NAME = "github-actions[bot]"
BOT_GIT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class PreflightError(RuntimeError):
    """Raised when required environment variables or Antigravity auth are missing."""


def validate_antigravity_auth(
    config: ActionConfig,
    env: dict[str, str] | None = None,
    home_dir: Path | None = None,
) -> None:
    """Verify that at least one Antigravity authentication mechanism is available."""
    environ = env if env is not None else dict(os.environ)
    if config.antigravity_api_key:
        return
    if environ.get("ANTIGRAVITY_API_KEY", "").strip() or environ.get("GEMINI_API_KEY", "").strip():
        return
    adc_path = environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if adc_path and Path(adc_path).is_file():
        return

    home = home_dir if home_dir is not None else Path.home()
    local_auth_dirs = (
        home / ".gemini",
        home / ".config" / "antigravity",
    )
    if any(path.exists() for path in local_auth_dirs):
        return

    raise PreflightError(
        "No Antigravity authentication detected. Provide 'antigravity-api-key' "
        "(ANTIGRAVITY_API_KEY / GEMINI_API_KEY), configure GCP ADC "
        "(GOOGLE_APPLICATION_CREDENTIALS), or authenticate Antigravity locally."
    )


def _run_subprocess(
    args: list[str] | str,
    *,
    cwd: Path,
    shell: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        shell=shell,
        text=True,
        capture_output=True,
        check=False,
    )


def count_autofix_attempts(workspace: Path, commit_message: str) -> int:
    """Count recent commits matching the autofix commit message or bot author."""
    result = _run_subprocess(
        ["git", "log", "-n", "50", "--format=%s%x1f%an"],
        cwd=workspace,
    )
    if result.returncode != 0:
        return 0

    count = 0
    for line in result.stdout.splitlines():
        if "\x1f" not in line:
            continue
        subject, author = line.split("\x1f", 1)
        if subject.strip() == commit_message.strip() or author.strip() == BOT_GIT_NAME:
            count += 1
    return count


def has_working_tree_changes(workspace: Path) -> bool:
    """Return True if git working tree has uncommitted changes."""
    result = _run_subprocess(["git", "status", "--porcelain"], cwd=workspace)
    return result.returncode == 0 and bool(result.stdout.strip())


def commit_and_push_changes(config: ActionConfig) -> str:
    """Stage, commit, and push changes to the PR head branch, returning the commit SHA."""
    ws = config.workspace
    for cmd in (
        ["git", "config", "user.name", BOT_GIT_NAME],
        ["git", "config", "user.email", BOT_GIT_EMAIL],
        ["git", "add", "-A"],
        ["git", "commit", "-m", config.commit_message],
    ):
        res = _run_subprocess(cmd, cwd=ws)
        if res.returncode != 0:
            raise RuntimeError(f"Git command {cmd!r} failed: {res.stderr.strip()}")

    push_cmd = ["git", "push"]
    if config.head_ref:
        push_cmd = ["git", "push", "origin", f"HEAD:{config.head_ref}"]
    push_res = _run_subprocess(push_cmd, cwd=ws)
    if push_res.returncode != 0:
        raise RuntimeError(f"Git push failed: {push_res.stderr.strip()}")

    sha_res = _run_subprocess(["git", "rev-parse", "HEAD"], cwd=ws)
    if sha_res.returncode != 0:
        raise RuntimeError(f"Failed to read HEAD SHA: {sha_res.stderr.strip()}")
    return sha_res.stdout.strip()


def run_test_suite(config: ActionConfig) -> tuple[bool, str]:
    """Run the configured test command in the workspace."""
    if not config.test_command:
        return True, ""
    res = _run_subprocess(config.test_command, cwd=config.workspace, shell=True)
    combined = "\n".join(part for part in (res.stdout, res.stderr) if part).strip()
    return res.returncode == 0, combined[-4000:]


def write_github_outputs(output_path: Path | None, summary: AutofixSummary) -> None:
    """Write action outputs to $GITHUB_OUTPUT."""
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"fixed-count={summary.fixed_count}",
        f"skipped-count={summary.skipped_count}",
        f"remaining-threads={summary.remaining_threads}",
        f"commit-sha={summary.commit_sha}",
    ]
    with output_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


async def _publish_thread_feedback(
    client: GitHubClient,
    threads: list[ReviewThread],
    outcomes: dict[str, ThreadFixOutcome],
    commit_sha: str,
    *,
    auto_resolve: bool,
) -> tuple[int, int]:
    fixed_count = 0
    skipped_count = 0
    short_sha = commit_sha[:7] if commit_sha else ""

    for thread in threads:
        outcome = outcomes.get(thread.thread_id)
        if outcome and outcome.fixed and commit_sha:
            summary_text = outcome.summary or "Applied automated fix."
            reply_body = f"✅ Fixed via Antigravity in commit `{short_sha}`.\n\n{summary_text}"
            await client.reply_to_thread(thread.thread_id, reply_body)
            if auto_resolve:
                await client.resolve_thread(thread.thread_id)
            fixed_count += 1
        else:
            reason = (
                outcome.reason
                if outcome and outcome.reason
                else "Automated fix was not applied; deferred for human review."
            )
            await client.reply_to_thread(
                thread.thread_id,
                f"⏭️ Skipped automated fix: {reason}",
            )
            skipped_count += 1

    return fixed_count, skipped_count


async def run_autofix(config: ActionConfig) -> AutofixSummary:
    """Execute the full CodeRabbit autofix pipeline."""
    validate_antigravity_auth(config)

    async with GitHubClient(
        config.github_token,
        api_url=config.github_api_url,
        graphql_url=config.github_graphql_url,
    ) as client:
        attempts = count_autofix_attempts(config.workspace, config.commit_message)
        if attempts >= config.max_attempts:
            await client.post_pr_comment(
                config.owner,
                config.repo,
                config.pr_number,
                (
                    f"⚠️ Antigravity CodeRabbit Autofix stopped: reached the maximum of "
                    f"`{config.max_attempts}` automated fix attempts on this PR. "
                    "Please review remaining threads manually."
                ),
            )
            summary = AutofixSummary(fixed_count=0, skipped_count=0, remaining_threads=0)
            write_github_outputs(config.github_output, summary)
            return summary

        threads, total_unresolved = await client.fetch_unresolved_coderabbit_threads(
            config.owner,
            config.repo,
            config.pr_number,
            ignore_nitpicks=config.ignore_nitpicks,
        )
        if not threads:
            summary = AutofixSummary(
                fixed_count=0,
                skipped_count=0,
                remaining_threads=total_unresolved,
            )
            write_github_outputs(config.github_output, summary)
            return summary

        outcomes = await run_agent_fixes(threads, config)

        passed, test_output = run_test_suite(config)
        if not passed:
            outcomes = await run_agent_fixes(threads, config, repair_feedback=test_output)
            passed, test_output = run_test_suite(config)
            if not passed:
                raise RuntimeError(
                    "Verification tests failed after self-repair pass; aborting without commit."
                )

        commit_sha = ""
        if has_working_tree_changes(config.workspace):
            commit_sha = commit_and_push_changes(config)

        fixed_count, skipped_count = await _publish_thread_feedback(
            client,
            threads,
            outcomes,
            commit_sha,
            auto_resolve=config.auto_resolve,
        )

        resolved_count = fixed_count if config.auto_resolve else 0
        remaining = max(0, total_unresolved - resolved_count)
        summary = AutofixSummary(
            fixed_count=fixed_count,
            skipped_count=skipped_count,
            remaining_threads=remaining,
            commit_sha=commit_sha,
        )
        write_github_outputs(config.github_output, summary)
        return summary


def main() -> int:
    """CLI / GitHub Action entrypoint."""
    try:
        config = ActionConfig.from_env()
        asyncio.run(run_autofix(config))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
