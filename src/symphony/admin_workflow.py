"""Reviewed, namespace-scoped WORKFLOW.md publication through ConfigMap CAS.

Never resolve credentials here. The document stays server-side; only allowlisted
form values and safe diagnostics cross the administrative boundary.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .config import (
    GITHUB_LOGIN,
    TELEMETRY_CAPTURE_MODES,
    TRACKER_KINDS,
    Config,
    parse_workflow,
)
from .prompt import validate_template

_REQUEST_TIMEOUT = (3, 10)
_MAX_DOCUMENT = 512 * 1024
_MAX_PATCH = 256 * 1024
_MAX_TEXT = 128 * 1024
_MAX_ITEMS = 256
_MAX_INTEGER = 2**53 - 1
_COMMAND_ANNOTATION = "symphony.dev/admin-command"
_HASH_ANNOTATION = "symphony.dev/admin-document-hash"
_REFERENCE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\Z")
_DNS_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\Z")
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]*\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_BRANCH_BAD = re.compile(r"(?:\.\.|[~^:?*\[\\])")
_QUANTITY = re.compile(
    r"(?:[1-9]\d*(?:\.\d+)?|0\.\d*[1-9]\d*)(?:[EPTGMK]i|[EPTGMk]|m|[eE][+-]?\d+)?\Z"
)
_CREDENTIAL = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"lin_api_[A-Za-z0-9]{12,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,})|"
    r"[a-z][a-z0-9+.-]*://[^\s/@]+@|"
    r"\bBearer\s+(?!\$)[A-Za-z0-9_.+/-]{8,}|"
    # A value of 8+ characters: shorter text after `password=` or `Authorization:` is prose or
    # shell (`sed 's/^password=//p'`, `Authorization: Bearer $TOKEN`), not a credential.
    r"\b(?:api[_-]?key|(?:access[_-]?)?token|password|authorization)\s*[:=]\s*"
    r"[\"']?(?!(?:\$|\{\{))[A-Za-z0-9_+/.-]{8,}",
    re.IGNORECASE,
)


class WorkflowError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        diagnostics: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.diagnostics = diagnostics or []


@dataclass(frozen=True)
class _Field:
    attribute: str
    kind: str = "text"
    minimum: int | float = 1
    optional: bool = False


# Config is the source of defaults. These are paths/types, not preview values.
_FIELDS = {
    "tracker.kind": _Field("tracker_kind"),
    "tracker.provider.project_slug": _Field("provider.project_slug", optional=True),
    "tracker.provider.team_key": _Field("provider.team_key", optional=True),
    "tracker.provider.assignee": _Field("provider.assignee", optional=True),
    "tracker.provider.api_key": _Field("provider.api_key", "secret", optional=True),
    "tracker.provider.owner": _Field("provider.owner", optional=True),
    "tracker.provider.project_number": _Field("provider.project_number", "integer", optional=True),
    "tracker.provider.status_field": _Field("provider.status_field", optional=True),
    "tracker.active_states": _Field("active_states", "json"),
    "tracker.terminal_states": _Field("terminal_states", "json"),
    "tracker.required_labels": _Field("required_labels", "json"),
    "tracker.states": _Field("tracker_states", "json"),
    "tracker.investigation_label": _Field("investigation_label"),
    "polling.interval_ms": _Field("polling_interval_ms", "integer"),
    "agent.max_concurrent_agents": _Field("max_concurrent_agents", "integer"),
    "agent.max_concurrent_agents_by_state": _Field("max_concurrent_agents_by_state", "json"),
    "agent.max_turns": _Field("max_turns", "integer"),
    "agent.max_retry_backoff_ms": _Field("max_retry_backoff_ms", "integer"),
    "agent.stall_timeout_ms": _Field("stall_timeout_ms", "integer", -_MAX_INTEGER),
    "agent.input_timeout_ms": _Field("input_timeout_ms", "integer"),
    "engine.command": _Field("engine_command"),
    "engine.provider": _Field("engine_provider", optional=True),
    "engine.model": _Field("engine_model", optional=True),
    "engine.thinking": _Field("engine_thinking", optional=True),
    "engine.turn_timeout_ms": _Field("turn_timeout_ms", "integer"),
    "engine.read_timeout_ms": _Field("read_timeout_ms", "integer"),
    "engine.auto_retry": _Field("auto_retry", "boolean"),
    "kubernetes.app_name": _Field("app_name"),
    "kubernetes.namespace": _Field("namespace", optional=True),
    "kubernetes.agent_runtime": _Field("agent_runtime"),
    "kubernetes.agent_images": _Field("agent_images", "json"),
    "kubernetes.base_branches": _Field("base_branches", "json"),
    "kubernetes.repo_runtimes": _Field("repo_runtimes", "json"),
    "kubernetes.components": _Field("components", "json"),
    "kubernetes.agent_image": _Field("agent_image", optional=True),
    "kubernetes.sandbox.cpu": _Field("sandbox.cpu", "number", 0.001),
    "kubernetes.sandbox.memory_mib": _Field("sandbox.memory_mib", "integer"),
    "kubernetes.sandbox.timeout_s": _Field("sandbox.timeout_s", "integer"),
    "kubernetes.sandbox.idle_timeout_s": _Field("sandbox.idle_timeout_s", "integer"),
    "kubernetes.runner.timeout_s": _Field("runner.timeout_s", "integer"),
    "kubernetes.runner.wallclock_guard_s": _Field("runner.wallclock_guard_s", "integer"),
    "kubernetes.privileged_agents": _Field("privileged_agents", "boolean"),
    "kubernetes.runtime_class": _Field("runtime_class", optional=True),
    "kubernetes.image_pull_secrets": _Field("image_pull_secrets", "json"),
    "kubernetes.egress_allowlist": _Field("egress_allowlist", "json"),
    "workspace.root": _Field("workspace_root"),
    "kubernetes.default_repo": _Field("default_repo", optional=True),
    "kubernetes.sessions_enabled": _Field("sessions_enabled", "boolean"),
    "kubernetes.storage_class": _Field("storage_class", optional=True),
    "kubernetes.session_volume_size": _Field("session_volume_size"),
    "kubernetes.redispatch_cooldown_s": _Field("redispatch_cooldown_s", "integer", 0),
    "hooks.timeout_ms": _Field("hooks_timeout_ms", "integer"),
    "hooks.after_create": _Field("hooks.after_create", optional=True),
    "hooks.before_run": _Field("hooks.before_run", optional=True),
    "hooks.after_run": _Field("hooks.after_run", optional=True),
    "hooks.before_remove": _Field("hooks.before_remove", optional=True),
    "prompt_body": _Field("prompt_body", optional=True),
    "telemetry.enabled": _Field("telemetry_enabled", "boolean"),
    "telemetry.endpoint": _Field("telemetry_endpoint", optional=True),
    "telemetry.capture_content": _Field("telemetry_capture_content"),
    "review_watch.enabled": _Field("review_watch_enabled", "boolean"),
    "review_watch.interval_s": _Field("review_watch_interval_s", "integer"),
    "review_watch.states": _Field("review_watch_states", "json"),
    "review_watch.answer_max_turns": _Field("answer_max_turns", "integer"),
    "review_watch.answer_prompt": _Field("answer_prompt", optional=True),
    "notify.users": _Field("notify_users", "json"),
    "assistant.enabled": _Field("assistant_enabled", "boolean"),
    "assistant.slack.channels": _Field("assistant_slack_channels", "json"),
    "assistant.discord.channels": _Field("assistant_discord_channels", "json"),
    "assistant.repositories": _Field("assistant_repositories", "json"),
    "assistant.sandbox.enabled": _Field("assistant_sandbox_enabled", "boolean"),
    "kubernetes.learning.enabled": _Field("learning_enabled", "boolean"),
    "kubernetes.learning.reflect_on": _Field("reflect_on", "json"),
    "kubernetes.learning.agents_md_budget_lines": _Field("agents_md_budget_lines", "integer"),
    "kubernetes.learning.learnings_label": _Field("learnings_label"),
    "kubernetes.learning.decay_after_incorporations": _Field(
        "decay_after_incorporations", "integer"
    ),
}


def _invalid(key: str, message: str) -> WorkflowError:
    return WorkflowError(
        "invalid_workflow",
        "The proposed workflow is invalid.",
        422,
        [{"key": key, "message": message}],
    )


def _digest(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _at(data: Mapping, path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def _effective(cfg: Config, field: _Field) -> Any:
    root, _, rest = field.attribute.partition(".")
    value = getattr(cfg, root)
    return _at(value, rest) if rest else value


def _secret_strings(
    data: Any,
    *,
    sensitive_parent: bool = False,
    path: tuple[object, ...] = (),
) -> set[str]:
    """Track credential values, distinguishing Kubernetes object references."""
    found: set[str] = set()
    if isinstance(data, str):
        if sensitive_parent and data and not _REFERENCE.fullmatch(data):
            found.add(data)
    elif isinstance(data, Mapping):
        for key, value in data.items():
            child_path = (*path, key)
            if child_path == ("kubernetes", "image_pull_secrets"):
                continue  # Names of Secret objects, never their credential values.
            sensitive = sensitive_parent or (
                isinstance(key, str)
                and bool(
                    re.search(
                        r"(?:api[_-]?key|private[_-]?key|token|password|secret|credential|authorization)",
                        key,
                        re.I,
                    )
                )
            )
            found.update(_secret_strings(value, sensitive_parent=sensitive, path=child_path))
    elif isinstance(data, (list, tuple)):
        for value in data:
            found.update(_secret_strings(value, sensitive_parent=sensitive_parent, path=path))
    return found


def _sensitive(value: Any, secrets: set[str]) -> bool:
    if isinstance(value, str):
        return bool(_CREDENTIAL.search(value)) or any(secret in value for secret in secrets)
    if isinstance(value, Mapping):
        return any(
            _sensitive(key, secrets) or _sensitive(item, secrets) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_sensitive(item, secrets) for item in value)
    return False


def _check_value(key: str, value: Any, namespace: str, *, submitted: bool) -> Any:
    field = _FIELDS[key]
    if field.kind in ("integer", "number"):
        if field.optional and value in (None, ""):
            return ""  # unset; the patch writes null
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < field.minimum
            or value > _MAX_INTEGER
            or not math.isfinite(value)
            or (field.kind == "integer" and int(value) != value)
        ):
            raise _invalid(key, "Use a finite number within this field's allowed range.")
        return int(value) if field.kind == "integer" else value
    if field.kind == "boolean":
        if not isinstance(value, bool):
            raise _invalid(key, "Use a boolean value.")
        return value
    if field.kind == "json":
        if submitted:
            if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_TEXT:
                raise _invalid(key, "Use bounded JSON text.")
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                raise _invalid(key, "Use valid JSON.") from None
        if key in ("kubernetes.learning.reflect_on", "assistant.repositories") and value is None:
            return None
        if key in ("agent.max_concurrent_agents_by_state", "kubernetes.agent_images"):
            if not isinstance(value, Mapping) or len(value) > _MAX_ITEMS:
                raise _invalid(key, "Use a bounded JSON object.")
            normalized: set[str] = set()
            for name, item in value.items():
                if not isinstance(name, str) or not name.strip() or len(name) > 253:
                    raise _invalid(key, "Map names must be nonempty bounded strings.")
                if key == "agent.max_concurrent_agents_by_state":
                    if (
                        isinstance(item, bool)
                        or not isinstance(item, int)
                        or not 0 < item <= _MAX_INTEGER
                        or name.strip().casefold() in normalized
                    ):
                        raise _invalid(
                            key, "Use positive integer limits and distinct normalized state names."
                        )
                    normalized.add(name.strip().casefold())
                elif not isinstance(item, str) or not _IMAGE.fullmatch(item) or "://" in item:
                    raise _invalid(key, "Use image references without URLs or credentials.")
            return value
        if key == "kubernetes.base_branches":
            if not isinstance(value, Mapping) or len(value) > _MAX_ITEMS:
                raise _invalid(key, "Use a bounded JSON object.")
            for repo, branch in value.items():
                if not isinstance(repo, str) or not _REPO.fullmatch(repo):
                    raise _invalid(key, "Keys must be owner/name repositories.")
                if (
                    not isinstance(branch, str)
                    or not branch
                    or len(branch) > 255
                    or " " in branch
                    or branch.startswith("-")
                    or branch.endswith("/")
                    or branch.endswith(".lock")
                    or _BRANCH_BAD.search(branch)
                ):
                    raise _invalid(key, "Values must be valid git branch names.")
            return {k.lower(): v for k, v in value.items()}
        if key == "kubernetes.repo_runtimes":
            if not isinstance(value, Mapping) or len(value) > _MAX_ITEMS:
                raise _invalid(key, "Use a bounded JSON object.")
            for repo, runtime in value.items():
                if not isinstance(repo, str) or not _REPO.fullmatch(repo):
                    raise _invalid(key, "Keys must be owner/name repositories.")
                if not isinstance(runtime, str) or not runtime.strip() or len(runtime) > 63 or not _LABEL.fullmatch(runtime):
                    raise _invalid(key, "Values must be valid runtime names.")
            return {k.lower(): v for k, v in value.items()}
        if key == "kubernetes.components":
            if not isinstance(value, Mapping) or len(value) > _MAX_ITEMS:
                raise _invalid(key, "Use a bounded JSON object.")
            _COMP_NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')
            _COMP_PATH = re.compile(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*\Z')
            for repo, comps in value.items():
                if not isinstance(repo, str) or not _REPO.fullmatch(repo):
                    raise _invalid(key, "Keys must be owner/name repositories.")
                if not isinstance(comps, Mapping) or len(comps) > 64:
                    raise _invalid(key, "Each repository's components must be a bounded mapping.")
                for cname, cdef in comps.items():
                    if not isinstance(cname, str) or not _COMP_NAME.fullmatch(cname):
                        raise _invalid(key, f"Component name {cname!r} must match [a-z0-9][a-z0-9-]*.")
                    if not isinstance(cdef, Mapping):
                        raise _invalid(key, f"Component {cname!r} must be a mapping with 'path'.")
                    cpath = cdef.get('path')
                    if not isinstance(cpath, str) or not cpath or '..' in cpath or cpath.startswith('/'):
                        raise _invalid(key, f"Component {cname!r} path must be relative without '..'.")
                    if not _COMP_PATH.fullmatch(cpath):
                        raise _invalid(key, f"Component {cname!r} path is invalid.")
                    crt = cdef.get('runtime')
                    if crt is not None and (not isinstance(crt, str) or not crt.strip() or len(crt) > 63):
                        raise _invalid(key, f"Component {cname!r} runtime must be a string.")
                    unknown = set(cdef) - {'path', 'runtime'}
                    if unknown:
                        raise _invalid(key, f"Component {cname!r} has unknown keys: {', '.join(sorted(unknown))}.")
            return {k.lower(): v for k, v in value.items()}
        if key == "notify.users":
            # service (`slack`, `discord`) -> GitHub login -> that service's user id.
            if not isinstance(value, Mapping) or not set(value) <= {"slack", "discord"}:
                raise _invalid(key, "Use a JSON object with `slack` and `discord` maps.")
            return {service: _string_map(key, section) for service, section in value.items()}
        if key == "tracker.states":
            # role -> Linear state name.
            return _string_map(key, value)
        if (
            not isinstance(value, (list, tuple))
            or len(value) > _MAX_ITEMS
            or not all(isinstance(item, str) and len(item) <= 4096 for item in value)
        ):
            raise _invalid(key, "Use a bounded JSON array of strings.")
        if key == "kubernetes.image_pull_secrets" and not all(
            len(item) <= 253 and _DNS_NAME.fullmatch(item) for item in value
        ):
            raise _invalid(key, "Use Kubernetes Secret names, never credential values.")
        return list(value)
    if value is None and not submitted and field.optional:
        value = ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_TEXT or "\x00" in value:
        raise _invalid(key, "Use bounded text without NUL characters.")
    if not field.optional and not value.strip():
        raise _invalid(key, "A nonempty value is required.")
    if field.kind == "secret":
        if submitted and value and not _REFERENCE.fullmatch(value):
            raise _invalid(key, "Use only an environment reference such as $LINEAR_API_KEY.")
        return value
    if key == "tracker.kind" and value not in TRACKER_KINDS:
        raise _invalid(key, "Select an installed tracker adapter.")
    if key == "tracker.provider.owner" and value and not GITHUB_LOGIN.fullmatch(value):
        raise _invalid(key, "Use the GitHub organization or user login that owns the project.")
    if key == "kubernetes.namespace" and value not in ("", namespace):
        raise _invalid(key, "Workflow namespace must remain the authorized namespace.")
    if key == "kubernetes.app_name" and (len(value) > 63 or not _LABEL.fullmatch(value)):
        raise _invalid(key, "Use a Kubernetes DNS label of at most 63 characters.")
    if (
        key in ("kubernetes.runtime_class", "kubernetes.storage_class")
        and value
        and (len(value) > 253 or not _DNS_NAME.fullmatch(value))
    ):
        raise _invalid(key, "Use a Kubernetes resource name.")
    if (
        key == "kubernetes.agent_image"
        and value
        and (not _IMAGE.fullmatch(value) or "://" in value)
    ):
        raise _invalid(key, "Use an image reference without a URL or credentials.")
    if (
        key == "kubernetes.default_repo"
        and value
        and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
    ):
        raise _invalid(key, "Use owner/name without a URL or credentials.")
    if key == "kubernetes.session_volume_size" and not _QUANTITY.fullmatch(value):
        raise _invalid(key, "Use a positive Kubernetes storage quantity.")
    if key == "telemetry.capture_content" and value not in TELEMETRY_CAPTURE_MODES:
        raise _invalid(key, "Select a supported content capture mode.")
    if key == "telemetry.endpoint" and value:
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in ("http", "https")
                and parsed.hostname
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            )
        except ValueError:
            valid = False
        if not valid:
            raise _invalid(
                key,
                "Use an HTTP(S) collector URL with no credentials, query, or fragment.",
            )
    return value


def _string_map(key: str, value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > _MAX_ITEMS or not all(
        isinstance(name, str) and 0 < len(name.strip()) <= 253
        and isinstance(item, str) and 0 < len(item.strip()) <= 253
        for name, item in value.items()
    ):
        raise _invalid(key, "Use a bounded JSON object of nonempty strings.")
    return dict(value)


@dataclass
class _Document:
    text: str
    yaml: YAML
    data: CommentedMap
    header: str
    footer: str
    body: str
    cfg: Config


def _load(text: str) -> _Document:
    try:
        valid_size = isinstance(text, str) and len(text.encode("utf-8")) <= _MAX_DOCUMENT
    except UnicodeError:
        valid_size = False
    if not valid_size:
        raise _invalid(
            "workflow", "Workflow document is missing, invalid UTF-8, or exceeds the size limit."
        )
    opening = re.match(r"\A---[ \t]*(?:\r?\n)", text)
    closing = (
        re.search(r"^---[ \t]*(?:\r?\n|$)", text[opening.end() :], re.MULTILINE)
        if opening
        else None
    )
    if not opening or not closing:
        raise _invalid("workflow", "Use YAML front matter followed by a Liquid body.")
    start = opening.end()
    end = start + closing.start()
    body_start = start + closing.end()
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.allow_duplicate_keys = False
    yaml.width = 4096
    try:
        data = yaml.load(text[start:end])
        if not isinstance(data, CommentedMap):
            raise ValueError("mapping required")
        # YAML aliases can cause a patch of one path to mutate unknown policy at
        # another path. Reject shared containers instead of silently broadening it.
        seen: set[int] = set()

        def visit(value: Any, depth: int = 0) -> None:
            if depth > 32:
                raise ValueError("deep YAML")
            if isinstance(value, (Mapping, list)):
                if id(value) in seen:
                    raise ValueError("aliased YAML container")
                seen.add(id(value))
                if len(value) > 4096:
                    raise ValueError("large YAML collection")
                for child in value.values() if isinstance(value, Mapping) else value:
                    visit(child, depth + 1)

        visit(data)
        cfg = parse_workflow(text)
    except Exception:
        # Parser messages can quote source, including literal credentials.
        raise _invalid(
            "workflow", "Workflow YAML or Liquid is invalid; inspect the policy on the host."
        ) from None
    return _Document(text, yaml, data, text[:start], text[end:body_start], text[body_start:], cfg)


def _values(document: _Document) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    redacted: list[str] = []
    secrets = _secret_strings(document.data)
    for key, field in _FIELDS.items():
        value = _effective(document.cfg, field)
        if field.kind == "secret" and value is not None and not isinstance(value, str):
            redacted.append(key)
            values[key] = ""
            continue
        if key == "prompt_body":
            value = document.body
        if (
            (field.kind == "secret" and value and not _REFERENCE.fullmatch(value))
            or _sensitive(value, secrets)
            or _sensitive(_at(document.data, key), secrets)
        ):
            redacted.append(key)
            value = ""
        elif field.kind == "json":
            try:
                value = _json(value)
            except (TypeError, ValueError, RecursionError):
                raise _invalid(
                    key, "The existing field is not valid JSON-compatible policy."
                ) from None
        elif value is None:
            value = ""
        if not isinstance(value, (str, bool, int, float)) or (
            isinstance(value, (int, float))
            and (value < -_MAX_INTEGER or value > _MAX_INTEGER or not math.isfinite(value))
        ):
            raise _invalid(key, "The existing field is not a supported form value.")
        values[key] = value
    return values, redacted


def _complete(document: _Document, namespace: str) -> None:
    for key, field in _FIELDS.items():
        value = _effective(document.cfg, field)
        # Inspect explicit input too: the core parser intentionally normalizes
        # state maps and filters invalid limits for non-admin callers.
        raw = _at(document.data, key)
        if raw is not None and key != "prompt_body":
            _check_value(key, raw, namespace, submitted=False)
        _check_value(key, value, namespace, submitted=False)
    cfg = document.cfg
    if cfg.runner["wallclock_guard_s"] >= min(cfg.runner["timeout_s"], cfg.sandbox["timeout_s"]):
        raise _invalid(
            "kubernetes.runner.wallclock_guard_s",
            "The attempt guard must precede both Job and workspace deadlines.",
        )
    if cfg.tracker_kind == "linear":
        if not (cfg.provider.get("project_slug") or cfg.provider.get("team_key")):
            raise _invalid(
                "tracker.provider.project_slug", "Linear requires a project slug or team key."
            )
        if not cfg.provider.get("api_key"):
            raise _invalid(
                "tracker.provider.api_key",
                "Linear requires a host-provisioned credential reference.",
            )
    try:
        validate_template(cfg.answer_prompt)
    except Exception:
        raise _invalid("workflow", "Runtime requires valid Liquid templates.") from None


DEFAULT_WORKFLOW_PATH = "/opt/symphony/WORKFLOW.default.md"


def default_workflow() -> str:
    """The WORKFLOW.md the image ships, used until the ConfigMap holds a published one."""
    path = os.environ.get("SYMPHONY_DEFAULT_WORKFLOW") or DEFAULT_WORKFLOW_PATH
    return Path(path).read_text(encoding="utf-8")


class WorkflowRepository:
    def __init__(self, core: Any, namespace: str, name: str = "symphony-workflow") -> None:
        if not isinstance(namespace, str) or len(namespace) > 63 or not _LABEL.fullmatch(namespace):
            raise _invalid("kubernetes.namespace", "A valid configured namespace is required.")
        self.core = core
        self.namespace = namespace
        self.name = name

    def _read_map(self) -> Any:
        """The workflow ConfigMap. A fresh install ships it empty: until a first publication
        the document is the image's default (`SYMPHONY_DEFAULT_WORKFLOW`), and publishing
        writes it."""
        try:
            config_map = self.core.read_namespaced_config_map(
                name=self.name,
                namespace=self.namespace,
                _request_timeout=_REQUEST_TIMEOUT,
            )
            if not config_map.metadata.resource_version:
                raise ValueError("missing source")
            data = dict(config_map.data or {})
            if not isinstance(data.get("WORKFLOW.md"), str):
                data["WORKFLOW.md"] = default_workflow()
                config_map.data = data
            return config_map
        except Exception:
            raise WorkflowError(
                "workflow_unavailable", "Workflow ConfigMap is unavailable.", 503
            ) from None

    def read(self) -> dict:
        config_map = self._read_map()
        document = _load(config_map.data["WORKFLOW.md"])
        values, redacted = _values(document)
        return {
            "revision": config_map.metadata.resource_version,
            "documentHash": _digest(document.text),
            "values": values,
            "redactedKeys": redacted,
            "observed": [],
        }

    def _prepare(self, config_map: Any, base_revision: str, patch: dict) -> tuple[str, dict]:
        if not isinstance(base_revision, str) or not base_revision or len(base_revision) > 256:
            raise _invalid("baseRevision", "A bounded base revision is required.")
        if base_revision != config_map.metadata.resource_version:
            raise WorkflowError(
                "workflow_conflict", "Workflow changed; reload and review the latest version.", 412
            )
        if not isinstance(patch, dict) or not patch or len(patch) > len(_FIELDS):
            raise _invalid("patch", "Provide a nonempty bounded field patch.")
        if any(not isinstance(key, str) or key not in _FIELDS for key in patch):
            raise _invalid("patch", "The patch contains an unknown field.")
        try:
            if len(_json(patch).encode("utf-8")) > _MAX_PATCH:
                raise ValueError("large patch")
        except (TypeError, ValueError, RecursionError):
            raise _invalid("patch", "Provide a bounded patch of typed form values.") from None
        document = _load(config_map.data["WORKFLOW.md"])
        body_start = len(document.text) - len(document.body)
        before, redacted = _values(document)
        secrets = _secret_strings(document.data)
        changed = False
        yaml_changed = False
        for key in sorted(patch):
            if key in redacted:
                raise _invalid(
                    key,
                    "This legacy credential-bearing field is locked; migrate it on the host first.",
                )
            value = _check_value(key, patch[key], self.namespace, submitted=True)
            if _sensitive(value, secrets):
                raise _invalid(
                    key, "Literal credentials are not accepted; use host-provisioned references."
                )
            encoded = _json(value) if _FIELDS[key].kind == "json" else value
            if encoded == before[key]:
                continue
            changed = True
            if key == "prompt_body":
                document.body = value
                continue
            yaml_changed = True
            parts = key.split(".")
            parent = document.data
            for part in parts[:-1]:
                if parent.get(part) is None:
                    parent[part] = CommentedMap()
                if not isinstance(parent[part], CommentedMap):
                    raise _invalid(key, "The containing policy section must be a mapping.")
                parent = parent[part]
            # Empty nullable inputs restore actual runtime null semantics rather
            # than leaving an empty assignee filter or an empty storage class.
            if (
                value == ""
                and _FIELDS[key].optional
                and key not in ("review_watch.answer_prompt", "engine.provider", "engine.model")
                and not key.startswith("hooks.")
            ):
                value = None
            parent[parts[-1]] = value
        if not changed:
            raise _invalid("patch", "The patch does not change any effective policy values.")
        if yaml_changed:
            stream = io.StringIO()
            try:
                document.yaml.dump(document.data, stream)
            except Exception:
                raise _invalid(
                    "workflow", "The proposed YAML document cannot be serialized safely."
                ) from None
            text = document.header + stream.getvalue() + document.footer + document.body
        else:
            text = document.text[:body_start] + document.body
        reconstructed = _load(text)
        _complete(reconstructed, self.namespace)
        after, after_redacted = _values(reconstructed)
        if any(key in after_redacted for key in patch):
            raise _invalid("patch", "The patch introduces credential-bearing content.")
        diff = [
            {"key": key, "before": before[key], "after": after[key]}
            for key in sorted(patch)
            if before[key] != after[key]
        ]
        if not diff:
            raise _invalid("patch", "The patch does not change any effective policy values.")
        return text, {"baseRevision": base_revision, "documentHash": _digest(text), "diff": diff}

    def validate(self, base_revision: str, patch: dict) -> dict:
        _, review = self._prepare(self._read_map(), base_revision, patch)
        return review

    def publish(self, payload: dict, command_id: str, *, before_write: Callable[[], None]) -> str:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"baseRevision", "patch", "documentHash"}
            or not isinstance(payload.get("documentHash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["documentHash"])
            or not isinstance(command_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", command_id)
        ):
            raise _invalid("workflow", "A valid reviewed command is required.")
        config_map = self._read_map()
        annotations = config_map.metadata.annotations or {}
        if annotations.get(_COMMAND_ANNOTATION) == command_id:
            if (
                annotations.get(_HASH_ANNOTATION) == payload["documentHash"]
                and _digest(config_map.data["WORKFLOW.md"]) == payload["documentHash"]
            ):
                return config_map.metadata.resource_version
            raise WorkflowError(
                "workflow_conflict", "Published command content no longer matches its review.", 412
            )
        text, review = self._prepare(config_map, payload["baseRevision"], payload["patch"])
        if review["documentHash"] != payload["documentHash"]:
            raise WorkflowError(
                "workflow_conflict", "Reviewed content changed; validate and review again.", 412
            )
        replacement = copy.deepcopy(config_map)
        replacement.data["WORKFLOW.md"] = text
        replacement.metadata.annotations = dict(annotations)
        replacement.metadata.annotations.update(
            {_COMMAND_ANNOTATION: command_id, _HASH_ANNOTATION: review["documentHash"]}
        )
        before_write()
        try:
            result = self.core.replace_namespaced_config_map(
                name=self.name,
                namespace=self.namespace,
                body=replacement,
                _request_timeout=_REQUEST_TIMEOUT,
            )
            if not result.metadata.resource_version:
                raise ValueError("missing revision")
            return result.metadata.resource_version
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 409:
                raise WorkflowError(
                    "workflow_conflict", "Workflow changed during publication; review again.", 412
                ) from None
            if status == 422:
                raise _invalid("workflow", "Kubernetes rejected the proposed ConfigMap.") from None
            raise WorkflowError(
                "workflow_unavailable", "Workflow publication could not be confirmed.", 503
            ) from None
