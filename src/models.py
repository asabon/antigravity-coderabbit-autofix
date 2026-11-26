"""Data models and configuration schemas for Antigravity CodeRabbit Autofix."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


def _parse_bool(value: str | bool | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_csv(value: str | list[str] | None, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [item.strip() for item in value if item.strip()]
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class ActionConfig(BaseModel):
    """Runtime configuration parsed from GitHub Action inputs and environment variables."""

    github_token: str = Field(..., min_length=1)
    github_repository: str = Field(..., min_length=3)
    pr_number: int = Field(..., gt=0)
    antigravity_api_key: str = ""
    max_attempts: int = Field(default=3, ge=1)
    ignore_nitpicks: bool = True
    deny_write_paths: list[str] = Field(default_factory=lambda: [".github/**", "*.lock", ".env*"])
    protect_tests: bool = True
    test_command: str = ""
    commit_message: str = "fix: address CodeRabbit review feedback via Antigravity"
    auto_resolve: bool = True
    workspace: Path = Field(default_factory=Path.cwd)
    github_output: Path | None = None
    github_api_url: str = "https://api.github.com"
    github_graphql_url: str = "https://api.github.com/graphql"
    head_ref: str = ""

    @field_validator("github_repository")
    @classmethod
    def _validate_repo(cls, v: str) -> str:
        if "/" not in v or len(v.split("/")) != 2:
            raise ValueError("GITHUB_REPOSITORY must be in 'owner/repo' format.")
        return v

    @property
    def owner(self) -> str:
        return self.github_repository.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.github_repository.split("/", 1)[1]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ActionConfig:
        """Build ActionConfig from environment variables and GitHub event payload."""
        environ = env if env is not None else dict(os.environ)

        pr_number = _extract_pr_number(environ)
        head_ref = environ.get("GITHUB_HEAD_REF", "") or _extract_head_ref(environ)
        github_output_raw = environ.get("GITHUB_OUTPUT", "").strip()
        api_url = environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        graphql_url = environ.get("GITHUB_GRAPHQL_URL", f"{api_url}/graphql")

        max_attempts_raw = environ.get("INPUT_MAX_ATTEMPTS", "3").strip() or "3"

        return cls(
            github_token=environ.get("GITHUB_TOKEN", "").strip(),
            github_repository=environ.get("GITHUB_REPOSITORY", "").strip(),
            pr_number=pr_number,
            antigravity_api_key=(
                environ.get("ANTIGRAVITY_API_KEY", "").strip()
                or environ.get("GEMINI_API_KEY", "").strip()
            ),
            max_attempts=int(max_attempts_raw),
            ignore_nitpicks=_parse_bool(environ.get("INPUT_IGNORE_NITPICKS"), True),
            deny_write_paths=_parse_csv(
                environ.get("INPUT_DENY_WRITE_PATHS"),
                [".github/**", "*.lock", ".env*"],
            ),
            protect_tests=_parse_bool(environ.get("INPUT_PROTECT_TESTS"), True),
            test_command=environ.get("INPUT_TEST_COMMAND", "").strip(),
            commit_message=(
                environ.get("INPUT_COMMIT_MESSAGE", "").strip()
                or "fix: address CodeRabbit review feedback via Antigravity"
            ),
            auto_resolve=_parse_bool(environ.get("INPUT_AUTO_RESOLVE"), True),
            workspace=Path(environ.get("GITHUB_WORKSPACE", "") or Path.cwd()).resolve(),
            github_output=Path(github_output_raw) if github_output_raw else None,
            github_api_url=api_url,
            github_graphql_url=graphql_url,
            head_ref=head_ref,
        )


def _load_event_payload(environ: dict[str, str]) -> dict:
    event_path = environ.get("GITHUB_EVENT_PATH", "").strip()
    if not event_path:
        return {}
    path = Path(event_path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_pr_number(environ: dict[str, str]) -> int:
    raw = environ.get("PR_NUMBER", "").strip() or environ.get("INPUT_PR_NUMBER", "").strip()
    if raw:
        return int(raw)
    payload = _load_event_payload(environ)
    pr = payload.get("pull_request") or payload.get("issue") or {}
    number = pr.get("number")
    if isinstance(number, int) and number > 0:
        return number
    raise ValueError("Unable to determine PR number from PR_NUMBER or GITHUB_EVENT_PATH.")


def _extract_head_ref(environ: dict[str, str]) -> str:
    payload = _load_event_payload(environ)
    pr = payload.get("pull_request") or {}
    head = pr.get("head") or {}
    ref = head.get("ref")
    return ref if isinstance(ref, str) else ""


class ReviewThread(BaseModel):
    """An unresolved CodeRabbit pull request review thread."""

    thread_id: str
    path: str
    line: int | None = None
    diff_hunk: str = ""
    body: str
    author: str
    is_resolved: bool = False
    is_nitpick: bool = False


class ThreadFixOutcome(BaseModel):
    """Outcome of the agent's attempt to resolve a single review thread."""

    thread_id: str
    fixed: bool
    summary: str = ""
    reason: str = ""


class AgentFixResponse(BaseModel):
    """Structured response returned by the Antigravity agent."""

    outcomes: list[ThreadFixOutcome] = Field(default_factory=list)


class AutofixSummary(BaseModel):
    """Final execution summary written to GitHub Action outputs."""

    fixed_count: int = 0
    skipped_count: int = 0
    remaining_threads: int = 0
    commit_sha: str = ""
