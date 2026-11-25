"""Edge helpers shared by every entrypoint — importable WITHOUT the Kubernetes client.

The split mirrors the constitution's rule for the Modal port's app.py (glue only, zero
orchestration logic), with one further cut: everything here is decidable without a
cluster, so the test suite can assert the wiring — gateway routes, telemetry endpoint
shape, tracker construction — without kubernetes-the-package installed. The pieces
that DO talk to the API server live in `kube.py` and `effects.py`.
"""

from __future__ import annotations

import functools
import os
import time
import urllib.parse

import structlog

from . import questions, state
from .config import ConfigError, ConfigHolder, resolve_secret
from .ghapp import InstallationCredential
from .kv import RedisKV
from .trackers.github import GitHubTracker
from .trackers.linear import LinearTracker
from .trackers.memory import MemoryTracker

APP_NAME = os.environ.get("SYMPHONY_APP_NAME", "symphony")
WORKFLOW_MOUNT = os.environ.get("SYMPHONY_WORKFLOW_MOUNT", "/workflow")
SESSION_MOUNT = "/omp-sessions"
WORKSPACE_CONTAINER = "workspace"
# PID 1 of the agent pods (run with `bash -c`). It must reap: agents leave orphans (omp's
# broker, browsers) that would stay zombies under a `sleep infinity` PID 1, and a zombie
# makes the generation stop unprovable. bash reaps every child on SIGCHLD, adopted ones too.
WORKSPACE_INIT = "trap 'exit 0' TERM; while :; do sleep 3600 & wait $!; done"

# OTLP's signal path for traces. The runner sets the per-signal endpoint, which the SDK
# uses verbatim, so the path must already be part of it.
TELEMETRY_TRACES_PATH = "/v1/traces"

# In-cluster service ports (internals §9; deploy/*.yaml must agree).
API_PORT = 8080
GATEWAY_PORT = 8090

# Exact engine pin (docker/agent.Dockerfile and design/admin/package.json must agree).
OMP_VERSION = "18.20.2"

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.KeyValueRenderer(),
    ]
)
log = structlog.get_logger()


def now_ms() -> int:
    return int(time.time() * 1000)


def pod_namespace(cfg=None) -> str:
    """The namespace everything runs in.

    Order: explicit config, explicit env, the serviceaccount file the kubelet mounts
    into every pod, then 'default' for local drives. In-cluster the file always wins
    over guessing — a daemon deployed to `symphony-prod` must never address services
    in `default` because an operator forgot one env var.
    """
    if cfg is not None and getattr(cfg, "namespace", None):
        return cfg.namespace
    env = os.environ.get("POD_NAMESPACE", "")
    if env:
        return env
    try:
        with open(
            "/var/run/secrets/kubernetes.io/serviceaccount/namespace", encoding="utf-8"
        ) as fh:
            return fh.read().strip() or "default"
    except OSError:
        return "default"


def load_config(holder: ConfigHolder, path: str | None = None) -> None:
    """Reload WORKFLOW.md from the mounted ConfigMap (hot reload: the kubelet syncs
    ConfigMap updates into the mount within about a minute; invalid content keeps the
    last-known-good version, exactly as the spec requires)."""
    path = path or os.path.join(WORKFLOW_MOUNT, "WORKFLOW.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        holder.last_error = "The workflow file could not be read."
        holder.rejected_document_hash = None
        log.warning("workflow_read_failed", error=str(exc))
        return
    if not holder.update(text, os.environ):
        log.warning(
            "workflow_invalid_kept_last_good",
            rejected_document_hash=holder.rejected_document_hash,
        )


class ObservedTracker:
    """Edge-only wrapper that logs issues the adapter refused to route (FR-008).

    An issue held back for an unresolvable repository is never claimed, so it never gets
    a state entry — this event is the *only* record that it was seen and refused. Without
    it the ticket would simply sit on the board while nobody could tell why, which is the
    silent failure this feature exists to prevent.

    It lives here, at the edge, rather than in the tick: `orchestrator.py` must stay
    untouched, and it has no business interpreting a provider's routing reasons anyway.
    """

    def __init__(self, inner, required_labels=()) -> None:
        self._inner = inner
        # Gate order mirrors `orchestrator.eligible`: the required label first, routing
        # second. On a team-scoped board most issues are ordinary team work that was
        # never a candidate, and reporting those as "not routable" every tick buries the
        # events that matter under noise proportional to the size of the board.
        self._required = {label.casefold() for label in required_labels}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _is_candidate(self, issue) -> bool:
        return self._required.issubset({label.casefold() for label in issue.labels})

    def fetch_issues_by_states(self, states):
        issues = self._inner.fetch_issues_by_states(states)
        for issue in issues:
            if (
                issue.state_type == "active"
                and issue.undispatchable_reason
                and self._is_candidate(issue)
            ):
                log.warning(
                    "issue_not_routable",
                    issue_id=issue.id,
                    identifier=issue.identifier,
                    reason=issue.undispatchable_reason,
                )
        return issues


class TrackerNotConfigured(Exception):
    """The WORKFLOW names a tracker that cannot be used yet: setup is incomplete.

    Not an outage and not invalid policy — the credential or the board scope simply has not
    been provided. `reason` is a fixed, secret-free sentence an admin can act on. `code` and
    `status_code` follow the command error convention, so a command that needs the tracker
    fails cleanly with 409 instead of stalling the command queue as an outage would."""

    code = "setup_incomplete"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


GITHUB_APP_ENV = ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY")


def make_tracker(cfg):
    """The WORKFLOW's tracker, or `TrackerNotConfigured` when setup has not provided what it
    needs (credential, board scope). Reads the process environment at call time, so a
    credential that appears (local mode applies `secrets.env` at once) is used next tick."""
    return ObservedTracker(_build_tracker(cfg, _comment_recorder(cfg)), cfg.required_labels)


def _comment_recorder(cfg):
    """What the tracker adapter calls with the id of each comment it writes, from any
    process (daemon, runner, merger, assistant, reflector): `questions.record_own`, in the
    Redis they share. None without `REDIS_URL` (a script or a test). Best effort: a comment
    is never refused over it, and the failure is logged."""
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return None
    client, ns = _redis(url), pod_namespace(cfg)

    def record(comment_id: str) -> None:
        try:
            questions.record_own(client, ns, comment_id)
        except Exception as exc:  # noqa: BLE001 — the comment exists; recording is advice
            log.warning("own_comment_unrecorded", comment=comment_id, error=str(exc)[:200])

    return record


@functools.lru_cache(maxsize=4)
def _redis(url: str):
    return RedisKV(url=url).client


class UnconfiguredTracker:
    """The tracker a process holds while setup is incomplete: every read raises
    `TrackerNotConfigured`, so tracker-independent work (durable commands such as a
    workflow publication) still proceeds and tracker-bound work fails with the reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def fetch_issues_by_states(self, states):
        raise TrackerNotConfigured(self.reason)

    def fetch_issues_by_ids(self, ids):
        raise TrackerNotConfigured(self.reason)

    def agent_tool_specs(self):
        return []

    def execute_agent_tool(self, name: str, args: dict) -> dict:
        raise TrackerNotConfigured(self.reason)

    def secret_environment_names(self) -> list[str]:
        return []


SETUP_LOG_EVERY_MS = 60_000


class SetupGate:
    """Reports whether this process can work, once per step (internals §3, setup).

    Every update writes `state.SETUP_KEY` (`{status, reason, at}`, refreshed each step like
    the heartbeat) for the overview; an incomplete setup logs `daemon_setup_incomplete` at
    most once a minute, so an unconfigured install is visible without flooding the log."""

    def __init__(self, log_every_ms: int = SETUP_LOG_EVERY_MS) -> None:
        self._log_every_ms = log_every_ms
        self._logged_at: int | None = None
        self.reason: str | None = None

    def update(self, kv, now: int, reason: str | None) -> bool:
        """Record `reason` (None ⇒ ready); returns True when ready."""
        if reason is None and self.reason is not None:
            log.info("daemon_setup_complete")
            self._logged_at = None
        self.reason = reason
        state.report_setup(kv, now, reason)
        if reason is None:
            return True
        if self._logged_at is None or now - self._logged_at >= self._log_every_ms:
            log.warning("daemon_setup_incomplete", reason=reason)
            self._logged_at = now
        return False


def usable_tracker(cfg, kv, gate: SetupGate, now: int):
    """`(tracker, ready)` for one step: the WORKFLOW's tracker and True, or — while setup is
    incomplete — an `UnconfiguredTracker` and False, with the gate updated either way."""
    try:
        tracker = make_tracker(cfg)
    except TrackerNotConfigured as exc:
        gate.update(kv, now, exc.reason)
        return UnconfiguredTracker(exc.reason), False
    gate.update(kv, now, None)
    return tracker, True


def _tracker_credential() -> InstallationCredential:
    """The GitHub tracker's App installation token: one per process, renewed as it nears
    expiry (a tracker is built every tick; minting each time would waste the App's quota).
    Unscoped: the board spans repositories, so it carries the installation's own
    permissions (issues, projects, metadata — docs/configuration.md). Keyed by the App's
    environment, so credentials that change in place (local setup) are picked up at once."""
    return _credential_for(*(os.environ.get(name, "") for name in GITHUB_APP_ENV))


@functools.lru_cache(maxsize=1)
def _credential_for(app_id: str, installation_id: str, private_key: str) -> InstallationCredential:
    return InstallationCredential.from_env(
        {
            "GITHUB_APP_ID": app_id,
            "GITHUB_APP_INSTALLATION_ID": installation_id,
            "GITHUB_APP_PRIVATE_KEY": private_key,
        },
        now_ms,
    )


def tracker_credentials(cfg) -> list[str]:
    """The tracker credential values a process holds (the Linear key, the GitHub App's
    private key), for redaction (`sessiontrace`). Read the way `_build_tracker` reads them."""
    if cfg.tracker_kind == "github":
        return [os.environ.get(name, "") for name in GITHUB_APP_ENV]
    if cfg.tracker_kind == "linear":
        try:
            return [resolve_secret(cfg.provider.get("api_key", "$LINEAR_API_KEY"), os.environ)]
        except ConfigError:
            return []
    return []


def _build_tracker(cfg, on_comment):
    if cfg.tracker_kind == "memory":
        # A memory board declared in WORKFLOW.md (`tracker.issues`) is seeded identically
        # in every process that mounts it — daemon, runner Job and API see one board.
        tracker = MemoryTracker(cfg.active_states, cfg.terminal_states, on_comment=on_comment)
        for item in getattr(cfg, "memory_issues", ()):
            tracker.add_issue(
                item["id"],
                item["identifier"],
                state=item.get("state", "Todo"),
                title=item.get("title", item["identifier"]),
                description=item.get("description", ""),
                labels=tuple(item.get("labels", ())),
            )
        return tracker
    if cfg.tracker_kind == "github":
        missing = [name for name in GITHUB_APP_ENV if not os.environ.get(name, "").strip()]
        if missing:
            raise TrackerNotConfigured(
                f"The GitHub tracker needs the GitHub App credentials ({', '.join(missing)} "
                "not set)."
            )
        return GitHubTracker(
            owner=cfg.provider["owner"],
            project_number=cfg.provider["project_number"],
            status_field=cfg.provider.get("status_field") or "Status",
            active_states=cfg.active_states,
            terminal_states=cfg.terminal_states,
            token=lambda: _tracker_credential().token(),
            default_repo=cfg.default_repo,
            done_state=cfg.tracker_states["done"],
        )
    reference = cfg.provider.get("api_key", "$LINEAR_API_KEY")
    try:
        api_key = resolve_secret(reference, os.environ)
    except ConfigError:
        api_key = None
    if not isinstance(api_key, str) or not api_key.strip():
        name = reference[1:] if isinstance(reference, str) and reference.startswith("$") else None
        raise TrackerNotConfigured(
            f"The Linear API key is not set ({name} is empty or missing)."
            if name
            else "The Linear API key in tracker.provider.api_key is empty."
        )
    if not (cfg.provider.get("project_slug") or cfg.provider.get("team_key")):
        raise TrackerNotConfigured(
            "The Linear board scope is not set (tracker.provider.team_key or project_slug)."
        )
    return LinearTracker(
        api_key=api_key,
        project_slug=cfg.provider.get("project_slug", ""),
        team_key=cfg.provider.get("team_key"),
        active_states=cfg.active_states,
        terminal_states=cfg.terminal_states,
        assignee=cfg.provider.get("assignee"),
        default_repo=cfg.default_repo,
        on_comment=on_comment,
    )


# --------------------------------------------------------------------------
# in-cluster addressing
# --------------------------------------------------------------------------
def gateway_url(cfg) -> str:
    """The sandbox gateway's base URL — the only Symphony service workspaces reach.

    The Service name is known before anything is deployed, so the default is the
    in-cluster DNS name derived from app_name + namespace. `SYMPHONY_GATEWAY_URL`
    overrides it (base URL, no path) for local drives and smoke tests.
    """
    override = os.environ.get("SYMPHONY_GATEWAY_URL", "")
    if override:
        return override.rstrip("/")
    ns = pod_namespace(cfg)
    return f"http://{cfg.app_name}-gateway.{ns}.svc.cluster.local:{GATEWAY_PORT}"


def telemetry_url(cfg) -> str:
    """Where the workspace pod ships spans: the operator's collector, or the gateway's
    traces route."""
    if cfg.telemetry_endpoint:
        return cfg.telemetry_endpoint  # operator's own collector; validated at config load
    return otlp_endpoint(gateway_url(cfg))


def otlp_endpoint(base: str) -> str:
    """Turn a collector base URL into its traces endpoint. The per-signal variable is used
    verbatim; a missing path posts every batch to the wrong route, silently."""
    if not base:
        return ""
    return base.rstrip("/") + TELEMETRY_TRACES_PATH


def telemetry_env(cfg, issue_id: str, seq: int, bridge_token: str) -> dict[str, str]:
    """OMP's native OTLP configuration for one attempt, or the explicit off-switch.

    Only traces are exported, over `http/protobuf` — the one transport the engine ships.
    The endpoint and headers are the per-signal variants: the common
    `OTEL_EXPORTER_OTLP_ENDPOINT` would get `/v1/traces` appended a second time.

    The credential is the attempt's existing bridge token — no new secret, and it inherits
    the rotation and clearing the bridge token already has (FR-016). OMP percent-decodes
    header values, so the space after `Bearer` travels encoded.
    """
    if not cfg.telemetry_enabled:
        return {"OTEL_SDK_DISABLED": "true"}
    endpoint = telemetry_url(cfg)
    if not endpoint:
        log.warning("telemetry_endpoint_unresolved", issue_id=issue_id)
        return {"OTEL_SDK_DISABLED": "true"}
    env = {
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": (
            "Authorization=" + urllib.parse.quote(f"Bearer {bridge_token}", safe="")
        ),
        "OTEL_SERVICE_NAME": "symphony-agent",
        # The ONLY way a batch identifies its attempt: OTLP has no field of ours to carry it.
        "OTEL_RESOURCE_ATTRIBUTES": f"symphony.issue_id={issue_id},symphony.dispatch_seq={seq}",
    }
    if cfg.telemetry_capture_content != "none":
        env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = cfg.telemetry_capture_content
    return env
