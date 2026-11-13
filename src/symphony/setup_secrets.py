"""Where first-run setup stores integration credentials, and how their consumers pick them up.

The setup API (`api/setup.py`) verifies a credential against the real service, then stores it
here, write-only: values are never read back (only whether a name is set), logged, returned
or kept in Redis. Groups map to the Secrets the components already read through `envFrom`:

| group          | Secret              | names                                                         |
|----------------|---------------------|---------------------------------------------------------------|
| `linear`       | `linear-api-key`    | `LINEAR_API_KEY`, `LINEAR_WEBHOOK_SECRET`                      |
| `github-app`   | `github-app`        | `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` |
| `api`          | `symphony-api`      | `GITHUB_WEBHOOK_SECRET`                                        |
| `slack`        | `symphony-slack`    | `SLACK_BOT_TOKEN`, `SLACK_CHANNEL`, `SLACK_WEBHOOK_URL`, `SLACK_APP_TOKEN` |
| `discord`      | `symphony-discord`  | `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_WEBHOOK_URL` |
| `model-provider` (read-only) | `model-provider` | host model credentials; only presence is checked     |

Backends: `KubernetesSecretStore` (here) and `setup_secrets_local.LocalSecretStore` (a 0600
`secrets.env` for the local stack). Kubernetes: the Secrets are pre-created empty by the
manifests and the API may only `get`/`patch` them, so a store is a merge PATCH of
`stringData`; then every Deployment that reads the group restarts (template annotation
`symphony.dev/restarted-at`): the daemon, each merger (`component=merger`), the assistant,
the API itself last. `restart(["assistant"])` restarts only the assistant (it reads the
published WORKFLOW at start). Runner and reflector Jobs read the Secrets when they are
created and need nothing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from typing import Protocol

GROUPS: dict[str, tuple[str, ...]] = {
    "linear": ("LINEAR_API_KEY", "LINEAR_WEBHOOK_SECRET"),
    "github-app": ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY"),
    "api": ("GITHUB_WEBHOOK_SECRET",),
    "slack": ("SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL", "SLACK_APP_TOKEN"),
    "discord": ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_WEBHOOK_URL"),
}
READ_ONLY_GROUPS = frozenset({"model-provider"})
SECRET_NAMES: dict[str, str] = {
    "linear": "linear-api-key",
    "github-app": "github-app",
    "api": "symphony-api",
    "slack": "symphony-slack",
    "discord": "symphony-discord",
    "model-provider": "model-provider",
}
# Which Deployments read a group through envFrom, in restart order (the API, which is
# serving the request that changed the value, always last).
CONSUMERS: dict[str, tuple[str, ...]] = {
    "linear": ("daemon", "merger", "assistant", "api"),
    "github-app": ("daemon", "merger", "assistant", "api"),
    "api": ("api",),
    "slack": ("daemon", "merger", "assistant", "api"),
    "discord": ("daemon", "merger", "assistant", "api"),
    # Not a Secret group: the assistant alone, after a WORKFLOW `assistant:` change.
    "assistant": ("assistant",),
}
DEPLOYMENTS = {"daemon": "symphony-daemon", "assistant": "symphony-assistant",
               "api": "symphony-api"}
MERGER_SELECTOR = "app=symphony,component=merger"
RESTART_ANNOTATION = "symphony.dev/restarted-at"
_TIMEOUT = (3, 10)


class SecretStoreError(Exception):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class SecretStore(Protocol):
    mode: str

    def put(self, group: str, values: Mapping[str, str], *, restart: bool = True) -> list[str]:
        """Store `values` (names of `group`); returns the consumers restarted."""

    def has(self, group: str, name: str) -> bool: ...

    def remove(self, group: str, names: Iterable[str], *, restart: bool = True) -> list[str]: ...

    def restart(self, groups: Iterable[str]) -> list[str]:
        """Make the consumers of `groups` read the stored values; returns them."""


def check_names(group: str, names: Iterable[str]) -> None:
    """Shared by every backend: a group's names only, never a read-only group."""
    if group in READ_ONLY_GROUPS:
        raise SecretStoreError("read_only_group", f"{group} is managed by the host", 422)
    allowed = GROUPS.get(group)
    if allowed is None:
        raise SecretStoreError("unknown_group", f"unknown secret group {group!r}", 422)
    unknown = sorted(set(names) - set(allowed))
    if unknown:
        raise SecretStoreError(
            "unknown_secret", f"{', '.join(unknown)} is not part of {group}", 422
        )


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


class KubernetesSecretStore:
    mode = "kubernetes"

    def __init__(
        self,
        core,
        namespace: str,
        apps=None,
        *,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> None:
        self.core = core
        self.namespace = namespace
        if apps is None:
            from kubernetes import client

            apps = client.AppsV1Api(core.api_client)
        self.apps = apps
        self._now = now

    def _secret(self, group: str):
        name = SECRET_NAMES[group]
        try:
            return self.core.read_namespaced_secret(
                name=name, namespace=self.namespace, _request_timeout=_TIMEOUT
            )
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise SecretStoreError(
                "secret_unavailable", f"Secret {name} could not be read", 503
            ) from None

    def has(self, group: str, name: str) -> bool:
        if group not in SECRET_NAMES:
            raise SecretStoreError("unknown_group", f"unknown secret group {group!r}", 422)
        secret = self._secret(group)
        return bool(secret is not None and (secret.data or {}).get(name))

    def _patch(self, group: str, body: dict) -> None:
        name = SECRET_NAMES[group]
        try:
            self.core.patch_namespaced_secret(
                name=name, namespace=self.namespace, body=body, _request_timeout=_TIMEOUT
            )
        except Exception as exc:
            if _status(exc) == 404:
                raise SecretStoreError(
                    "secret_missing",
                    f"Secret {name} does not exist; apply the install manifests (they create "
                    "it empty) and retry",
                    409,
                ) from None
            if _status(exc) == 403:
                raise SecretStoreError(
                    "secret_forbidden", f"the API may not patch Secret {name}", 503
                ) from None
            raise SecretStoreError(
                "secret_unavailable", f"Secret {name} could not be written", 503
            ) from None

    def put(self, group: str, values: Mapping[str, str], *, restart: bool = True) -> list[str]:
        check_names(group, values)
        if any(not isinstance(v, str) or not v for v in values.values()):
            raise SecretStoreError("invalid_secret", "secret values must be non-empty text", 422)
        self._patch(group, {"stringData": dict(values)})
        return self.restart([group]) if restart else []

    def remove(self, group: str, names: Iterable[str], *, restart: bool = True) -> list[str]:
        names = list(names)
        check_names(group, names)
        if names:
            # A null in a merge patch deletes the key.
            self._patch(group, {"data": dict.fromkeys(names)})
        return self.restart([group]) if restart and names else []

    def restart(self, groups: Iterable[str]) -> list[str]:
        roles: list[str] = []
        for group in groups:
            for role in CONSUMERS.get(group, ()):
                if role not in roles:
                    roles.append(role)
        order = [role for role in ("daemon", "merger", "assistant", "api") if role in roles]
        names: list[str] = []
        for role in order:
            if role == "merger":
                names.extend(self._mergers())
            else:
                names.append(DEPLOYMENTS[role])
        stamp = self._now().isoformat().replace("+00:00", "Z")
        body = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: stamp}}}}}
        for name in names:
            try:
                self.apps.patch_namespaced_deployment(
                    name=name, namespace=self.namespace, body=body, _request_timeout=_TIMEOUT
                )
            except Exception as exc:
                if _status(exc) == 404:
                    continue  # not deployed (e.g. no merger yet): nothing to restart
                code = "restart_forbidden" if _status(exc) == 403 else "restart_failed"
                raise SecretStoreError(
                    code,
                    f"the value was stored but Deployment {name} could not be restarted; "
                    f"restart it by hand (kubectl rollout restart deployment/{name})",
                    503,
                ) from None
        return names

    def _mergers(self) -> list[str]:
        try:
            listing = self.apps.list_namespaced_deployment(
                namespace=self.namespace, label_selector=MERGER_SELECTOR, _request_timeout=_TIMEOUT
            )
        except Exception:
            raise SecretStoreError(
                "restart_failed", "the merger Deployments could not be listed", 503
            ) from None
        return sorted(item.metadata.name for item in listing.items)
