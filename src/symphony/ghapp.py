"""GitHub App installation credentials — pure, no network.

The App's private key is a SIGNING key: possession mints tokens for every installation.
It is subject to the same absolute rule as the tracker credential — host only, never in
a sandbox (constitution III). What reaches the sandbox is a derived, 1-hour installation
token, delivered through a git credential helper rather than embedded in a URL.

The HTTP exchange is injected so this module stays testable without a network or a real
App; `from_env` supplies the real one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .endpoints import github_api_url

# Refresh this far ahead of expiry. Sized to leave room for one failed mint AND a retry
# while the current token is still valid, so a credential problem surfaces as its own
# diagnosable failure (FR-018) rather than as a push rejected hours into an attempt.
REFRESH_MARGIN_MS = 10 * 60 * 1000

# The API caps assertion lifetime at 10 minutes; back-date `iat` so a slow clock on this
# host does not produce a token the API considers issued in the future.
ASSERTION_TTL_S = 9 * 60
CLOCK_SKEW_S = 60


class CredentialError(Exception):
    """Minting or renewal failed. Distinct on purpose: the operator must be able to tell
    this apart from a permissions problem on the repository."""


class NotAuthorizedForRepo(CredentialError):
    """The application is not installed on the requested repository.

    Its own type because the operator action differs: this means "install the application
    on that repository", while a plain CredentialError means "something is wrong with the
    application's credentials". An operator who cannot tell them apart debugs the wrong
    thing. Surfaces as the `repo-not-authorized` reason.
    """

    def __init__(self, repo: str) -> None:
        super().__init__(
            f"the application is not installed on {repo!r} — install it there, or fix the "
            "repo: designation on the ticket"
        )
        self.repo = repo
        self.reason = NOT_AUTHORIZED_REASON


NOT_AUTHORIZED_REASON = "repo-not-authorized"


def build_assertion(app_id: str, private_key: str, now_s: int) -> str:
    """RS256 JWT proving control of the App. Signed here, never sent to a sandbox."""
    import jwt

    return jwt.encode(
        {"iat": now_s - CLOCK_SKEW_S, "exp": now_s + ASSERTION_TTL_S, "iss": app_id},
        private_key,
        algorithm="RS256",
    )


def credential_helper_script(token_file: str | None = None) -> str:
    """A git credential helper that reads the token from a file.

    Git calls this with `get` and expects `username=`/`password=` on stdout. Reading
    from a file is what decouples credential lifetime from clone time: refresh becomes
    a single file write, with no remote rewriting and no race against in-flight git
    operations.
    """
    from .workspace import TOKEN_FILE

    path = token_file or TOKEN_FILE
    return (
        "#!/bin/sh\n"
        '[ "$1" = "get" ] || exit 0\n'
        "echo username=x-access-token\n"
        f'echo "password=$(cat {path} 2>/dev/null)"\n'
    )


# The installation-token scope of a workspace working a ticket: push its branch, open and
# comment on its pull request (conversation comments are issue-comment endpoints, which
# GitHub accepts with `pull_requests: write` on a pull request), and read CI. No `issues`,
# `projects` or `workflows`: agent code cannot edit tickets or boards (the tracker tools
# stay host-side), and cannot change CI definitions.
WORKSPACE = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
    "checks": "read",
    "actions": "read",
    "statuses": "read",
}
# The scope of an investigation (internals §11): it can read the code and the pull
# requests, and nothing it holds can push a branch or open a pull request.
READ_ONLY = {"contents": "read", "pull_requests": "read", "metadata": "read"}


class InstallationCredential:
    """Mints and renews the installation token for one attempt.

    Held in process memory and written only into the sandbox's token file. Never
    persisted to shared state — the Dict is multi-writer and is not a credential store.
    """

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
        now_ms: Callable[[], int],
        exchange: Callable[..., dict],
        target_repos: Sequence[str] = (),
        permissions: Mapping[str, str] | None = None,
    ) -> None:
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        # Narrows the credential to this issue's repositories and no others — and, when
        # given, to these permissions (None ⇒ the installation's own). Applied on the
        # initial mint and on every renewal: a renewal that silently widened scope would
        # defeat the purpose, and renewals happen unattended, hours in.
        self.target_repos = tuple(dict.fromkeys(repo.lower() for repo in target_repos))
        self.permissions = dict(permissions) if permissions is not None else None
        self._now_ms = now_ms
        self._exchange = exchange
        self._token: str | None = None
        self.expires_at_ms: int = 0

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        now_ms: Callable[[], int],
        target_repos: Sequence[str] = (),
        permissions: Mapping[str, str] | None = None,
    ) -> InstallationCredential:
        return cls(
            app_id=env.get("GITHUB_APP_ID", ""),
            private_key=env.get("GITHUB_APP_PRIVATE_KEY", ""),
            installation_id=env.get("GITHUB_APP_INSTALLATION_ID", ""),
            now_ms=now_ms,
            exchange=_http_exchange,
            target_repos=target_repos,
            permissions=permissions,
        )

    def needs_refresh(self) -> bool:
        return self._token is None or self._now_ms() >= self.expires_at_ms - REFRESH_MARGIN_MS

    def token(self, force_refresh: bool = False) -> str:
        if self._token is not None and not force_refresh and not self.needs_refresh():
            return self._token
        if not (self.app_id and self.private_key and self.installation_id):
            raise CredentialError(
                "GitHub App is not configured: GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY and "
                "GITHUB_APP_INSTALLATION_ID must all be present on the host"
            )
        renewing = self._token is not None
        try:
            body = access_token_request(self.target_repos, self.permissions)
            assertion = build_assertion(self.app_id, self.private_key, self._now_ms() // 1000)
            result = self._exchange(assertion, self.installation_id, self.target_repos, body)
        except NotAuthorizedForRepo:
            raise
        except Exception as exc:  # noqa: BLE001 — normalized into one diagnosable error
            verb = "renew" if renewing else "mint"
            raise CredentialError(
                f"could not {verb} the installation token for installation "
                f"{self.installation_id!r}: {exc}"
            ) from exc
        self._token = str(result["token"])
        # the API's own expiry is authoritative — never assume the nominal hour
        self.expires_at_ms = int(result["expires_at_ms"])
        return self._token


def access_token_request(
    target_repos: Sequence[str], permissions: Mapping[str, str] | None = None
) -> dict:
    """The body of the installation-token request: scoped to exactly `target_repos` and,
    when given, to exactly `permissions` (otherwise the installation's own).

    The API takes repository NAMES within the installation's account, so every repository
    must share one owner — a cross-owner set cannot be covered by any one installation."""
    body: dict = {}
    if target_repos:
        owners = {repo.split("/", 1)[0] for repo in target_repos}
        if len(owners) > 1:
            raise NotAuthorizedForRepo(", ".join(target_repos))
        body["repositories"] = [repo.split("/", 1)[-1] for repo in target_repos]
    if permissions is not None:
        body["permissions"] = dict(permissions)
    return body


def _http_exchange(  # pragma: no cover — network edge
    assertion: str, installation_id: str, target_repos: Sequence[str], body: dict
) -> dict:
    import datetime as _dt

    import httpx

    resp = httpx.post(
        f"{github_api_url()}/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {assertion}", "Accept": "application/vnd.github+json"},
        json=body or None,
        timeout=20.0,
    )
    if resp.status_code in (403, 422) and target_repos:
        # the installation does not cover (all of) these repositories
        raise NotAuthorizedForRepo(", ".join(target_repos))
    resp.raise_for_status()
    payload = resp.json()
    expires = _dt.datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    return {"token": payload["token"], "expires_at_ms": int(expires.timestamp() * 1000)}
