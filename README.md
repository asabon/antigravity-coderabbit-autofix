# Antigravity CodeRabbit Autofix

[![GitHub Marketplace](https://img.shields.io/badge/Marketplace-Antigravity%20CodeRabbit%20Autofix-blue?logo=github)](https://github.com/marketplace)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A GitHub Action powered by the **Google Antigravity SDK (`google-antigravity`)** that automatically resolves unresolved review comments from **CodeRabbit (`coderabbitai[bot]`)**, verifies fixes against your test suite, commits the changes, and replies/resolves the corresponding review threads.

---

## Prerequisites

To use this Action, the runner environment must have access to **Google Antigravity**:

1. **GitHub-hosted runners (`ubuntu-latest`)**: Provide a valid `GEMINI_API_KEY` or `ANTIGRAVITY_API_KEY` via repository secrets, or configure Google Cloud Application Default Credentials (ADC / Workload Identity Federation).
2. **Self-hosted runners (`self-hosted`)**: Ensure Antigravity is installed and authenticated locally on the host machine (e.g., via a Google AI Pro subscription), or pass an API key explicitly.

---

## Quick Start

Create `.github/workflows/coderabbit-autofix.yml` in your repository:

```yaml
name: Auto-fix CodeRabbit Reviews

on:
  pull_request_review:
    types: [submitted]

concurrency:
  group: coderabbit-autofix-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  autofix:
    if: >
      github.event.review.user.login == 'coderabbitai[bot]' &&
      github.event.review.state != 'approved'
    runs-on: ubuntu-latest # Or 'self-hosted'
    permissions:
      contents: write
      pull-requests: write

    steps:
      - name: Checkout PR branch
        uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.ref }}
          fetch-depth: 0

      - name: Run Antigravity CodeRabbit Autofix
        uses: asabon/antigravity-coderabbit-autofix@v1
        with:
          github-token: ${{ secrets.GITHUB_TOKEN }}
          antigravity-api-key: ${{ secrets.ANTIGRAVITY_API_KEY }}
          test-command: "pytest"
          max-attempts: 3
          ignore-nitpicks: true
```

---

## Inputs

| Input | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `github-token` | GitHub token with `contents: write` and `pull-requests: write` permissions. | Yes | `${{ github.token }}` |
| `antigravity-api-key` | API key for Antigravity / Gemini. Optional if using self-hosted local auth or GCP ADC. | No | `""` |
| `max-attempts` | Maximum number of auto-fix commits allowed per PR to prevent infinite review loops. | No | `"3"` |
| `ignore-nitpicks` | Skip low-severity suggestions marked as nitpicks (`[nitpick]` / `🧹 Nitpick`). | No | `"true"` |
| `deny-write-paths` | Comma-separated glob patterns that the agent is strictly forbidden from modifying. | No | `".github/**,*.lock,.env*"` |
| `protect-tests` | Block the agent from modifying or deleting existing test files to bypass failures. | No | `"true"` |
| `test-command` | Shell command executed to verify fixes before committing (e.g., `pytest`, `npm test`). | No | `""` |
| `commit-message` | Commit message used when pushing automated fixes. | No | `"fix: address CodeRabbit review feedback via Antigravity"` |
| `auto-resolve` | Automatically mark fixed review threads as resolved on GitHub. | No | `"true"` |

## Outputs

| Output | Description |
| :--- | :--- |
| `fixed-count` | Number of CodeRabbit review threads successfully fixed and committed. |
| `skipped-count` | Number of review threads skipped or deferred to human review. |
| `remaining-threads` | Count of unresolved CodeRabbit review threads remaining on the PR. |
| `commit-sha` | The SHA of the created fix commit (empty if no changes were committed). |

---

## Architecture & Implementation Specification

> **Note for AI Coding Agents (Antigravity):** Use the specification below as the authoritative blueprint when implementing, refactoring, or testing this repository.

### 1. Directory Structure

```text
.
├── action.yml                  # Composite GitHub Action definition
├── pyproject.toml              # Python project metadata & dependencies (google-antigravity, httpx, etc.)
├── README.md                   # Documentation & architectural blueprint
├── src/
│   ├── __init__.py
│   ├── main.py                 # Entrypoint: loop guards, orchestration, git commit/push, outputs
│   ├── github_client.py        # GitHub GraphQL & REST client (fetch threads, reply, resolve)
│   ├── agent_runner.py         # Antigravity SDK wrapper (Policy, Hooks, structured execution)
│   └── models.py               # Pydantic / dataclass schemas for review threads & fix outcomes
├── tests/
│   ├── test_github_client.py   # Unit tests with mocked GraphQL/REST payloads
│   ├── test_agent_runner.py    # Unit tests for Policy & Hook guardrails
│   └── test_main.py            # Integration tests for loop prevention & git workflow
└── .github/
    └── workflows/
        └── ci.yml              # Self-validation CI (lint, typecheck, unit tests)
```

### 2. Core Execution Pipeline (`src/main.py`)

1. **Preflight & Auth Validation:**
   - Verify required environment variables (`GITHUB_TOKEN`, `GITHUB_REPOSITORY`, PR number).
   - Check Antigravity authentication availability (`ANTIGRAVITY_API_KEY`, `GEMINI_API_KEY`, GCP ADC, or local CLI session). Fail fast with an actionable error message if none is detected.
2. **Infinite Loop Guard:**
   - Inspect the PR's git commit history (`git log`) for commits matching `commit-message` (or bot author).
   - If the count is `>= max-attempts`, post a summary comment notifying human maintainers and exit gracefully (`0`).
3. **Fetch & Filter Unresolved Threads (`src/github_client.py`):**
   - Query GitHub GraphQL API (`pullRequest.reviewThreads`) to retrieve all threads where `isResolved == false` and author login is `coderabbitai[bot]`.
   - Extract `thread_id`, `path`, `line`, `diffHunk`, and comment body.
   - If `ignore-nitpicks == true`, exclude threads categorized as minor nitpicks.
   - Exit early if no actionable threads remain.
4. **Execute Agent Fixes (`src/agent_runner.py`):**
   - Initialize `google.antigravity.Agent` with declarative `Policy`:
     - Restrict writes to workspace (`Policy.workspace_only()`).
     - Enforce `deny_write` on `deny-write-paths` (default: `.github/**`, `*.lock`, `.env*`).
     - Deny destructive or network-mutating shell commands (`git push*`, `rm -rf*`, `gh pr merge*`).
   - Register `@agent.pre_tool_call_decide` hook:
     - When `protect-tests == true`, block `edit_file` / `write_file` operations targeting test paths (`*test*`, `tests/**`, `__tests__/**`) so the agent cannot weaken assertions to pass tests.
   - Prompt the agent with structured thread contexts and require a structured JSON summary mapping each `thread_id` to `{fixed: bool, summary: str, reason: str}`.
5. **Verification & Git Commit:**
   - If `test-command` is configured, run it in a subprocess. If tests fail, optionally allow one self-repair pass or abort without pushing broken code.
   - Check `git status --porcelain`. If working tree has changes:
     - Configure git user (`github-actions[bot]`).
     - Stage, commit with `commit-message`, and push to the PR head branch.
6. **Thread Feedback & Resolution (`src/github_client.py`):**
   - For each thread marked `fixed: true` (and verified committed):
     - Post an inline reply via GraphQL (`addPullRequestReviewThreadReply`) summarizing the change and referencing the commit SHA.
     - If `auto-resolve == true`, resolve the thread via GraphQL (`resolveReviewThread`).
   - For threads marked `fixed: false`:
     - Post a concise reply explaining why automated modification was skipped, leaving the thread unresolved for human review.
7. **Write GitHub Action Outputs:**
   - Write `fixed-count`, `skipped-count`, `remaining-threads`, and `commit-sha` to `$GITHUB_OUTPUT`.

### 3. Technical Constraints & Standards

- **Python Version:** Python 3.11+
- **Action Type:** Composite Action (`runs.using: "composite"`) so it works seamlessly on both `ubuntu-latest` and `self-hosted` runners without Docker build overhead.
- **Dependencies:** Keep external dependencies minimal (`google-antigravity`, `httpx`, `pydantic`).
- **Error Handling:** Never leak tokens or API keys in logs or PR comments.
