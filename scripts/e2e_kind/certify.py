"""Kubernetes setup certification on kind: a minimal install made complete by the setup API,
then MCP, the assistant and Seiji, all in the cluster. Run by scripts/e2e_kind_setup.sh after
`kubectl apply -k scripts/e2e_kind` (the operator made only the Secrets `symphony-api` and
`model-provider`):

    uv run python scripts/e2e_kind/certify.py --context kind-symphony-e2e-setup \\
        --token-file F --state-dir DIR [--summary-out FILE]

Areas (each check reads the cluster, the API audit log or the mocks' recorded state):

0. NetworkPolicy: the shipped policies are enforced (probe pods with the workspace and the
   daemon labels, and the gateway itself).
1. Minimal install and setup: the daemon idles with `daemon_setup_incomplete`; the setup steps
   run against the in-cluster mocks (GitHub App, repositories, notifications, tracker,
   assistant, webhooks); the integration Secrets are patched, never created (same UIDs; the
   Kubernetes audit log has no `create`); the restart order is daemon, mergers, assistant,
   then API; after the tracker step the daemon dispatches the ticket that waited on the
   board; RBAC refuses anything outside the named objects.
2. MCP through the gateway: the mock MCP server with a stored header; a token of a role in
   the server's `roles` passes, another role and an unknown token get 403 `mcp_forbidden`;
   the implementer's workspace and the assistant call the tool, and the mock sees only the
   stored header.
3. The assistant Deployment: Socket Mode to the Slack mock, a mention answered in the thread.
4. Seiji through the gateway: the implementer acquires a device and keeps it; the end of the
   attempt releases the lease.

Prints one JSON summary. No credential value is printed: the mock tokens and the MCP tokens
stay in files of mode 0600, in Secrets, or inside the pods.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from scripts.mocks.linear.store import DEFAULT_API_KEY as LINEAR_KEY  # noqa: E402
from scripts.mocks.slack.server import (  # noqa: E402
    BOT_USER_ID,
    DEFAULT_APP_TOKEN,
    DEFAULT_CHANNEL,
    DEFAULT_TOKEN,
)

NS, MOCKS_NS = "symphony", "symphony-e2e-mocks"
TEAM, APP, PERSON, SLACK_PERSON = "ENG", "acme/app", "octocat", "U0OCTOCAT"
CHANNEL = DEFAULT_CHANNEL["id"]
MCP_TOOL, MCP_HEADER = "lookup_ticket_context", "X-Acme-Tenant"
SEIJI_PUBLIC = "https://seiji.e2e.invalid"
MCP_URL = f"http://mcp.{MOCKS_NS}.svc:4600/mcp"
SEIJI_URL = f"http://seiji.{MOCKS_NS}.svc:4780"
APP_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read", "checks": "read",
                   "actions": "write", "statuses": "read", "issues": "write"}
MERGER_LABELS = ("symphony", "reviewed", "wait-ci", "train-ci", "review-requested", "in-audit",
                 "review-bypass", "needs-maintainer", "symphony-learnings", "investigation", "ci")
INTEGRATION_SECRETS = ("linear-api-key", "github-app", "symphony-slack", "symphony-discord", "symphony-mcp",
                       "symphony-api", "symphony-providers", "model-provider")
AGENT_TIMEOUT_S = 1500
# setup_secrets.restart: the daemon, each merger, the assistant, then the API that serves the write
FULL_ORDER = ["symphony-daemon", "symphony-merger", "symphony-assistant", "symphony-api"]
WORKING = "Working on it…"


class Failure(RuntimeError):
    pass


def until(predicate, timeout: float, what: str, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise Failure(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ts(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------------------- records
class Area:
    def __init__(self, key: str, title: str) -> None:
        self.key, self.title = key, title
        self.checks: list[dict] = []
        self.timings: dict[str, float] = {}
        self.error: str | None = None

    def check(self, name: str, ok: bool, evidence=None, *, fatal: bool = True) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "evidence": evidence})
        print(f"  [{'PASS' if ok else 'FAIL'}] {self.key}: {name}", flush=True)
        if not ok and fatal:
            raise Failure(f"{self.key}: {name}")
        return bool(ok)

    def time(self, name: str, seconds: float) -> None:
        self.timings[name] = round(seconds, 2)

    @property
    def ok(self) -> bool:
        return self.error is None and all(c["ok"] for c in self.checks)

    def summary(self) -> dict:
        return {"area": self.key, "title": self.title, "ok": self.ok, "error": self.error,
                "timings_s": self.timings, "checks": self.checks}


# --------------------------------------------------------------------------- cluster access
class Forward:
    """`kubectl port-forward` to a Service, started again when its pod went away."""

    def __init__(self, cert: Cert, namespace: str, service: str, port: int) -> None:
        self.cert, self.namespace, self.service, self.port = cert, namespace, service, port
        self.local = free_port()
        self.proc: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{self.local}"

    def _open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.local), timeout=1):
                return True
        except OSError:
            return False

    def ensure(self) -> None:
        if self.proc is not None and self.proc.poll() is None and self._open():
            return
        self.stop()
        log = (self.cert.state / f"pf-{self.service}.log").open("a")
        self.proc = subprocess.Popen(
            ["kubectl", "--context", self.cert.context, "-n", self.namespace, "port-forward",
             f"svc/{self.service}", f"{self.local}:{self.port}"], stdout=log, stderr=subprocess.STDOUT)
        until(lambda: self.proc.poll() is not None or self._open(), 20, f"port-forward {self.service}")
        if self.proc.poll() is not None:
            raise Failure(f"port-forward {self.service} exited")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(5)
        self.proc = None


class Cert:
    def __init__(self, context: str, token: str, state: Path) -> None:
        self.context, self.token, self.state = context, token, state
        self.cluster = context.removeprefix("kind-")
        self.http = httpx.Client(timeout=30)
        self.fwd = {
            "api": Forward(self, NS, "symphony-api", 8080),
            "linear": Forward(self, MOCKS_NS, "linear", 4100),
            "github": Forward(self, MOCKS_NS, "github", 4200),
            "slack": Forward(self, MOCKS_NS, "slack", 4300),
            "mcp": Forward(self, MOCKS_NS, "mcp", 4600),
            "seiji": Forward(self, MOCKS_NS, "seiji", 4780),
        }
        self.areas: dict[str, Area] = {}
        self.restarts: list[dict] = []  # (step, restarted, window) of each setup write
        self.tokens = json.loads((state / "mock-tokens.json").read_text())

    # -- kubectl ------------------------------------------------------------
    def kubectl(self, *args: str, ns: str | None = NS, check: bool = True, stdin: str | None = None,
                timeout: float = 120) -> str:
        argv = ["kubectl", "--context", self.context, *(["-n", ns] if ns else []), *args]
        r = subprocess.run(argv, capture_output=True, text=True, input=stdin, timeout=timeout, check=False)
        if check and r.returncode != 0:
            raise Failure(f"kubectl {' '.join(args)[:200]}: {r.stderr.strip()[-600:]}")
        return r.stdout

    def get_json(self, kind: str, name: str, ns: str = NS):
        out = self.kubectl("get", kind, name, "-o", "json", "--ignore-not-found", ns=ns)
        return json.loads(out) if out.strip() else None

    def logs(self, target: str, since: str | None = None) -> str:
        args = ["logs", target, "--tail", "-1"] + ([f"--since={since}"] if since else [])
        return self.kubectl(*args, check=False)

    def exec_python(self, target: str, code: str, container: str | None = None) -> str:
        """Run `code` inside a pod (`python -`): values the code reads there never leave it."""
        args = ["exec", "-i", target] + (["-c", container] if container else []) + ["--", "python", "-"]
        return self.kubectl(*args, stdin=code, timeout=180)

    def rollout(self, deployment: str) -> None:
        self.kubectl("rollout", "status", f"deployment/{deployment}", "--timeout", "300s", timeout=320)

    def audit(self) -> list[dict]:
        out = subprocess.run(["docker", "exec", f"{self.cluster}-control-plane", "cat",
                              "/var/log/kubernetes/kube-apiserver-audit.log"],
                             capture_output=True, text=True, check=True).stdout
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    # -- HTTP -----------------------------------------------------------------
    def api(self, method: str, path: str, body=None, headers: dict | None = None,
            timeout: float = 120) -> httpx.Response:
        """A Symphony API call through the port-forward (the API restarts during setup). A read is
        retried when the forward drops; a write is sent once, on a new forward to the current pod."""
        deadline = time.monotonic() + timeout
        if method != "GET":
            self.fwd["api"].stop()
        while True:
            self.fwd["api"].ensure()
            try:
                return self.http.request(method, self.fwd["api"].url + path, json=body, headers={
                    "Authorization": f"Bearer {self.token}", **(headers or {})}, timeout=60)
            except httpx.TransportError as exc:
                if (method != "GET" and not isinstance(exc, httpx.ConnectError)) or time.monotonic() > deadline:
                    raise Failure(f"API {method} {path}: {type(exc).__name__}") from exc
                self.fwd["api"].stop()
                time.sleep(1)

    def mock(self, name: str, method: str, route: str, body=None, params=None) -> dict:
        self.fwd[name].ensure()
        r = self.http.request(method, self.fwd[name].url + route, json=body, params=params)
        if r.status_code >= 400:
            raise Failure(f"{name} mock {method} {route}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def setup_write(self, area: Area, step: str, method: str, path: str, body=None) -> dict:
        """One setup write: its answer, then the rollouts of what it restarted."""
        start = time.time()
        r = self.api(method, path, body)
        took = time.time() - start
        try:
            doc = r.json()
        except ValueError:
            doc = {"text": r.text[:300]}
        if r.status_code != 200:
            raise Failure(f"{method} {path}: {r.status_code} {json.dumps(doc)[:500]}")
        restarted = list(doc.get("restarted") or [])
        for name in restarted:
            self.rollout(name)
        if "symphony-api" in restarted:
            self.fwd["api"].stop()
        until(lambda: self.api("GET", "/readyz").status_code == 200, 120, "the API after " + step)
        # The patches happen before the answer: the window is the request itself.
        self.restarts.append({"step": step, "restarted": restarted, "from": start - 0.5, "to": start + took + 0.5})
        area.time(f"{method} {path} (answer)", took)
        area.time(f"{method} {path} → restarted rollouts complete", time.time() - start)
        return doc

    # -- the attempt, recorded while it runs ------------------------------------
    STAGED = (
        "import json, os\n"
        "p = '/run/symphony/engine/mcp.json'\n"
        "d = json.load(open(p))\n"
        "servers = d.get('mcpServers', d)\n"
        "print(json.dumps({'mode': oct(os.stat(p).st_mode & 0o777), 'servers': {n: {'url': s.get('url'),"
        " 'headers': sorted((s.get('headers') or {}).keys()), 'bearer_prefix': (s.get('headers') or {})"
        ".get('Authorization', '')[:11]} for n, s in servers.items()}}))\n"
    )

    def session_meta(self, identifier: str) -> dict | None:
        keys = self.kubectl("exec", "deploy/symphony-redis", "--", "redis-cli", "--scan", "--pattern",
                            f"sym:{NS}:session:*:meta").split()
        for key in keys:
            raw = self.kubectl("exec", "deploy/symphony-redis", "--", "redis-cli", "get", key, check=False)
            try:
                meta = json.loads(raw)
            except ValueError:
                continue
            if meta.get("identifier") == identifier and meta.get("kind") == "implement":
                return {**meta, "sid": key[len(f"sym:{NS}:session:"):-len(":meta")]}
        return None

    def seiji_state_in_cluster(self) -> dict:
        out = self.kubectl("exec", "deploy/seiji", "--", "python", "-c",
                           "import urllib.request; print(urllib.request.urlopen("
                           "'http://127.0.0.1:4780/_control/state').read().decode())", ns=MOCKS_NS, check=False)
        return json.loads(out) if out.strip() else {"leases": []}

    def start_watch(self, job: str) -> None:
        """Record the attempt while it runs: it may end before the later areas look. kubectl only
        (the port-forwards stay with the main thread): the staged MCP config of the workspace,
        the Seiji lease, the session's held leases and the runner log (the Job is deleted at
        terminal cleanup)."""
        from symphony.workspace import sandbox_name

        ident = self.ticket["identifier"]
        pod = sandbox_name(ident)
        w = self.watch = {"job": job, "pod": pod}
        log_path = self.state / "runner.log"

        def loop() -> None:
            deadline = time.monotonic() + AGENT_TIMEOUT_S
            ended_at = None
            while time.monotonic() < deadline:
                try:
                    out = self.kubectl("logs", f"job/{job}", "--tail", "-1", check=False)
                    if out.strip():
                        log_path.write_text(out)
                    obj = self.get_json("pod", pod)
                    if "staged" not in w and obj and obj.get("status", {}).get("phase") == "Running":
                        staged = self.kubectl("exec", "-i", pod, "-c", "workspace", "--", "python3", "-",
                                              stdin=self.STAGED, check=False).strip()
                        if staged:
                            text = self.kubectl("exec", pod, "-c", "workspace", "--", "cat",
                                                "/run/symphony/engine/mcp.json", check=False)
                            w["staged"] = json.loads(staged.splitlines()[-1])
                            w["staged_has_stored_value"] = any(v in text for v in self.tokens.values())
                            del text
                    if "lease" not in w:
                        leases = self.seiji_state_in_cluster().get("leases") or []
                        if leases:
                            w["lease"], w["lease_at"] = leases[0]["lease_id"], time.time()
                    meta = self.session_meta(ident)
                    if meta and "session_held" not in w:
                        # the session's held ids (internals §18); `mcp_leases` of the API meta
                        held = self.kubectl("exec", "deploy/symphony-redis", "--", "redis-cli", "hkeys",
                                            f"sym:{NS}:mcp:session-held:{meta['sid']}").split()
                        if held:
                            w["session_held"] = held
                    if w.get("lease") and "held_keys" not in w:
                        held = self.kubectl("exec", "deploy/symphony-redis", "--", "redis-cli", "--scan",
                                            "--pattern", f"sym:{NS}:mcp:held:*").split()
                        if held:
                            w["held_keys"] = len(held)
                    if meta and meta.get("state") == "ended" and ended_at is None:
                        ended_at = w["ended_at"] = time.time()
                        w["end"] = {k: meta.get(k) for k in ("outcome", "turn", "last_message", "tokens")}
                    if ended_at and time.time() - ended_at > 30:
                        break
                except Exception as exc:  # noqa: BLE001 — the areas report what is missing
                    w.setdefault("errors", []).append(repr(exc)[:200])
                time.sleep(2)
            w["done"] = True

        threading.Thread(target=loop, daemon=True).start()

    def setup_status(self) -> dict:
        return self.api("GET", "/v1/setup").json()

    def step(self, doc: dict, step_id: str) -> dict:
        return next(s for s in doc["steps"] if s["id"] == step_id)

    def secrets_view(self) -> dict:
        out = {}
        for name in INTEGRATION_SECRETS:
            obj = self.get_json("secret", name)
            out[name] = None if obj is None else {
                "uid": obj["metadata"]["uid"], "created": obj["metadata"]["creationTimestamp"],
                "keys": sorted((obj.get("data") or {}).keys())}
        return out

    def sessions(self) -> list[dict]:
        r = self.api("GET", "/v1/sessions?limit=200")
        return r.json().get("items", []) if r.status_code == 200 else []

    def slack_replies(self, root: str) -> list[dict]:
        msgs = self.mock("slack", "GET", "/_mock/messages")["messages"]
        return [m for m in msgs if m.get("via") == "chat.postMessage" and m.get("thread_ts") == root]

    def mention(self, text: str) -> dict:
        body = self.mock("slack", "POST", "/_control/socket/event", {"event": {
            "type": "app_mention", "user": SLACK_PERSON, "channel": CHANNEL, "text": f"<@{BOT_USER_ID}> {text}"}})
        return {"root": body["ts"], "envelope": body["envelope_id"]}

    def settled(self, root: str, needle, timeout: float = 600) -> dict:
        """The bot's first message in the thread once `needle` holds and the text is stable 4 s."""
        last: dict = {}

        def done():
            msgs = self.slack_replies(root)
            if not msgs or not needle(msgs[0]["text"]):
                last.clear()
                return None
            if last.get("text") != msgs[0]["text"]:
                last.update(text=msgs[0]["text"], at=time.monotonic())
                return None
            return msgs[0] if time.monotonic() - last["at"] >= 4 else None

        return until(done, timeout, "the assistant's answer", interval=1)

    # ======================================================================= areas
    def run(self) -> None:
        for key, title, fn in (
            ("0", "NetworkPolicy enforced", self.a0_networkpolicy),
            ("1", "Minimal install and setup", self.a1_setup),
            ("2", "MCP through the gateway", self.a2_mcp),
            ("3", "Assistant Deployment on Slack Socket Mode", self.a3_assistant),
            ("4", "Seiji through the gateway, lease cleanup", self.a4_seiji),
        ):
            area = self.areas[key] = Area(key, title)
            print(f"== area {key}: {title}", flush=True)
            started = time.time()
            try:
                fn(area)
            except Exception as exc:  # noqa: BLE001 — one area's failure is its own result
                area.error = f"{type(exc).__name__}: {exc}"
                print(f"  [ERROR] {key}: {area.error[:400]}", flush=True)
                if key == "1":
                    break  # the later areas need a complete setup
            area.time("area total", time.time() - started)

    # -- 0 ------------------------------------------------------------------
    PROBE = (
        "import json, socket, sys\n"
        "out = {}\n"
        "for name, host, port in json.loads(sys.argv[1]):\n"
        "    s = socket.socket(); s.settimeout(3)\n"
        "    try:\n"
        "        s.connect((host, port)); out[name] = 'open'\n"
        "    except Exception as exc:\n"
        "        out[name] = 'blocked (' + type(exc).__name__ + ')'\n"
        "    finally:\n"
        "        s.close()\n"
        "print(json.dumps(out))\n"
    )

    def probe_pod(self, name: str, component: str, targets: list) -> dict:
        overrides = {"spec": {"automountServiceAccountToken": False}}
        out = self.kubectl(
            "run", name, "--rm", "-i", "--quiet", "--restart=Never", "--image=symphony-orchestrator:e2e",
            "--image-pull-policy=IfNotPresent", f"--labels=app=symphony,component={component}",
            f"--overrides={json.dumps(overrides)}", "--command", "--", "python", "-c", self.PROBE,
            json.dumps(targets), timeout=180)
        return json.loads(out.strip().splitlines()[-1])

    def a0_networkpolicy(self, area: Area) -> None:
        targets = [["redis", "symphony-redis", 6379], ["api", "symphony-api", 8080],
                   ["gateway", "symphony-gateway", 8090],
                   ["github mock (D3a)", f"github.{MOCKS_NS}.svc", 4200],
                   ["linear mock", f"linear.{MOCKS_NS}.svc", 4100],
                   ["mcp mock", f"mcp.{MOCKS_NS}.svc", 4600]]
        ws = self.probe_pod("np-probe-workspace", "workspace", targets)
        area.check("a pod with the workspace labels reaches only the gateway and the GitHub mock (D3a); "
                   "Redis, the API and the other mocks are blocked",
                   ws["gateway"] == "open" and ws["github mock (D3a)"] == "open"
                   and all(ws[k].startswith("blocked") for k in ("redis", "api", "linear mock", "mcp mock")), ws)
        daemon = self.probe_pod("np-probe-daemon", "daemon", targets)
        area.check("a pod with the daemon labels reaches Redis and the API (its egress is open); "
                   "the gateway refuses it (ingress for agents only)",
                   daemon["redis"] == "open" and daemon["api"] == "open" and daemon["gateway"].startswith("blocked"),
                   daemon)
        code = self.PROBE.replace("json.loads(sys.argv[1])", repr(
            [["linear mock", f"linear.{MOCKS_NS}.svc", 4100], ["mcp mock (D3b)", f"mcp.{MOCKS_NS}.svc", 4600],
             ["seiji mock (D3b)", f"seiji.{MOCKS_NS}.svc", 4780], ["api", "symphony-api", 8080],
             ["redis", "symphony-redis", 6379]]))
        gw = json.loads(self.exec_python("deploy/symphony-gateway", code).strip().splitlines()[-1])
        area.check("the gateway reaches Redis and the MCP and Seiji mocks (D3b); the API and the Linear mock "
                   "are blocked (private ranges closed)",
                   gw["redis"] == "open" and gw["mcp mock (D3b)"] == "open" and gw["seiji mock (D3b)"] == "open"
                   and gw["api"].startswith("blocked") and gw["linear mock"].startswith("blocked"), gw)

    # -- 1 ------------------------------------------------------------------
    def a1_setup(self, area: Area) -> None:
        t0 = time.time()
        before = self.secrets_view()
        area.check("the operator made only symphony-api and model-provider; the manifests made the other "
                   "integration Secrets empty",
                   before["symphony-api"] and before["model-provider"]
                   and all(before[n] is not None and before[n]["keys"] == []
                           for n in ("linear-api-key", "github-app", "symphony-slack", "symphony-discord",
                                     "symphony-mcp", "symphony-providers")),
                   {n: (v or {}).get("keys") for n, v in before.items()})
        doc = self.setup_status()
        area.check("GET /v1/setup on the fresh install: mode kubernetes, not complete",
                   doc["mode"] == "kubernetes" and doc["complete"] is False,
                   {s["id"]: s["status"] for s in doc["steps"]})

        # The idle components
        idle = until(lambda: "daemon_setup_incomplete" in self.logs("deploy/symphony-daemon")
                     and self.logs("deploy/symphony-daemon"), 120, "daemon_setup_incomplete")
        line = next(ln for ln in idle.splitlines() if "daemon_setup_incomplete" in ln)
        overview = self.api("GET", "/v1/overview").json()["daemon"]
        pods = json.loads(self.kubectl("get", "pods", "-o", "json"))["items"]
        restarts = {p["metadata"]["labels"].get("component"): sum(c.get("restartCount", 0)
                    for c in p["status"].get("containerStatuses") or []) for p in pods}
        area.check("the daemon idles: daemon_setup_incomplete, overview daemon.setup incomplete, no runner Job, "
                   "no pod restarted",
                   overview.get("setup") == "incomplete" and not self.kubectl("get", "jobs", "-o", "name").strip()
                   and not any(restarts.values()),
                   {"log": line[:300], "overview.daemon": {k: overview.get(k) for k in ("alive", "setup")},
                    "restart_counts": restarts})
        merger = until(lambda: "merger_not_configured" in self.logs("deploy/symphony-merger")
                       and self.logs("deploy/symphony-merger"), 120, "merger_not_configured")
        assistant = until(lambda: "assistant_not_configured" in self.logs("deploy/symphony-assistant")
                          and self.logs("deploy/symphony-assistant"), 120, "assistant_not_configured")
        area.check("the merger and the assistant idle too (merger_not_configured, assistant_not_configured)", True,
                   {"merger": next(ln for ln in merger.splitlines() if "merger_not_configured" in ln)[:300],
                    "assistant": next(ln for ln in assistant.splitlines()
                                      if "assistant_not_configured" in ln)[:300]})

        # The vendors: an App installed on acme/app, the ticket waiting on the board
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
        public_key = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        self.mock("github", "POST", "/_mock/app", {"public_key": public_key})
        person_token = self.mock("github", "POST", "/_mock/users", {"login": PERSON, "name": "Octo Cat"})["token"]
        self.mock("github", "POST", "/_mock/repos", {"owner": "acme", "name": "app",
                                                      "seed": "/app/scripts/e2e_mocked/seed/app",
                                                      "labels": list(MERGER_LABELS)})
        self.mock("github", "POST", "/_mock/installations", {"account": "acme", "repositories": ["app"],
                                                              "permissions": APP_PERMISSIONS})
        app_env = self.mock("github", "GET", "/_mock/app")["env"]
        self.mock("linear", "POST", "/_mock/labels", {"name": f"repo:{APP}", "team": TEAM})
        self.ticket = self.mock("linear", "POST", "/_mock/issues", {
            "title": "Look up the ticket context and check the app on an iPhone", "team": TEAM, "state": "Todo",
            "labels": ["symphony", "investigation", f"repo:{APP}"], "description": TICKET})
        ident = self.ticket["identifier"]
        area.check("the ticket waits on the Linear board (Todo, symphony, investigation, repo:acme/app)",
                   bool(ident), {"identifier": ident})

        # GitHub App
        out = self.setup_write(area, "github_app", "PUT", "/v1/setup/github-app", {
            "app_id": str(app_env["GITHUB_APP_ID"]), "installation_id": str(app_env["GITHUB_APP_INSTALLATION_ID"]),
            "private_key": private_key})
        area.check("PUT /v1/setup/github-app: verified against the GitHub mock (repositories, no missing "
                   "permission)",
                   out.get("missing_permissions") == [] and APP in (out.get("repositories") or []),
                   {k: out.get(k) for k in ("repositories", "missing_permissions", "restarted")})
        area.check("restart order of the github-app group: daemon, merger, assistant, then API",
                   out.get("restarted") == FULL_ORDER, out.get("restarted"), fatal=False)
        # Repositories
        out = self.setup_write(area, "repositories", "PUT", "/v1/setup/repositories", {"default_repo": APP})
        area.check("PUT /v1/setup/repositories: default_repo acme/app", out.get("default_repo") == APP,
                   {k: out.get(k) for k in ("default_repo", "mergers", "without_merger")})
        # The operator's workflow settings (an audited publication, applied by the idle daemon)
        self.publish_workflow(area)
        self.publish_models(area)
        # Notifications: the slack group
        out = self.setup_write(area, "notifications", "PUT", "/v1/setup/notifications",
                               {"slack": {"bot_token": DEFAULT_TOKEN, "channel": CHANNEL}})
        tests = [m for m in self.mock("slack", "GET", "/_mock/messages")["messages"] if m.get("channel") == CHANNEL]
        area.check("PUT /v1/setup/notifications (Slack bot): the test message reached the Slack mock first",
                   bool(out.get("slack", {}).get("configured")) and bool(tests),
                   {"slack": out.get("slack"), "restarted": out.get("restarted"),
                    "mock_messages": [m.get("text", "")[:80] for m in tests[:2]]})
        area.check("restart order of the slack group: daemon, merger, assistant, then API",
                   out.get("restarted") == FULL_ORDER, out.get("restarted"), fatal=False)
        # MCP servers are stored now so that the first attempt stages them (areas 2 and 4)
        self.store_mcp(area)

        # Tracker: the last required step; the daemon must dispatch the waiting ticket
        jobs_before = self.kubectl("get", "jobs", "-o", "name").strip()
        tracker_at = time.time()
        out = self.setup_write(area, "tracker", "PUT", "/v1/setup/tracker",
                               {"kind": "linear", "api_key": LINEAR_KEY, "team_key": TEAM})
        area.check("PUT /v1/setup/tracker: verified against the Linear mock, the WORKFLOW tracker published",
                   out.get("kind") == "linear" and bool(out.get("states")),
                   {k: out.get(k) for k in ("kind", "states", "warnings", "restarted")})
        area.check("restart order of the linear group: daemon, merger, assistant, then API",
                   out.get("restarted") == FULL_ORDER, out.get("restarted"), fatal=False)
        # Every Deployment that reads linear-api-key and github-app got the new values.
        names = self.exec_python("deploy/symphony-assistant", (
            "import json, os\n"
            "print(json.dumps({n: n in os.environ for n in ('LINEAR_API_KEY', 'GITHUB_APP_ID')}))\n"))
        area.check("the restarted assistant sees the stored tracker key and GitHub App (names only)",
                   all(json.loads(names.strip().splitlines()[-1]).values()),
                   json.loads(names.strip().splitlines()[-1]), fatal=False)
        # The tracker step restarts the daemon: the new pod starts configured, so its overview
        # report is `ready` (an idle pod that turns configured logs daemon_setup_complete).
        ready = until(lambda: self.api("GET", "/v1/overview").json()["daemon"].get("setup") == "ready", 180,
                      "overview daemon.setup ready")
        area.time("tracker step sent → daemon.setup ready", time.time() - tracker_at)
        job = until(lambda: self.kubectl("get", "jobs", "-l", "component=runner", "-o", "name").strip(), 180,
                    "the runner Job")
        self.start_watch(job.split("/", 1)[1])
        area.time("tracker step sent → runner Job", time.time() - tracker_at)
        meta = until(lambda: next((m for m in self.sessions() if m.get("identifier") == ident), None), 300,
                     f"the session of {ident}")
        area.time("tracker step sent → session", time.time() - tracker_at)
        self.sid = meta["sid"]
        dispatch = [ln for ln in self.logs("deploy/symphony-daemon").splitlines()
                    if "event='dispatch'" in ln and ident in ln]
        area.check("after the tracker step the daemon reports setup ready and dispatches the waiting ticket",
                   ready and not jobs_before and bool(job) and bool(dispatch),
                   {"overview.daemon.setup": "ready", "dispatch_log": dispatch[0][:300] if dispatch else None,
                    "runner_job": job, "session": {k: meta.get(k) for k in ("sid", "kind", "identifier", "model")}})

        # Assistant and webhooks (optional steps)
        out = self.setup_write(area, "assistant", "PUT", "/v1/setup/assistant", {
            "slack": {"app_token": DEFAULT_APP_TOKEN, "channels": [CHANNEL]}, "repositories": [APP]})
        area.check("PUT /v1/setup/assistant: the app token checked with apps.connections.open; a new app token "
                   "restarts the slack group, else the assistant alone",
                   (out.get("slack") or {}).get("channels") == [CHANNEL] and "symphony-assistant" in out.get(
                       "restarted", []), {k: out.get(k) for k in ("slack", "repositories", "restarted")})
        out = self.setup_write(area, "webhooks", "POST", "/v1/setup/webhooks", {})
        hooks = {k: {"url": v.get("url"), "secret_returned": bool(v.get("secret"))}
                 for k, v in out.items() if isinstance(v, dict) and "url" in v}
        area.check("POST /v1/setup/webhooks: both secrets returned once, URLs from SYMPHONY_PUBLIC_URL",
                   all(h["secret_returned"] and h["url"].startswith("http://symphony-api.symphony.svc:8080/")
                       for h in hooks.values()) and len(hooks) == 2, {"webhooks": hooks,
                                                                      "restarted": out.get("restarted")})
        self.mock("linear", "PUT", "/_mock/webhooks", {"url": out["linear"]["url"], "secret": out["linear"]["secret"]})
        self.mock("github", "POST", "/_mock/webhook", {"url": out["github"]["url"], "secret": out["github"]["secret"]})
        out = None  # the secrets are not kept
        # One event of each vendor: a Linear comment, and a person's GitHub issue (issues.opened)
        self.mock("linear", "POST", f"/_mock/issues/{ident}/comments", {"body": "Webhook check from the operator."})
        self.fwd["github"].ensure()
        r = self.http.post(self.fwd["github"].url + f"/repos/{APP}/issues", json={"title": "Webhook check"},
                           headers={"Authorization": f"Bearer {person_token}", "Accept": "application/vnd.github+json"})
        if r.status_code != 201:
            raise Failure(f"GitHub mock: create issue {r.status_code}")

        def delivered():
            doc = self.setup_status()
            detail = self.step(doc, "webhooks")["detail"]
            return doc if all((detail.get(s) or {}).get("last_delivery_at") for s in ("linear", "github")) else None

        doc = until(delivered, 120, "a signed delivery from each mock", interval=3)
        area.check("a signed Linear and a signed GitHub webhook reached the API (webhooks step last_delivery_at)",
                   True, self.step(doc, "webhooks")["detail"])
        area.check("GET /v1/setup: complete, every step done",
                   doc["complete"] is True and all(s["status"] == "done" for s in doc["steps"]),
                   {s["id"]: [s["status"], s.get("error")] for s in doc["steps"]}, fatal=False)
        area.time("setup start → complete", time.time() - t0)

        # Patched, never created
        after = self.secrets_view()
        area.check("the integration Secrets kept their UID and creation time; setup added the names",
                   all(after[n]["uid"] == before[n]["uid"] and after[n]["created"] == before[n]["created"]
                       for n in INTEGRATION_SECRETS)
                   and set(after["linear-api-key"]["keys"]) == {"LINEAR_API_KEY", "LINEAR_WEBHOOK_SECRET"}
                   and set(after["github-app"]["keys"]) == {"GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID",
                                                            "GITHUB_APP_PRIVATE_KEY"}
                   and "SLACK_APP_TOKEN" in after["symphony-slack"]["keys"]
                   and "GITHUB_WEBHOOK_SECRET" in after["symphony-api"]["keys"],
                   {n: {"uid_kept": after[n]["uid"] == before[n]["uid"], "keys": after[n]["keys"]}
                    for n in INTEGRATION_SECRETS})
        events = [e for e in self.audit() if e.get("user", {}).get("username") == "system:serviceaccount:symphony:"
                  "symphony-api" and e.get("stage") == "ResponseComplete"]
        writes = [e for e in events if (e.get("objectRef") or {}).get("resource") == "secrets"
                  and e["verb"] not in ("get", "list", "watch")]
        by_verb: dict[str, list[str]] = {}
        for e in writes:
            by_verb.setdefault(e["verb"], []).append(e["objectRef"].get("name"))
        area.check("Kubernetes audit log: the API wrote Secrets only with patch (no create, no delete)",
                   set(by_verb) == {"patch"} and all(e["responseStatus"]["code"] == 200 for e in writes),
                   {verb: sorted(set(names)) for verb, names in by_verb.items()} | {"writes": len(writes)})
        patches = [e for e in events if (e.get("objectRef") or {}).get("resource") == "deployments"
                   and e["verb"] == "patch"]
        windows = []
        for w in self.restarts:
            seen = [e["objectRef"]["name"] for e in sorted(patches, key=lambda e: e["requestReceivedTimestamp"])
                    if w["from"] <= ts(e["requestReceivedTimestamp"]) <= w["to"]]
            windows.append({"step": w["step"], "answer": w["restarted"], "audit_patches": seen})
        area.check("Kubernetes audit log: each step patched the Deployments in the order it answered",
                   all(w["answer"] == w["audit_patches"] for w in windows), windows)

        # RBAC
        self.rbac(area)

    def publish_models(self, area: Area) -> None:
        """The test account has no access to the default Haiku model of the assistant, planner and
        probe roles (Bedrock 403): the operator moves them to Opus in the team configuration."""
        current = self.api("GET", "/v1/admin/config")
        generation = current.headers.get("ETag", "").strip('"')
        models = {role: {"model": "global.anthropic.claude-opus-5-5", "thinking": "low"}
                  for role in ("assistant", "planner", "probe")}
        r = self.api("PUT", "/v1/admin/config", {"models": models, "reason": "kind setup certification: Opus"},
                     headers={"If-Match": generation})
        area.check("PUT /v1/admin/config: the assistant, planner and probe roles on Opus (models step stays done)",
                   r.status_code == 200 and all(r.json()["models"][role]["model"] == models[role]["model"]
                                                for role in models),
                   {"status": r.status_code, "roles": {role: r.json().get("models", {}).get(role) for role in models}})

    def publish_workflow(self, area: Area) -> None:
        current = self.api("GET", "/v1/workflow")
        revision = current.headers.get("ETag", "").strip('"')
        patch = {"kubernetes.agent_runtime": "node", "kubernetes.privileged_agents": False,
                 "kubernetes.learning.enabled": False, "kubernetes.sandbox.cpu": 1,
                 "kubernetes.sandbox.memory_mib": 2048, "agent.max_turns": 4, "agent.max_concurrent_agents": 1,
                 "polling.interval_ms": 5000,
                 "notify.users": json.dumps({"slack": {PERSON: SLACK_PERSON}, "discord": {}})}
        r = self.api("PUT", "/v1/workflow", {"patch": patch,
                                             "reason": "kind setup certification: small node sandbox"},
                     headers={"If-Match": revision})
        if r.status_code != 202:
            raise Failure(f"PUT /v1/workflow: {r.status_code} {r.text[:400]}")
        cid = r.json()["id"]
        done = until(lambda: (c := self.api("GET", f"/v1/commands/{cid}").json())["state"] in ("succeeded", "failed")
                     and c, 120, "the workflow command")
        area.check("PUT /v1/workflow (node runtime, no privileged agents, learning off) applied by the idle "
                   "daemon", done["state"] == "succeeded", {"state": done["state"], "result": done.get("result"),
                                                             "patch_keys": sorted(patch)})

    def store_mcp(self, area: Area) -> None:
        r = self.api("PUT", "/v1/admin/mcp/mock", {"transport": "http", "url": MCP_URL,
                                                   "roles": ["implementer", "assistant"], "enabled": True})
        area.check("PUT /v1/admin/mcp/mock (http, roles implementer and assistant)", r.status_code == 200,
                   {"status": r.status_code, "body": r.json()})
        r = self.api("PUT", "/v1/admin/mcp/mock/headers", {"headers": {
            "Authorization": f"Bearer {self.tokens['MCP_MOCK_TOKEN']}", MCP_HEADER: "acme-e2e"}})
        area.check("PUT /v1/admin/mcp/mock/headers → the header names only",
                   r.status_code == 200 and sorted(r.json()["headers_set"]) == sorted(["Authorization", MCP_HEADER])
                   and self.tokens["MCP_MOCK_TOKEN"] not in r.text, r.json())
        r = self.api("PUT", "/v1/admin/mcp/seiji", {"preset": "seiji", "url": f"{SEIJI_URL}/mcp",
                                                    "roles": ["implementer"], "enabled": True,
                                                    "rewrite_origins": [SEIJI_PUBLIC]})
        stored = r.json().get("server", r.json()) if r.status_code == 200 else {"text": r.text[:300]}
        area.check("PUT /v1/admin/mcp/seiji with preset seiji → a plain definition with cleanup",
                   r.status_code == 200 and stored.get("http_base") == SEIJI_URL
                   and (stored.get("cleanup") or {}).get("release_tool") == "release_device", stored)
        r = self.api("PUT", "/v1/admin/mcp/seiji/headers",
                     {"headers": {"Authorization": f"Bearer {self.tokens['SEIJI_MOCK_TOKEN']}"}})
        area.check("PUT /v1/admin/mcp/seiji/headers → the header names only",
                   r.status_code == 200 and r.json().get("headers_set") == ["Authorization"]
                   and self.tokens["SEIJI_MOCK_TOKEN"] not in r.text, r.json())

    def can_i(self, sa: str, verb: str, resource: str) -> bool:
        r = subprocess.run(["kubectl", "--context", self.context, "-n", NS, "auth", "can-i", verb, resource,
                            f"--as=system:serviceaccount:{NS}:{sa}"], capture_output=True, text=True, check=False)
        return r.stdout.strip() == "yes"

    def rbac(self, area: Area) -> None:
        self.kubectl("create", "secret", "generic", "e2e-decoy", "--from-literal=value=not-a-credential",
                     check=False)
        allowed = {
            "symphony-api": [("patch", "secret/linear-api-key"), ("patch", "secret/github-app"),
                             ("patch", "secret/symphony-slack"), ("patch", "secret/symphony-mcp"),
                             ("update", "secret/symphony-providers"), ("get", "secret/model-provider"),
                             ("update", "configmap/symphony-workflow"), ("patch", "deployment/symphony-daemon"),
                             ("patch", "deployment/symphony-merger"), ("patch", "deployment/symphony-assistant"),
                             ("patch", "deployment/symphony-api"), ("list", "deployments")],
            "symphony-daemon": [("create", "jobs"), ("delete", "pods"), ("update", "configmap/symphony-workflow")],
            "symphony-runner": [("create", "pods"), ("create", "persistentvolumeclaims"),
                                ("get", "secret/model-provider"), ("get", "secret/symphony-mcp")],
            "symphony-merger": [("get", "secret/symphony-providers"), ("get", "secret/symphony-mcp")],
            "symphony-assistant": [("get", "secret/symphony-providers"), ("get", "secret/symphony-mcp")],
        }
        denied = {
            "symphony-api": [("create", "secrets"), ("delete", "secret/linear-api-key"), ("list", "secrets"),
                             ("get", "secret/e2e-decoy"), ("patch", "secret/model-provider"),
                             ("patch", "secret/e2e-decoy"), ("patch", "deployment/symphony-gateway"),
                             ("patch", "deployment/symphony-redis"), ("create", "deployments"),
                             ("update", "configmap/symphony-api-config"), ("create", "pods/exec"),
                             ("create", "jobs"), ("get", "secret/linear-api-key -n kube-system")],
            "symphony-daemon": [("get", "secret/linear-api-key"), ("create", "pods"), ("list", "secrets"),
                                ("update", "configmap/symphony-api-config")],
            "symphony-runner": [("get", "secret/linear-api-key"), ("get", "secret/github-app"),
                                ("get", "secret/symphony-slack"), ("create", "jobs"), ("list", "secrets")],
            "symphony-gateway": [("get", "secret/symphony-mcp"), ("list", "pods"), ("get", "secret/e2e-decoy")],
            "symphony-merger": [("get", "secret/linear-api-key"), ("get", "secret/model-provider"),
                                ("list", "secrets"), ("create", "pods")],
            "symphony-assistant": [("get", "secret/linear-api-key"), ("list", "secrets"), ("create", "jobs")],
        }

        def run(table: dict) -> dict:
            out = {}
            for sa, rows in table.items():
                for verb, res in rows:
                    parts = res.split()
                    out[f"{sa}: {verb} {res}"] = self.can_i(sa, verb, " ".join(parts))
            return out

        yes, no = run(allowed), run(denied)
        area.check("RBAC (kubectl auth can-i as each ServiceAccount): every named object is allowed",
                   all(yes.values()), {k: v for k, v in yes.items() if not v} or f"{len(yes)} allowed")
        area.check("RBAC: everything outside the named objects is refused",
                   not any(no.values()), {k: v for k, v in no.items() if v} or f"{len(no)} refused")
        code = (
            "import httpx, json\n"
            "sa = '/var/run/secrets/kubernetes.io/serviceaccount/'\n"
            "token = open(sa + 'token').read().strip()\n"
            "c = httpx.Client(base_url='https://kubernetes.default.svc', verify=sa + 'ca.crt', "
            "headers={'Authorization': 'Bearer ' + token}, timeout=10)\n"
            "base = '/api/v1/namespaces/symphony/secrets'\n"
            "out = {\n"
            " 'create secret': c.post(base, json={'apiVersion': 'v1', 'kind': 'Secret', "
            "'metadata': {'name': 'e2e-made-by-api'}}).status_code,\n"
            " 'get secret e2e-decoy': c.get(base + '/e2e-decoy').status_code,\n"
            " 'delete secret linear-api-key': c.delete(base + '/linear-api-key').status_code,\n"
            " 'patch deployment symphony-gateway': c.patch('/apis/apps/v1/namespaces/symphony/deployments/"
            "symphony-gateway', content=b'{}', headers={'Content-Type': 'application/merge-patch+json'}).status_code,\n"
            " 'get secret linear-api-key (allowed name)': c.get(base + '/linear-api-key').status_code,\n"
            "}\n"
            "print(json.dumps(out))\n"
        )
        real = json.loads(self.exec_python("deploy/symphony-api", code).strip().splitlines()[-1])
        area.check("real requests with the API's own token from its pod: create, delete and other names "
                   "get 403; the named Secret answers",
                   all(v == 403 for k, v in real.items() if "allowed" not in k)
                   and real["get secret linear-api-key (allowed name)"] == 200
                   and self.get_json("secret", "e2e-made-by-api") is None, real)

    # -- 2 ------------------------------------------------------------------
    def a2_mcp(self, area: Area) -> None:
        listed = self.api("GET", "/v1/admin/mcp")
        area.check("GET /v1/admin/mcp lists both servers and never a header value",
                   listed.status_code == 200 and self.tokens["MCP_MOCK_TOKEN"] not in listed.text
                   and self.tokens["SEIJI_MOCK_TOKEN"] not in listed.text,
                   [{k: x.get(k) for k in ("name", "transport", "url", "roles", "headers_set")}
                    for x in listed.json()["servers"]])
        tested = self.api("POST", "/v1/admin/mcp/mock/test").json()
        area.check("POST /v1/admin/mcp/mock/test from the API pod: ok, the mock's tools",
                   tested.get("ok") is True and MCP_TOOL in (tested.get("tools") or []), tested)
        # The role check, with tokens minted in the assistant pod (the gateway admits it); the
        # tokens never leave the pod.
        code = (
            "import hashlib, json, os, httpx, redis\n"
            "from symphony.mcp_servers import mint_token, revoke_token\n"
            "r = redis.Redis.from_url(os.environ['REDIS_URL'])\n"
            "ns = os.environ['POD_NAMESPACE']\n"
            "gw = 'http://symphony-gateway.' + ns + '.svc:8090/v1/mcp/mock'\n"
            "rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',"
            " 'capabilities': {}, 'clientInfo': {'name': 'kind-cert', 'version': '1'}}}\n"
            "hdr = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}\n"
            "out = {}\n"
            "for role in ('implementer', 'planner', 'reviewer'):\n"
            "    tok = mint_token(r, ns, role, 'kind-cert:' + role, 120)\n"
            "    resp = httpx.post(gw, json=rpc, headers={**hdr, 'Authorization': 'Bearer ' + tok}, timeout=20)\n"
            "    out[role] = [resp.status_code, resp.text[:60] if resp.status_code != 200 else 'initialize ok']\n"
            "    revoke_token(r, ns, tok)\n"
            "    resp = httpx.post(gw, json=rpc, headers={**hdr, 'Authorization': 'Bearer ' + tok}, timeout=20)\n"
            "    out[role + ' after revoke'] = resp.status_code\n"
            "resp = httpx.post(gw, json=rpc, headers={**hdr, 'Authorization': 'Bearer mcp_unknown'}, timeout=20)\n"
            "out['unknown token'] = [resp.status_code, resp.text[:60]]\n"
            "print(json.dumps(out))\n"
        )
        # The attempt runs next to these checks: wait for its end, so the mock's record of the
        # probe below has no other request in it.
        until(lambda: self.watch.get("done"), AGENT_TIMEOUT_S, "the attempt to end", interval=2)
        n_before = len(self.mock("mcp", "GET", "/_control/requests")["requests"])
        roles = json.loads(self.exec_python("deploy/symphony-assistant", code).strip().splitlines()[-1])
        calls = self.mock("mcp", "GET", "/_control/requests")["requests"][n_before:]
        area.check("role check at the gateway: implementer (in roles) passes; planner and reviewer (not in "
                   "roles), a revoked token and an unknown token get 403 mcp_forbidden",
                   roles["implementer"][0] == 200 and all(
                       roles[k][0] == 403 and "mcp_forbidden" in roles[k][1]
                       for k in ("planner", "reviewer", "unknown token"))
                   and all(roles[f"{k} after revoke"] == 403 for k in ("implementer", "planner", "reviewer")),
                   roles)
        area.check("the mock saw exactly the one admitted request, with the stored Authorization and "
                   f"{MCP_HEADER} (injected by the gateway)",
                   len(calls) == 1 and calls[0]["has_auth"] and MCP_HEADER.lower() in calls[0]["headers"],
                   calls)
        # The implementer's workspace (recorded while the attempt ran)
        staged = self.watch.get("staged") or {}
        gw = "http://symphony-gateway.symphony.svc"
        area.check("the workspace's staged mcp.json points both servers at the gateway with an mcp_ session "
                   "token; no stored value in it",
                   set(staged.get("servers") or {}) == {"mock", "seiji"}
                   and all(s["url"].startswith(gw) and s["url"].endswith(f":8090/v1/mcp/{name}")
                           and s["bearer_prefix"] == "Bearer mcp_" for name, s in staged["servers"].items())
                   and self.watch.get("staged_has_stored_value") is False,
                   {"staged": staged, "stored_value_in_file": self.watch.get("staged_has_stored_value")})
        ident = self.ticket["identifier"]
        ws_calls = [c for c in self.mock("mcp", "GET", "/_control/requests")["requests"][:n_before]
                    if c["rpc_method"] == "tools/call"]
        area.check("the implementer's workspace called the mock MCP tool through the gateway; every request had "
                   "the stored headers", bool(ws_calls) and all(c["has_auth"] and MCP_HEADER.lower() in c["headers"]
                                                               for c in ws_calls), ws_calls[:1])
        issue = self.mock("linear", "GET", f"/_mock/issues/{ident}")
        want = f"{ident}: no linked incidents; owner team ENG."
        area.check("the MCP result reached the ticket (the report comment)",
                   any(want in c["body"] for c in issue.get("comments") or []),
                   {"state": issue.get("state"),
                    "comment": next((c["body"][:300] for c in issue.get("comments") or [] if want in c["body"]),
                                    None)})

    # -- 3 ------------------------------------------------------------------
    def a3_assistant(self, area: Area) -> None:
        until(lambda: self.mock("slack", "GET", "/_control/socket")["connections"] >= 1, 120,
              "the assistant's Socket Mode connection")
        sock = self.mock("slack", "GET", "/_control/socket")
        step = until(lambda: (s := self.step(self.setup_status(), "assistant"))["detail"].get("running") and s, 120,
                     "the assistant heartbeat")
        area.check("the assistant pod opened Socket Mode on the Slack mock (apps.connections.open, then the "
                   "WebSocket); the setup step shows it running and connected",
                   sock["connections"] >= 1 and step["status"] == "done"
                   and (step["detail"].get("slack") or {}).get("connected") is True,
                   {"socket": {k: sock.get(k) for k in ("connections", "opened")},
                    "assistant_step": {k: step["detail"].get(k) for k in ("running", "slack", "repositories")}})
        t0 = time.monotonic()
        word = f"PONG-{secrets.randbelow(9000) + 1000}"
        sent = self.mention(f"Reply with the single word {word} and nothing else.")
        acks = until(lambda: [a for a in self.mock("slack", "GET", "/_control/socket")["acks"]
                              if a["envelope_id"] == sent["envelope"]], 10, "the envelope ack")
        area.check("the app_mention envelope was acked within 3 s", acks[0]["delay_s"] < 3, acks[0])
        working = until(lambda: next((m for m in self.slack_replies(sent["root"])
                                      if m["args"].get("text") == WORKING), None), 60, "Working on it…")
        area.time("mention → Working on it…", time.monotonic() - t0)
        answer = self.settled(sent["root"], lambda text: word in text)
        area.time("mention → final answer", time.monotonic() - t0)
        sessions = [m for m in self.sessions() if m.get("kind") == "assist" and m.get("thread") == sent["root"]]
        area.check("the mention is answered in its thread (Working on it…, then the same message updated), "
                   "with an assist session",
                   answer["ts"] == working["ts"] and word in answer["text"] and len(sessions) == 1,
                   {"answer": answer["text"][:200], "session": {k: sessions[0].get(k) for k in (
                       "sid", "kind", "transport", "channel", "role", "asker")} if sessions else None})
        # The assistant role through the gateway
        t1 = time.monotonic()
        n_before = len([c for c in self.mock("mcp", "GET", "/_control/requests")["requests"]
                        if c["rpc_method"] == "tools/call"])
        sent = self.mention(f"Call the MCP tool {MCP_TOOL} of the `mock` MCP server with the identifier ENG-4242 "
                            "and reply with its summary, verbatim.")
        want = "ENG-4242: no linked incidents; owner team ENG."
        answer = self.settled(sent["root"], lambda text: want in text)
        area.time("MCP question → answer", time.monotonic() - t1)
        calls = [c for c in self.mock("mcp", "GET", "/_control/requests")["requests"]
                 if c["rpc_method"] == "tools/call"][n_before:]
        area.check("assistant role: the answer carries the MCP result; the mock saw the call with the stored "
                   "headers", bool(calls) and all(c["has_auth"] and MCP_HEADER.lower() in c["headers"] for c in calls),
                   {"answer": answer["text"][:200], "tools_call": calls[:1]})

    # -- 4 ------------------------------------------------------------------
    def a4_seiji(self, area: Area) -> None:
        tested = self.api("POST", "/v1/admin/mcp/seiji/test").json()
        area.check("POST /v1/admin/mcp/seiji/test: ok, Seiji's tools, /v1/me",
                   tested.get("ok") is True and "acquire_device" in (tested.get("tools") or [])
                   and isinstance(tested.get("me"), dict),
                   {k: tested.get(k) for k in ("ok", "protocol_version", "me")} | {"tools": len(tested.get("tools")
                                                                                          or [])})
        until(lambda: self.watch.get("done"), AGENT_TIMEOUT_S, "the attempt to end", interval=2)
        w = self.watch
        if w.get("lease_at") and w.get("ended_at"):
            area.time("lease seen in the mock → session ended", w["ended_at"] - w["lease_at"])
        lease = w.get("lease")
        area.check("the implementer acquired a device through the gateway (the mock's lease); while the attempt "
                   "ran, Redis held it for the session (mcp:session-held) and for its token (mcp:held)",
                   bool(lease) and f"seiji/{lease}" in (w.get("session_held") or []) and bool(w.get("held_keys")),
                   {"lease": lease, "session_held": w.get("session_held"), "held_token_sets": w.get("held_keys")})
        released = until(lambda: next((x for x in self.mock("seiji", "GET", "/_control/state")["leases"]
                                       if x["lease_id"] == lease and x["status"] == "released"), None), 120,
                         "the lease release")
        state = self.mock("seiji", "GET", "/_control/state")
        tools = [x["tool"] for x in state["requests"] if x.get("rpc_method") == "tools/call"]
        held_left = self.kubectl("exec", "deploy/symphony-redis", "--", "redis-cli", "--scan", "--pattern",
                                 f"sym:{NS}:mcp:held*").split()
        log = (self.state / "runner.log").read_text() if (self.state / "runner.log").exists() else ""
        cleanup = [ln for ln in log.splitlines() if "event='mcp_cleanup'" in ln]
        finished = [ln for ln in log.splitlines() if "event='runner_finished'" in ln]
        stamp = lambda ln: ts(ln.split("timestamp='", 1)[1].split("'", 1)[0])  # noqa: E731
        if cleanup and finished:
            area.time("runner_finished → mcp_cleanup (lease released)", stamp(cleanup[-1]) - stamp(finished[-1]))
        area.check("end of the attempt: the runner's session cleanup released the kept lease (mcp_cleanup "
                   "held=1 released=1 in the runner log, one release_device); the lease ended `released`; "
                   "no held set left in Redis",
                   tools.count("release_device") == 1 and released["end_reason"] == "released" and not held_left
                   and any("released=1" in ln and "failed=0" in ln for ln in cleanup),
                   {"tools_call": tools, "lease": {k: released.get(k) for k in ("lease_id", "status", "end_reason")},
                    "mcp_cleanup": [ln[:200] for ln in cleanup], "held_keys_left": held_left})
        area.check("Seiji saw only the stored token (every request auth valid, no other bearer)",
                   all(x["auth"] == "valid" for x in state["requests"]) and state["other_bearers"] == [],
                   {"requests": len(state["requests"]), "other_bearers": len(state["other_bearers"])})
        end = w.get("end") or {}
        area.check("the attempt ended", bool(end), {k: end.get(k) for k in ("outcome", "turn", "last_message")} | {
            "tokens": (end.get("tokens") or {}).get("total") if isinstance(end.get("tokens"), dict) else None,
            "watch_errors": (w.get("errors") or [])[:3]})

    def diagnostics(self) -> dict:
        out = {}
        for name, args in {
            "objects": ("get", "pods,jobs,pvc", "-o", "wide"),
            "daemon": ("logs", "deploy/symphony-daemon", "--tail", "40"),
            "runner": ("logs", "-l", "component=runner", "--tail", "60"),
            "api": ("logs", "deploy/symphony-api", "--tail", "30"),
            "assistant": ("logs", "deploy/symphony-assistant", "--tail", "30"),
            "gateway": ("logs", "deploy/symphony-gateway", "--tail", "30"),
        }.items():
            try:
                out[name] = self.kubectl(*args, check=False).strip().splitlines()[-60:]
            except Exception as exc:  # noqa: BLE001 — diagnostics are best effort
                out[name] = [repr(exc)]
        return out


TICKET = (
    "This is an investigation: write a short report, change no code, open no pull request.\n\n"
    "1. Call the MCP tool `lookup_ticket_context` of the `mock` server (offered to you as "
    "`mcp__mock_lookup_ticket_context`) with this ticket's identifier. Put its summary, verbatim, in "
    "your report.\n"
    "2. Call the Seiji tool `acquire_device` (offered to you as `mcp__seiji_acquire_device`) with "
    "`platform` `ios`. Put the `lease_id` in your report.\n"
    "3. Call `run_actions` with `name` `Home screen` and `actions` "
    "`[{\"screenshot\": {\"name\": \"home\"}}]`.\n\n"
    "Do NOT call `release_device`: this ticket checks that Symphony releases the device when your "
    "session ends."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--context", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--summary-out")
    args = parser.parse_args()
    cert = Cert(args.context, Path(args.token_file).read_text().strip(), Path(args.state_dir))
    started = time.time()
    summary: dict = {}
    try:
        cert.run()
    finally:
        for fwd in cert.fwd.values():
            fwd.stop()
    summary["areas"] = [a.summary() for a in cert.areas.values()]
    summary["ok"] = len(cert.areas) == 5 and all(a.ok for a in cert.areas.values())
    if not summary["ok"]:
        summary["diagnostics"] = cert.diagnostics()
    summary["total_s"] = round(time.time() - started, 2)
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    for secret in cert.tokens.values():
        if secret in text:
            raise SystemExit("refusing to print a summary that holds a mock token")
    print(text)
    if args.summary_out:
        Path(args.summary_out).write_text(text + "\n", encoding="utf-8")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
