"""External endpoints (internals §14), read from the environment at call time.

Defaults are production; overrides serve GitHub Enterprise Server and the certification mocks.
"""

from __future__ import annotations

import os

DEFAULTS = {
    "LINEAR_API_URL": "https://api.linear.app/graphql",
    "GITHUB_API_URL": "https://api.github.com",
    "GITHUB_GRAPHQL_URL": "https://api.github.com/graphql",
    "GITHUB_SERVER_URL": "https://github.com",
    "SLACK_API_URL": "https://slack.com/api",
    "DISCORD_API_URL": "https://discord.com/api/v10",
}
NAMES = tuple(DEFAULTS)


def _get(name: str) -> str:
    return (os.environ.get(name) or DEFAULTS[name]).rstrip("/")


def linear_api_url() -> str:
    return _get("LINEAR_API_URL")


def github_api_url() -> str:
    return _get("GITHUB_API_URL")


def github_graphql_url() -> str:
    return _get("GITHUB_GRAPHQL_URL")


def github_server_url() -> str:
    return _get("GITHUB_SERVER_URL")


def slack_api_url() -> str:
    return _get("SLACK_API_URL")


def discord_api_url() -> str:
    return _get("DISCORD_API_URL")


def overrides() -> dict[str, str]:
    """The endpoint variables set in this environment, to hand to child processes and pods."""
    return {name: os.environ[name] for name in NAMES if os.environ.get(name)}


def public_url() -> str | None:
    """`SYMPHONY_PUBLIC_URL`: the address people and vendors reach the API at (webhook URLs
    shown by setup). No default: when unset, the caller falls back to the request's base URL.
    Not an external endpoint, so it is not part of `overrides()`."""
    value = (os.environ.get("SYMPHONY_PUBLIC_URL") or "").strip().rstrip("/")
    return value or None
