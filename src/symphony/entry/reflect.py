"""Reflector — one Job per terminal issue.

`reflector.py` stays pure; this file is the Kubernetes edge: it authenticates as the
App installation, harvests, and runs the distillation as a one-shot engine execution in
an EPHEMERAL pod, with the TeamConfig `reflector` role model.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import sys
import uuid

from .. import ghapp, kube
from .. import reflector as reflector_mod
from ..config import ConfigHolder, bind_role_model
from ..edge import (
    TrackerNotConfigured,
    gateway_url,
    load_config,
    log,
    make_tracker,
    now_ms,
    pod_namespace,
)
from ..kube import WORKSPACE_CONTAINER, PodSandbox
from ..kv import RedisKV
from ..mcp_servers import McpLaunch, McpRepository
from ..provider_runtime import prepare_provider
from ..teamconfig import TeamConfig

DISTILL_POD_DEADLINE_S = 10 * 60


def _distill_pod_manifest(cfg, name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"app": cfg.app_name, "component": "distill"}},
        "spec": {
            # Same isolation boundary as the workspace pod: this one runs the engine
            # too, and an operator who upgraded the agent boundary (Sysbox/Kata) did
            # not mean "except during retrospectives".
            **({"runtimeClassName": cfg.runtime_class} if cfg.runtime_class else {}),
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "activeDeadlineSeconds": DISTILL_POD_DEADLINE_S,
            "terminationGracePeriodSeconds": 5,
            "imagePullSecrets": [{"name": s} for s in cfg.image_pull_secrets],
            "containers": [
                {
                    "name": WORKSPACE_CONTAINER,
                    "image": kube.agent_image_for(cfg),
                    "command": ["sleep", "infinity"],
                    "resources": {
                        "requests": {"cpu": "1", "memory": "2048Mi"},
                        "limits": {"cpu": "1", "memory": "2048Mi"},
                    },
                }
            ],
        },
    }


def make_distill(core, ns: str, mcp: McpLaunch | None = None):
    def k8s_engine_distill(harvest, cfg):
        """Real distillation (FR-027): one-shot engine run in an EPHEMERAL pod.

        Infra failure ⇒ exception — the retro token stays 'started' and the nightly
        sweep retries; no metrics are recorded before distillation, so a retry
        inflates nothing.
        """
        prompt = reflector_mod.build_distill_prompt(harvest)
        name = f"distill-{uuid.uuid4().hex[:8]}"
        core.create_namespaced_pod(ns, _distill_pod_manifest(cfg, name))
        tokens: list[str] = []

        def stage_mcp(write_file, agent_dir: str) -> None:
            # A2: the reflector role's MCP servers, with a token revoked when this run ends.
            tokens.append(mcp.stage(write_file, agent_dir, "reflector", f"distill:{name}", 15 * 60))

        try:
            kube.wait_workspace_ready(core, ns, name, timeout_s=300)
            wrapped = PodSandbox(core, ns, name)
            # Same credential path as an agent workspace: this pod runs the engine too,
            # with the reflector role's API key staged privately (internals §13).
            prepare_provider(wrapped, core, ns, cfg, stage_mcp if mcp is not None else None)
            # base64 rather than a heredoc: the prompt embeds review threads and diffs,
            # i.e. arbitrary text that could contain any delimiter.
            encoded = base64.b64encode(prompt.encode()).decode()
            code, out = wrapped.exec_script(f"echo {encoded} | base64 -d > /tmp/distill-prompt.md")
            if code != 0:
                raise RuntimeError(
                    f"could not stage the distill prompt (exit {code}): {out[-300:]}"
                )
            # -p is the engine's non-interactive print mode: prompt in, final answer out.
            code, out = wrapped.exec_script(
                "exec omp -p --no-title --auto-approve "
                f"--provider {shlex.quote(cfg.engine_provider)} --model {shlex.quote(cfg.engine_model)} "
                f"--thinking {shlex.quote(cfg.engine_thinking or 'medium')} "
                '-- "$(cat /tmp/distill-prompt.md)"',
                timeout_s=9 * 60,
            )
            if code != 0:
                raise RuntimeError(f"engine distillation failed (exit {code}): {out[-300:]}")
            source = f"PR #{harvest.pr_number}" if harvest.pr_number else harvest.issue_identifier
            return reflector_mod.parse_candidates(out, source=source)
        finally:
            for token in tokens:
                mcp.end(token)
            kube.delete_pod(core, ns, name)

    return k8s_engine_distill


def main() -> str:
    issue_id = os.environ["SYMPHONY_ISSUE_ID"]
    entry = json.loads(os.environ.get("SYMPHONY_ENTRY_JSON") or "{}")
    call_id = os.environ.get("SYMPHONY_CALL_ID") or f"local-{uuid.uuid4().hex[:8]}"

    kv = RedisKV()
    holder = ConfigHolder()
    load_config(holder)
    if holder.config is None:
        log.warning("reflector_setup_incomplete", issue_id=issue_id,
                    reason="WORKFLOW.md is missing, empty or invalid.")
        return "setup-incomplete"
    try:
        tracker = make_tracker(holder.config)
    except TrackerNotConfigured as exc:
        # Nothing to harvest from: exit 0 with the reason. The retro token stays as it was,
        # so the nightly sweep fires the retrospective once setup is complete.
        log.warning("reflector_setup_incomplete", issue_id=issue_id, reason=exc.reason)
        return "setup-incomplete"
    ns = pod_namespace(holder.config)
    # internals §4: the unit of work reads the team configuration once, at its start, and
    # reports the generation it applied.
    team = TeamConfig(kv.client, ns)
    team_doc = team.get()
    cfg = bind_role_model(holder.config, team_doc, "reflector")
    team.observe(f"reflector:{entry.get('identifier') or issue_id}", int(team_doc["generation"]))
    core, _batch = kube.get_clients()
    # FR-019: the reflector authenticates as the same App installation. It mints its own
    # token from the host-held private key rather than receiving one from the runner.
    # FR-013/FR-019: the repository comes from the PERSISTED run data, not from the
    # ticket — by now the workspace is gone and the designation may have changed. Lessons
    # land in the PRIMARY repository only, so the token is scoped to it alone.
    target_repo = entry.get("target_repo") or os.environ.get("SYMPHONY_TARGET_REPO", "")
    entry["target_repo"] = target_repo or None
    github = reflector_mod.HttpxGitHub(
        token=ghapp.InstallationCredential.from_env(
            os.environ, now_ms, target_repos=(target_repo,) if target_repo else ()
        ).token(),
        repo=target_repo,
        base_branch=cfg.base_branch(target_repo) if target_repo else None,
    )
    fetch_workpad = getattr(tracker, "fetch_issue_workpad", None)
    outcome = reflector_mod.run_retrospective(
        kv,
        github,
        cfg,
        issue_id,
        entry,
        call_id,
        now_ms(),
        distill=make_distill(core, ns, McpLaunch(kv.client, ns, McpRepository(core, ns), gateway_url(cfg))),
        fetch_workpad=fetch_workpad,  # the ticket's Workpad feeds the harvest (FR-027)
    )
    log.info("reflector_finished", issue_id=issue_id, outcome=outcome)
    return outcome


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — the retro token stays 'started'; the
        # nightly sweep re-adopts and retries (at-least-once)
        log.error("reflector_crashed", error=str(exc))
        sys.exit(1)
