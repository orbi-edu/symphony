"""K8sEffects — orchestrator.Effects implemented over Jobs and workspace pods.

Port of the Modal edge's ModalEffects, primitive by primitive:

    spawn_runner        Function.spawn            →  Job create (deterministic name)
    cancel_runner       FunctionCall.cancel       →  Job delete (Foreground)
    is_call_running     FunctionCall.get(0)       →  Job conditions
    kill_agent_process  sb.exec pkill             →  pod exec pkill
    cleanup_workspace   before_remove+terminate   →  hook + pod delete + session PVC delete
    release_workspace   (idle_timeout implicit)   →  idle-since annotation (reaper reads it)
"""

from __future__ import annotations

import os
import re
import shlex
import time

from . import codehost, ghapp, kube
from .edge import log, make_tracker, now_ms
from .kube import PodSandbox
from .trackers.base import Issue
from .workspace import kill_agent_processes, run_hook, sandbox_name

# structlog's key=value rendering of an `error=` field
_LOGGED_ERROR = re.compile(r"\berror='((?:[^'\\]|\\.)*)'")


def _generation_stop_script(issue_id: str, seq: int) -> str:
    # Workspaces are Linux containers. Environment markers are set by run_issue for
    # the engine and inherited by its children; a delayed request cannot match a new seq.
    return (
        f"wanted_issue={shlex.quote('SYMPHONY_ISSUE_ID=' + issue_id)}\n"
        f"wanted_seq={shlex.quote('SYMPHONY_DISPATCH_SEQ=' + str(seq))}\n"
        r"""
[ -d /proc/self ] || exit 1
generation_pids() {
    for path in /proc/[0-9]*/environ; do
        [ -d "${path%/environ}" ] || continue
        if environment=$(tr '\000' '\n' < "$path" 2>/dev/null); then
            :
        else
            # A vanished process is harmless, and so is a zombie: it has exited, holds
            # nothing, and its environ cannot be read (ESRCH). State is read only after the
            # failure: any process may exit mid-scan, and a zombie never comes back to life.
            # A status that can no longer be read means the process is gone.
            [ -d "${path%/environ}" ] || continue
            state=$(sed -n 's/^State:[[:space:]]*//p' "${path%/environ}/status" 2>/dev/null) ||
                continue
            case $state in
                Z*|X*) continue ;;
            esac
            # An unreadable live process is not proof.
            return 1
        fi
        if printf '%s\n' "$environment" | grep -Fqx -- "$wanted_issue" &&
           printf '%s\n' "$environment" | grep -Fqx -- "$wanted_seq"; then
            pid=${path#/proc/}
            printf '%s\n' "${pid%/environ}"
        fi
    done
}
for round in 1 2 3 4 5; do
    pids=$(generation_pids) || exit 1
    [ -n "$pids" ] || exit 0
    signal=TERM
    [ "$round" != 5 ] || signal=KILL
    kill -"$signal" $pids 2>/dev/null || true
    sleep 1
done
pids=$(generation_pids) || exit 1
[ -z "$pids" ]
"""
    )



def kill_agent_generation(sandbox, issue_id: str, seq: int) -> None:
    code, _ = sandbox.exec_script(_generation_stop_script(issue_id, seq), timeout_s=15)
    if code != 0:
        raise RuntimeError("Agent generation termination could not be confirmed")

class K8sEffects:
    def __init__(self, cfg, core, batch, namespace: str) -> None:
        self.cfg = cfg
        self._core = core
        self._batch = batch
        self._ns = namespace
        self.authoritative = lambda: True
        # Installation tokens live an hour and the daemon lives indefinitely, so they are
        # cached per repository and renewed through the credential's own expiry check
        # rather than minted per call — the review watch polls, and minting on every poll
        # would turn a read into a signing operation.
        self._credentials: dict[str, ghapp.InstallationCredential] = {}
        self._reader = codehost.GitHubReader(self._installation_token)

    def _require_lease(self) -> None:
        if not self.authoritative():
            raise RuntimeError("Daemon lease lost before external effect")

    def _installation_token(self, repo: str) -> str:
        cred = self._credentials.get(repo)
        if cred is None:
            cred = ghapp.InstallationCredential.from_env(os.environ, now_ms, target_repos=(repo,))
            self._credentials[repo] = cred
        return cred.token()

    def open_pulls(self, repo: str) -> list[dict]:
        return self._reader.open_pulls(repo)

    def pr_conversation(self, repo: str, number: int) -> list[dict]:
        return self._reader.conversation(repo, number)

    def spawn_runner(self, issue: Issue, attempt: int, seq: int) -> None:
        self._require_lease()
        kube.spawn_runner_job(
            self._batch, self._ns, self.cfg, issue.id, issue.identifier, attempt, seq
        )

    def cancel_runner(self, call_id: str, *, issue_id: str, seq: int) -> None:
        # A deletion request is not proof the old process stopped. Foreground deletion
        # retains the Job until dependent pods are gone; never release a successor early.
        from kubernetes.client.exceptions import ApiException

        try:
            job = self._batch.read_namespaced_job(call_id, self._ns, _request_timeout=10)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        labels = job.metadata.labels or {}
        containers = job.spec.template.spec.containers
        runner = next((container for container in containers if container.name == "runner"), None)
        original_env = {item.name: item.value for item in runner.env or []} if runner else {}
        # These values belong to the immutable Job template, not hot-reloaded policy.
        # The lifecycle supplies the exact dispatch identity; UID fences the deletion.
        if (labels.get("component") != "runner"
                or runner is None
                or runner.command != ["python", "-m", "symphony.entry.run_issue"]
                or original_env.get("SYMPHONY_ISSUE_ID") != issue_id
                or original_env.get("SYMPHONY_SEQ") != str(seq)
                or original_env.get("SYMPHONY_CALL_ID") != call_id):
            raise RuntimeError("Refusing to cancel an unowned runner Job")
        self._require_lease()
        self._batch.delete_namespaced_job(
            call_id, self._ns,
            body={"propagationPolicy": "Foreground", "preconditions": {"uid": job.metadata.uid}},
            _request_timeout=10,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self._batch.read_namespaced_job(call_id, self._ns, _request_timeout=10)
            except ApiException as exc:
                if exc.status == 404:
                    return
                raise
            time.sleep(0.5)
        raise RuntimeError("Runner Job termination is not yet confirmed")

    def is_call_running(self, call_id: str) -> bool:
        return kube.job_status(self._batch, self._ns, call_id) == "running"

    def failure_evidence(self, call_id: str) -> dict:
        """What the cluster knows about a failed runner Job: its failure condition, how the
        runner container ended, the last error it logged and its last log lines (the agent's
        own output excluded). Best effort: each part that cannot be read is left out."""
        out: dict = {"job": call_id}
        try:
            job = self._batch.read_namespaced_job(call_id, self._ns, _request_timeout=10)
            for c in (job.status and job.status.conditions) or []:
                if c.type == "Failed" and c.status == "True":
                    out["reason"] = f"{c.reason}: {c.message}"
            pods = self._core.list_namespaced_pod(
                self._ns, label_selector=f"job-name={call_id}", _request_timeout=10).items
        except Exception as exc:  # noqa: BLE001 — evidence is a courtesy
            log.warning("failure_evidence_unavailable", job=call_id, error=str(exc)[:200])
            return out
        if not pods:
            return out
        pod = max(pods, key=lambda p: p.metadata.creation_timestamp)
        for status in pod.status.container_statuses or []:
            ended = status.state and status.state.terminated
            if status.name == "runner" and ended:
                out["container"] = f"{ended.reason} (exit {ended.exit_code})"
        try:
            text = self._core.read_namespaced_pod_log(
                pod.metadata.name, self._ns, container="runner", tail_lines=300, _request_timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("failure_evidence_log_unavailable", job=call_id, error=str(exc)[:200])
            return out
        lines = [line for line in (text or "").splitlines()
                 if line.strip() and "event='engine_stdout'" not in line]
        errors = [m.group(1) for line in lines for m in [_LOGGED_ERROR.search(line)] if m]
        if errors:
            out["error"] = errors[-1]
        out["lines"] = lines[-40:]
        return out

    def kill_agent_process(
        self, sandbox_name_: str | None, *, issue_id: str | None = None, seq: int | None = None
    ) -> None:
        pod = kube.get_workspace(self._core, self._ns, sandbox_name_) if sandbox_name_ else None
        self._require_lease()
        if pod is not None and pod.status.phase not in {"Failed", "Succeeded"}:
            sandbox = PodSandbox(self._core, self._ns, sandbox_name_)
            if issue_id is not None and seq is not None:
                kill_agent_generation(sandbox, issue_id, seq)
                stopped = True
            else:
                stopped = kill_agent_processes(sandbox)
            if not stopped:
                raise RuntimeError("Agent process termination could not be confirmed")

    def cleanup_workspace(self, issue_id: str, entry: dict) -> None:
        name = entry.get("sandbox_name") or (
            sandbox_name(entry["identifier"]) if entry.get("identifier") else None
        )
        pod = kube.get_workspace(self._core, self._ns, name) if name else None
        if pod is not None:
            if pod.status.phase not in {"Failed", "Succeeded"}:
                sandbox = PodSandbox(self._core, self._ns, name)
                try:
                    # A hook failure must never block cleanup (FR-015).
                    run_hook(sandbox, "before_remove", self.cfg, log=lambda e, **kw: log.warning(e, **kw))
                except Exception as exc:  # noqa: BLE001
                    log.warning("before_remove_exec_failed_ignored", error=str(exc))
            self._require_lease()
            kube.delete_pod(self._core, self._ns, name)
        if name:
            self._require_lease()
            try:
                kube.delete_session_pvc(self._core, self._ns, name)
            except Exception:  # noqa: BLE001 — the claim may never have created one
                pass

    def release_workspace(self, issue_id: str, entry: dict) -> None:
        """Claim released while KEEPING the workspace (FR-014). The annotation starts
        the idle clock; the reaper — not this call — decides when the pod goes."""
        name = entry.get("sandbox_name")
        if name:
            self._require_lease()
            kube.set_idle_since(self._core, self._ns, name, now_ms())

    def spawn_reflector(self, issue_id: str, entry: dict) -> None:
        self._require_lease()
        kube.spawn_reflector_job(self._batch, self._ns, self.cfg, issue_id, entry)

    def emit(self, event: str, **fields) -> None:
        log.info(event, **fields)

    def report_to_ticket(self, issue_id: str, message: str) -> None:
        """Post an orchestrator-authored note on the ticket.

        Goes through the tracker adapter, so the credential stays on the host. Best effort
        by construction: an unreachable tracker must never turn a failed attempt into a
        failed daemon — the retry is already scheduled and losing the note costs
        visibility, not correctness.
        """
        try:
            make_tracker(self.cfg).comment(issue_id, message)
        except Exception as exc:  # noqa: BLE001 — visibility is not worth the daemon
            log.warning("ticket_report_failed", issue_id=issue_id, error=str(exc))

    def notify(self, event: str, text: str, *, link: str | None = None) -> None:
        from . import notify  # never raises: a channel failure is logged there

        notify.post(event, text, link=link)
