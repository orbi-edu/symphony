"""Kubernetes edge — the only module that talks to the API server.

Concrete wrappers for the pure interfaces, mirroring what app.py's Modal classes did:

- `PodSandbox`      implements `workspace.SandboxLike` over `pod exec` (WebSocket);
- `EngineProc`      implements the `omp_rpc` Line{Reader,Writer} protocols over an
                    interactive exec into the workspace pod;
- workspace pods    replace `modal.Sandbox` (named `ws-<key>`, a reaping PID 1,
                    exec'd into; per-workspace PVC replaces the session Volume);
- Jobs              replace `Function.spawn` (one Job per dispatch, `backoffLimit: 0`
                    — platform retries stay off, recovery belongs to the orchestrator,
                    constitution IV).

Everything decidable lives in the pure modules; this file holds manifests and streams.
"""

from __future__ import annotations

import os
import queue
import shlex
import threading
import time

from . import endpoints
from .edge import SESSION_MOUNT, WORKFLOW_MOUNT, WORKSPACE_CONTAINER, WORKSPACE_INIT, log
from .omp_rpc import split_frames
from .workspace import dns_name, workspace_key

IDLE_ANNOTATION = "symphony.dev/idle-since"
# The cluster autoscaler does not remove a node that runs a pod with this set to "false".
SAFE_TO_EVICT_ANNOTATION = "cluster-autoscaler.kubernetes.io/safe-to-evict"

# Secret/ConfigMap names, overridable so several installs can share a namespace.
SECRET_TRACKER = os.environ.get("SYMPHONY_SECRET_TRACKER", "linear-api-key")
SECRET_GITHUB = os.environ.get("SYMPHONY_SECRET_GITHUB", "github-app")
SECRET_PROVIDER = os.environ.get("SYMPHONY_SECRET_PROVIDER", "model-provider")
SECRET_SLACK = os.environ.get("SYMPHONY_SECRET_SLACK", "symphony-slack")
SECRET_DISCORD = os.environ.get("SYMPHONY_SECRET_DISCORD", "symphony-discord")

# Runtime -> image. The deploy step builds and pushes these (scripts/build_images.sh);
# WORKFLOW.md's kubernetes.agent_images overrides per runtime, kubernetes.agent_image
# overrides outright (the Modal port's snapshot_image successor).
DEFAULT_AGENT_IMAGES: dict[str, str] = {
    "node": "symphony-agent:node",
    "dotnet9": "symphony-agent:dotnet9",
    "dotnet10": "symphony-agent:dotnet10",
    "polyglot": "symphony-agent:polyglot",
}

# Generous on purpose: a cold node pulling the .NET SDK image takes minutes, and giving
# up mid-pull burns the attempt for a reason that fixes itself.
WORKSPACE_READY_TIMEOUT_S = 600

_PASSTHROUGH_ENV = (
    "REDIS_URL",
    "SYMPHONY_APP_NAME",
    "SYMPHONY_GATEWAY_URL",
    "SYMPHONY_ORCHESTRATOR_IMAGE",
    "SYMPHONY_RUNNER_SERVICE_ACCOUNT",
    "SYMPHONY_WORKFLOW_CONFIGMAP",
    "SYMPHONY_SECRET_TRACKER",
    "SYMPHONY_SECRET_GITHUB",
    "SYMPHONY_SECRET_PROVIDER",
    "SYMPHONY_SECRET_PROVIDERS",
    "SYMPHONY_SECRET_SLACK",
    "SYMPHONY_SECRET_DISCORD",
    *endpoints.NAMES,
)


class WorkspaceDead(Exception):
    """The workspace pod exists but cannot run (Failed/Succeeded/deadline-exceeded)."""


def get_clients():
    from kubernetes import client
    from kubernetes import config as kconfig

    try:
        kconfig.load_incluster_config()
    except Exception:  # noqa: BLE001 — local drives and tests use the operator's kubeconfig
        kconfig.load_kube_config()
    return client.CoreV1Api(), client.BatchV1Api()


def _api_exception():
    from kubernetes.client.rest import ApiException

    return ApiException


def runner_job_name(identifier: str, seq: int) -> str:
    """Deterministic per dispatch: a platform-level duplicate create collides on the
    name (409) the same way a duplicate dispatch collides on the token."""
    return f"sym-run-{dns_name(workspace_key(identifier), 24)}-s{seq}"


def reflector_job_name(identifier: str) -> str:
    return f"sym-reflect-{dns_name(workspace_key(identifier), 24)}"


def agent_image_for(cfg, runtime: str | None = None) -> str:
    if cfg.agent_image:
        return cfg.agent_image
    images = {**DEFAULT_AGENT_IMAGES, **(cfg.agent_images or {})}
    rt = runtime or cfg.agent_runtime
    if rt not in images:
        log.warning("unknown_agent_runtime_using_default", runtime=rt)
        return images["node"]
    return images[rt]


def orchestrator_image() -> str:
    image = os.environ.get("SYMPHONY_ORCHESTRATOR_IMAGE", "")
    if not image:
        raise RuntimeError(
            "SYMPHONY_ORCHESTRATOR_IMAGE is not set — the daemon cannot spawn runner "
            "jobs without knowing its own image (set it in deploy/daemon.yaml)"
        )
    return image


# --------------------------------------------------------------------------
# exec plumbing
# --------------------------------------------------------------------------
class PodSandbox:
    """Implements SandboxLike over `pod exec`. The Kubernetes counterpart of the Modal
    port's ModalSandbox: same env-exports contract (hooks included), same merged-output
    contract — the reason a run failed has to survive to the operator."""

    def __init__(self, core, namespace: str, name: str, env: dict[str, str] | None = None) -> None:
        self._core = core
        self._ns = namespace
        self.name = name
        self.id = name  # pods are addressed by name; kept for state-schema parity
        # Per-attempt variables every exec in this pod sees, HOOKS INCLUDED (the clone
        # hook reads ${SYMPHONY_TARGET_REPO} — feature 003).
        self.env = dict(env or {})
        self.environment_file: str | None = None

    def _exports(self, extra: dict[str, str] | None = None) -> str:
        source = (
            f". {shlex.quote(self.environment_file)} || exit $?\n" if self.environment_file else ""
        )
        environment = {**self.env, **extra} if extra else self.env
        return source + "".join(
            f"export {key}={shlex.quote(value)}\n" for key, value in sorted(environment.items())
        )

    def _stream(self, command: list[str], stdin: bool):
        from kubernetes.stream import stream

        return stream(
            self._core.connect_get_namespaced_pod_exec,
            self.name,
            self._ns,
            command=command,
            container=WORKSPACE_CONTAINER,
            stderr=True,
            stdin=stdin,
            stdout=True,
            tty=False,
            _preload_content=False,
        )

    def exec_script(
        self, script: str, timeout_s: float | None = None, *, stdin: str | None = None
    ) -> tuple[int, str]:
        """Runs a script and returns (exit_code, combined output).

        stderr is merged into stdout at the shell (`2>&1`) for the same reason the Modal
        port did it: every diagnostic a hook produces goes to stderr, and reading only
        stdout turns every hook failure into a bare exit code with an empty message.

        The server enforces no exec deadline, so the timeout is applied twice: GNU
        `timeout` inside the pod (kills the process tree, exit 124) and a client-side
        wall guard in case the stream itself wedges (returns -1 with partial output,
        uniform with the Modal port's stream-error path).
        Private file payloads use stdin, never the Kubernetes exec command/argv.
        """
        wrapped = f"{self._exports()}{{ {script}\n}} 2>&1"
        command = ["bash", "-lc", wrapped]
        if timeout_s:
            command = ["timeout", "-k", "10", str(int(timeout_s)), "bash", "-lc", wrapped]
        deadline = time.monotonic() + (timeout_s or 3600) + 60
        parts: list[str] = []
        try:
            ws = self._stream(command, stdin=stdin is not None)
        except Exception as exc:  # noqa: BLE001 — a dead pod reports as a failed exec
            return -1, f"exec failed: {exc}"
        try:
            if stdin:
                ws.write_stdin(stdin)
            while ws.is_open():
                if time.monotonic() > deadline:
                    return -1, "".join(parts)
                ws.update(timeout=1)
                if ws.peek_stdout():
                    parts.append(ws.read_stdout())
                if ws.peek_stderr():
                    parts.append(ws.read_stderr())
            if ws.peek_stdout():
                parts.append(ws.read_stdout())
            if ws.peek_stderr():
                parts.append(ws.read_stderr())
            code = ws.returncode
        except Exception:  # noqa: BLE001 — stream errors report failure uniformly
            return -1, "".join(parts)
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        return code if code is not None else 0, "".join(parts)

    def write_private_file(self, path: str, content: str) -> tuple[int, str]:
        """Stage bounded UTF-8 bytes through stdin, with no credential in exec argv."""
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("A private file requires an absolute path.")
        size = len(content.encode("utf-8"))
        if size > 512 * 1024:
            raise ValueError("Private file exceeds the supported size.")
        temporary = shlex.quote(f"{path}.pending")
        destination = shlex.quote(path)
        return self.exec_script(
            f"umask 077; head -c {size} > {temporary} && "
            f'test "$(wc -c < {temporary})" -eq {size} && '
            f"chmod 600 {temporary} && mv -f -- {temporary} {destination}",
            timeout_s=30,
            stdin=content,
        )


class EngineProc:
    """One interactive exec: the engine's stdio, adapted to the omp_rpc protocols.

    The reader pump owns the frame reassembly: the WebSocket delivers CHUNKS, not lines
    — a frame may arrive split across two reads. `split_frames` owns the framing rule
    (LF only, U+2028-safe) because it is the PROTOCOL's rule, not this transport's.
    Engine stderr is diagnostics and is drained into the pod logs, never into frames.
    """

    def __init__(self, ws, sandbox: PodSandbox, pidfile: str) -> None:
        self._ws = ws
        self._sandbox = sandbox
        self._pidfile = pidfile
        self._q: queue.Queue[str | None] = queue.Queue()
        self._closed = threading.Event()
        name = sandbox.name

        def pump() -> None:
            carry = ""
            try:
                while ws.is_open():
                    ws.update(timeout=1)
                    if ws.peek_stdout():
                        frames, carry = split_frames(carry + ws.read_stdout())
                        for frame in frames:
                            self._q.put(frame)
                    if ws.peek_stderr():
                        for line in ws.read_stderr().splitlines():
                            if line.strip():
                                log.info("engine_stderr", pod=name, line=line[:500])
                if carry.strip():
                    self._q.put(carry)
            except Exception as exc:  # noqa: BLE001 — a broken stream reads as silence
                log.warning("engine_stream_error", pod=name, error=str(exc))
            finally:
                self._closed.set()
                self._q.put(None)

        threading.Thread(target=pump, daemon=True).start()

    def write_line(self, line: str) -> None:
        self._ws.write_stdin(line + "\n")

    def read_line(self, timeout_s: float) -> str | None:
        try:
            return self._q.get(timeout=timeout_s)  # None = stream closed (silence to the client)
        except queue.Empty:
            return None

    def terminate(self, grace_s: float) -> bool:
        """SIGTERM this generation's engine (by the pid it recorded at launch) and wait for
        its exec stream to close — the engine exports its last span batch on SIGTERM."""
        if not self._closed.is_set():
            pidfile = shlex.quote(self._pidfile)
            self._sandbox.exec_script(
                f'pid=$(cat {pidfile} 2>/dev/null) && [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null; '
                "true",
                timeout_s=15,
            )
        exited = self._closed.wait(grace_s)
        if exited:
            self._sandbox.exec_script(f"rm -f -- {shlex.quote(self._pidfile)}", timeout_s=15)
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 — the stream may already be gone
            pass
        return exited


def open_engine(
    sandbox: PodSandbox, launch_script: str, workdir: str, env: dict[str, str], pidfile: str
) -> EngineProc:
    """Exec the engine inside the workspace pod with an interactive stdin.

    `pod exec` takes no env or workdir of its own, so both ride in the script — same
    export trick every other exec in this pod already uses. The shell records its pid
    before `exec`-ing the engine (same pid), so the stop path can SIGTERM exactly this
    generation's engine.
    """
    pf = shlex.quote(pidfile)
    script = (
        f"{sandbox._exports(env)}mkdir -p {shlex.quote(os.path.dirname(pidfile))} && "
        f"echo $$ > {pf} && cd {shlex.quote(workdir)} && {launch_script}"
    )
    ws = sandbox._stream(["bash", "-lc", script], stdin=True)
    return EngineProc(ws, sandbox, pidfile)


# --------------------------------------------------------------------------
# workspace pod + session PVC lifecycle
# --------------------------------------------------------------------------
def session_pvc_name(pod_name: str) -> str:
    return f"{pod_name}-omp"


def _workspace_manifest(cfg, name: str, image: str) -> dict:
    mem = f"{cfg.sandbox['memory_mib']}Mi"
    cpu = str(cfg.sandbox["cpu"])
    volume: dict = (
        {"persistentVolumeClaim": {"claimName": session_pvc_name(name)}}
        if cfg.sessions_enabled
        else {"emptyDir": {}}
    )
    mounts = [{"name": "omp-sessions", "mountPath": SESSION_MOUNT}]
    volumes = [{"name": "omp-sessions", **volume}]
    if cfg.privileged_agents or cfg.runtime_class:
        # dockerd cannot put overlay2 on top of the container's own overlayfs (the
        # kernel rejects nested overlay upperdirs), so without a real mount it silently
        # falls back to the vfs storage driver — every layer becomes a full copy and
        # pulls crawl. An emptyDir gives /var/lib/docker a proper backing filesystem;
        # it dies with the pod, which is fine — images are a cache, not state. Same
        # trick the official docker:dind image documents.
        #
        # Kept under a runtime_class too, deliberately: Sysbox would otherwise provision
        # an IMPLICIT host mount here (its special-dir handling), which works but leaves
        # per-pod state on the node outside the kubelet's cleanup. An explicit emptyDir
        # keeps the lifecycle in Kubernetes' hands on every runtime, and Sysbox skips
        # its implicit mount when the spec already provides one.
        mounts.append({"name": "docker-lib", "mountPath": "/var/lib/docker"})
        volumes.append({"name": "docker-lib", "emptyDir": {}})
    spec_extra: dict = {}
    if cfg.runtime_class:
        # The isolation upgrade path (SPEC D6): `sysbox-runc` = dockerd without
        # privileged via user namespaces; a Kata class = VM boundary. The RuntimeClass
        # object (installed with the runtime) owns node scheduling for it.
        spec_extra["runtimeClassName"] = cfg.runtime_class
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {"app": cfg.app_name, "component": "workspace"},
        },
        "spec": {
            **spec_extra,
            "restartPolicy": "Never",
            # The agent must not be able to reach the API server: the pod carries no
            # ServiceAccount token at all. This is the K8s edge of constitution III —
            # nothing in the sandbox may hold a credential beyond the derived, scoped
            # ones the runner writes into it.
            "automountServiceAccountToken": False,
            # modal.Sandbox's `timeout`: hard ceiling on the pod's life. The runner's
            # wall-clock guard exits attempts long before this fires.
            "activeDeadlineSeconds": int(cfg.sandbox["timeout_s"]),
            "terminationGracePeriodSeconds": 10,
            "imagePullSecrets": [{"name": s} for s in cfg.image_pull_secrets],
            "containers": [
                {
                    "name": WORKSPACE_CONTAINER,
                    "image": image,
                    "command": ["bash", "-c", WORKSPACE_INIT],
                    "resources": {
                        "requests": {"cpu": cpu, "memory": mem},
                        "limits": {"cpu": cpu, "memory": mem},
                    },
                    # dockerd needs privileged; the flag is the operator's explicit
                    # trade-off (kubernetes.privileged_agents, SPEC deviations).
                    "securityContext": {"privileged": bool(cfg.privileged_agents)},
                    "volumeMounts": mounts,
                    # endpoint overrides (internals §14), only when set
                    **({"env": [{"name": k, "value": v} for k, v in endpoints.overrides().items()]}
                       if endpoints.overrides() else {}),
                }
            ],
            "volumes": volumes,
        },
    }


def _session_pvc_manifest(cfg, pod_name: str) -> dict:
    spec: dict = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": cfg.session_volume_size}},
    }
    if cfg.storage_class:
        spec["storageClassName"] = cfg.storage_class
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": session_pvc_name(pod_name),
            "labels": {"app": cfg.app_name, "component": "workspace-sessions"},
        },
        "spec": spec,
    }


def get_workspace(core, ns: str, name: str):
    ApiException = _api_exception()
    try:
        return core.read_namespaced_pod(name, ns)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def create_workspace(core, ns: str, cfg, name: str, runtime: str | None = None) -> bool:
    """Create the workspace pod (and its session PVC) and wait for it to run.

    Returns True when THIS caller created it; False when it lost the create race and
    reattached — the same AlreadyExistsError contract Sandbox.create had, and the
    dispatch fencing upstream is what makes either answer safe.
    """
    ApiException = _api_exception()
    if cfg.sessions_enabled:
        try:
            core.create_namespaced_persistent_volume_claim(ns, _session_pvc_manifest(cfg, name))
        except ApiException as exc:
            if exc.status != 409:  # already exists: a previous attempt's sessions — the point
                raise
    created = True
    try:
        core.create_namespaced_pod(ns, _workspace_manifest(cfg, name, agent_image_for(cfg, runtime)))
    except ApiException as exc:
        if exc.status != 409:
            raise
        created = False  # lost the create race — reattach
    wait_workspace_ready(core, ns, name)
    return created


def workspace_needs_recreate(pod, cfg, runtime: str | None = None) -> bool:
    """True when the running pod's image differs from the resolved image.

    Used to rebuild the workspace when the runtime changes between attempts
    (e.g., a component label is added). The session PVC is kept."""
    if pod is None:
        return False
    target_image = agent_image_for(cfg, runtime)
    for container in (pod.spec.containers or []):
        if container.name == WORKSPACE_CONTAINER:
            return container.image != target_image
    return False


def wait_workspace_ready(
    core, ns: str, name: str, timeout_s: float = WORKSPACE_READY_TIMEOUT_S
) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        pod = get_workspace(core, ns, name)
        if pod is None:
            raise WorkspaceDead(f"workspace pod {name} vanished while waiting for it")
        phase = (pod.status and pod.status.phase) or ""
        if phase == "Running":
            return
        if phase in ("Failed", "Succeeded"):
            raise WorkspaceDead(f"workspace pod {name} is {phase}")
        if time.monotonic() > deadline:
            raise WorkspaceDead(f"workspace pod {name} not ready after {int(timeout_s)}s ({phase})")
        time.sleep(2)


def delete_pod(core, ns: str, name: str, wait: bool = False, timeout_s: float = 120) -> None:
    ApiException = _api_exception()
    try:
        core.delete_namespaced_pod(name, ns)
    except ApiException as exc:
        if exc.status == 404:
            return
        raise
    if not wait:
        return
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if get_workspace(core, ns, name) is None:
            return
        time.sleep(2)


def delete_session_pvc(core, ns: str, pod_name: str) -> None:
    ApiException = _api_exception()
    try:
        core.delete_namespaced_persistent_volume_claim(session_pvc_name(pod_name), ns)
    except ApiException as exc:
        if exc.status != 404:
            raise


def set_idle_since(core, ns: str, name: str, at_ms: int | None) -> None:
    """Mark (or clear) the workspace as released. The reaper reads this annotation —
    the K8s stand-in for modal.Sandbox's idle_timeout, which no pod has natively."""
    ApiException = _api_exception()
    body = {"metadata": {"annotations": {IDLE_ANNOTATION: str(at_ms) if at_ms else None}}}
    try:
        core.patch_namespaced_pod(name, ns, body)
    except ApiException as exc:
        if exc.status != 404:
            log.warning("idle_annotation_failed", pod=name, error=str(exc))


def reap_idle_workspaces(core, ns: str, cfg, now_ms: int) -> int:
    """Delete workspace pods idle beyond sandbox.idle_timeout_s. The session PVC stays:
    the next attempt's pod resumes the engine session, and /workspace is rebuilt from
    the clone hook + the WIP branch — the same degradation path the Modal port had."""
    idle_ms = int(cfg.sandbox.get("idle_timeout_s", 1800)) * 1000
    reaped = 0
    pods = core.list_namespaced_pod(ns, label_selector=f"app={cfg.app_name},component=workspace")
    for pod in pods.items:
        annotations = pod.metadata.annotations or {}
        since = annotations.get(IDLE_ANNOTATION)
        if not since:
            continue
        try:
            since_ms = int(since)
        except ValueError:
            continue
        if now_ms - since_ms > idle_ms:
            log.info("idle_workspace_reaped", pod=pod.metadata.name)
            delete_pod(core, ns, pod.metadata.name)
            reaped += 1
    return reaped


# --------------------------------------------------------------------------
# jobs (runner / reflector)
# --------------------------------------------------------------------------
def _env_list(extra: dict[str, str]) -> list[dict]:
    out = [{"name": k, "value": v} for k, v in extra.items()]
    for key in _PASSTHROUGH_ENV:
        value = os.environ.get(key)
        if value and key not in extra:
            out.append({"name": key, "value": value})
    out.append(
        {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}
    )
    return out


def _job_manifest(
    cfg,
    name: str,
    module: str,
    env: dict[str, str],
    deadline_s: int,
    component: str,
) -> dict:
    workflow_cm = os.environ.get("SYMPHONY_WORKFLOW_CONFIGMAP", f"{cfg.app_name}-workflow")
    sa = os.environ.get("SYMPHONY_RUNNER_SERVICE_ACCOUNT", f"{cfg.app_name}-runner")
    labels = {"app": cfg.app_name, "component": component}
    mem = f"{cfg.runner['memory_mib']}Mi"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            # retries=0, as the Modal functions declared: the platform re-running work
            # blindly is poison for an orchestrator with its own retry policy. Even so,
            # any re-execution that slips through is absorbed by the dispatch tokens.
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 3600,
            "activeDeadlineSeconds": deadline_s,
            "template": {
                "metadata": {
                    "labels": labels,
                    # A drain or scale-down would kill the attempt in flight; the daemon,
                    # not the platform, decides when a runner ends.
                    "annotations": {SAFE_TO_EVICT_ANNOTATION: "false"},
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": sa,
                    "imagePullSecrets": [{"name": s} for s in cfg.image_pull_secrets],
                    "containers": [
                        {
                            "name": component,
                            "image": orchestrator_image(),
                            "command": ["python", "-m", module],
                            "env": _env_list(env),
                            # A request keeps the pod out of BestEffort, which the kubelet
                            # evicts first under node pressure. No CPU limit: the runner
                            # waits on I/O and throttling only slows the attempt.
                            "resources": {
                                "requests": {"cpu": str(cfg.runner["cpu"]), "memory": mem},
                                "limits": {"memory": mem},
                            },
                            "envFrom": [
                                # Optional like every integration: a Job is only spawned
                                # once setup configured the tracker, and a missing App is
                                # reported by the runner instead of a stuck pod.
                                {"secretRef": {"name": SECRET_TRACKER, "optional": True}},
                                {"secretRef": {"name": SECRET_GITHUB, "optional": True}},
                                # Slack and Discord are optional: without them
                                # `notify.post` does nothing.
                                {"secretRef": {"name": SECRET_SLACK, "optional": True}},
                                {"secretRef": {"name": SECRET_DISCORD, "optional": True}},
                            ],
                            "volumeMounts": [
                                {"name": "workflow", "mountPath": WORKFLOW_MOUNT, "readOnly": True}
                            ],
                        }
                    ],
                    "volumes": [{"name": "workflow", "configMap": {"name": workflow_cm}}],
                },
            },
        },
    }


def create_job(batch, ns: str, manifest: dict) -> bool:
    """False when the name already exists — a duplicate spawn that the deterministic
    name absorbed, exactly as the dispatch token would have."""
    ApiException = _api_exception()
    try:
        batch.create_namespaced_job(ns, manifest)
        return True
    except ApiException as exc:
        if exc.status == 409:
            log.warning("job_already_exists", job=manifest["metadata"]["name"])
            return False
        raise


def spawn_runner_job(
    batch, ns: str, cfg, issue_id: str, identifier: str, attempt: int, seq: int
) -> str:
    name = runner_job_name(identifier, seq)
    manifest = _job_manifest(
        cfg,
        name,
        "symphony.entry.run_issue",
        {
            "SYMPHONY_ISSUE_ID": issue_id,
            "SYMPHONY_ATTEMPT": str(attempt),
            "SYMPHONY_SEQ": str(seq),
            "SYMPHONY_CALL_ID": name,
        },
        deadline_s=int(cfg.runner.get("timeout_s", 14400)),
        component="runner",
    )
    create_job(batch, ns, manifest)
    return name


def spawn_reflector_job(batch, ns: str, cfg, issue_id: str, entry: dict) -> str:
    import json

    name = reflector_job_name(entry.get("identifier") or issue_id)
    manifest = _job_manifest(
        cfg,
        name,
        "symphony.entry.reflect",
        {
            "SYMPHONY_ISSUE_ID": issue_id,
            "SYMPHONY_ENTRY_JSON": json.dumps(entry),
            "SYMPHONY_CALL_ID": name,
        },
        deadline_s=15 * 60,
        component="reflector",
    )
    create_job(batch, ns, manifest)
    return name


def job_status(batch, ns: str, name: str) -> str:
    """'running' | 'finished' | 'missing'. A Job with no terminal condition is running
    — Pending pods included, which matches FunctionCall's view that a call not yet
    finished is in flight."""
    ApiException = _api_exception()
    try:
        job = batch.read_namespaced_job(name, ns)
    except ApiException as exc:
        if exc.status == 404:
            return "missing"
        raise
    for condition in (job.status and job.status.conditions) or []:
        if condition.type in ("Complete", "Failed") and condition.status == "True":
            return "finished"
    return "running"


def delete_job(batch, ns: str, name: str) -> None:
    ApiException = _api_exception()
    try:
        batch.delete_namespaced_job(name, ns, propagation_policy="Foreground")
    except ApiException as exc:
        if exc.status != 404:
            raise
