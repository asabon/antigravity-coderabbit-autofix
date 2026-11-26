"""Unit tests for src.github_client."""

from __future__ import annotations

import json

import httpx
import pytest

from src.github_client import GitHubClient, GitHubClientError, is_nitpick_comment


def _graphql_response(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def test_is_nitpick_comment() -> None:
    assert is_nitpick_comment("[nitpick] Consider renaming this variable.")
    assert is_nitpick_comment("🧹 Nitpick: minor formatting issue.")
    assert is_nitpick_comment("**Nitpick (non-blocking):** add trailing comma.")
    assert not is_nitpick_comment("Potential null pointer dereference here.")


@pytest.mark.asyncio
async def test_fetch_unresolved_coderabbit_threads_filters_and_paginates() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append(body["variables"])
        cursor = body["variables"].get("cursor")
        if cursor is None:
            return _graphql_response(
                {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                                "nodes": [
                                    {
                                        "id": "T_1",
                                        "isResolved": False,
                                        "path": "src/app.py",
                                        "line": 42,
                                        "originalLine": 40,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "body": "Fix SQL injection risk.",
                                                    "diffHunk": "@@ -40,1 +42,1 @@",
                                                    "author": {"login": "coderabbitai[bot]"},
                                                }
                                            ]
                                        },
                                    },
                                    {
                                        "id": "T_resolved",
                                        "isResolved": True,
                                        "path": "src/app.py",
                                        "line": 10,
                                        "originalLine": 10,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "body": "Already resolved.",
                                                    "diffHunk": "",
                                                    "author": {"login": "coderabbitai[bot]"},
                                                }
                                            ]
                                        },
                                    },
                                    {
                                        "id": "T_human",
                                        "isResolved": False,
                                        "path": "src/app.py",
                                        "line": 15,
                                        "originalLine": 15,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "body": "Human comment.",
                                                    "diffHunk": "",
                                                    "author": {"login": "octocat"},
                                                }
                                            ]
                                        },
                                    },
                                ],
                            }
                        }
                    }
                }
            )
        return _graphql_response(
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "T_nitpick",
                                    "isResolved": False,
                                    "path": "src/utils.py",
                                    "line": None,
                                    "originalLine": 7,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "body": "🧹 Nitpick: use f-string.",
                                                "diffHunk": "@@ -7,1 +7,1 @@",
                                                "author": {"login": "coderabbitai"},
                                            }
                                        ]
                                    },
                                }
                            ],
                        }
                    }
                }
            }
        )

    transport = httpx.MockTransport(handler)
    async with GitHubClient("secret-token", transport=transport) as client:
        threads, total = await client.fetch_unresolved_coderabbit_threads(
            "owner", "repo", 12, ignore_nitpicks=True
        )

    assert len(calls) == 2
    assert total == 2
    assert len(threads) == 1
    assert threads[0].thread_id == "T_1"
    assert threads[0].path == "src/app.py"
    assert threads[0].line == 42

    # When ignore_nitpicks=False, nitpick thread is included too.
    async with GitHubClient("secret-token", transport=httpx.MockTransport(handler)) as client:
        all_threads, total_all = await client.fetch_unresolved_coderabbit_threads(
            "owner", "repo", 12, ignore_nitpicks=False
        )
    assert total_all == 2
    assert [t.thread_id for t in all_threads] == ["T_1", "T_nitpick"]
    assert all_threads[1].line == 7


@pytest.mark.asyncio
async def test_reply_resolve_and_pr_comment() -> None:
    recorded: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        recorded.append((request.url.path, payload))
        if request.url.path == "/graphql":
            return _graphql_response({"ok": True})
        return httpx.Response(201, json={"id": 999})

    async with GitHubClient("ghp_123", transport=httpx.MockTransport(handler)) as client:
        await client.reply_to_thread("T_1", "Fixed!")
        await client.resolve_thread("T_1")
        await client.post_pr_comment("owner", "repo", 5, "Max attempts reached.")

    assert recorded[0][0] == "/graphql"
    assert recorded[0][1]["variables"] == {"threadId": "T_1", "body": "Fixed!"}
    assert recorded[1][0] == "/graphql"
    assert recorded[1][1]["variables"] == {"threadId": "T_1"}
    assert recorded[2][0] == "/repos/owner/repo/issues/5/comments"
    assert recorded[2][1] == {"body": "Max attempts reached."}


@pytest.mark.asyncio
async def test_github_client_error_does_not_leak_token() -> None:
    secret = "ghp_super_secret_token_value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    async with GitHubClient(secret, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubClientError) as exc_info:
            await client.fetch_unresolved_coderabbit_threads("owner", "repo", 1)

    assert secret not in str(exc_info.value)
    assert "401" in str(exc_info.value)
