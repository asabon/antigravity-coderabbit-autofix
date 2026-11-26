"""GitHub GraphQL and REST API client for managing CodeRabbit review threads."""

from __future__ import annotations

import re
from typing import Any

import httpx

from src.models import ReviewThread

CODERABBIT_LOGINS = frozenset({"coderabbitai[bot]", "coderabbitai"})

_NITPICK_PATTERNS = (
    re.compile(r"\[nitpick\]", re.IGNORECASE),
    re.compile(r"🧹\s*nitpick", re.IGNORECASE),
    re.compile(r"nitpick\s*\(non-blocking\)", re.IGNORECASE),
    re.compile(r"🛠️\s*nitpick", re.IGNORECASE),
)

_FETCH_THREADS_QUERY = """
query FetchReviewThreads($owner: String!, $repo: String!, $prNumber: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $prNumber) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          path
          line
          originalLine
          comments(first: 10) {
            nodes {
              body
              diffHunk
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
"""

_REPLY_TO_THREAD_MUTATION = """
mutation ReplyToReviewThread($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment {
      id
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation ResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""


class GitHubClientError(RuntimeError):
    """Raised when a GitHub API request fails."""


def is_nitpick_comment(body: str) -> bool:
    """Return True if the comment body is marked as a low-severity nitpick."""
    return any(pattern.search(body) for pattern in _NITPICK_PATTERNS)


class GitHubClient:
    """Async client for GitHub GraphQL and REST APIs."""

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        graphql_url: str = "https://api.github.com/graphql",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("GitHub token must not be empty.")
        self._api_url = api_url.rstrip("/")
        self._graphql_url = graphql_url
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            transport=transport,
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._graphql_url,
                json={"query": query, "variables": variables},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubClientError(
                f"GitHub GraphQL request failed with HTTP {exc.response.status_code}."
            ) from None
        except httpx.HTTPError as exc:
            raise GitHubClientError(
                f"GitHub GraphQL network error: {type(exc).__name__}."
            ) from None

        payload = response.json()
        if payload.get("errors"):
            messages = "; ".join(
                str(err.get("message", "Unknown GraphQL error")) for err in payload["errors"]
            )
            raise GitHubClientError(f"GitHub GraphQL error: {messages}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubClientError("GitHub GraphQL response missing 'data' field.")
        return data

    async def fetch_unresolved_coderabbit_threads(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        ignore_nitpicks: bool = True,
    ) -> tuple[list[ReviewThread], int]:
        """Fetch unresolved CodeRabbit review threads for a pull request.

        Returns:
            A tuple of (actionable_threads, total_unresolved_coderabbit_count).
        """
        actionable: list[ReviewThread] = []
        total_unresolved = 0
        cursor: str | None = None

        while True:
            data = await self._graphql(
                _FETCH_THREADS_QUERY,
                {
                    "owner": owner,
                    "repo": repo,
                    "prNumber": pr_number,
                    "cursor": cursor,
                },
            )
            pr_data = (data.get("repository") or {}).get("pullRequest")
            if not pr_data:
                raise GitHubClientError(f"Pull request #{pr_number} not found in {owner}/{repo}.")

            threads_conn = pr_data.get("reviewThreads") or {}
            nodes = threads_conn.get("nodes") or []

            for node in nodes:
                if not isinstance(node, dict) or node.get("isResolved", False):
                    continue

                comments = (node.get("comments") or {}).get("nodes") or []
                if not comments:
                    continue

                first_comment = comments[0] or {}
                author_login = ((first_comment.get("author") or {}).get("login") or "").strip()
                if author_login not in CODERABBIT_LOGINS:
                    continue

                body = str(first_comment.get("body") or "")
                nitpick = is_nitpick_comment(body)
                total_unresolved += 1

                if ignore_nitpicks and nitpick:
                    continue

                line = node.get("line")
                if line is None:
                    line = node.get("originalLine")

                actionable.append(
                    ReviewThread(
                        thread_id=str(node["id"]),
                        path=str(node.get("path") or ""),
                        line=line if isinstance(line, int) else None,
                        diff_hunk=str(first_comment.get("diffHunk") or ""),
                        body=body,
                        author=author_login,
                        is_resolved=False,
                        is_nitpick=nitpick,
                    )
                )

            page_info = threads_conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                break

        return actionable, total_unresolved

    async def reply_to_thread(self, thread_id: str, body: str) -> None:
        """Post an inline reply to a pull request review thread."""
        await self._graphql(
            _REPLY_TO_THREAD_MUTATION,
            {"threadId": thread_id, "body": body},
        )

    async def resolve_thread(self, thread_id: str) -> None:
        """Mark a pull request review thread as resolved."""
        await self._graphql(
            _RESOLVE_THREAD_MUTATION,
            {"threadId": thread_id},
        )

    async def post_pr_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        """Post a top-level comment on a pull request."""
        url = f"{self._api_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        try:
            response = await self._client.post(url, json={"body": body})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubClientError(
                f"GitHub REST comment request failed with HTTP {exc.response.status_code}."
            ) from None
        except httpx.HTTPError as exc:
            raise GitHubClientError(f"GitHub REST network error: {type(exc).__name__}.") from None
