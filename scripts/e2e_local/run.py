"""Local-mode certification: Redis + `symphony.entry.local` + real omp + headless Tern.

    scripts/e2e_local.sh [--out DIR] [--keep]

1. a disposable Redis (unix socket), a throwaway git repository, and the local stack on free
   ports, with the in-memory tracker and the real omp (`OMP_BIN`, default the Tern-managed one);
2. `symphony login` with the bootstrap token the stack prints, into an isolated HOME and
   XDG_CONFIG_HOME with the keyring disabled (the token lands in a 0600 file there, never in
   the user's keychain), then `symphony tern install --no-plugin` into an isolated
   TERN_CONFIG_DIR;
3. a ticket asking for a multi-step bash task, created through `POST /v1/dev/issues`;
4. a headless Tern opens `tern acp symphony --resume <sid>`, a screenshot of the live chat,
   a steer typed in the chat, then the checks: b.txt exists, `symphony.intent_applied`
   (steer) is on the session stream, `symphony status --json` and the MCP `list_sessions`
   tool see the session; a second screenshot;
5. the ticket goes to Done: terminal cleanup, workspace removed, reflector skipped.

Prints one JSON summary (timings, screenshot paths, checks). No credential value is printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import redis

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
VENV_BIN = REPO / ".venv" / "bin"
DEFAULT_OMP = Path.home() / "Library/Application Support/Tern/omp/bin/omp"
TOKEN_RE = re.compile(r"sym_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
NAMESPACE = "e2e-local"
IDENTIFIER = "E2E-1"
STEER = "also create b.txt with beta"
TASK = (
    "Do these steps in order, each as its own bash command:\n"
    "1. Create a.txt containing the single line alpha.\n"
    "2. Run `sleep 45` (it stands in for a slow build; do not skip or shorten it).\n"
    "3. Create c.txt containing the number of lines of a.txt (use wc -l).\n"
    "4. Run `ls -1` and `git status --short` and report what they show."
)


class Failure(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def until(predicate, timeout: float, what: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise Failure(f"timed out ({timeout:.0f}s) waiting for {what}")


def private_file(path: Path, body: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)


class Run:
    def __init__(self, tmp: Path, out: Path) -> None:
        self.tmp = tmp
        self.out = out
        self.procs: list[subprocess.Popen] = []
        self.summary: dict = {"tmp": str(tmp), "checks": {}, "timings_s": {}, "screenshots": []}
        self.token = ""
        self.url = ""
        self.sid = ""
        self.issue_id = ""
        self.failures: list[str] = []

    # -- processes ---------------------------------------------------------------
    def spawn(self, argv, *, env=None, log: Path, cwd=None) -> subprocess.Popen:
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=fd, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        os.close(fd)
        self.procs.append(proc)
        return proc

    def stop_all(self) -> None:
        if getattr(self, "tern_sock", None) and Path(self.tern_sock).exists():
            subprocess.run(["tern", "ctl", "--control", self.tern_sock, "quit"],
                           capture_output=True, timeout=15, check=False)
        for proc in reversed(self.procs):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(40)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def client_env(self) -> dict:
        """The person's side: isolated HOME/XDG, no keyring, no ambient Symphony credential."""
        env = dict(os.environ)
        for name in ("SYMPHONY_URL", "SYMPHONY_TOKEN", "SYMPHONY_BOOTSTRAP_TOKEN"):
            env.pop(name, None)
        env.update(
            HOME=str(self.tmp / "home"),
            XDG_CONFIG_HOME=str(self.tmp / "xdg"),
            XDG_CACHE_HOME=str(self.tmp / "cache"),
            PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
            TERN_CONFIG_DIR=str(self.tmp / "tern-config"),
            PATH=f"{VENV_BIN}:{env.get('PATH', '')}",
        )
        return env

    def symphony(self, *args: str, stdin: str | None = None) -> dict:
        proc = subprocess.run(
            [str(VENV_BIN / "symphony"), *args, "--json"], input=stdin, capture_output=True,
            text=True, env=self.client_env(), timeout=60, check=False,
        )
        try:
            body = json.loads(proc.stdout or "{}")
        except ValueError as exc:
            raise Failure(f"symphony {args[0]}: not JSON: {proc.stdout[:300]} {proc.stderr[-300:]}") from exc
        if proc.returncode != 0:
            raise Failure(f"symphony {' '.join(args)} failed: {body} {proc.stderr[-300:]}")
        return body

    def api(self) -> httpx.Client:
        return httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {self.token}"},
                            timeout=15)

    # -- tern ----------------------------------------------------------------------
    def ctl(self, *words: str, check: bool = True) -> dict:
        proc = subprocess.run(["tern", "ctl", "--control", self.tern_sock, *words],
                              capture_output=True, text=True, timeout=90, check=False)
        line = (proc.stdout.strip().splitlines() or ["{}"])[-1]
        reply = json.loads(line)
        if check and not reply.get("ok"):
            raise Failure(f"tern ctl {words[0]}: {reply.get('error')}")
        return reply

    def screen_text(self) -> str:
        """Every visible element's text, in document order."""
        texts: list[str] = []

        def walk(node: dict) -> None:
            if node.get("text"):
                texts.append(node["text"])
            for child in node.get("children") or ():
                walk(child)

        for node in self.ctl("tree").get("tree") or ():
            walk(node)
        return "\n".join(texts)

    def shot(self, name: str) -> str:
        self.ctl("wait", "400")
        png = Path(self.ctl("shot", name)["png"])
        target = self.out / f"{name}.png"
        shutil.copyfile(png, target)
        self.summary["screenshots"].append(str(target))
        return str(target)

    # -- the certification ----------------------------------------------------------
    def main(self, omp: Path, provider: str, model: str) -> None:
        for sub in ("home", "xdg", "cache", "tern-config", "root", "bin", "tern-out"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=True)

        # Redis on a unix socket only.
        redis_sock = self.tmp / "redis.sock"
        self.spawn(["redis-server", "--port", "0", "--unixsocket", str(redis_sock), "--save", "",
                    "--appendonly", "no", "--dir", str(self.tmp)], log=self.tmp / "redis.log")
        rclient = redis.Redis(unix_socket_path=str(redis_sock), decode_responses=True)
        until(lambda: redis_sock.exists() and rclient.ping(), 10, "redis")

        # A throwaway repository the hook clones.
        repo = self.tmp / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=E2E", "-c", "user.email=e2e@example.invalid"]
        subprocess.run([*git, "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "README.md").write_text("# e2e local\n")
        subprocess.run([*git, "add", "README.md"], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True)

        # omp through a path without spaces (the engine command is a shell word list).
        (self.tmp / "bin" / "omp").symlink_to(omp)
        version = subprocess.run([str(self.tmp / "bin" / "omp"), "--version"], capture_output=True,
                                 text=True, timeout=30, check=False).stdout.strip()
        self.summary["omp_version"] = version
        workflow = (HERE / "WORKFLOW.md.tmpl").read_text()
        for key, value in {"@REPO@": str(repo), "@OMP@": str(self.tmp / "bin" / "omp"),
                           "@PROVIDER@": provider, "@MODEL@": model}.items():
            workflow = workflow.replace(key, value)
        (self.tmp / "WORKFLOW.md").write_text(workflow)

        # The stack.
        port, gateway_port = free_port(), free_port()
        stack_env = dict(os.environ)
        for name in ("SYMPHONY_BOOTSTRAP_TOKEN", "SYMPHONY_GATEWAY_URL", "SYMPHONY_URL", "SYMPHONY_TOKEN"):
            stack_env.pop(name, None)
        stack_env.update(REDIS_URL=f"unix://{redis_sock}?db=0", POD_NAMESPACE=NAMESPACE,
                         PYTHONUNBUFFERED="1")
        stack_log = self.tmp / "stack.log"
        stack = self.spawn(
            [sys.executable, "-m", "symphony.entry.local", "--root", str(self.tmp / "root"),
             "--workflow", str(self.tmp / "WORKFLOW.md"), "--port", str(port),
             "--gateway-port", str(gateway_port)],
            env=stack_env, log=stack_log, cwd=str(REPO),
        )
        until(lambda: stack.poll() is not None or "Symphony local at" in stack_log.read_text(),
              30, "the local stack")
        if stack.poll() is not None:
            raise Failure("the local stack exited:\n" + stack_log.read_text()[-2000:])
        self.url = f"http://127.0.0.1:{port}"
        match = until(lambda: TOKEN_RE.search(stack_log.read_text()), 5, "the bootstrap token")
        self.token = match.group(0)
        self.summary["checks"]["bootstrap_token_printed"] = True

        # 2. login (token through stdin) and Tern install, all isolated.
        login = self.symphony("login", "--url", self.url, "--token", "-", stdin=self.token + "\n")
        self.summary["checks"]["login"] = {k: login.get(k) for k in ("name", "role", "storage")}
        if login.get("storage") != "file":
            raise Failure(f"login stored the token in {login.get('storage')}, not the isolated file")
        installed = self.symphony("tern", "install", "--no-plugin")
        self.summary["checks"]["tern_install"] = sorted(installed) if isinstance(installed, dict) else installed

        # 3. the ticket.
        http = self.api()
        created_at = time.time()
        response = http.post("/v1/dev/issues", json={
            "identifier": IDENTIFIER, "title": "Test files in the repository", "description": TASK,
        })
        if response.status_code != 201:
            raise Failure(f"dev issue: {response.status_code} {response.text}")
        self.issue_id = response.json()["id"]
        self.sid = f"{self.issue_id}:1"

        def running():
            r = http.get(f"/v1/sessions/{self.sid}")
            return r.status_code == 200 and r.json()["meta"].get("state") == "running"

        until(running, 60, "the session to run")
        started_at = time.time()
        self.summary["timings_s"]["ticket_created_to_agent_running"] = round(started_at - created_at, 3)
        store_key = f"sym:{NAMESPACE}:session:{self.sid}:events"

        def first_event_at(kind: str):
            for _, fields in rclient.xrange(store_key):
                if fields["kind"] == kind:
                    return int(fields["t"]) / 1000
            return None

        agent_start = until(lambda: first_event_at("agent_start"), 60, "agent_start")
        self.summary["timings_s"]["ticket_created_to_agent_start_event"] = round(agent_start - created_at, 3)

        # 4. Tern: the live chat.
        self.tern_sock = str(self.tmp / "tern.sock")
        tern_env = self.client_env()
        self.spawn(["tern", "serve", "--control", self.tern_sock, "--out", str(self.tmp / "tern-out")],
                   env=tern_env, log=self.tmp / "tern.log", cwd=str(self.tmp / "home"))
        until(lambda: Path(self.tern_sock).exists(), 20, "tern serve")
        self.ctl("size", "1500", "1000")
        self.ctl("ready")
        self.ctl("run", json.dumps(f"tern acp symphony --resume {self.sid}"))
        until(lambda: "a.txt" in self.screen_text(), 60, "the live chat to show the agent's work")
        # Wait for the slow step, so the screenshot shows the agent mid-task.
        until(lambda: "sleep" in self.screen_text(), 90, "the agent to start the slow step")
        self.summary["chat_text_live"] = self.screen_text()[-1500:]
        self.shot("01-live-chat")

        typed_at = time.time()
        self.ctl("type", json.dumps(STEER))
        self.ctl("key", "Enter")

        def steer_applied():
            for _, fields in rclient.xrange(store_key):
                if fields["kind"] == "symphony.intent_applied":
                    data = json.loads(fields["data"])
                    if data.get("kind") == "steer" and not data.get("ignored"):
                        return int(fields["t"]) / 1000, data
            return None

        applied_at, applied = until(steer_applied, 30, "symphony.intent_applied (steer)")
        self.summary["timings_s"]["steer_typed_to_intent_applied"] = round(applied_at - typed_at, 3)
        self.summary["checks"]["intent_applied"] = applied

        # status and MCP while the session is live. A failure here is recorded and reported
        # at the end, so the steer's effect is still verified.
        try:
            status = self.symphony("status")
            self.summary["checks"]["status_counts"] = status.get("counts")
            if not (status.get("counts") or {}).get("running"):
                self.failures.append(f"symphony status --json shows no running session: {status}")
        except Failure as exc:
            self.failures.append(str(exc))
        mcp = http.post("/mcp", headers={"Accept": "application/json, text/event-stream",
                                         "MCP-Protocol-Version": "2025-06-18"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "list_sessions", "arguments": {}}})
        listed = json.dumps(mcp.json().get("result"))
        self.summary["checks"]["mcp_list_sessions_has_session"] = self.sid in listed
        if self.sid not in listed:
            self.failures.append(f"MCP list_sessions does not list {self.sid}: {listed[:500]}")

        workspace = self.tmp / "root" / "workspaces" / IDENTIFIER
        b_txt = workspace / "b.txt"
        until(lambda: b_txt.exists() and "beta" in b_txt.read_text(), 240, "b.txt with beta")
        self.summary["timings_s"]["steer_typed_to_b_txt"] = round(time.time() - typed_at, 3)
        self.summary["checks"]["b_txt"] = b_txt.read_text().strip()
        until(lambda: "Delivered to the agent" in self.screen_text(), 15, "the chat's delivery note")
        self.summary["chat_text_after_steer"] = self.screen_text()[-1500:]
        self.shot("02-steer-applied")

        def ended():
            meta = http.get(f"/v1/sessions/{self.sid}").json()["meta"]
            return meta.get("state") == "ended" and meta

        meta = until(ended, 300, "the attempt to finish")
        self.summary["checks"]["session_outcome"] = meta.get("outcome")
        self.summary["checks"]["files"] = sorted(p.name for p in workspace.iterdir() if p.name != ".git")
        self.summary["checks"]["last_message"] = meta.get("last_message")
        time.sleep(1.5)
        self.summary["chat_text_ended"] = self.screen_text()[-1500:]
        self.shot("03-session-ended")

        # 5. Done: terminal cleanup and the skipped reflector.
        http.post(f"/v1/dev/issues/{self.issue_id}/state", json={"state": "Done"})
        until(lambda: not workspace.exists(), 30, "the workspace to be removed")
        until(lambda: "reflector_skipped_local_mode" in stack_log.read_text(), 10, "the reflector skip log")
        self.summary["checks"]["terminal_cleanup"] = True
        self.summary["checks"]["reflector_skipped_logged"] = True
        if self.failures:
            raise Failure("; ".join(self.failures))


def main() -> int:
    parser = argparse.ArgumentParser(description="Certify the local mode end to end.")
    parser.add_argument("--out", help="where the screenshots go (default: <tmp>/shots)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = parser.parse_args()
    omp = Path(os.environ.get("OMP_BIN", str(DEFAULT_OMP)))
    if not omp.exists():
        print(f"omp not found at {omp} (set OMP_BIN)", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="sym-e2e-local-", dir="/tmp"))
    out = Path(args.out).resolve() if args.out else tmp / "shots"
    run = Run(tmp, out)
    code = 0
    try:
        run.main(omp, os.environ.get("E2E_PROVIDER", "amazon-bedrock"),
                 os.environ.get("E2E_MODEL", "global.anthropic.claude-opus-5-5"))
        run.summary["ok"] = True
    except Failure as exc:
        if getattr(run, "tern_sock", None) and Path(run.tern_sock).exists():
            try:
                (tmp / "failure-screen.txt").write_text(run.screen_text())
                run.shot("failure")
            except Exception:  # noqa: BLE001 — the failure itself is what gets reported
                pass
        run.summary["ok"] = False
        run.summary["error"] = str(exc)
        code = 1
    finally:
        run.stop_all()
        print(json.dumps(run.summary, indent=2, ensure_ascii=False))
        if not args.keep and code == 0 and args.out:
            shutil.rmtree(tmp, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
