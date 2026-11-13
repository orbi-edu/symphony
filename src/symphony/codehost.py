"""Code-host reads for the review watch — the network edge, kept deliberately thin.

Contract: specs/005-review-conversation-watch/spec.md

Only reads, only on the host, and only with the installation token that is already minted
and already scoped to the issue's repository (constitution III — nothing new enters the
sandbox on this path; nothing enters the sandbox on this path at all).

Every call answers with a value or raises `CodeHostUnavailable`. The caller treats that as
"no information this cycle" and moves on: a review watch that can fail a polling cycle would
be a worse problem than the silence it exists to fix (FR-005).
"""

from __future__ import annotations

from collections.abc import Callable

from .endpoints import github_api_url

TIMEOUT_S = 15.0
# One page is plenty: we only need the newest comment, and the API returns them oldest-first,
# so we ask for the last page instead of walking all of them.
PER_PAGE = 100


class CodeHostUnavailable(Exception):
    """Unreachable, rate-limited, or refused. Never fatal to a polling cycle."""


class GitHubReader:
    """Reads one repository's pull-request conversations.

    `token_for` is injected rather than held: installation tokens live an hour, and the
    daemon runs for a day. Asking for it per call keeps renewal someone else's problem.
    """

    def __init__(self, token_for: Callable[[str], str], http_get: Callable | None = None) -> None:
        self._token_for = token_for
        self._get = http_get or _httpx_get

    def _json(self, repo: str, path: str, params: dict | None = None):
        try:
            token = self._token_for(repo)
        except Exception as exc:  # noqa: BLE001 — a credential problem is an outage here
            raise CodeHostUnavailable(f"no credential for {repo}: {exc}") from exc
        try:
            return self._get(f"{github_api_url()}/repos/{repo}/{path}", token, params or {})
        except Exception as exc:  # noqa: BLE001
            raise CodeHostUnavailable(str(exc)) from exc

    def open_pulls(self, repo: str) -> list[dict]:
        """Open pull requests, newest first.

        Listed rather than searched: the search API's rate limit is an order of magnitude
        tighter and its index lags, so a freshly opened pull request can be missing from
        results that still return 200.
        """
        data = self._json(repo, "pulls", {"state": "open", "per_page": PER_PAGE, "sort": "created",
                                          "direction": "desc"})
        return data if isinstance(data, list) else []

    def conversation(self, repo: str, number: int) -> list[dict]:
        """Every comment a reviewer could have left: the pull request's own thread, the
        inline review comments, and the review summaries. A question asked in any of the
        three is equally a question, and answering only the first would look arbitrary from
        the outside."""
        out: list[dict] = []
        for path in (f"issues/{number}/comments", f"pulls/{number}/comments"):
            data = self._json(repo, path, {"per_page": PER_PAGE, "sort": "created",
                                           "direction": "desc"})
            if isinstance(data, list):
                out.extend(d for d in data if isinstance(d, dict))
        reviews = self._json(repo, f"pulls/{number}/reviews", {"per_page": PER_PAGE})
        if isinstance(reviews, list):
            # A review with no body is a verdict, not a question — an approval must not wake
            # the agent to answer it.
            out.extend(
                {**r, "created_at": r.get("submitted_at")}
                for r in reviews
                if isinstance(r, dict) and (r.get("body") or "").strip()
            )
        return out


def _httpx_get(url: str, token: str, params: dict):  # pragma: no cover — network edge
    import httpx

    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        params=params,
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()
