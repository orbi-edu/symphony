"""Verification against the real services, for first-run setup (`api/setup.py`).

Every check takes the candidate credential explicitly (it is verified before it is stored)
and raises `SetupError` with a stable code and a plain English message that never repeats the
credential. Network failures are `vendor_unreachable` (503), never a verdict on the value.
"""

from __future__ import annotations

import time

import httpx

from .. import ghapp, notify
from ..endpoints import (
    discord_api_url,
    github_api_url,
    github_graphql_url,
    linear_api_url,
    slack_api_url,
)
from ..trackers.github import PROJECT_QUERY as GITHUB_PROJECT_QUERY
from ..trackers.linear import PROJECT_QUERY as LINEAR_PROJECT_QUERY
from ..trackers.linear import TEAM_QUERY as LINEAR_TEAM_QUERY

TIMEOUT_S = 10.0
_LEVEL = {"read": 1, "write": 2, "admin": 3}
# What Symphony needs from its GitHub App installation: the workspace scope
# (`ghapp.WORKSPACE`), `issues` and `actions: write` for the daemon and the merger (comments,
# labels, CI cancel/rerun), and a projects permission when GitHub Projects is the tracker.
REQUIRED_PERMISSIONS = {
    **ghapp.WORKSPACE,
    "issues": "write",
    "actions": "write",
}
PROJECT_PERMISSIONS = ("organization_projects", "repository_projects")
VIEWER_QUERY = "query SymphonySetupViewer { viewer { id name } }"
TEST_TEXT = "Symphony is connected: this is the setup test message."


class SetupError(Exception):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _unreachable(service: str) -> SetupError:
    return SetupError("vendor_unreachable", f"{service} could not be reached; try again", 503)


# -- Linear ------------------------------------------------------------------------------
def _linear(client: httpx.Client, api_key: str, query: str, variables: dict) -> dict:
    """The `data` of a Linear query; entities that do not exist come back as null."""
    try:
        response = client.post(
            linear_api_url(), headers={"Authorization": api_key},
            json={"query": query, "variables": variables}, timeout=TIMEOUT_S,
        )
    except httpx.HTTPError:
        raise _unreachable("Linear") from None
    if response.status_code >= 500:
        raise _unreachable("Linear")
    try:
        body = response.json()
    except ValueError:
        body = {}
    codes = " ".join(
        str((e.get("extensions") or {}).get("code") or (e.get("extensions") or {}).get("type") or "")
        for e in body.get("errors") or []
    ).upper()
    if response.status_code in (401, 403) or "AUTHENTICATION" in codes or "FORBIDDEN" in codes:
        raise SetupError("linear_unauthorized", "Linear rejected the API key")
    return body.get("data") or {}


def verify_linear(
    client: httpx.Client, api_key: str, *, team_key: str | None = None,
    project_slug: str | None = None,
) -> dict:
    """The key works and names an existing team or project: `{viewer, states}`."""
    viewer = (_linear(client, api_key, VIEWER_QUERY, {}).get("viewer") or {})
    if not viewer.get("id"):
        raise SetupError("linear_unauthorized", "Linear rejected the API key")
    if project_slug:
        project = _linear(client, api_key, LINEAR_PROJECT_QUERY, {"id": project_slug}).get("project")
        if not project:
            raise SetupError("linear_project_not_found", f"Linear has no project {project_slug!r}")
        teams = (project.get("teams") or {}).get("nodes") or []
    else:
        found = _linear(client, api_key, LINEAR_TEAM_QUERY, {"key": team_key})
        teams = (found.get("teams") or {}).get("nodes") or []
        if not teams:
            raise SetupError("linear_team_not_found", f"Linear has no team with key {team_key!r}")
    states: list[str] = []
    for team in teams:
        for state in (team.get("states") or {}).get("nodes") or []:
            if state.get("name") and state["name"] not in states:
                states.append(state["name"])
    return {"viewer": viewer.get("name") or "", "states": states}


# -- GitHub App --------------------------------------------------------------------------
def _missing(granted: dict, *, projects: bool) -> list[str]:
    missing = [
        f"{name}: {level}"
        for name, level in REQUIRED_PERMISSIONS.items()
        if _LEVEL.get(granted.get(name, ""), 0) < _LEVEL[level]
    ]
    if projects and not any(_LEVEL.get(granted.get(p, ""), 0) >= 2 for p in PROJECT_PERMISSIONS):
        missing.append("organization_projects: write")
    return missing


def verify_github_app(
    client: httpx.Client, app_id: str, installation_id: str, private_key: str, *,
    projects: bool = False,
) -> dict:
    """Mint an installation token: `{token, repositories, permissions, missing_permissions}`."""
    if not (str(app_id).strip().isdigit() and str(installation_id).strip().isdigit()):
        raise SetupError("invalid_github_app", "the App id and installation id are numbers")
    try:
        assertion = ghapp.build_assertion(str(app_id).strip(), private_key, int(time.time()))
    except Exception:
        raise SetupError("invalid_private_key", "the private key is not a valid PEM RSA key") from None
    headers = {"Authorization": f"Bearer {assertion}", "Accept": "application/vnd.github+json"}
    try:
        response = client.post(
            f"{github_api_url()}/app/installations/{str(installation_id).strip()}/access_tokens",
            headers=headers, timeout=TIMEOUT_S,
        )
    except httpx.HTTPError:
        raise _unreachable("GitHub") from None
    if response.status_code == 401:
        raise SetupError(
            "github_app_unauthorized", "GitHub rejected the App id or private key"
        )
    if response.status_code == 404:
        raise SetupError(
            "github_installation_not_found", "this App has no installation with that id"
        )
    if response.status_code >= 500:
        raise _unreachable("GitHub")
    if response.status_code != 201:
        raise SetupError("github_app_rejected", f"GitHub answered {response.status_code}")
    payload = response.json()
    token = payload["token"]
    granted = payload.get("permissions") or {}
    return {
        "token": token,
        "repositories": installation_repositories(client, token),
        "permissions": granted,
        "missing_permissions": _missing(granted, projects=projects),
    }


def installation_repositories(client: httpx.Client, token: str) -> list[str]:
    repos: list[str] = []
    url = f"{github_api_url()}/installation/repositories?per_page=100"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    for _ in range(50):
        try:
            response = client.get(url, headers=headers, timeout=TIMEOUT_S)
        except httpx.HTTPError:
            raise _unreachable("GitHub") from None
        if response.status_code != 200:
            raise SetupError("github_app_rejected", "the installation's repositories are unreadable")
        repos.extend(r["full_name"].lower() for r in response.json().get("repositories") or [])
        url = response.links.get("next", {}).get("url")
        if not url:
            break
    return sorted(set(repos))


def verify_github_project(
    client: httpx.Client, token: str, owner: str, number: int, status_field: str = "Status"
) -> dict:
    """The project exists and has a single-select status field: `{states}`."""
    try:
        response = client.post(
            github_graphql_url(),
            headers={"Authorization": f"bearer {token}"},
            json={"query": GITHUB_PROJECT_QUERY, "variables": {"owner": owner, "number": number}},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError:
        raise _unreachable("GitHub") from None
    if response.status_code >= 500:
        raise _unreachable("GitHub")
    body = response.json() if response.content else {}
    project = ((body.get("data") or {}).get("repositoryOwner") or {}).get("projectV2")
    if not project:
        raise SetupError(
            "github_project_not_found",
            f"GitHub project {owner}#{number} was not found or the App cannot read it",
        )
    fields = {f.get("name"): f for f in (project.get("fields") or {}).get("nodes") or [] if f}
    status = fields.get(status_field)
    if not status or status.get("dataType") != "SINGLE_SELECT":
        raise SetupError(
            "github_status_field_missing",
            f"GitHub project {owner}#{number} has no single-select field {status_field!r}",
        )
    return {"states": [o["name"] for o in status.get("options") or []]}


# -- Notifications -----------------------------------------------------------------------
def slack_sender(config: dict):
    if config.get("bot_token") and config.get("channel"):
        return lambda message: notify._send_slack_bot(config["bot_token"], config["channel"], message)
    if config.get("webhook_url"):
        return lambda message: notify._send_slack_webhook(config["webhook_url"], message)
    return None


def discord_sender(config: dict):
    headers = {"User-Agent": notify.DISCORD_USER_AGENT}
    if config.get("bot_token") and config.get("channel_id"):
        url = f"{discord_api_url()}/channels/{config['channel_id']}/messages"
        auth = {**headers, "Authorization": f"Bot {config['bot_token']}"}
        return lambda message: notify._send_discord(url, auth, {}, message, None)
    if config.get("webhook_url"):
        return lambda message: notify._send_discord(
            config["webhook_url"], headers, {"wait": "true"}, message, None
        )
    return None


def send_test(channel: str, send) -> dict:
    """One test message: `{ok, error}` with `error` a code (`rejected`, `unreachable`)."""
    message = (
        notify.format_message("setup_test", TEST_TEXT)
        if channel == "slack"
        else notify.format_discord_message("setup_test", TEST_TEXT)
    )
    try:
        ok = bool(send(message))
    except httpx.HTTPError:
        return {"ok": False, "error": "unreachable"}
    return {"ok": ok, "error": None if ok else "rejected"}


# -- Assistant ---------------------------------------------------------------------------
# Discord application flags (docs: Application Object › Application Flags). Either one lets a
# bot read message content over the Gateway.
GATEWAY_MESSAGE_CONTENT = 1 << 18
GATEWAY_MESSAGE_CONTENT_LIMITED = 1 << 19


def _slack_call(client: httpx.Client, method: str, token: str) -> dict:
    try:
        response = client.post(
            f"{slack_api_url()}/{method}", headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError:
        raise _unreachable("Slack") from None
    if response.status_code >= 500 or response.status_code == 429:
        raise _unreachable("Slack")
    try:
        return response.json()
    except ValueError:
        raise _unreachable("Slack") from None


def verify_slack_app_token(client: httpx.Client, app_token: str) -> None:
    """`apps.connections.open` accepts the app-level token. The socket URL is not opened."""
    body = _slack_call(client, "apps.connections.open", app_token)
    if not body.get("ok"):
        raise SetupError(
            "slack_app_token_rejected",
            f"Slack rejected the app token ({body.get('error') or 'error'}). Use an app-level "
            "token (xapp-…) with the connections:write scope, and turn on Socket Mode.",
        )


def verify_slack_bot(client: httpx.Client, bot_token: str) -> dict:
    """`auth.test` accepts the bot token: `{team, user}`."""
    body = _slack_call(client, "auth.test", bot_token)
    if not body.get("ok"):
        raise SetupError(
            "slack_bot_token_rejected",
            f"Slack rejected the bot token ({body.get('error') or 'error'}).",
        )
    return {"team": body.get("team") or "", "user": body.get("user") or ""}


def verify_discord_app(client: httpx.Client, bot_token: str) -> str:
    """`GET /applications/@me`: the message-content intent, `full` or `limited`."""
    try:
        response = client.get(
            f"{discord_api_url()}/applications/@me",
            headers={"Authorization": f"Bot {bot_token}", "User-Agent": notify.DISCORD_USER_AGENT},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError:
        raise _unreachable("Discord") from None
    if response.status_code in (401, 403):
        raise SetupError("discord_bot_token_rejected", "Discord rejected the bot token.")
    if response.status_code != 200:
        raise _unreachable("Discord")
    try:
        flags = int(response.json().get("flags") or 0)
    except (ValueError, TypeError, AttributeError):
        raise _unreachable("Discord") from None
    if flags & GATEWAY_MESSAGE_CONTENT:
        return "full"
    if flags & GATEWAY_MESSAGE_CONTENT_LIMITED:
        return "limited"
    raise SetupError(
        "message_content_intent_missing",
        "The Discord bot cannot read messages. In the Developer Portal, open the application, "
        "then Bot, and turn on Message Content Intent.",
    )
