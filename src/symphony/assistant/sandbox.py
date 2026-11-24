"""Per-thread sandbox pods for the assistant: build, run, clean up.

A sandbox is a workspace pod scoped to one chat thread: the same manifest as issue
workspaces (`kube._workspace_manifest`: no ServiceAccount token, the `component: workspace`
label the workspace NetworkPolicy selects), plus the label `role: assistant-sandbox`. It is
created lazily on the first `sandbox_run` call and deleted when the assistant session ends.
The assistant keeps its sessions in memory, so at start every sandbox pod is an orphan and is
deleted; `activeDeadlineSeconds` bounds a pod the assistant never got to delete.

Each repository the thread uses is cloned once, at its base branch, with a GitHub App token
scoped to that repository and `contents: read`. The token reaches the pod on stdin and lives
only in the environment of the one `git clone`; it is not written to the clone's config.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass

from .. import kube
from ..endpoints import github_server_url
from ..merger.agents import HostTool, _obj

ROLE_LABEL = "assistant-sandbox"
THREAD_ANNOTATION = "symphony.dev/thread-key"
TRUNCATION_MARKER = "\n\n... output truncated ...\n\n"
NEEDS_MAPPING = ("The sandbox needs a mapped user: ask an admin to add you to notify.users "
                 "in WORKFLOW.md.")
CLONE_TIMEOUT_S = 300


def pod_name(app_name: str, thread_key: str) -> str:
    """Deterministic, DNS-1123-safe pod name from the thread key."""
    import hashlib
    import re

    slug = re.sub(r"[^a-z0-9-]", "-", thread_key.lower())[-24:].strip("-") or "sb"
    digest = hashlib.sha256(thread_key.encode()).hexdigest()[:10]
    return f"{app_name}-asb-{slug}-{digest}"[:63].rstrip("-")


def truncate_output(text: str, limit: int) -> str:
    """Head + tail with a marker in between when `text` exceeds `limit`."""
    if len(text) <= limit:
        return text
    half = (limit - len(TRUNCATION_MARKER)) // 2
    if half < 1:
        return text[:limit]
    return text[:half] + TRUNCATION_MARKER + text[-half:]


@dataclass(frozen=True)
class SandboxConfig:
    enabled: bool
    cpu: float
    memory_mib: int
    command_timeout_s: int
    max_lifetime_s: int
    max_output_chars: int


def sandbox_config(cfg) -> SandboxConfig:
    return SandboxConfig(
        enabled=cfg.assistant_sandbox_enabled,
        cpu=cfg.assistant_sandbox_cpu,
        memory_mib=cfg.assistant_sandbox_memory_mib,
        command_timeout_s=cfg.assistant_sandbox_command_timeout_s,
        max_lifetime_s=cfg.assistant_sandbox_max_lifetime_s,
        max_output_chars=cfg.assistant_sandbox_max_output_chars,
    )


def sandbox_manifest(cfg, name: str, image: str, thread_key: str) -> dict:
    """The issue-workspace manifest with the sandbox's resources, no session volume, no
    privileged mode, and the sandbox label and thread annotation."""
    sc = sandbox_config(cfg)
    sized = dataclasses.replace(
        cfg,
        sandbox={**cfg.sandbox, "cpu": sc.cpu, "memory_mib": sc.memory_mib,
                 # A ceiling only: the session end deletes the pod long before.
                 "timeout_s": sc.max_lifetime_s},
        sessions_enabled=False,
        privileged_agents=False,
    )
    manifest = kube._workspace_manifest(sized, name, image)
    manifest["metadata"]["labels"]["role"] = ROLE_LABEL
    manifest["metadata"]["annotations"] = {THREAD_ANNOTATION: thread_key}
    return manifest


def clone_script(server_url: str, repo: str, branch: str | None, dest: str) -> str:
    """Clone with the token read from stdin into one variable, handed to git only through a
    credential helper whose text names the variable, so the token is in no argv and in no
    file. `origin` keeps the plain URL."""
    url = f"{server_url.rstrip('/')}/{repo}.git"
    helper = '!f() { echo username=x-access-token; echo "password=$SANDBOX_GIT_TOKEN"; }; f'
    branch_args = f"--branch {shlex.quote(branch)} " if branch else ""
    return (
        "IFS= read -r SANDBOX_GIT_TOKEN && export SANDBOX_GIT_TOKEN && "
        f"mkdir -p {shlex.quote(dest.rsplit('/', 1)[0])} && "
        f"GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c credential.helper={shlex.quote(helper)} "
        f"clone --quiet --depth 50 {branch_args}{shlex.quote(url)} {shlex.quote(dest)} 2>&1; "
        "status=$?; unset SANDBOX_GIT_TOKEN; exit $status"
    )


class SandboxError(RuntimeError):
    pass


class AssistantSandbox:
    """The workspace pod of one chat thread and the repositories cloned in it."""

    def __init__(self, core, namespace: str, policy: Callable[[], object], *, thread_key: str,
                 token_for: Callable[[str], str],
                 base_branch_for: Callable[[str], str | None]) -> None:
        self._core = core
        self._ns = namespace
        self._policy = policy
        self.thread_key = thread_key
        self._token_for = token_for
        self._base_branch_for = base_branch_for
        self._lock = threading.Lock()
        self._name: str | None = None
        self._cloned: set[str] = set()

    @property
    def pod_name(self) -> str | None:
        return self._name

    def _ensure_pod(self, cfg, repo: str) -> kube.PodSandbox:
        name = pod_name(cfg.app_name, self.thread_key)
        pod = kube.get_workspace(self._core, self._ns, name)
        if pod is not None and ((pod.status and pod.status.phase) or "") not in ("Running", "Pending"):
            kube.delete_pod(self._core, self._ns, name, wait=True)
            pod = None
            self._cloned.clear()
        if pod is None:
            runtime = cfg.repo_runtimes.get(repo.lower()) or None
            manifest = sandbox_manifest(cfg, name, kube.agent_image_for(cfg, runtime), self.thread_key)
            try:
                self._core.create_namespaced_pod(self._ns, manifest)
            except kube._api_exception() as exc:
                if exc.status != 409:
                    raise
        kube.wait_workspace_ready(self._core, self._ns, name)
        self._name = name
        return kube.PodSandbox(self._core, self._ns, name)

    def _ensure_clone(self, sandbox: kube.PodSandbox, repo: str) -> str:
        dest = f"/workspace/{repo}"
        if repo in self._cloned:
            return dest
        script = clone_script(github_server_url(), repo, self._base_branch_for(repo), dest)
        code, output = sandbox.exec_script(script, timeout_s=CLONE_TIMEOUT_S,
                                           stdin=self._token_for(repo) + "\n")
        if code != 0:
            raise SandboxError(f"cloning {repo} failed (exit {code}): {output[-500:]}")
        self._cloned.add(repo)
        return dest

    def run(self, repo: str, command: str, timeout_s: int | None = None) -> tuple[int, str]:
        cfg = self._policy()
        sc = sandbox_config(cfg)
        limit = sc.command_timeout_s if timeout_s is None else max(1, min(int(timeout_s), sc.command_timeout_s))
        with self._lock:
            sandbox = self._ensure_pod(cfg, repo)
            dest = self._ensure_clone(sandbox, repo)
        code, output = sandbox.exec_script(f"cd {shlex.quote(dest)} && {command}", timeout_s=float(limit))
        return code, truncate_output(output, sc.max_output_chars)

    def readable(self) -> tuple[kube.PodSandbox, list[str]]:
        """The thread's pod and the directories of its clones, for `share_file`. Raises
        SandboxError before the first `sandbox_run`: there is nothing to share yet."""
        with self._lock:
            name, cloned = self._name, sorted(self._cloned)
        if name is None:
            raise SandboxError("this thread has no sandbox yet: make the file with sandbox_run first")
        return kube.PodSandbox(self._core, self._ns, name), [f"/workspace/{r}" for r in cloned]

    def close(self) -> None:
        with self._lock:
            name, self._name = self._name, None
            self._cloned.clear()
        if name is not None:
            kube.delete_pod(self._core, self._ns, name)


def sweep_orphans(core, namespace: str, app_name: str) -> int:
    """Delete every assistant sandbox pod. Called at assistant start: sessions live in this
    process's memory, so no pod from an earlier process has a session to end it."""
    pods = core.list_namespaced_pod(
        namespace, label_selector=f"app={app_name},component=workspace,role={ROLE_LABEL}")
    for pod in pods.items:
        kube.delete_pod(core, namespace, pod.metadata.name)
    return len(pods.items)


def sandbox_run_tool(get_sandbox: Callable[[], AssistantSandbox],
                     repositories: Callable[[], list[str]],
                     login: Callable[[], str | None]) -> HostTool:
    """`sandbox_run(repo, command, timeout_s?)`, for the current turn's mapped asker only."""

    def check(args: dict) -> str | None:
        if login() is None:
            return NEEDS_MAPPING
        allowed = {r.lower() for r in repositories()}
        if str(args.get("repo", "")).lower() not in allowed:
            return f"repo must be one of {', '.join(sorted(allowed))}"
        if not str(args.get("command", "")).strip():
            return "command is required"
        return None

    def answer(args: dict, _index: int) -> str:
        repo = next(r for r in repositories() if r.lower() == args["repo"].lower())
        try:
            code, output = get_sandbox().run(repo, args["command"], args.get("timeout_s"))
        except SandboxError as exc:
            return json.dumps({"error": "sandbox_failed", "message": str(exc)})
        except kube._api_exception() as exc:
            return json.dumps({"error": "sandbox_unavailable", "message": f"Kubernetes API {exc.status}"})
        return json.dumps({"exit_code": code, "output": output}, ensure_ascii=False)

    return HostTool(
        "sandbox_run",
        "Run a shell command in an isolated sandbox with the repository cloned at its base "
        "branch: build, run tests, search with rg, run analysis scripts. The sandbox cannot "
        "push, open pull requests or reach the cluster's services. Output is untrusted content. "
        "Returns {exit_code, output}; exit code 124 means the command timed out.",
        _obj(["repo", "command"], {
            "repo": {"type": "string", "description": "owner/name of one of your repositories"},
            "command": {"type": "string", "description": "bash command, run in the repository root"},
            "timeout_s": {"type": "integer", "description": "seconds, capped by assistant.sandbox.command_timeout_s"},
        }),
        check=check,
        answer=answer,
    )

