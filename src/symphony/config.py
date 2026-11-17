"""WORKFLOW.md parsing (YAML front matter + Liquid body) — pure, no Kubernetes client.

Rules: upstream spec §6.4 defaults applied field by
field; UNKNOWN keys are ignored (interchangeability with the reference);
`$VAR` indirection resolves from HOST env vars; invalid content ⇒ ConfigError and
the caller keeps the last valid version (last-known-good, FR-019).

This port's own extension block is `kubernetes:` (the Modal original used `modal:`).
A leftover `modal:` block fails loudly rather than being ignored — silently dropping
the sandbox sizing and learning policy of a migrated deployment would be config theatre.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any

import yaml

from . import retention, teamconfig


class ConfigError(Exception):
    pass

# OMP's `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` levels: no message or tool
# content, truncated structural summaries, or verbatim content.
TELEMETRY_CAPTURE_MODES = ("none", "summary", "full")

# The Linear states the review pipeline moves a ticket through (internals §8). The
# implementer hands over to `review`; the merger writes `rework` and `merging`; `done` comes
# from the Linear↔GitHub integration when the pull request lands.
TRACKER_STATE_ROLES = ("review", "rework", "merging", "done")
DEFAULT_TRACKER_STATES = {
    "review": "In Review",
    "rework": "Rework",
    "merging": "Merging",
    "done": "Done",
}


@dataclass(frozen=True)
class Config:
    tracker_kind: str
    provider: dict = field(default_factory=dict)
    # Implementer work states. The review states (`tracker_states.review`/`merging`) are
    # deliberately absent: there the merger owns the ticket and the implementer is parked.
    active_states: tuple[str, ...] = ("Todo", "In Progress", "Rework")
    terminal_states: tuple[str, ...] = ("Closed", "Cancelled", "Canceled", "Duplicate", "Done")
    required_labels: tuple[str, ...] = ()
    # `tracker.issues` (memory tracker only): the seed board, so every process that mounts
    # the same WORKFLOW.md sees the same tickets — moving one means republishing the file.
    memory_issues: tuple[dict, ...] = ()
    tracker_states: dict = field(default_factory=lambda: dict(DEFAULT_TRACKER_STATES))
    # Tickets with this label are investigations: they end in a written report, not a PR.
    investigation_label: str = "investigation"
    # A safety poll only: webhooks wake the daemon (`wake.wait`), so this bounds the
    # latency of a MISSED webhook, not of a normal dispatch (internals §6).
    polling_interval_ms: int = 15_000
    workspace_root: str = "/workspace"
    hooks: dict = field(default_factory=dict)
    hooks_timeout_ms: int = 60_000
    max_concurrent_agents: int = 5
    max_concurrent_agents_by_state: dict = field(default_factory=dict)
    max_turns: int = 20
    max_retry_backoff_ms: int = 300_000
    # How long `symphony_ask` waits for a human before the agent proceeds on its own
    # judgment (internals §3). The run never hangs on an absent human.
    input_timeout_ms: int = 900_000
    # engine block: the agent execution engine (replaces the retired codex: block)
    engine_command: str = "omp --mode rpc"
    # Optional fallback for the role model. The published TeamConfig wins at runtime
    # (internals §4, `bind_role_model`); named with the `engine_` prefix because `provider`
    # alone is already the TRACKER provider mapping.
    engine_provider: str | None = None
    engine_model: str | None = None
    engine_thinking: str | None = None
    auto_retry: bool = True  # engine absorbs transient provider errors (research R12)
    turn_timeout_ms: int = 3_600_000  # maximum stream SILENCE, reset by every event
    read_timeout_ms: int = 5_000  # synchronous command/response
    # daemon concern, not an engine concern — lives under agent: (misfiled under codex: in 001)
    stall_timeout_ms: int = 300_000
    # kubernetes block: (extension specific to this port)
    app_name: str = "symphony"
    namespace: str | None = None  # None ⇒ the pod's own namespace (in-cluster detection)
    sandbox: dict = field(
        default_factory=lambda: {
            "cpu": 2,
            "memory_mib": 4096,
            "timeout_s": 14400,
            "idle_timeout_s": 1800,
        }
    )
    runner: dict = field(default_factory=lambda: {"timeout_s": 14400, "wallclock_guard_s": 12600})
    agent_runtime: str = "node"  # sandbox image family: node | dotnet9 | dotnet10 | polyglot (edge registry)
    # Explicit image override for the workspace pod (replaces the Modal port's
    # snapshot_image): when set, it wins over the agent_images/agent_runtime lookup.
    agent_image: str | None = None
    # runtime -> image reference; merged over the edge's built-in defaults, so an
    # operator can repoint one runtime at their own registry without listing all of them.
    agent_images: dict = field(default_factory=dict)
    # Per-repository base branch override: `owner/repo` (lowercased) → branch name.
    # When set, the merger, assistant and reflector treat this branch as the repository's
    # main branch instead of the auto-detected `origin/HEAD`.
    base_branches: dict = field(default_factory=dict)
    # Per-repository runtime override: `owner/repo` (lowercased) → runtime name.
    # When set, the workspace pod uses this runtime instead of the global agent_runtime.
    repo_runtimes: dict = field(default_factory=dict)
    # Monorepo component definitions: `owner/repo` → {component_name → {path, runtime}}.
    # Each component names a sub-tree (path) and optionally a runtime; the component:
    # label on a ticket selects it, and resolve_workspace merges the precedence chain.
    components: dict = field(default_factory=dict)
    # The workspace pod runs a container daemon only if allowed to: dockerd needs a
    # privileged container (the K8s counterpart of Modal's experimental vm_runtime).
    # Off by default — turning it on is an isolation trade-off the operator must own.
    privileged_agents: bool = False
    # RuntimeClass for the AGENT pods (workspace + distill) — the isolation upgrade
    # path. `sysbox-runc` runs dockerd WITHOUT privileged (user namespaces); a Kata
    # class gives a VM boundary (keep privileged_agents on for docker there — it is
    # contained to the guest). Never applied to orchestrator Jobs: they are trusted
    # and need no container capability.
    runtime_class: str | None = None
    storage_class: str | None = None  # for per-workspace session PVCs; None ⇒ cluster default
    session_volume_size: str = "1Gi"
    sessions_enabled: bool = True  # False ⇒ emptyDir (sessions die with the pod)
    image_pull_secrets: tuple[str, ...] = ()
    # Used ONLY when an issue carries no `repo:` designation of its own. An issue's own
    # designation always wins — a default is a convenience, never an override (FR-004).
    default_repo: str | None = None
    # Declared, not enforced: plain NetworkPolicy filters by IP, not by domain name, so
    # DNS-level egress control needs a CNI that supports it (e.g. Cilium FQDN policies).
    egress_allowlist: tuple[str, ...] = ()
    redispatch_cooldown_s: int = 900
    learning_enabled: bool = True
    reflect_on: tuple[str, ...] | None = None  # None ⇒ inherits terminal_states in full
    agents_md_budget_lines: int = 100
    learnings_label: str = "symphony-learnings"
    decay_after_incorporations: int = 10  # a lesson unused after N incorporations decays
    # Session telemetry (feature 004). Enabled by default so the review report works with no
    # operator action; `endpoint` set means "send to MY collector instead of the built-in
    # one", which trades the inline ticket summary for a full waterfall elsewhere.
    telemetry_enabled: bool = True
    telemetry_endpoint: str | None = None
    telemetry_capture_content: str = "none"
    # Review conversation watch (feature 005). Off by default: it dispatches real model work
    # from a poll, so an operator opts in rather than discovers it on a bill.
    review_watch_enabled: bool = False
    review_watch_interval_s: int = 120
    review_watch_states: tuple[str, ...] = ()  # empty ⇒ any parked (non-active, non-terminal)
    answer_max_turns: int = 3
    answer_prompt: str = ""
    # `slack` and `discord`: GitHub login (casefolded) → that service's user id, for mentions
    # in notifications (internals §8).
    notify_users: dict = field(default_factory=lambda: {"slack": {}, "discord": {}})
    # The chat assistant (`assistant:`; src/symphony/assistant). Off unless enabled. It answers
    # @mentions in these channel ids and every DM; `assistant_repositories` None means every
    # repository the tracker routes to (`default_repo`).
    assistant_enabled: bool = False
    assistant_slack_channels: tuple[str, ...] = ()
    assistant_discord_channels: tuple[str, ...] = ()
    assistant_repositories: tuple[str, ...] | None = None
    assistant_max_concurrent: int = 3
    assistant_turn_timeout_s: int = 600
    assistant_ticket_state: str = "Backlog"
    assistant_ticket_labels: tuple[str, ...] = ()
    # `assistant.soul`: the persona file in Git (`assistant/soul.py`). None repo means the first
    # `assistant.repositories` entry, else `default_repo`.
    assistant_soul_repo: str | None = None
    assistant_soul_path: str = ".symphony/assistant.md"
    # `assistant.memory`: per-person memory for people mapped in `notify.users`.
    assistant_memory_enabled: bool = True
    assistant_memory_ttl_days: int = 90
    # `retention:` how long ended sessions and audit entries stay in Redis (`retention.py`).
    retention_sessions_days: int = retention.SESSIONS_DAYS
    retention_audit_days: int = retention.AUDIT_DAYS
    # `backup:` the target of the nightly backup and archive (`backup.py`). With neither
    # `s3` ({endpoint, bucket, prefix, region}) nor `dir`, the backup CronJob idles.
    backup_s3: dict | None = None
    backup_dir: str | None = None
    backup_keep: int = 7
    prompt_body: str = ""

    @property
    def effective_reflect_on(self) -> tuple[str, ...]:
        """FR-026: every terminal issue triggers a retro — the default inherits
        terminal_states IN FULL (all spellings + Duplicate)."""
        return self.reflect_on if self.reflect_on is not None else self.terminal_states

    def base_branch(self, repo: str) -> str | None:
        """The configured base branch for `repo`, or None (auto-detect)."""
        return self.base_branches.get(repo.lower()) or None

    def repo_runtime(self, repo: str) -> str | None:
        """The configured runtime for `repo`, or None (use agent_runtime)."""
        return self.repo_runtimes.get(repo.lower()) or None



def base_branches_env(cfg: Config, repos: tuple[str, ...] | list[str]) -> str:
    """Space-separated `owner/name=branch` pairs for target repos with a base branch override.

    Returns "" when no repo has an override. Used as the SYMPHONY_BASE_BRANCHES env var."""
    pairs = []
    for repo in repos:
        branch = cfg.base_branch(repo)
        if branch:
            pairs.append(f"{repo.lower()}={branch}")
    return " ".join(pairs)


_COMPONENT_NAME = re.compile(r'[a-z0-9][a-z0-9-]*\Z')
_COMPONENT_PATH = re.compile(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*\Z')


def known_runtimes(cfg: Config) -> frozenset[str]:
    """All runtime names the config recognises: built-in defaults + operator overrides."""
    from .kube import DEFAULT_AGENT_IMAGES
    return frozenset(DEFAULT_AGENT_IMAGES) | frozenset(cfg.agent_images)


def resolve_workspace(cfg: Config, issue) -> tuple[str, str | None, str | None]:
    """Resolve (runtime, component_name, component_path) for an issue.

    Precedence: runtime: label > component.runtime > repo_runtimes[primary] > agent_runtime.
    agent_image (single image) still wins at IMAGE selection time (kube.agent_image_for).
    """
    primary = issue.target_repo
    # Extract runtime: and component: labels
    runtime_labels = [l for l in issue.labels if l.lower().startswith('runtime:')]
    component_labels = [l for l in issue.labels if l.lower().startswith('component:')]

    component_name: str | None = None
    component_path: str | None = None
    component_runtime: str | None = None

    if component_labels:
        cname = component_labels[0][len('component:'):].strip().lower()
        repo_components = cfg.components.get(primary.lower(), {}) if primary else {}
        comp = repo_components.get(cname)
        if comp:
            component_name = cname
            component_path = comp.get('path')
            component_runtime = comp.get('runtime') or None

    # Precedence for runtime
    if runtime_labels:
        runtime = runtime_labels[0][len('runtime:'):].strip().lower()
    elif component_runtime:
        runtime = component_runtime
    elif primary and cfg.repo_runtimes.get(primary.lower()):
        runtime = cfg.repo_runtimes[primary.lower()]
    else:
        runtime = cfg.agent_runtime

    return runtime, component_name, component_path

TRACKER_KINDS = ("linear", "github", "memory")


# Keys retired by the move off the previous engine. They configured an approval and
# sandbox layer the current engine does NOT have, so accepting-and-ignoring them would
# be configuration theatre: `thread_sandbox` in particular would tell an operator the
# agent is sandboxed when only the container isolates it (FR-009/FR-035).
RETIRED_ENGINE_KEYS = ("approval_policy", "thread_sandbox", "turn_sandbox_policy")


def role_model(cfg: Config, team_doc: Mapping[str, Any] | None, role: str) -> dict:
    """The `{provider, model, thinking}` a role runs with, by internals §4 precedence:
    published TeamConfig (generation ≥ 1) > WORKFLOW.md `engine.provider/model` >
    `teamconfig.DEFAULT`."""
    if role not in teamconfig.ROLES:
        raise ConfigError(f"unknown role {role!r}")
    if team_doc and int(team_doc.get("generation") or 0) >= 1:
        return dict(team_doc["models"][role])
    default = teamconfig.DEFAULT["models"][role]
    if cfg.engine_provider and cfg.engine_model:
        return {
            "provider": cfg.engine_provider,
            "model": cfg.engine_model,
            "thinking": cfg.engine_thinking or default.get("thinking", "medium"),
        }
    return dict(default)


def bind_role_model(cfg: Config, team_doc: Mapping[str, Any] | None, role: str) -> Config:
    """`cfg` with `engine_provider/model/thinking` set to what `role` runs with right now.

    Every unit of work binds once at its start, so a TeamConfig publication reaches the
    next unit without a restart and never changes a unit midway."""
    ref = role_model(cfg, team_doc, role)
    return replace(
        cfg,
        engine_provider=ref["provider"],
        engine_model=ref["model"],
        engine_thinking=ref.get("thinking", "medium"),
    )


def resolve_secret(value: Any, env: Mapping[str, str]) -> Any:
    """$VAR indirection: resolved on the host, at time of use. Missing var ⇒ error."""
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in env:
            raise ConfigError(f"environment variable {name!r} not set (required by WORKFLOW.md)")
        return env[name]
    return value


def _split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        raise ConfigError("WORKFLOW.md must start with YAML front matter (---)")
    end = text.find("\n---", 3)
    if end == -1:
        raise ConfigError("unterminated YAML front matter")
    raw_yaml = text[3:end]
    body = text[end + len("\n---") :].lstrip("\n")
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML front matter: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("front matter must be a YAML mapping")
    return data, body


def _typed(section: dict, key: str, kind: type, default: Any) -> Any:
    value = section.get(key, default)
    if value is None:
        return default
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"key {key!r} must be {kind.__name__}, got bool")
    if not isinstance(value, kind):
        raise ConfigError(f"key {key!r} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _str_list(section: dict, key: str, default: tuple[str, ...] | None) -> tuple[str, ...] | None:
    value = section.get(key, None)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"key {key!r} must be a list of strings")
    return tuple(value)


_MEMORY_ISSUE_KEYS = {"id", "identifier", "title", "description", "state", "labels"}


def _memory_issues(tracker: dict) -> tuple[dict, ...]:
    raw = tracker.get("issues")
    if raw is None:
        return ()
    if tracker["kind"] != "memory":
        raise ConfigError("tracker.issues seeds the memory tracker only (tracker.kind: memory)")
    if not isinstance(raw, list):
        raise ConfigError("tracker.issues must be a list of tickets")
    out: list[dict] = []
    seen_ids: set[str] = set()
    seen_identifiers: set[str] = set()
    for n, item in enumerate(raw):
        where = f"tracker.issues[{n}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping")
        unknown = sorted(set(map(str, item)) - _MEMORY_ISSUE_KEYS)
        if unknown:
            raise ConfigError(f"{where} has unknown keys {', '.join(unknown)}")
        for key in ("id", "identifier", "title"):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ConfigError(f"{where}.{key} must be a nonempty string")
        description = item.get("description")
        if description is not None and not isinstance(description, str):
            raise ConfigError(f"{where}.description must be a string")
        state = item.get("state", "Todo")
        if not isinstance(state, str) or not state.strip():
            raise ConfigError(f"{where}.state must be a nonempty string")
        labels = _str_list(item, "labels", ())
        issue_id, identifier = item["id"].strip(), item["identifier"].strip()
        if issue_id in seen_ids or identifier in seen_identifiers:
            raise ConfigError(f"{where} repeats an id or identifier")
        seen_ids.add(issue_id)
        seen_identifiers.add(identifier)
        out.append({
            "id": issue_id,
            "identifier": identifier,
            "title": item["title"].strip(),
            "description": description,
            "state": state.strip(),
            "labels": labels,
        })
    return tuple(out)


def _check_tracker_states(cfg: Config) -> None:
    """The review pipeline only works if the state names line up with the state sets.

    A merger's `rework` that is not an active state would never redispatch the implementer;
    a `review` or `merging` that IS active would redispatch it while the merger owns the
    ticket; a `done` that is not terminal would keep a merged ticket tracked forever."""
    active = {s.casefold() for s in cfg.active_states}
    terminal = {s.casefold() for s in cfg.terminal_states}
    states = cfg.tracker_states
    if states["rework"].casefold() not in active:
        raise ConfigError(f"tracker.states.rework ({states['rework']!r}) must be an active state")
    for role in ("review", "merging"):
        if states[role].casefold() in active:
            raise ConfigError(
                f"tracker.states.{role} ({states[role]!r}) must not be an active state: "
                "the merger owns the ticket there, not the implementer"
            )
    if states["done"].casefold() not in terminal:
        raise ConfigError(f"tracker.states.done ({states['done']!r}) must be a terminal state")


# notify.users: service → its name in messages. A Discord user id is a snowflake.
NOTIFY_SERVICES = {"slack": "Slack", "discord": "Discord"}
DISCORD_USER_ID = re.compile(r"0|[1-9][0-9]*")


GITHUB_PROVIDER_KEYS = ("owner", "project_number", "status_field")
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


def _check_github_provider(provider: dict) -> None:
    """`tracker.kind: github`: the Projects (v2) board that holds the tickets."""
    # a null key is unset (the admin form clears a field that way)
    unknown = sorted(
        str(k) for k, v in provider.items() if k not in GITHUB_PROVIDER_KEYS and v is not None
    )
    if unknown:
        raise ConfigError(
            f"tracker.provider has unknown keys for github: {', '.join(unknown)} "
            f"(allowed: {', '.join(GITHUB_PROVIDER_KEYS)})"
        )
    owner = provider.get("owner")
    if not isinstance(owner, str) or not GITHUB_LOGIN.fullmatch(owner):
        raise ConfigError(
            "tracker.provider.owner must be the GitHub organization or user that owns the project"
        )
    number = provider.get("project_number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ConfigError(
            "tracker.provider.project_number must be the positive number of the GitHub project "
            "(the N in /orgs/<owner>/projects/N)"
        )
    status = provider.get("status_field")
    if status is not None and (not isinstance(status, str) or not status.strip()):
        raise ConfigError(
            "tracker.provider.status_field must name the project's single-select state field"
        )


_ASSISTANT_KEYS = frozenset({
    "enabled", "slack", "discord", "repositories", "max_concurrent", "turn_timeout_s",
    "ticket_state", "ticket_labels", "soul", "memory",
})
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SOUL_PATH = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*\Z")


def _sub(section: dict, name: str, keys: set[str]) -> dict:
    blk = section.get(name) or {}
    if not isinstance(blk, dict):
        raise ConfigError(f"assistant.{name} must be a mapping")
    extra = sorted(set(map(str, blk)) - keys)
    if extra:
        raise ConfigError(f"assistant.{name} has unknown keys {', '.join(extra)}")
    return blk


def _soul_and_memory(section: dict, defaults: Config) -> dict:
    soul = _sub(section, "soul", {"repo", "path"})
    repo = _typed(soul, "repo", str, None)
    if repo is not None and not _REPO.fullmatch(repo.strip()):
        raise ConfigError(f"assistant.soul.repo must be owner/name: {repo!r}")
    path = _typed(soul, "path", str, defaults.assistant_soul_path).strip()
    if not _SOUL_PATH.fullmatch(path) or ".." in path.split("/"):
        raise ConfigError(f"assistant.soul.path must be a relative file path: {path!r}")
    memory = _sub(section, "memory", {"enabled", "ttl_days"})
    ttl = _typed(memory, "ttl_days", int, defaults.assistant_memory_ttl_days)
    if ttl <= 0:
        raise ConfigError("assistant.memory.ttl_days must be positive")
    return {
        "assistant_soul_repo": repo.strip() if repo else None,
        "assistant_soul_path": path,
        "assistant_memory_enabled": _typed(memory, "enabled", bool, True),
        "assistant_memory_ttl_days": ttl,
    }


def _assistant(data: dict) -> dict:
    """The `assistant:` section as `Config` fields. Unknown keys raise: a typo would
    otherwise open the bot to a channel nobody chose, or close it silently."""
    section = data.get("assistant")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("section 'assistant' must be a mapping")
    unknown = sorted(set(map(str, section)) - _ASSISTANT_KEYS)
    if unknown:
        raise ConfigError(f"assistant has unknown keys {', '.join(unknown)}")
    channels: dict[str, tuple[str, ...]] = {}
    for service in ("slack", "discord"):
        blk = section.get(service) or {}
        if not isinstance(blk, dict):
            raise ConfigError(f"assistant.{service} must be a mapping")
        extra = sorted(set(map(str, blk)) - {"channels"})
        if extra:
            raise ConfigError(f"assistant.{service} has unknown keys {', '.join(extra)}")
        raw = blk.get("channels") or []
        if service == "discord" and isinstance(raw, list):
            # an unquoted snowflake is a YAML integer
            raw = [str(c) if isinstance(c, int) and not isinstance(c, bool) else c for c in raw]
        if not isinstance(raw, list) or not all(isinstance(c, str) and c.strip() for c in raw):
            raise ConfigError(f"assistant.{service}.channels must be a list of channel ids")
        if service == "discord" and not all(DISCORD_USER_ID.fullmatch(c.strip()) for c in raw):
            raise ConfigError("assistant.discord.channels: a Discord channel id is a number")
        channels[service] = tuple(dict.fromkeys(c.strip() for c in raw))
    repos = _str_list(section, "repositories", None)
    if repos is not None:
        bad = [r for r in repos if not _REPO.fullmatch(r)]
        if bad:
            raise ConfigError(f"assistant.repositories must name owner/name repositories: {bad[0]!r}")
        repos = tuple(dict.fromkeys(repos))
    defaults = Config(tracker_kind="memory")
    out = {
        "assistant_enabled": _typed(section, "enabled", bool, False),
        "assistant_slack_channels": channels["slack"],
        "assistant_discord_channels": channels["discord"],
        "assistant_repositories": repos,
        "assistant_max_concurrent": _typed(
            section, "max_concurrent", int, defaults.assistant_max_concurrent
        ),
        "assistant_turn_timeout_s": _typed(
            section, "turn_timeout_s", int, defaults.assistant_turn_timeout_s
        ),
        "assistant_ticket_state": _typed(
            section, "ticket_state", str, defaults.assistant_ticket_state
        ).strip(),
        "assistant_ticket_labels": _str_list(
            section, "ticket_labels", defaults.assistant_ticket_labels
        ),
        **_soul_and_memory(section, defaults),
    }
    if out["assistant_max_concurrent"] <= 0:
        raise ConfigError("assistant.max_concurrent must be positive")
    if out["assistant_turn_timeout_s"] <= 0:
        raise ConfigError("assistant.turn_timeout_s must be positive")
    if not out["assistant_ticket_state"]:
        raise ConfigError("assistant.ticket_state must be a nonempty state name")
    return out


def _section(data: dict, name: str, keys: set[str]) -> dict:
    section = data.get(name) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"section {name!r} must be a mapping")
    unknown = sorted(set(map(str, section)) - keys)
    if unknown:
        raise ConfigError(f"{name} has unknown keys {', '.join(unknown)}")
    return section


def _retention(data: dict) -> dict:
    """The `retention:` section. The archive runs once a day, so a window shorter than
    `retention.MIN_DAYS` could drop an entry before it is archived."""
    section = _section(data, "retention", {"sessions_days", "audit_days"})
    out = {}
    for key, default, high in (
        ("sessions_days", retention.SESSIONS_DAYS, retention.MAX_SESSIONS_DAYS),
        ("audit_days", retention.AUDIT_DAYS, retention.MAX_AUDIT_DAYS),
    ):
        value = _typed(section, key, int, default)
        if not retention.MIN_DAYS <= value <= high:
            raise ConfigError(f"retention.{key} must be between {retention.MIN_DAYS} and {high}")
        out[f"retention_{key}"] = value
    return out


_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_PREFIX_PART = re.compile(r"[A-Za-z0-9._-]+\Z")
_REGION = re.compile(r"[a-z0-9-]{1,32}\Z")


def _backup(data: dict) -> dict:
    """The `backup:` section: `s3` or `dir` (not both) and `keep`. Access keys never go
    here: they live in the `symphony-backup` Secret."""
    from urllib.parse import urlparse

    section = _section(data, "backup", {"s3", "dir", "keep"})
    keep = _typed(section, "keep", int, 7)
    if not 1 <= keep <= 365:
        raise ConfigError("backup.keep must be between 1 and 365")
    out: dict[str, Any] = {"backup_keep": keep, "backup_s3": None, "backup_dir": None}
    if section.get("s3") is not None:
        s3 = _section(section, "s3", {"endpoint", "bucket", "prefix", "region"})
        endpoint = _typed(s3, "endpoint", str, "").strip().rstrip("/")
        parsed = urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or (
            parsed.query or parsed.fragment or parsed.username or parsed.password
        ):
            raise ConfigError("backup.s3.endpoint must be an http(s) URL with no path")
        bucket = _typed(s3, "bucket", str, "").strip()
        if not _BUCKET.fullmatch(bucket) or ".." in bucket:
            raise ConfigError("backup.s3.bucket must be an S3 bucket name")
        parts = [p for p in _typed(s3, "prefix", str, "").strip().split("/") if p]
        if not all(_PREFIX_PART.fullmatch(p) and p not in (".", "..") for p in parts):
            raise ConfigError("backup.s3.prefix may hold only letters, digits, . _ - and /")
        region = _typed(s3, "region", str, "us-east-1").strip()
        if not _REGION.fullmatch(region):
            raise ConfigError("backup.s3.region must be a region code such as us-east-1")
        out["backup_s3"] = {
            "endpoint": endpoint,
            "bucket": bucket,
            "prefix": "".join(f"{p}/" for p in parts),
            "region": region,
        }
    if section.get("dir") is not None:
        directory = _typed(section, "dir", str, "").strip()
        if not directory.startswith("/"):
            raise ConfigError("backup.dir must be an absolute path")
        if out["backup_s3"] is not None:
            raise ConfigError("backup takes `s3` or `dir`, not both")
        out["backup_dir"] = directory
    return out


def parse_workflow(text: str, env: Mapping[str, str] | None = None) -> Config:
    env = env if env is not None else {}
    data, body = _split_front_matter(text)

    tracker = data.get("tracker") or {}
    if not isinstance(tracker, dict) or not isinstance(tracker.get("kind"), str):
        raise ConfigError("tracker.kind is required")
    if tracker["kind"] not in TRACKER_KINDS:
        raise ConfigError(f"unknown tracker.kind {tracker['kind']!r}")

    if "codex" in data:
        raise ConfigError(
            "section 'codex' was retired — rename it to 'engine'; WORKFLOW.md documents "
            "its keys"
        )
    if "modal" in data:
        raise ConfigError(
            "section 'modal' belongs to the Modal port — rename it to 'kubernetes' "
            "(snapshot_image is now agent_image; vm_runtime is now privileged_agents; "
            "environment is now namespace)"
        )

    polling = data.get("polling") or {}
    hooks = data.get("hooks") or {}
    agent = data.get("agent") or {}
    engine = data.get("engine") or {}
    k8s_blk = data.get("kubernetes") or {}
    learning = k8s_blk.get("learning") or {}
    telemetry = data.get("telemetry") or {}
    if not isinstance(telemetry, dict):
        raise ConfigError("section 'telemetry' must be a mapping")
    review_watch = data.get("review_watch") or {}
    if not isinstance(review_watch, dict):
        raise ConfigError("section 'review_watch' must be a mapping")
    for name, blk in (
        ("polling", polling),
        ("hooks", hooks),
        ("agent", agent),
        ("engine", engine),
        ("kubernetes", k8s_blk),
    ):
        if not isinstance(blk, dict):
            raise ConfigError(f"section {name!r} must be a mapping")

    # retired keys fail loudly rather than being ignored — see RETIRED_ENGINE_KEYS
    for key in RETIRED_ENGINE_KEYS:
        if key in engine:
            raise ConfigError(
                f"engine.{key} was retired: the current engine has no approval or sandbox "
                "layer, so this key would describe a property it does not deliver"
            )
    if "pre_pr_review" in k8s_blk:
        # Same rule: it ran the automated reviewer's CLI before the pull request opened. The
        # merger reviews every pull request now, so the key would promise a step that no
        # longer exists.
        raise ConfigError(
            "kubernetes.pre_pr_review was retired: symphony-merger reviews every pull "
            "request (docs/merger.md) — remove the key"
        )

    workspace_blk = data.get("workspace") or {}
    if not isinstance(workspace_blk, dict):
        raise ConfigError("section 'workspace' must be a mapping")
    raw_images = k8s_blk.get("agent_images") or {}
    if not isinstance(raw_images, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_images.items()
    ):
        raise ConfigError("kubernetes.agent_images must map runtime names to image references")
    raw_branches = k8s_blk.get("base_branches") or {}
    if not isinstance(raw_branches, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_branches.items()
    ):
        raise ConfigError("kubernetes.base_branches must map repositories to branch names")
    raw_repo_runtimes = k8s_blk.get('repo_runtimes') or {}
    if not isinstance(raw_repo_runtimes, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw_repo_runtimes.items()
    ):
        raise ConfigError('kubernetes.repo_runtimes must map repositories to runtime names')
    raw_components = k8s_blk.get('components') or {}
    if not isinstance(raw_components, dict):
        raise ConfigError('kubernetes.components must map repositories to component definitions')
    for repo, repo_comps in raw_components.items():
        if not isinstance(repo, str) or not _REPO.fullmatch(repo):
            raise ConfigError(f'kubernetes.components: {repo!r} is not a valid owner/name')
        if not isinstance(repo_comps, dict) or len(repo_comps) > 64:
            raise ConfigError(f'kubernetes.components.{repo}: must be a bounded mapping of components')
        for cname, cdef in repo_comps.items():
            if not isinstance(cname, str) or not _COMPONENT_NAME.fullmatch(cname):
                raise ConfigError(f'kubernetes.components.{repo}.{cname}: component name must match [a-z0-9][a-z0-9-]*')
            if not isinstance(cdef, dict):
                raise ConfigError(f'kubernetes.components.{repo}.{cname}: must be a mapping')
            cpath = cdef.get('path')
            if not isinstance(cpath, str) or not cpath or '..' in cpath or cpath.startswith('/'):
                raise ConfigError(f'kubernetes.components.{repo}.{cname}.path: must be a relative path without ..')
            if not _COMPONENT_PATH.fullmatch(cpath):
                raise ConfigError(f'kubernetes.components.{repo}.{cname}.path: invalid path')
            cruntime = cdef.get('runtime')
            if cruntime is not None and not isinstance(cruntime, str):
                raise ConfigError(f'kubernetes.components.{repo}.{cname}.runtime: must be a string')
    if not isinstance(tracker.get("provider", {}) or {}, dict):
        raise ConfigError("tracker.provider must be a mapping")
    if tracker["kind"] == "github":
        _check_github_provider(tracker.get("provider") or {})

    hook_scripts: dict = {}
    for name in ("after_create", "before_run", "after_run", "before_remove"):
        value = hooks.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ConfigError(f"hook {name!r} must be a string script")
        hook_scripts[name] = value

    raw_by_state = agent.get("max_concurrent_agents_by_state") or {}
    if not isinstance(raw_by_state, dict):
        raise ConfigError("agent.max_concurrent_agents_by_state must be a mapping")
    by_state = {
        str(k).strip().casefold(): v
        for k, v in raw_by_state.items()
        if isinstance(v, int) and not isinstance(v, bool) and v > 0
    }

    raw_states = tracker.get("states") or {}
    if not isinstance(raw_states, dict):
        raise ConfigError("tracker.states must be a mapping")
    unknown_roles = sorted(set(map(str, raw_states)) - set(TRACKER_STATE_ROLES))
    if unknown_roles:
        raise ConfigError(
            f"tracker.states has unknown keys {', '.join(unknown_roles)} "
            f"(want: {', '.join(TRACKER_STATE_ROLES)})"
        )
    tracker_states = dict(DEFAULT_TRACKER_STATES)
    for role, name in raw_states.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"tracker.states.{role} must be a nonempty state name")
        tracker_states[role] = name.strip()

    notify = data.get("notify") or {}
    if not isinstance(notify, dict):
        raise ConfigError("section 'notify' must be a mapping")
    raw_users = notify.get("users") or {}
    if not isinstance(raw_users, dict) or set(map(str, raw_users)) - set(NOTIFY_SERVICES):
        raise ConfigError(
            "notify.users takes a `slack` and a `discord` map of GitHub login to user id"
        )
    notify_users: dict[str, dict[str, str]] = {}
    for service, label in NOTIFY_SERVICES.items():
        section = raw_users.get(service) or {}
        if not isinstance(section, dict):
            raise ConfigError(f"notify.users.{service} must map GitHub logins to {label} user ids")
        mapped: dict[str, str] = {}
        for login, user_id in section.items():
            if service == "discord" and isinstance(user_id, int) and not isinstance(user_id, bool):
                user_id = str(user_id)  # an unquoted snowflake is a YAML integer
            if not (isinstance(login, str) and login.strip()
                    and isinstance(user_id, str) and user_id.strip()):
                raise ConfigError(
                    f"notify.users.{service} must map GitHub logins to {label} user ids"
                )
            if service == "discord" and not DISCORD_USER_ID.fullmatch(user_id.strip()):
                raise ConfigError(f"notify.users.discord.{login}: a Discord user id is a number")
            # GitHub logins are case-insensitive; consumers look up `login.casefold()`.
            mapped[login.strip().casefold()] = user_id.strip()
        notify_users[service] = mapped

    defaults = Config(tracker_kind=tracker["kind"])
    cfg = Config(
        tracker_kind=tracker["kind"],
        provider=dict(tracker.get("provider") or {}),
        active_states=_str_list(tracker, "active_states", defaults.active_states),
        terminal_states=_str_list(tracker, "terminal_states", defaults.terminal_states),
        required_labels=_str_list(tracker, "required_labels", defaults.required_labels),
        memory_issues=_memory_issues(tracker),
        tracker_states=tracker_states,
        investigation_label=_typed(
            tracker, "investigation_label", str, defaults.investigation_label
        ).strip().casefold(),
        polling_interval_ms=_typed(polling, "interval_ms", int, defaults.polling_interval_ms),
        workspace_root=_typed(workspace_blk, "root", str, defaults.workspace_root),
        hooks=hook_scripts,
        hooks_timeout_ms=_typed(hooks, "timeout_ms", int, defaults.hooks_timeout_ms),
        max_concurrent_agents=_typed(
            agent, "max_concurrent_agents", int, defaults.max_concurrent_agents
        ),
        max_concurrent_agents_by_state=by_state,
        max_turns=_typed(agent, "max_turns", int, defaults.max_turns),
        max_retry_backoff_ms=_typed(
            agent, "max_retry_backoff_ms", int, defaults.max_retry_backoff_ms
        ),
        input_timeout_ms=_typed(agent, "input_timeout_ms", int, defaults.input_timeout_ms),
        stall_timeout_ms=_typed(agent, "stall_timeout_ms", int, defaults.stall_timeout_ms),
        engine_command=_typed(engine, "command", str, defaults.engine_command),
        engine_provider=_typed(engine, "provider", str, None),
        engine_model=_typed(engine, "model", str, None),
        engine_thinking=_typed(engine, "thinking", str, None),
        auto_retry=_typed(engine, "auto_retry", bool, defaults.auto_retry),
        turn_timeout_ms=_typed(engine, "turn_timeout_ms", int, defaults.turn_timeout_ms),
        read_timeout_ms=_typed(engine, "read_timeout_ms", int, defaults.read_timeout_ms),
        app_name=_typed(k8s_blk, "app_name", str, defaults.app_name),
        namespace=_typed(k8s_blk, "namespace", str, None),
        sandbox={**defaults.sandbox, **(k8s_blk.get("sandbox") or {})},
        runner={**defaults.runner, **(k8s_blk.get("runner") or {})},
        agent_runtime=_typed(k8s_blk, "agent_runtime", str, defaults.agent_runtime),
        agent_image=_typed(k8s_blk, "agent_image", str, None),
        agent_images=dict(k8s_blk.get("agent_images") or {}),
        base_branches={k.lower(): v for k, v in raw_branches.items()},
        repo_runtimes={k.lower(): v for k, v in raw_repo_runtimes.items()},
        components={
            repo.lower(): {
                cname: {k2: v2 for k2, v2 in cdef.items() if k2 in ('path', 'runtime')}
                for cname, cdef in repo_comps.items()
            }
            for repo, repo_comps in raw_components.items()
        },
        privileged_agents=_typed(k8s_blk, "privileged_agents", bool, defaults.privileged_agents),
        runtime_class=_typed(k8s_blk, "runtime_class", str, None),
        storage_class=_typed(k8s_blk, "storage_class", str, None),
        session_volume_size=_typed(
            k8s_blk, "session_volume_size", str, defaults.session_volume_size
        ),
        sessions_enabled=_typed(k8s_blk, "sessions_enabled", bool, defaults.sessions_enabled),
        image_pull_secrets=_str_list(k8s_blk, "image_pull_secrets", defaults.image_pull_secrets),
        default_repo=_typed(k8s_blk, "default_repo", str, None),
        egress_allowlist=_str_list(k8s_blk, "egress_allowlist", defaults.egress_allowlist),
        redispatch_cooldown_s=_typed(
            k8s_blk, "redispatch_cooldown_s", int, defaults.redispatch_cooldown_s
        ),
        learning_enabled=_typed(learning, "enabled", bool, defaults.learning_enabled),
        reflect_on=_str_list(learning, "reflect_on", None),
        agents_md_budget_lines=_typed(
            learning, "agents_md_budget_lines", int, defaults.agents_md_budget_lines
        ),
        learnings_label=_typed(learning, "learnings_label", str, defaults.learnings_label),
        decay_after_incorporations=_typed(
            learning, "decay_after_incorporations", int, defaults.decay_after_incorporations
        ),
        telemetry_enabled=_typed(telemetry, "enabled", bool, defaults.telemetry_enabled),
        telemetry_endpoint=_typed(telemetry, "endpoint", str, None),
        telemetry_capture_content=_typed(
            telemetry, "capture_content", str, defaults.telemetry_capture_content
        ),
        review_watch_enabled=_typed(review_watch, "enabled", bool, defaults.review_watch_enabled),
        review_watch_interval_s=_typed(
            review_watch, "interval_s", int, defaults.review_watch_interval_s
        ),
        review_watch_states=_str_list(review_watch, "states", defaults.review_watch_states),
        answer_max_turns=_typed(review_watch, "answer_max_turns", int, defaults.answer_max_turns),
        answer_prompt=_typed(review_watch, "answer_prompt", str, defaults.answer_prompt),
        notify_users=notify_users,
        **_assistant(data),
        **_retention(data),
        **_backup(data),
        prompt_body=body,
    )

    # validates the Liquid bodies at parse time (syntax; variables are checked at render)
    from .prompt import validate_template

    if cfg.max_turns <= 0:
        raise ConfigError("agent.max_turns must be positive")
    if cfg.hooks_timeout_ms <= 0:
        raise ConfigError("hooks.timeout_ms must be positive")
    if cfg.input_timeout_ms <= 0:
        raise ConfigError("agent.input_timeout_ms must be positive")
    if cfg.polling_interval_ms <= 0:
        raise ConfigError("polling.interval_ms must be positive")
    if bool(cfg.engine_provider) != bool(cfg.engine_model):
        # Half a model reference is a typo, not a choice: falling back to the TeamConfig for
        # the missing half would run a model nobody named.
        raise ConfigError("engine.provider and engine.model must be set together (or neither)")
    if cfg.engine_thinking is not None and cfg.engine_thinking not in teamconfig.THINKING:
        raise ConfigError("engine.thinking must be one of " + ", ".join(teamconfig.THINKING))
    if not cfg.investigation_label:
        raise ConfigError("tracker.investigation_label must be a nonempty label")
    _check_tracker_states(cfg)
    required = {label.casefold() for label in cfg.required_labels}
    started = [label for label in cfg.assistant_ticket_labels if label.casefold() in required]
    if started:
        # A required label makes the daemon start the ticket: only "Create and start" adds it.
        raise ConfigError(
            f"assistant.ticket_labels must not hold the required label {started[0]!r}: "
            "only \"Create and start\" adds it"
        )
    if cfg.review_watch_enabled:
        if cfg.answer_max_turns <= 0:
            raise ConfigError("review_watch.answer_max_turns must be positive")
        if not cfg.answer_prompt.strip():
            # Without it an answer run would fall back to the implementation prompt and
            # resume building under a reviewer — the one thing this feature must not do.
            raise ConfigError(
                "review_watch.answer_prompt is required when review_watch.enabled is true"
            )
        validate_template(cfg.answer_prompt)
    if cfg.telemetry_capture_content not in TELEMETRY_CAPTURE_MODES:
        raise ConfigError("telemetry.capture_content must be " + ", ".join(TELEMETRY_CAPTURE_MODES))
    if cfg.telemetry_endpoint:
        from urllib.parse import urlparse

        parsed = urlparse(cfg.telemetry_endpoint)
        if not parsed.scheme or not parsed.hostname:
            raise ConfigError("telemetry.endpoint must be an absolute URL")


    valid_runtimes = known_runtimes(cfg)
    for repo, rt in cfg.repo_runtimes.items():
        if rt not in valid_runtimes:
            raise ConfigError(
                f'kubernetes.repo_runtimes: runtime {rt!r} for {repo} is not a known runtime '
                f'({", ".join(sorted(valid_runtimes))})'
            )
    for repo, repo_comps in cfg.components.items():
        for cname, cdef in repo_comps.items():
            crt = cdef.get('runtime')
            if crt and crt not in valid_runtimes:
                raise ConfigError(
                    f'kubernetes.components.{repo}.{cname}.runtime: {crt!r} is not a known runtime'
                )
    validate_template(cfg.prompt_body)
    return cfg


class ConfigHolder:
    """Hot reload with last-known-good: an invalid version is NEVER applied (FR-019).

    Invalid means the policy itself is wrong. A tracker credential that is not provisioned
    yet is not that: it is setup still in progress, reported by `edge.make_tracker`
    (`TrackerNotConfigured`) while the policy stays applied."""

    def __init__(self) -> None:
        self.config: Config | None = None
        self.last_error: str | None = None
        self.accepted_document_hash: str | None = None
        self.rejected_document_hash: str | None = None

    def update(self, text: str, env: Mapping[str, str] | None = None) -> bool:
        document_hash: str | None = None
        try:
            document_hash = sha256(text.encode("utf-8")).hexdigest()
            candidate = parse_workflow(text, env)
            self.config = candidate
            self.last_error = None
            self.accepted_document_hash = document_hash
            self.rejected_document_hash = None
            return True
        except Exception as exc:  # noqa: BLE001 — §6.2 backstop: ANY parse crash keeps last-good
            self.last_error = str(exc)
            self.rejected_document_hash = document_hash
            return False
