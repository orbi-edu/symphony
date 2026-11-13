"""First-run setup: what is still missing, and admin actions that verify, then store.

`GET /v1/setup` (viewer) → `{complete, mode, steps: [{id, title, status, required, detail,
error}]}` with `status` ∈ `done|todo|error|skipped`, `error` = `{code, message}` or null, and
`complete` = every required step is `done`. Statuses are computed from the running
configuration (process env, the published WORKFLOW, Redis) and the real services, never from
stored flags; vendor verifications are cached for `CACHE_S` and dropped by every write.

| step            | required | `detail`                                                                  |
|-----------------|----------|---------------------------------------------------------------------------|
| `tracker`       | yes      | `{kind, team_key, project_slug, owner, project_number, status_field, credential, states}` |
| `github_app`    | yes      | `{configured, repositories, missing_permissions}`                          |
| `repositories`  | yes      | `{default_repo, repositories, mergers, without_merger}`                    |
| `models`        | yes      | `{roles: [{role, provider, model, credential: "stored"|"host"|null}]}`     |
| `notifications` | no       | `{slack: {configured, last_test}, discord: {configured, last_test}}`       |
| `assistant`     | no       | `{enabled, running, heartbeat_at, threads, repositories, slack: {configured, connected, channels}, discord: {configured, connected, channels, intent}}` |
| `webhooks`      | no       | `{linear: {secret, in_use, last_delivery_at}, github: {…}}`                |

Actions (admin; each verifies first and refuses to store a value that fails, audited as
`setup.<step>`; credentials are write-only and never returned):

- `PUT /v1/setup/tracker` `{kind: "linear", api_key, team_key | project_slug}` or
  `{kind: "github", owner, project_number, status_field?}` → `{kind, states, warnings,
  restarted}`; publishes the WORKFLOW tracker section directly (no daemon command: a fresh
  install has no working daemon yet).
- `PUT /v1/setup/github-app` `{app_id, installation_id, private_key}` → `{repositories,
  missing_permissions, restarted}`.
- `PUT /v1/setup/repositories` `{default_repo: "owner/name" | null}` → `{default_repo,
  mergers, without_merger}`.
- `PUT /v1/setup/notifications` `{slack?, discord?}`: a channel that is absent is unchanged,
  `null` removes it; `slack` is `{bot_token, channel}` or `{webhook_url}`, `discord` is
  `{bot_token, channel_id}` or `{webhook_url}`. Each new channel gets a test message first.
- `PUT /v1/setup/assistant` `{slack?: {app_token?, channels}, discord?: {channels},
  repositories?: ["owner/name"] | null}` → `{slack: {connected, channels} | null, discord:
  {connected, channels, intent} | null, repositories, restarted}`. Slack checks the app
  token with `apps.connections.open` (the socket is not opened) and the stored bot token
  with `auth.test`; Discord checks the stored bot token with `GET /applications/@me` and its
  message-content intent flag. A missing `app_token` reuses the stored one. Then
  `SLACK_APP_TOKEN` is stored, WORKFLOW `assistant:` is published (`enabled: true`, the
  channels of each transport; a transport not sent gets no channels), and
  `symphony-assistant` restarts. The step's `running`, `connected` and `threads` come from
  the assistant's heartbeat (`sym:<ns>:assistant:heartbeat`).
- `POST /v1/setup/notifications/test` → `{results: {slack?: {ok, error}, discord?: …}}`.
- `POST /v1/setup/webhooks` `{rotate?}` → once, with `Cache-Control: no-store`: `{linear:
  {url, secret}, github: {url, secret, events}, restarted}`; 409 `webhooks_exist` without
  `rotate` when secrets are already set (they cannot be shown again).

In Kubernetes a stored credential reaches the components through a restart of the
Deployments that read it (`setup_secrets`); `restarted` names them, and the status reflects
the new value once the API itself has restarted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import metrics, wake
from ..admin_auth import Caller, token_active
from ..admin_workflow import WorkflowError
from ..endpoints import public_url
from ..setup_secrets import SecretStoreError
from . import setup_checks as checks
from .auth import Authenticator
from .control import Control
from .errors import ApiError

CACHE_S = 30.0
STEPS = (
    ("tracker", "Tracker", True),
    ("github_app", "GitHub App", True),
    ("repositories", "Repositories", True),
    ("models", "Models", True),
    ("notifications", "Notifications", False),
    ("assistant", "Assistant", False),
    ("webhooks", "Webhooks", False),
)
GITHUB_EVENTS = (
    "pull_request", "pull_request_review", "pull_request_review_comment", "issue_comment",
    "check_suite", "check_run", "workflow_run", "push",
)
# Host credentials (Secret `model-provider`) that serve an omp built-in provider. A provider
# missing here counts only when its key is stored in `symphony-providers`.
HOST_CREDENTIALS = {
    "amazon-bedrock": ("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_REGION"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google-vertex": ("GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
}
_REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+\Z")


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _error(exc: checks.SetupError | ApiError | SecretStoreError) -> dict:
    return {"code": exc.code, "message": exc.message}


def _as_api(exc: checks.SetupError | SecretStoreError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


class SetupService:
    def __init__(self, control: Control, *, http: httpx.Client | None = None) -> None:
        self.control = control
        self.s = control.s
        self.http = http or httpx.Client(timeout=checks.TIMEOUT_S)
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    # -- shared --------------------------------------------------------------------
    def _key(self, kind: str) -> str:
        return f"sym:{self.s.namespace}:setup:{kind}"

    def _cached(self, label: str, inputs: tuple, compute):
        """A vendor verification, reused for CACHE_S. The key is a digest of the inputs,
        so a credential is never kept beyond this process's memory of the result."""
        digest = label + ":" + hashlib.sha256(json.dumps(inputs).encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(digest)
            if hit and now - hit[0] < CACHE_S:
                if isinstance(hit[1], checks.SetupError):
                    raise hit[1]
                return hit[1]
        try:
            value = compute()
        except checks.SetupError as exc:
            if exc.status < 500:
                with self._lock:
                    self._cache[digest] = (now, exc)
            raise
        with self._lock:
            self._cache[digest] = (now, value)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _store(self):
        if self.s.secrets is None:
            raise ApiError(503, "secrets_unavailable", "the integration secret store is not wired")
        return self.s.secrets

    def _values(self) -> dict:
        if self.s.workflow is None:
            raise ApiError(503, "workflow_unavailable", "the workflow document is not reachable")
        return self.s.workflow.read()["values"]

    def _app_env(self) -> tuple[str, str, str]:
        return (_env("GITHUB_APP_ID"), _env("GITHUB_APP_INSTALLATION_ID"),
                os.environ.get("GITHUB_APP_PRIVATE_KEY") or "")

    def _app(self, *, projects: bool) -> dict:
        app_id, installation, key = self._app_env()
        return self._cached(
            "github_app", (app_id, installation, key, projects),
            lambda: checks.verify_github_app(self.http, app_id, installation, key, projects=projects),
        )

    # -- status ----------------------------------------------------------------------
    def status(self) -> dict:
        try:
            values = self._values()
            workflow_error = None
        except (ApiError, WorkflowError) as exc:
            values, workflow_error = {}, {"code": exc.code, "message": exc.message}
        kind = values.get("tracker.kind") or None
        steps = {
            "tracker": self._tracker_step(values, kind),
            "github_app": self._github_app_step(kind),
            "repositories": self._repositories_step(values),
            "models": self._models_step(),
            "notifications": self._notifications_step(),
            "assistant": self._assistant_step(values),
            "webhooks": self._webhooks_step(kind),
        }
        if workflow_error:
            for step in ("tracker", "repositories"):
                steps[step] = {"status": "error", "detail": steps[step]["detail"],
                               "error": workflow_error}
        out = []
        for step_id, title, required in STEPS:
            out.append({"id": step_id, "title": title, "required": required, **steps[step_id]})
        mode = getattr(self.s.secrets, "mode", "kubernetes")
        complete = all(step["status"] == "done" for step in out if step["required"])
        result = {"complete": complete, "mode": mode, "steps": out}
        # `symphony_setup_step` reads the last status (statuses only, never a detail).
        metrics.record_setup(self.s.client, self.s.namespace, result, self.s.now_ms())
        return result

    @staticmethod
    def _step(status: str, detail: dict, error: dict | None = None) -> dict:
        return {"status": status, "detail": detail, "error": error}

    def _tracker_step(self, values: dict, kind: str | None) -> dict:
        detail = {
            "kind": kind if kind in ("linear", "github") else None,
            "team_key": values.get("tracker.provider.team_key") or None,
            "project_slug": values.get("tracker.provider.project_slug") or None,
            "owner": values.get("tracker.provider.owner") or None,
            "project_number": values.get("tracker.provider.project_number") or None,
            "status_field": values.get("tracker.provider.status_field") or None,
            "credential": False,
            "states": [],
        }
        try:
            if kind == "linear":
                key = _env("LINEAR_API_KEY")
                detail["credential"] = bool(key)
                if not key or not (detail["team_key"] or detail["project_slug"]):
                    return self._step("todo", detail)
                found = self._cached(
                    "linear", (key, detail["team_key"], detail["project_slug"]),
                    lambda: checks.verify_linear(
                        self.http, key, team_key=detail["team_key"],
                        project_slug=detail["project_slug"],
                    ),
                )
            elif kind == "github":
                app = self._app_env()
                detail["credential"] = all(app)
                if not all(app) or not (detail["owner"] and detail["project_number"]):
                    return self._step("todo", detail)
                token = self._app(projects=True)["token"]
                field = detail["status_field"] or "Status"
                found = self._cached(
                    "github_project", (token[:8], detail["owner"], detail["project_number"], field),
                    lambda: checks.verify_github_project(
                        self.http, token, detail["owner"], int(detail["project_number"]), field
                    ),
                )
            else:
                return self._step("todo", detail)
        except checks.SetupError as exc:
            return self._step("error", detail, _error(exc))
        detail["states"] = found["states"]
        return self._step("done", detail)

    def _github_app_step(self, kind: str | None) -> dict:
        detail = {"configured": all(self._app_env()), "repositories": [], "missing_permissions": []}
        if not detail["configured"]:
            return self._step("todo", detail)
        try:
            app = self._app(projects=kind == "github")
        except checks.SetupError as exc:
            return self._step("error", detail, _error(exc))
        detail["repositories"] = app["repositories"]
        detail["missing_permissions"] = app["missing_permissions"]
        if app["missing_permissions"]:
            return self._step("error", detail, {
                "code": "github_permissions_missing",
                "message": "the GitHub App installation lacks: "
                + ", ".join(app["missing_permissions"]),
            })
        return self._step("done", detail)

    def _repositories_step(self, values: dict) -> dict:
        default = (values.get("kubernetes.default_repo") or "").lower() or None
        tracked = {
            str(entry["target_repo"]).lower()
            for _, entry in self.s.reader.issue_entries()
            if isinstance(entry.get("target_repo"), str) and _REPO.fullmatch(entry["target_repo"])
        }
        repositories = sorted(tracked | ({default} if default else set()))
        # A merger counts only while it beats (`MergerStore.alive`), not merely registered.
        mergers = (
            sorted(r for r in self.s.merger.repos() if self.s.merger.store(r).alive())
            if self.s.merger is not None else []
        )
        kubernetes = getattr(self.s.secrets, "mode", "kubernetes") == "kubernetes"
        without = [repo for repo in repositories if repo not in mergers] if kubernetes else []
        detail = {"default_repo": default, "repositories": repositories, "mergers": mergers,
                  "without_merger": without}
        if not repositories:
            return self._step("todo", detail)
        if without:
            return self._step("error", detail, {
                "code": "merger_missing",
                "message": "no merger is registered for " + ", ".join(without),
            })
        return self._step("done", detail)

    def _models_step(self) -> dict:
        models = self.s.teamconfig.get()["models"]
        stored: set[str] = set()
        if self.s.providers is not None:
            try:
                stored = {
                    item["id"] for item in self.s.providers.read()["items"]
                    if item.get("credentialConfigured") or item.get("auth") == "none"
                }
            except Exception:  # noqa: BLE001 — unreadable storage counts as nothing stored
                stored = set()
        host_cache: dict[str, bool] = {}

        def host(provider: str) -> bool:
            if provider not in host_cache:
                names = HOST_CREDENTIALS.get(provider, ())
                try:
                    host_cache[provider] = any(
                        self.s.secrets.has("model-provider", name) for name in names
                    ) if self.s.secrets is not None else False
                except SecretStoreError:
                    host_cache[provider] = False
            return host_cache[provider]

        roles = []
        for role, ref in models.items():
            provider = ref.get("provider")
            credential = "stored" if provider in stored else "host" if host(provider) else None
            roles.append({"role": role, "provider": provider, "model": ref.get("model"),
                          "credential": credential})
        missing = sorted({r["provider"] for r in roles if r["credential"] is None})
        if missing:
            return self._step("error", {"roles": roles}, {
                "code": "model_credential_missing",
                "message": "no credential for provider " + ", ".join(missing),
            })
        return self._step("done", {"roles": roles})

    def _last_test(self, channel: str) -> dict | None:
        raw = self.s.client.get(self._key(f"notify:{channel}"))
        return json.loads(raw) if raw else None

    def _channel_env(self, channel: str) -> dict:
        if channel == "slack":
            return {"bot_token": _env("SLACK_BOT_TOKEN"), "channel": _env("SLACK_CHANNEL"),
                    "webhook_url": _env("SLACK_WEBHOOK_URL")}
        return {"bot_token": _env("DISCORD_BOT_TOKEN"), "channel_id": _env("DISCORD_CHANNEL_ID"),
                "webhook_url": _env("DISCORD_WEBHOOK_URL")}

    def _sender(self, channel: str, config: dict):
        return (checks.slack_sender if channel == "slack" else checks.discord_sender)(config)

    def _notifications_step(self) -> dict:
        detail = {}
        for channel in ("slack", "discord"):
            detail[channel] = {
                "configured": self._sender(channel, self._channel_env(channel)) is not None,
                "last_test": self._last_test(channel),
            }
        configured = [c for c in detail if detail[c]["configured"]]
        if not configured:
            return self._step("skipped", detail)
        failed = [c for c in configured if detail[c]["last_test"] and not detail[c]["last_test"]["ok"]]
        if failed:
            return self._step("error", detail, {
                "code": "notification_failed",
                "message": "the last test message failed on " + ", ".join(failed),
            })
        if any(detail[c]["last_test"] is None for c in configured):
            return self._step("todo", detail)
        return self._step("done", detail)

    # -- assistant ---------------------------------------------------------------------
    def _heartbeat(self) -> dict:
        raw = self.s.client.get(f"sym:{self.s.namespace}:assistant:heartbeat")
        try:
            beat = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return beat if isinstance(beat, dict) else {}

    @staticmethod
    def _bot_token(name: str, transport: str) -> str:
        token = _env(name)
        if not token:
            raise checks.SetupError(
                f"{transport.lower()}_bot_token_missing",
                f"the assistant uses the {transport} bot token; set up {transport} "
                "notifications with a bot token first", 409,
            )
        return token

    def _verify_slack(self, app_token: str) -> None:
        bot = self._bot_token("SLACK_BOT_TOKEN", "Slack")

        def verify() -> bool:
            checks.verify_slack_app_token(self.http, app_token)
            checks.verify_slack_bot(self.http, bot)
            return True
        self._cached("assistant_slack", (app_token, bot), verify)

    def _verify_discord(self) -> str:
        bot = self._bot_token("DISCORD_BOT_TOKEN", "Discord")
        return self._cached("assistant_discord", (bot,),
                            lambda: checks.verify_discord_app(self.http, bot))

    @staticmethod
    def _json_value(values: dict, key: str, default: Any) -> Any:
        try:
            return json.loads(values.get(key) or "null") or default
        except ValueError:
            return default

    def _assistant_step(self, values: dict) -> dict:
        beat = self._heartbeat()
        running = beat.get("state") == "running"
        detail = {
            "enabled": values.get("assistant.enabled") is True,
            "running": running,
            "heartbeat_at": beat.get("at"),
            "threads": int(beat.get("threads") or 0) if running else 0,
            "repositories": self._json_value(values, "assistant.repositories", None),
            "slack": {"configured": bool(_env("SLACK_APP_TOKEN") and _env("SLACK_BOT_TOKEN")),
                      "connected": running and bool((beat.get("slack") or {}).get("connected")),
                      "channels": self._json_value(values, "assistant.slack.channels", [])},
            "discord": {"configured": bool(_env("DISCORD_BOT_TOKEN")),
                        "connected": running
                        and bool((beat.get("discord") or {}).get("connected")),
                        "channels": self._json_value(values, "assistant.discord.channels", []),
                        "intent": None},
        }
        if not detail["enabled"]:
            return self._step("skipped", detail)
        try:
            if detail["slack"]["channels"]:
                app_token = _env("SLACK_APP_TOKEN")
                if not app_token:
                    raise checks.SetupError(
                        "slack_app_token_missing", "no Slack app-level token is stored")
                self._verify_slack(app_token)
            if detail["discord"]["channels"]:
                detail["discord"]["intent"] = self._verify_discord()
        except checks.SetupError as exc:
            return self._step("error", detail, _error(exc))
        return self._step("done" if running else "todo", detail)

    def _webhooks_step(self, kind: str | None) -> dict:
        detail = {}
        for source, name in (("linear", "LINEAR_WEBHOOK_SECRET"), ("github", "GITHUB_WEBHOOK_SECRET")):
            raw = self.s.client.get(self._key(f"webhook:{source}"))
            detail[source] = {
                "secret": bool(_env(name)),
                "in_use": source == "github" or kind == "linear",
                "last_delivery_at": int(raw) if raw else None,
            }
        if not any(d["secret"] for d in detail.values()):
            return self._step("skipped", detail)
        waiting = [s for s, d in detail.items() if d["in_use"] and not (d["secret"] and d["last_delivery_at"])]
        return self._step("todo" if waiting else "done", detail)

    # -- actions ---------------------------------------------------------------------
    def _publish(self, caller: Caller, patch: dict) -> str:
        """Publish a WORKFLOW patch now (setup has no working daemon to go through)."""
        if self.s.workflow is None:
            raise ApiError(503, "workflow_unavailable", "the workflow document is not reachable")
        workflow = self.s.workflow
        current = workflow.read()
        try:
            review = workflow.validate(current["revision"], patch)
        except WorkflowError as exc:
            if any(d.get("key") == "patch" for d in exc.diagnostics):
                return current["revision"]  # already published as asked
            raise

        def before_write():
            if not token_active(self.s.tokens, caller.token_id, "admin"):
                raise ApiError(401, "invalid_token", "the token was revoked before saving")

        payload = {"baseRevision": current["revision"], "patch": patch,
                   "documentHash": review["documentHash"]}
        revision = workflow.publish(
            payload, f"setup-{uuid.uuid4().hex}", before_write=before_write
        )
        # The daemon re-reads WORKFLOW on its next tick; wake it so that tick is now.
        wake.wake(self.s.client, self.s.namespace, wake.DAEMON, "setup:workflow")
        return revision

    def _audit(self, caller: Caller, step: str, target: str) -> None:
        self.control._audit(caller, f"setup.{step}", target, f"setup: {step.replace('_', ' ')}")

    def put_tracker(self, caller: Caller, body: TrackerBody) -> dict:
        store = self._store()
        if body.kind == "linear":
            if bool(body.team_key) == bool(body.project_slug):
                raise ApiError(422, "invalid_request", "give exactly one of team_key or project_slug")
            try:
                found = checks.verify_linear(
                    self.http, body.api_key, team_key=body.team_key, project_slug=body.project_slug
                )
                store.put("linear", {"LINEAR_API_KEY": body.api_key}, restart=False)
            except (checks.SetupError, SecretStoreError) as exc:
                raise _as_api(exc) from None
            patch = {
                "tracker.kind": "linear",
                "tracker.provider.api_key": "$LINEAR_API_KEY",
                "tracker.provider.team_key": body.team_key or "",
                "tracker.provider.project_slug": body.project_slug or "",
            }
            groups, target = ["linear"], f"linear:{body.team_key or body.project_slug}"
        else:
            if not all(self._app_env()):
                raise ApiError(
                    409, "github_app_required", "set up the GitHub App before a GitHub tracker"
                )
            try:
                token = self._app(projects=True)["token"]
                found = checks.verify_github_project(
                    self.http, token, body.owner, body.project_number, body.status_field
                )
            except checks.SetupError as exc:
                raise _as_api(exc) from None
            patch = {
                "tracker.kind": "github",
                "tracker.provider.owner": body.owner,
                "tracker.provider.project_number": body.project_number,
                "tracker.provider.status_field": body.status_field,
            }
            groups, target = [], f"github:{body.owner}#{body.project_number}"
        self._publish(caller, patch)
        try:
            restarted = store.restart(groups) if groups else []
        except SecretStoreError as exc:
            raise _as_api(exc) from None
        self.invalidate()
        self._audit(caller, "tracker", target)
        return {"kind": body.kind, "states": found["states"],
                "warnings": self._state_warnings(found["states"]), "restarted": restarted}

    def _state_warnings(self, states: list[str]) -> list[dict]:
        """WORKFLOW state names the board does not have (the daemon would never see them)."""
        values = self._values()
        names: list[str] = []
        for key in ("tracker.active_states", "tracker.terminal_states"):
            try:
                names.extend(json.loads(values.get(key) or "[]"))
            except ValueError:
                pass
        try:
            names.extend(json.loads(values.get("tracker.states") or "{}").values())
        except (ValueError, AttributeError):
            pass
        known = {s.casefold() for s in states}
        missing = sorted({n for n in names if isinstance(n, str) and n.casefold() not in known})
        return [{"code": "state_missing", "state": name} for name in missing]

    def put_github_app(self, caller: Caller, body: GitHubAppBody) -> dict:
        store = self._store()
        try:
            kind = self._values().get("tracker.kind")
        except (ApiError, WorkflowError):
            kind = None
        try:
            app = checks.verify_github_app(
                self.http, body.app_id, body.installation_id, body.private_key,
                projects=kind == "github",
            )
            restarted = store.put("github-app", {
                "GITHUB_APP_ID": body.app_id.strip(),
                "GITHUB_APP_INSTALLATION_ID": body.installation_id.strip(),
                "GITHUB_APP_PRIVATE_KEY": body.private_key,
            })
        except (checks.SetupError, SecretStoreError) as exc:
            raise _as_api(exc) from None
        self.invalidate()
        self._audit(caller, "github_app", f"github-app:{body.app_id.strip()}")
        return {"repositories": app["repositories"],
                "missing_permissions": app["missing_permissions"], "restarted": restarted}

    def put_repositories(self, caller: Caller, body: RepositoriesBody) -> dict:
        default = body.default_repo.lower() if body.default_repo else None
        if default and all(self._app_env()):
            try:
                installed = self._app(projects=False)["repositories"]
            except checks.SetupError as exc:
                raise _as_api(exc) from None
            if default not in installed:
                raise ApiError(
                    422, "repo_not_installed", f"the GitHub App is not installed on {default}"
                )
        self._publish(caller, {"kubernetes.default_repo": default or ""})
        self.invalidate()
        self._audit(caller, "repositories", default or "none")
        step = self._repositories_step({"kubernetes.default_repo": default or ""})
        return {"default_repo": default, "mergers": step["detail"]["mergers"],
                "without_merger": step["detail"]["without_merger"]}

    def _record_test(self, channel: str, result: dict) -> dict:
        record = {**result, "at": int(time.time() * 1000)}
        self.s.client.set(self._key(f"notify:{channel}"), json.dumps(record))
        return record

    def put_notifications(self, caller: Caller, body: NotificationsBody) -> dict:
        store = self._store()
        changes: dict[str, dict | None] = {}
        for channel in ("slack", "discord"):
            if channel in body.model_fields_set:
                value = getattr(body, channel)
                changes[channel] = value.model_dump(exclude_none=True) if value else None
        if not changes:
            raise ApiError(422, "invalid_request", "send slack and/or discord")
        results = {}
        for channel, config in changes.items():
            if config is None:
                continue
            result = checks.send_test(channel, self._sender(channel, config))
            results[channel] = self._record_test(channel, result)
            if not result["ok"]:
                raise ApiError(
                    422, "notification_unverified",
                    f"the {channel} test message was {result['error']}; nothing was stored",
                )
        names = {"slack": ("SLACK_BOT_TOKEN", "SLACK_CHANNEL", "SLACK_WEBHOOK_URL"),
                 "discord": ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_WEBHOOK_URL")}
        try:
            for channel, config in changes.items():
                values = {}
                if config:
                    values = (
                        {"SLACK_BOT_TOKEN": config["bot_token"], "SLACK_CHANNEL": config["channel"]}
                        if channel == "slack" and "bot_token" in config
                        else {"DISCORD_BOT_TOKEN": config["bot_token"],
                              "DISCORD_CHANNEL_ID": config["channel_id"]}
                        if channel == "discord" and "bot_token" in config
                        else {names[channel][2]: config["webhook_url"]}
                    )
                stale = [n for n in names[channel] if n not in values]
                store.remove(channel, stale, restart=False)
                if values:
                    store.put(channel, values, restart=False)
                if config is None:
                    self.s.client.delete(self._key(f"notify:{channel}"))
            restarted = store.restart(list(changes))
        except SecretStoreError as exc:
            raise _as_api(exc) from None
        self._audit(caller, "notifications", ",".join(sorted(changes)))
        out: dict[str, Any] = {"restarted": restarted}
        for channel in ("slack", "discord"):
            configured = (
                changes[channel] is not None if channel in changes
                else self._sender(channel, self._channel_env(channel)) is not None
            )
            out[channel] = {"configured": configured, "last_test": results.get(channel)
                            or (self._last_test(channel) if configured else None)}
        return out

    def put_assistant(self, caller: Caller, body: AssistantBody) -> dict:
        store = self._store()
        if body.slack is None and body.discord is None:
            raise ApiError(422, "invalid_request", "send slack and/or discord")
        intent = None
        new_token = body.slack.app_token if body.slack else None
        try:
            if body.slack is not None:
                app_token = new_token or _env("SLACK_APP_TOKEN")
                if not app_token:
                    raise checks.SetupError(
                        "slack_app_token_missing", "give the Slack app-level token (xapp-…)")
                self.invalidate()
                self._verify_slack(app_token)
            if body.discord is not None:
                self.invalidate()
                intent = self._verify_discord()
            if new_token:
                store.put("slack", {"SLACK_APP_TOKEN": new_token}, restart=False)
        except (checks.SetupError, SecretStoreError) as exc:
            raise _as_api(exc) from None
        slack = body.slack.channels if body.slack else []
        discord = body.discord.channels if body.discord else []
        patch: dict[str, Any] = {
            "assistant.enabled": True,
            "assistant.slack.channels": json.dumps(slack),
            "assistant.discord.channels": json.dumps(discord),
        }
        if "repositories" in body.model_fields_set:
            repos = sorted({r.lower() for r in body.repositories}) if body.repositories else None
            patch["assistant.repositories"] = json.dumps(repos)
        self._publish(caller, patch)
        try:
            restarted = store.restart(["slack", "assistant"] if new_token else ["assistant"])
        except SecretStoreError as exc:
            raise _as_api(exc) from None
        self.invalidate()
        self._audit(caller, "assistant", ",".join(
            name for name in ("slack", "discord") if getattr(body, name) is not None))
        try:
            repositories = self._json_value(self._values(), "assistant.repositories", None)
        except (ApiError, WorkflowError):
            repositories = None
        return {
            "slack": {"connected": True, "channels": slack} if body.slack else None,
            "discord": ({"connected": True, "channels": discord, "intent": intent}
                        if body.discord else None),
            "repositories": repositories,
            "restarted": restarted,
        }

    def test_notifications(self) -> dict:
        results = {}
        for channel in ("slack", "discord"):
            send = self._sender(channel, self._channel_env(channel))
            if send is not None:
                results[channel] = self._record_test(channel, checks.send_test(channel, send))
        if not results:
            raise ApiError(409, "notifications_not_configured", "no Slack or Discord is configured")
        return {"results": results}

    def post_webhooks(self, caller: Caller, request: Request, rotate: bool) -> dict:
        store = self._store()
        try:
            exists = store.has("linear", "LINEAR_WEBHOOK_SECRET") or store.has(
                "api", "GITHUB_WEBHOOK_SECRET"
            )
        except SecretStoreError as exc:
            raise _as_api(exc) from None
        if exists and not rotate:
            raise ApiError(
                409, "webhooks_exist",
                "webhook secrets are already set and cannot be shown again; send rotate: true",
            )
        linear_secret, github_secret = secrets.token_hex(32), secrets.token_hex(32)
        try:
            store.put("linear", {"LINEAR_WEBHOOK_SECRET": linear_secret}, restart=False)
            store.put("api", {"GITHUB_WEBHOOK_SECRET": github_secret}, restart=False)
            restarted = store.restart(["linear", "api"])
        except SecretStoreError as exc:
            raise _as_api(exc) from None
        self._audit(caller, "webhooks", "rotate" if exists else "create")
        base = public_url() or str(request.base_url).rstrip("/")
        return {
            "linear": {"url": f"{base}/v1/webhooks/linear", "secret": linear_secret},
            "github": {"url": f"{base}/v1/webhooks/github", "secret": github_secret,
                       "events": list(GITHUB_EVENTS)},
            "restarted": restarted,
        }


# -- request bodies (secrets never echoed: validation errors name fields only) -------------
_Secret = Annotated[str, Field(min_length=1, max_length=16384, repr=False)]
_OptionalSecret = Annotated[str | None, Field(min_length=1, max_length=16384, repr=False)]
_WebhookUrl = Annotated[str | None, Field(pattern=r"^https?://", max_length=2048, repr=False)]


class TrackerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["linear", "github"]
    api_key: _OptionalSecret = None
    team_key: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9]{0,9}$")] | None = None
    project_slug: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    owner: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,39}$")] | None = None
    project_number: Annotated[int, Field(ge=1, le=10**9)] | None = None
    status_field: Annotated[str, Field(min_length=1, max_length=100)] = "Status"

    @model_validator(mode="after")
    def complete_for_kind(self):
        if self.kind == "linear" and not self.api_key:
            raise ValueError("api_key is required for Linear")
        if self.kind == "github" and not (self.owner and self.project_number):
            raise ValueError("owner and project_number are required for GitHub")
        return self


class GitHubAppBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_id: Annotated[str, Field(pattern=r"^\s*\d{1,12}\s*$")]
    installation_id: Annotated[str, Field(pattern=r"^\s*\d{1,15}\s*$")]
    private_key: _Secret


class RepositoriesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_repo: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")] | None = None


class SlackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_token: _OptionalSecret = None
    channel: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    webhook_url: _WebhookUrl = None

    @model_validator(mode="after")
    def one_shape(self):
        if not ((self.bot_token and self.channel) or self.webhook_url) or (
            self.webhook_url and self.bot_token
        ):
            raise ValueError("give bot_token with channel, or webhook_url")
        return self


class DiscordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bot_token: _OptionalSecret = None
    channel_id: Annotated[str, Field(pattern=r"^\d{1,20}$")] | None = None
    webhook_url: _WebhookUrl = None

    @model_validator(mode="after")
    def one_shape(self):
        if not ((self.bot_token and self.channel_id) or self.webhook_url) or (
            self.webhook_url and self.bot_token
        ):
            raise ValueError("give bot_token with channel_id, or webhook_url")
        return self


class NotificationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slack: SlackBody | None = None
    discord: DiscordBody | None = None


_SlackChannel = Annotated[str, Field(pattern=r"^[A-Z0-9]{1,32}$")]
_DiscordChannel = Annotated[str, Field(pattern=r"^\d{1,20}$")]


class AssistantSlackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_token: _OptionalSecret = None
    channels: Annotated[list[_SlackChannel], Field(max_length=100)] = []


class AssistantDiscordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: Annotated[list[_DiscordChannel], Field(max_length=100)] = []


class AssistantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slack: AssistantSlackBody | None = None
    discord: AssistantDiscordBody | None = None
    repositories: Annotated[
        list[Annotated[str, Field(pattern=r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")]],
        Field(max_length=256),
    ] | None = None


class WebhooksBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rotate: bool = False


def build_router(control: Control, auth: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/setup")
    viewer = Depends(auth.dependency("viewer"))
    admin = Depends(auth.dependency("admin"))
    setup = control.setup

    @router.get("")
    def status(_: Caller = viewer):
        return setup.status()

    @router.put("/tracker")
    def tracker(body: TrackerBody, caller: Caller = admin):
        return setup.put_tracker(caller, body)

    @router.put("/github-app")
    def github_app(body: GitHubAppBody, caller: Caller = admin):
        return setup.put_github_app(caller, body)

    @router.put("/repositories")
    def repositories(body: RepositoriesBody, caller: Caller = admin):
        return setup.put_repositories(caller, body)

    @router.put("/notifications")
    def notifications(body: NotificationsBody, caller: Caller = admin):
        return setup.put_notifications(caller, body)

    @router.put("/assistant")
    def assistant(body: AssistantBody, caller: Caller = admin):
        return setup.put_assistant(caller, body)

    @router.post("/notifications/test")
    def notifications_test(_: Caller = admin):
        return setup.test_notifications()

    @router.post("/webhooks")
    def webhooks(request: Request, body: WebhooksBody | None = None, caller: Caller = admin):
        result = setup.post_webhooks(caller, request, bool(body and body.rotate))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router
