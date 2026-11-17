"""Runner — one Job per issue-dispatch. Port of the Modal edge's `run_issue`.

The security topology is unchanged (constitution III): this process holds the tracker
credential and the App signing key; the workspace pod receives only derived, short-lived,
repository-scoped material — the installation token through a git credential helper — plus
the model API key the team configured for the attempt's provider, staged privately
(internals §13). The tracker itself is reached only through the runner's host tools.
"""

from __future__ import annotations

import os
import shlex
import sys
import time
import uuid

from .. import ghapp, kube, orchestrator, state
from .. import runner as runner_mod
from ..admin_commands import AdminStore
from ..config import Config, ConfigHolder, base_branches_env
from ..edge import (
    SESSION_MOUNT,
    TrackerNotConfigured,
    gateway_url,
    load_config,
    log,
    make_tracker,
    now_ms,
    pod_namespace,
    telemetry_env,
)
from ..effects import kill_agent_generation
from ..endpoints import overrides as endpoint_overrides
from ..kube import PodSandbox
from ..kv import RedisKV
from ..mcp_servers import McpLaunch, McpRepository
from ..omp_rpc import OmpClient
from ..provider_runtime import prepare_provider
from ..sessions import SessionStore
from ..teamconfig import TeamConfig
from ..trackers.base import Issue
from ..workspace import (
    CREDENTIAL_DIR,
    install_credential_helper,
    kill_agent_processes,
    run_hook,
    sandbox_name,
    workspace_matches_repo,
)


def engine_launch(cfg: Config, session_dir: str) -> str:
    """The engine command line for one attempt: the bound model and thinking level,
    headless approvals, and the per-issue session directory. No extension is loaded."""
    return (
        f"mkdir -p {shlex.quote(session_dir)} && "
        f"exec {cfg.engine_command} "
        "--no-title --auto-approve "
        f"--session-dir {shlex.quote(session_dir)} "
        f"--provider {shlex.quote(cfg.engine_provider)} --model {shlex.quote(cfg.engine_model)} "
        f"--thinking {shlex.quote(cfg.engine_thinking or 'medium')}"
    )


def engine_env(cfg: Config, issue_id: str, seq: int, attempt_token: str, repos: tuple[str, ...]) -> dict[str, str]:
    """The engine's environment. Nothing here is a long-lived credential: the attempt
    token authorizes only this dispatch's telemetry at the gateway and dies with it."""
    return {
        # generation markers: `kill_agent_generation` matches exactly these
        "SYMPHONY_ISSUE_ID": issue_id,
        "SYMPHONY_DISPATCH_SEQ": str(seq),
        # the hook clones ${SYMPHONY_TARGET_REPO}; multi-repo work also gets the rest
        "SYMPHONY_TARGET_REPO": repos[0] if repos else "",
        "SYMPHONY_TARGET_REPOS": " ".join(repos),
        **telemetry_env(cfg, issue_id, seq, attempt_token),
        # endpoint overrides (internals §14) so the agent's git/curl reach the same hosts
        **endpoint_overrides(),
    }


def installation_credential(
    issue: Issue, cfg: Config, environ, now_ms
) -> ghapp.InstallationCredential | None:
    """The attempt's GitHub App credential, scoped to the ticket's repositories and to
    explicit permissions — never the installation's own, which may include tickets.

    Implementation tickets get `ghapp.WORKSPACE`. Investigation tickets get a read-only
    token (internals §11): the deliverable is a report, and without write scopes the agent
    cannot push or open a pull request. A memory-tracker install without a GitHub App
    (local and kind certification) runs with no code-hosting credential at all; with
    Linear the App is mandatory.
    """
    if cfg.tracker_kind == "memory" and not environ.get("GITHUB_APP_ID"):
        return None
    label = cfg.investigation_label.casefold()
    investigating = any(lbl.casefold() == label for lbl in issue.labels)
    return ghapp.InstallationCredential.from_env(
        environ,
        now_ms,
        target_repos=runner_mod.target_repos(issue),
        permissions=ghapp.READ_ONLY if investigating else ghapp.WORKSPACE,
    )


def fail_setup_incomplete(
    kv, cfg: Config, issue_id: str, attempt: int, seq: int, reason: str, now: int
) -> str:
    """End an attempt that cannot start because setup is incomplete (no usable tracker),
    with the reason as its failure summary, instead of a traceback.

    Fenced like every runner write: only the attempt that still owns its claim records the
    failure; a superseded or replayed Job changes nothing."""
    if not runner_mod.fencing_ok(kv, issue_id, attempt, seq):
        return "fenced"
    entry = dict(kv.get(state.issue_key(issue_id)) or {})
    owned = {"attempt": attempt, "dispatch_seq": seq, "status": entry.get("status")}
    summary = f"Setup is incomplete: {reason}"
    entry.update(orchestrator.retry_after_failure(entry, cfg, now, summary), bridge_token=None)
    if not state.fenced_put(
        kv, state.issue_key(issue_id), entry, owned, state.dispatch_key(issue_id, seq)
    ):
        return "fenced"
    log.warning("runner_setup_incomplete", issue_id=issue_id, seq=seq, reason=reason)
    return "failed"


def main() -> str:
    issue_id = os.environ["SYMPHONY_ISSUE_ID"]
    attempt = int(os.environ["SYMPHONY_ATTEMPT"])
    seq = int(os.environ["SYMPHONY_SEQ"])
    call_id = os.environ.get("SYMPHONY_CALL_ID") or f"local-{uuid.uuid4().hex[:8]}"

    kv = RedisKV()
    holder = ConfigHolder()
    load_config(holder)
    if holder.config is None:
        log.error("no_valid_workflow_config", rejected_document_hash=holder.rejected_document_hash)
        return "no-config"
    cfg = holder.config
    try:
        tracker = make_tracker(cfg)
    except TrackerNotConfigured as exc:
        return fail_setup_incomplete(kv, cfg, issue_id, attempt, seq, exc.reason, now_ms())
    core, _batch = kube.get_clients()
    ns = pod_namespace(cfg)
    if holder.accepted_document_hash:
        AdminStore(kv.client, ns).observe_workflow(
            f"runner:{issue_id}:{seq}", holder.accepted_document_hash
        )
    started = now_ms()
    # Scoped per attempt to the issue's own repositories — the security dividend of
    # per-issue targeting (feature 003). Resolved once ensure_sandbox knows the issue.
    installation: ghapp.InstallationCredential | None = None
    # A2: one MCP token per engine launch, role `implementer`, revoked when the attempt ends.
    mcp = McpLaunch(kv.client, ns, McpRepository(core, ns), gateway_url(cfg))
    mcp_tokens: list[str] = []
    token_ttl_s = max(600, (runner_mod.compute_env_wall_deadline(started, cfg) - started) // 1000 + 600)

    def stage_mcp(write_file, agent_dir: str) -> None:
        mcp_tokens.append(
            mcp.stage(write_file, agent_dir, "implementer", f"{issue_id}:{seq}", token_ttl_s)
        )

    def ensure_sandbox(issue: Issue, bound: Config) -> PodSandbox:
        nonlocal installation
        repos = runner_mod.target_repos(issue)
        installation = installation_credential(issue, bound, os.environ, now_ms)
        if installation is None:
            log.warning("github_app_not_configured", issue_id=issue_id)
        name = sandbox_name(issue.identifier)
        pod = kube.get_workspace(core, ns, name)
        if pod is not None and any(
            source.secret_ref is not None
            for container in (pod.spec.containers or [])
            if container.name == kube.WORKSPACE_CONTAINER
            for source in (container.env_from or [])
        ):
            # Secret envFrom is immutable and exposes every provider key. Replace
            # pre-upgrade pods once; subsequent attempts use fresh private files.
            log.info("workspace_credential_binding_recreating", pod=name)
            kube.delete_pod(core, ns, name, wait=True)
            pod = None
        created = False
        if pod is not None and ((pod.status and pod.status.phase) or "") not in (
            "Running",
            "Pending",
        ):
            # dead workspace — activeDeadline fired or the node evicted it. The pod
            # object lingers (restartPolicy Never); replace it and rebuild from the
            # clone hook + WIP branch, the same path a Modal sandbox death took.
            log.warning("workspace_pod_dead_recreating", pod=name)
            kube.delete_pod(core, ns, name, wait=True)
            pod = None
        if pod is None:
            created = kube.create_workspace(core, ns, bound, name)
        else:
            kube.wait_workspace_ready(core, ns, name)
        # The clone hook needs these BEFORE it runs, which is why they belong to the
        # sandbox wrapper rather than to the engine launch.
        bb_env = base_branches_env(bound, repos)
        wrapped = PodSandbox(
            core,
            ns,
            name,
            env={
                "SYMPHONY_TARGET_REPO": repos[0] if repos else "",
                "SYMPHONY_TARGET_REPOS": " ".join(repos),
                **({"SYMPHONY_BASE_BRANCHES": bb_env} if bb_env else {}),
            },
        )
        if (
            not created
            and repos
            and not workspace_matches_repo(wrapped, bound.workspace_root, repos[0])
        ):
            # The ticket was re-pointed at a different repository since this workspace
            # was built. `after_create` — which owns the clone — runs only on creation,
            # so reusing this one would work the issue against the previous repository's
            # code with a credential scoped to the new one.
            log.warning("workspace_repo_mismatch_recreating", pod=name, target_repo=repos[0])
            kube.delete_pod(core, ns, name, wait=True)
            return ensure_sandbox(issue, bound)
        if not created:
            if not kill_agent_processes(wrapped):
                # zombie survived the pkill: terminate and start clean (§3.7.3)
                log.warning("zombie_survived_terminating_workspace", pod=name)
                kube.delete_pod(core, ns, name, wait=True)
                return ensure_sandbox(issue, bound)
        kube.set_idle_since(core, ns, name, None)  # busy again: the reaper must not touch it
        # The credential helper must exist BEFORE after_create, which clones the repo.
        if installation is not None:
            install_credential_helper(wrapped, installation.token())
        prepare_provider(wrapped, core, ns, bound, stage_mcp)
        if created:
            run_hook(wrapped, "after_create", bound, log=lambda e, **kw: log.warning(e, **kw))
        run_hook(wrapped, "before_run", bound, log=lambda e, **kw: log.warning(e, **kw))
        return wrapped

    def refresh_credentials(wrapped: PodSandbox) -> bool:
        """Installation tokens live an hour; attempts run up to four (FR-017)."""
        try:
            if installation is not None and installation.needs_refresh():
                install_credential_helper(wrapped, installation.token(force_refresh=True))
            return True
        except Exception as exc:  # noqa: BLE001 — reported as its own failure reason
            log.error("credential_refresh_failed", error=str(exc))
            return False

    def open_client(wrapped: PodSandbox, attempt_token: str, issue: Issue, bound: Config) -> OmpClient:
        # Per-issue session directory on the workspace's own PVC: sessions are NEVER
        # mixed across agents (FR-014); terminal cleanup removes the claim. The engine
        # takes the directory as an explicit flag, so HOME is left alone.
        proc = kube.open_engine(
            wrapped,
            engine_launch(bound, f"{SESSION_MOUNT}/{wrapped.name}"),
            bound.workspace_root,
            env=engine_env(bound, issue_id, seq, attempt_token, runner_mod.target_repos(issue)),
            pidfile=f"{CREDENTIAL_DIR}/engine-{seq}.pid",
        )
        return OmpClient(proc, proc, proc)

    env = runner_mod.RunnerEnv(
        kv=kv,
        tracker=tracker,
        cfg=cfg,
        now_ms=now_ms,
        call_id=call_id,
        ensure_sandbox=ensure_sandbox,
        open_client=open_client,
        # SIGTERM already stopped the engine; this sweeps the generation's leftovers
        # (tool subprocesses) and confirms nothing of it survives.
        kill_agent=lambda wrapped: kill_agent_generation(wrapped, issue_id, seq),
        # start the idle clock: the reaper — the idle_timeout stand-in — takes it from here
        release_sandbox=lambda wrapped: kube.set_idle_since(core, ns, wrapped.name, now_ms()),
        wall_deadline_ms=runner_mod.compute_env_wall_deadline(started, cfg),
        sessions=SessionStore(kv.client, ns),
        team=TeamConfig(kv.client, ns),
        sleep_s=time.sleep,
        refresh_credentials=refresh_credentials,
        log=lambda event, **kw: log.warning(event, **kw),
        mcp_guidance=lambda: mcp.prompt_guidance("implementer"),
    )
    try:
        outcome = runner_mod.run_attempt(env, issue_id, attempt, seq)
    finally:
        for token in mcp_tokens:
            mcp.end(token)
    # The reason travels with the outcome. It was already persisted to the state entry,
    # but nothing surfaced it: the operator saw `outcome='failed'` on repeat and had to
    # read the store by hand to learn why.
    entry = kv.get(state.issue_key(issue_id)) or {}
    log.info(
        "runner_finished",
        issue_id=issue_id,
        attempt=attempt,
        seq=seq,
        outcome=outcome,
        reason=entry.get("failure_summary"),
    )
    return outcome


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — an infra crash marks the Job Failed;
        # recovery is the daemon's (stall / orphan-claim reconciliation), never a
        # platform retry (backoffLimit: 0)
        log.error("runner_crashed", error=str(exc))
        sys.exit(1)
