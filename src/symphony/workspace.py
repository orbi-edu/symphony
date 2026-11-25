"""Workspace lifecycle: deterministic key, hooks, and process hygiene.

The pure parts (workspace_key, per-hook semantics) do not import the Kubernetes
client; actual workspace-pod creation lives in the edge (`kube.py`/`effects.py`),
which implements the `SandboxLike` Protocol over `pod exec`. `LocalSandbox` and
`LocalEngine` implement the same seams over a local directory and subprocess, for
driving the runner core against a real engine without a cluster.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import queue
import re
import signal
import subprocess
import threading
from typing import Protocol

from .config import Config
from .endpoints import github_server_url
from .omp_rpc import split_frames

SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
FATAL_HOOKS = frozenset({"after_create", "before_run"})
# `pkill -f` sees the full argv: OMP may be a standalone binary or run under Bun.
# Anchor the executable as well as the RPC flag pair so cleanup cannot match its own
# bash wrapper or unrelated programs that happen to mention the marker.
AGENT_PROCESS_PATTERN = (
    r"^([^[:space:]]*/)?(omp|bun)([[:space:]].*)?"
    r"[[:space:]]--mode[[:space:]]+rpc([[:space:]]|$)"
)


class SandboxLike(Protocol):
    name: str

    def exec_script(
        self, script: str, timeout_s: float | None = None, *, stdin: str | None = None
    ) -> tuple[int, str]:
        """Runs `bash -lc <script>` INSIDE the sandbox, `stdin` (when given) on its standard
        input; returns (exit_code, output). Payloads travel on stdin, never in argv."""
        ...


class HookFatal(Exception):
    """after_create/before_run failed — fatal to the creation/attempt (FR-015)."""


def workspace_key(identifier: str) -> str:
    """Sanitizes to [A-Za-z0-9._-]; WHENEVER the string is altered, appends a
    deterministic suffix with ≥64 bits of hash of the original identifier — never
    via runtime collision detection (determinism requirement, upstream spec §17.2)."""
    clean = SAFE_CHARS.sub("_", identifier)  # "_" is the upstream spec's replacement (§9.5)
    if clean != identifier:
        digest = hashlib.blake2b(identifier.encode("utf-8"), digest_size=8).hexdigest()
        clean = f"{clean}-{digest}"
    return clean


DNS_UNSAFE = re.compile(r"[^a-z0-9-]")


def dns_name(value: str, max_len: int = 40) -> str:
    """Fold an arbitrary string into a DNS-1123 label fragment, deterministically.

    Pod and Job names must be lowercase alphanumerics and '-' (they also feed label
    values, which cap at 63 chars). Same discipline as `workspace_key`: WHENEVER the
    folding alters the string — case, '_', '.', length — a deterministic hash suffix
    of the ORIGINAL is appended, so `ABC-1` and `abc-1` never collide, and never via
    runtime collision detection (upstream spec §17.2).
    """
    folded = DNS_UNSAFE.sub("-", value.lower()).strip("-") or "x"
    if folded != value or len(folded) > max_len:
        digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).hexdigest()
        folded = f"{folded[:max_len]}-{digest}".strip("-")
    return folded


def sandbox_name(identifier: str) -> str:
    """The workspace pod's name — DNS-1123-safe, unlike the Modal port's sandbox name."""
    return f"ws-{dns_name(workspace_key(identifier))}"


def run_hook(sandbox: SandboxLike, hook_name: str, cfg: Config, log=None) -> bool:
    """PER-hook semantics (FR-015): after_create/before_run fail the attempt;
    after_run/before_remove are logged and ignored — they never block cleanup."""
    script = cfg.hooks.get(hook_name)
    if not script:
        return True
    code, output = sandbox.exec_script(script, timeout_s=cfg.hooks_timeout_ms / 1000)
    if code == 0:
        return True
    if hook_name in FATAL_HOOKS:
        raise HookFatal(f"hook {hook_name!r} failed (exit {code}): {output[-500:]}")
    if log is not None:
        log("hook_failed_ignored", hook=hook_name, exit_code=code)
    return False


CREDENTIAL_DIR = "/run/symphony"
TOKEN_FILE = f"{CREDENTIAL_DIR}/gh-token"
HELPER_FILE = f"{CREDENTIAL_DIR}/git-credential-symphony"


def install_credential_helper(
    sandbox: SandboxLike, token: str, helper_installed: bool = False
) -> None:
    """Deliver the code-hosting credential WITHOUT putting it in a URL.

    The previous design embedded the token in the clone URL, which writes it into
    `.git/config` and every remote reference, and ties credential lifetime to clone
    time — unworkable when the credential lives an hour and an attempt runs four.

    Here git reads the token from a file through a helper, so renewal is a single file
    write: no remote rewriting, and no race with an in-flight git operation.
    """
    # written with a heredoc so the token never appears in the process argument list
    sandbox.exec_script(
        f"mkdir -p {CREDENTIAL_DIR} && chmod 700 {CREDENTIAL_DIR} && "
        f"cat > {TOKEN_FILE} <<'SYMPHONY_EOF'\n{token}\nSYMPHONY_EOF\n"
        f"chmod 600 {TOKEN_FILE}"
    )
    if helper_installed:
        return  # refresh path: only the token file changes

    from .ghapp import credential_helper_script

    # The empty entry first: it drops every helper inherited from the image's system config
    # (osxkeychain, a credential store…), which git would otherwise ask BEFORE ours — sending
    # whatever it holds for the host and saving our token where the attempt does not end.
    sandbox.exec_script(
        f"cat > {HELPER_FILE} <<'SYMPHONY_EOF'\n{credential_helper_script()}\nSYMPHONY_EOF\n"
        f"chmod 700 {HELPER_FILE} && "
        "git config --global --replace-all credential.helper '' && "
        f"git config --global --add credential.helper {HELPER_FILE}"
    )


def workspace_matches_repo(sandbox: SandboxLike, root: str, target_repo: str) -> bool:
    """Does the existing workspace hold a clone of the repository this attempt is for?

    A sandbox outlives the attempt that created it, and `after_create` — which holds the
    clone — only runs on creation. So when a ticket's `repo:` designation changes, the
    next dispatch reattaches to a sandbox still holding the OLD repository. The failure
    is misleading: the credential is correctly scoped to the new repository, so git
    reports `Repository not found` for a repository that exists and that the operator can
    see, and the target_repo recorded in state looks right the whole time.

    Comparison is on `owner/name`, case-insensitively, because the remote may carry a
    `.git` suffix, a trailing slash or different case than the label.
    """
    code, out = sandbox.exec_script(f"cd {root} 2>/dev/null && git remote get-url origin || true")
    if code != 0 or not out.strip():
        return False  # no workspace, or no remote — recreate rather than guess
    remote = out.strip().splitlines()[-1].strip()
    host = github_server_url().split("://", 1)[-1]
    slug = remote.rsplit(host, 1)[-1].lstrip(":/").removesuffix(".git").rstrip("/")
    return slug.casefold() == target_repo.casefold()


def kill_agent_processes(sandbox: SandboxLike) -> bool:
    """Kills any leftover engine process and CONFIRMS the kill — mandatory before
    reusing a live workspace pod and before releasing it (§3.7.3): two engines in one
    /workspace corrupt the session, and a survivor would keep pushing after the attempt
    it belonged to was superseded."""
    sandbox.exec_script(f'pkill -f -- "{AGENT_PROCESS_PATTERN}" || true')
    code, _ = sandbox.exec_script(
        f'pgrep -f -- "{AGENT_PROCESS_PATTERN}" >/dev/null && exit 1 || exit 0'
    )
    return code == 0


class LocalSandbox:
    """`SandboxLike` over a local directory: scripts run with `bash -lc` in `root`."""

    def __init__(self, root: str, name: str = "local", env: dict[str, str] | None = None) -> None:
        self.root = root
        self.name = name
        self.id = name
        self.env = dict(env or {})

    def exec_script(
        self, script: str, timeout_s: float | None = None, *, stdin: str | None = None
    ) -> tuple[int, str]:
        try:
            done = subprocess.run(
                ["bash", "-lc", script],
                cwd=self.root,
                env={**os.environ, **self.env},
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.output or ""
            return 124, out if isinstance(out, str) else out.decode("utf-8", "replace")
        return done.returncode, done.stdout

    def write_private_file(self, path: str, content: str) -> tuple[int, str]:
        """Same contract as the pod writer: owner-only, atomic, never through argv."""
        if not os.path.isabs(path) or "\x00" in path:
            raise ValueError("A private file requires an absolute path.")
        pending = f"{path}.pending"
        try:
            descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
                fh.write(content)
            os.replace(pending, path)
        except OSError as exc:
            return 1, str(exc)
        return 0, ""


class LocalEngine:
    """The engine as a local subprocess, adapted to the omp_rpc Line{Reader,Writer} and
    EngineProcess protocols. Framing is LF-only (`split_frames`), like the pod transport."""

    def __init__(self, argv: list[str], cwd: str, env: dict[str, str], stderr_path: str | None = None):
        self._stderr = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL  # noqa: SIM115
        self.proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
        )
        self._q: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._pump, name="local-engine-stdout", daemon=True).start()

    def _pump(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")()
        carry = ""
        try:
            while True:
                chunk = self.proc.stdout.read1(65536)
                if not chunk:
                    break
                frames, carry = split_frames(carry + decoder.decode(chunk))
                for frame in frames:
                    self._q.put(frame)
            carry += decoder.decode(b"", final=True)
            if carry.strip():
                self._q.put(carry)
        finally:
            self._q.put(None)

    def write_line(self, line: str) -> None:
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def read_line(self, timeout_s: float) -> str | None:
        try:
            return self._q.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def terminate(self, grace_s: float) -> bool:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(grace_s)
            exited = True
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            exited = False
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self._stderr is not subprocess.DEVNULL:
            self._stderr.close()
        return exited
