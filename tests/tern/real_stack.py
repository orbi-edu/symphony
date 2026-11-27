"""The real Symphony stack for the Tern plugin: Redis, the control API, the `symphony` CLI.

`RealHarness` is `Harness` with the fake backend replaced by:

- a disposable `redis-server`, seeded through the real stores (`SessionStore`, `TeamConfig`,
  `TokenStore`, `MergerStore`) plus the daemon's issue entries and heartbeat;
- the real control API (`real_api.py`: production wiring, in-memory provider Secret);
- the real CLI from this checkout's venv first on PATH, with an isolated HOME and
  `XDG_CONFIG_HOME` and the keychain disabled, so the credential lands in the 0600 file;
- `symphony login` + `symphony tern install --no-plugin` into the isolated `TERN_CONFIG_DIR`, so
  the ACP agent (`symphony acp`) and the MCP server are configured the way a person gets them.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import redis
from harness import REPO, Harness, TernError

from symphony import state
from symphony.apitokens import TokenStore
from symphony.merger.store import MergerStore
from symphony.sessions import SessionStore
from symphony.teamconfig import TeamConfig

HERE = Path(__file__).resolve().parent
BOOTSTRAP = "sym_0a1b2c3d_" + "TernRealStackBootstrapToken0123456789abcdef"
NAMESPACE = "tern-real"
# The one personal API key the Linear mock accepts (Linear keys: lin_api_ + 40 characters).
LINEAR_MOCK_KEY = "lin_api_" + "ternrealstack".ljust(40, "0")
# The bearer token the MCP mock requires (MCP_MOCK_TOKEN): the gateway must add it.
MCP_MOCK_TOKEN = "tern-real-mcp-token"
OMP_CATALOG = Path("/tmp/omp-src/packages/catalog/src/models.json")
KIMI = "global.moonshotai.kimi-k3.1"


def redis_binary() -> str | None:
    return shutil.which("redis-server")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def now_ms() -> int:
    return int(time.time() * 1000)


def entry(role: str, text: str, n: int) -> dict:
    return {"type": "message", "id": f"e{n}", "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def seed(client: redis.Redis) -> dict:
    """A team mid-day. Returns the ids the tests act on."""
    now = now_ms()
    sessions = SessionStore(client, NAMESPACE)
    issues = [
        ("iss-139", 2, "ENG-139", "Paginate the invoices API for the mobile app", "acme/billing", "running", "In Progress",
         "alice"),
        ("iss-142", 1, "ENG-142", "Export the sales report as CSV", "acme/web", "running", "In Progress", "bob"),
        ("iss-151", 1, "ENG-151", "Resend invite button on the teams page", "acme/web", "claimed", "In Progress",
         "Carol Diaz"),
        ("iss-147", 3, "ENG-147", "Fix tax rounding in BRL", "acme/billing", "retry_queued", "In Progress", None),
        ("iss-133", 1, "ENG-133", "Move the email queue to SQS", "acme/web", "parked", "In Review", "alice"),
    ]
    for issue_id, seq, ident, title, repo, status, tracker, assignee in issues:
        client.set(state.issue_key(issue_id), json.dumps({
            "identifier": ident, "title": title, "target_repo": repo, "status": status,
            "tracker_state": tracker, "dispatch_seq": seq, "attempt": seq, "model": KIMI,
            "tokens": {"total": 50_000 * seq, "cost_estimate": 0.65 * seq},
            "claimed_at": now - 600_000, "updated_at": now - 30_000 * seq,
            "pr_url": "https://github.com/acme/web/pull/815" if ident == "ENG-133" else None,
            "assignee": assignee,
        }))
    sid_wait, sid_run = "iss-139:2", "iss-142:1"
    sessions.open(sid_wait, kind="implement", identifier="ENG-139", title=issues[0][3], issue_id="iss-139",
                  seq=2, repo="acme/billing", branch="symphony/eng-139-pagination", model=f"amazon-bedrock/{KIMI}",
                  state="running")
    sessions.update(sid_wait, turn=4, tokens={"input": 61200, "output": 8400, "total": 69600, "cost": 0.94},
                    last_message="I mapped the three consumers of the endpoint. The pagination format is still open.")
    sessions.append_transcript(sid_wait, [
        entry("user", "Implement the /v1/invoices pagination described in ENG-139.", 1),
        entry("assistant", "I read the ticket and the three consumers of the endpoint. Before I change the contract, "
              "I need to know which pagination the mobile app will use.", 2),
    ])
    sessions.request_input(sid_wait, "ask-1",
                           "Should /v1/invoices paginate by cursor or by offset? The mobile app still uses offset.",
                           ["Cursor, with offset for compatibility", "Offset only"])
    sessions.open(sid_run, kind="implement", identifier="ENG-142", title=issues[1][3], issue_id="iss-142", seq=1,
                  repo="acme/web", branch="symphony/eng-142-export-csv", model=f"amazon-bedrock/{KIMI}", state="running")
    sessions.update(sid_run, turn=7, tokens={"input": 162000, "output": 22100, "total": 184100, "cost": 2.41},
                    pr_url="https://github.com/acme/web/pull/821",
                    last_message="The exporter tests pass. Now covering time zones in the CSV header.")
    sessions.append_transcript(sid_run, [
        entry("user", "Implement the CSV export of the sales report (ENG-142).", 1),
        entry("assistant", "I created `SalesCsvExporter` with a fixed header and comma escaping. "
              "The exporter tests pass. Time zones are not covered yet.", 2),
    ])
    # What the runner's `symphony_attach_evidence` host tool writes on the session.
    sessions.update(sid_run, evidence=[
        {"title": "Exported report in the browser", "kind": "screenshot",
         "url": "https://symphony.example/evidence/eng-142/export.png"},
        {"title": "Exporter test output", "kind": "log",
         "url": "https://symphony.example/evidence/eng-142/pytest.txt"},
    ])
    # ENG-147 held by a person, as the daemon writes it when it applies a `hold` (internals §12).
    client.set(state.hold_key("iss-147"), json.dumps(
        state.hold_record("bob", "Taking over to fix the rounding by hand", now - 1_200_000)))
    sessions.open("iss-151:1", kind="implement", identifier="ENG-151", title=issues[2][3], issue_id="iss-151",
                  seq=1, repo="acme/web", model=f"amazon-bedrock/{KIMI}", state="starting")
    sessions.open("merger:acme:gateway:812:review:1", kind="review", identifier="PR #812",
                  title="Review: session cache in the gateway", repo="acme/gateway", model="amazon-bedrock/deepseek.v3.2",
                  state="running")
    beat_daemon(client)

    config = TeamConfig(client, NAMESPACE)
    config.publish({"models": {"oracle": {"provider": "openai", "model": "gpt-5.5", "thinking": "medium"}}},
                         expected_generation=0, actor="alice", reason="Oracle on a different provider for independence")
    for consumer, gen in (("daemon", 1), ("merger:review:acme/web", 1), ("merger:train:acme/web", 0), ("runner:ENG-142", 1),
                          ("member:bob", 0)):
        config.observe(consumer, gen)

    tokens = TokenStore(client, NAMESPACE)
    tokens.create("bob", "operator", actor="alice", expires_in_s=90 * 86400)
    tokens.create("ci-bot", "viewer", actor="alice")
    _, old = tokens.create("intern", "operator", actor="alice", expires_in_s=30 * 86400)
    tokens.revoke(old["id"], actor="alice")

    seed_merger(client, NAMESPACE)
    seed_limits_and_holds(client)
    return {"waiting": sid_wait, "running": sid_run}


def seed_merger(client: redis.Redis, namespace: str) -> None:
    """Two repositories' mergers mid-pass, written through `MergerStore` with its real states:
    reviews in each phase, a clean review parked until a person approves it, train records in
    every `REC_STATES` value, and each merger registered with a live heartbeat."""
    now = now_ms()
    web = MergerStore(client, namespace, "acme/web")
    gateway = MergerStore(client, namespace, "acme/gateway")
    for merger in (web, gateway):
        merger.register(ttl_ms=3_600_000)
    gateway.set_reviewing(812, head_sha="4be1a07c9d", worker="merger-0", phase="verify", started_at=now - 240_000,
                          author="bob", reviewers=[{"login": "alice", "state": "requested"}])
    gateway.add_session(812, "merger:acme:gateway:812:review:1")
    gateway.add_session(812, "merger:acme:gateway:812:oracle:1")
    web.set_reviewing(815, head_sha="a91c33e012", worker="merger-0", phase="review", started_at=now - 70_000,
                      author="symphony", reviewers=[{"login": "bob", "state": "requested"},
                                                    {"login": "carol", "state": "commented"}])
    web.add_session(815, "merger:acme:web:815:review:1")
    web.park({"pr": 818, "head_sha": "77d0e1f3aa", "kind": "clean", "waiting": "approval",
              "pr_title": "Show the delivery date at checkout", "title": "**Waiting for CI**",
              "author": "symphony", "reviewers": [{"login": "bob", "state": "requested"},
                                                  {"login": "carol", "state": "approved"}],
              "parked_at": now - 420_000, "comment": "https://github.com/acme/web/pull/818#issuecomment-1"})
    for merger, pr, rec_state, branch, author, ago_ms, reviewers in (
        (web, 402, "finalizing", "symphony/eng-129-stripe-v14", "symphony", 900_000, [("alice", "approved")]),
        (gateway, 806, "train", "fix/login-redirect", "bob", 1_300_000, [("carol", "approved")]),
        (web, 809, "resolve", "symphony/eng-120-remove-flag", "symphony", 1_900_000, []),
        (web, 811, "reviewed", "feat/team-invites", "carol", 2_400_000, [("alice", "approved"), ("dave", "commented")]),
        (web, 813, "publishing", "symphony/eng-133-sqs", "symphony", 60_000, [("bob", "requested")]),
    ):
        merger.put_rec({"pr": pr, "state": rec_state, "head_sha": f"{pr:06x}", "branch": branch,
                        "author": author, "reviewed_at": now - ago_ms,
                        "reviewers": [{"login": login, "state": st} for login, st in reviewers]})
    web.save_batch({"members": [{"pr": 402, "reviewed": True}]})
    gateway.save_batch({"members": [{"pr": 806, "reviewed": True}]})
    web.set_queue([
        {"pr": 821, "title": "Export the sales report as CSV", "author": "symphony", "ready_at": now - 40_000, "kind": "first",
         "reviewers": [{"login": "alice", "state": "requested"}]},
    ])
    gateway.set_queue([
        {"pr": 790, "title": "Refactor the gateway HTTP client", "author": "carol", "ready_at": now - 5_400_000,
         "kind": "rereview", "reviewers": []},
    ])
    web.acquire("train", "merger-web-0", 120_000)
    gateway.acquire("train", "merger-gateway-0", 120_000)
    # The label filter each merger publishes (`merger.label_filter`): web takes labeled PRs only,
    # gateway is turned off for its repository (`merger.label_filter.repos`), so it takes every PR.
    web.label_filter = {"active": True, "labels": ["symphony", "symphony-learnings"]}
    gateway.label_filter = {"active": False, "labels": ["symphony", "symphony-learnings"]}


def seed_limits_and_holds(client: redis.Redis) -> None:
    """One rate-limit record per stage the runner writes (usage.py: `low_priority`, `wrap_up`
    from omp's usage limit, `throttled` from a retry) and a hold/release pair accepted through
    the real command store, so the Dashboard and Audit views show those codes."""
    import uuid

    from symphony.admin_commands import AdminStore
    from symphony.usage import ratelimits_key

    now = now_ms()
    for model, doc in (
        ("anthropic/claude-haiku-5", {"stage": "low_priority", "remaining": 18, "limit": 100,
                                      "reset_at": now + 540_000, "source": "usage_limit"}),
        ("openai/gpt-5.5", {"stage": "wrap_up", "remaining": 3, "limit": 100,
                            "reset_at": now + 1_800_000, "source": "usage_limit"}),
        (f"amazon-bedrock/{KIMI}", {"stage": "throttled", "retry_after_ms": 12_000, "source": "retry"}),
    ):
        client.hset(ratelimits_key(NAMESPACE), model, json.dumps({**doc, "observed_at": now - 60_000}))
    store = AdminStore(client, NAMESPACE)
    for action, reason in (("hold", "Taking over ENG-151 to test by hand"),
                           ("release", "Manual test finished. Continue.")):
        store.accept(actor="bob", role="operator", credential_fingerprint="seed", action=action,
                     target={"issueId": "iss-151"}, reason=reason, idempotency_key=uuid.uuid4().hex)
    store.provider_audit(actor="alice", provider_id="anthropic", outcome="succeeded", operation_id=uuid.uuid4().hex)


def beat_daemon(client: redis.Redis) -> None:
    """What a live daemon keeps fresh: its heartbeat and its lease. Commands (pause, resume) are
    only admitted while both are (admin_commands.py)."""
    now = now_ms()
    client.set(state.HEARTBEAT_KEY, json.dumps(now))
    client.set(state.LOCK_KEY, json.dumps({"owner": "daemon-tern-test", "lease_expires": now + 60_000}))


class RealHarness(Harness):
    def __init__(self, *, logged_in: bool = True, size: str = "1440 960") -> None:
        super().__init__(logged_in=logged_in, size=size)
        self.home = self.tmp / "home"
        self.cli_env: dict = {}
        self.ids: dict = {}
        self._beat = threading.Event()
        self.mocks: list[subprocess.Popen] = []
        self.secrets_file = self.tmp / "secrets.env"

    def _wait_port(self, port: int, proc: subprocess.Popen, log: Path, what: str) -> None:
        deadline = time.monotonic() + 20
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise TernError(f"{what} did not start:\n" + log.read_text()) from None
                time.sleep(0.05)

    def start_linear_mock(self) -> str:
        """The Linear vendor mock (scripts/mocks/linear, from Linear's schema): team ENG and the
        one API key it accepts, LINEAR_MOCK_KEY. Returns its GraphQL URL."""
        port = free_port()
        log = self.tmp / "linear-mock.log"
        proc = subprocess.Popen(
            [str(REPO / ".venv" / "bin" / "python"), "-m", "scripts.mocks.linear", "--port", str(port),
             "--state", str(self.tmp / "linear-mock"), "--team", "ENG:Engineering", "--api-key", LINEAR_MOCK_KEY],
            stdout=log.open("w"), stderr=subprocess.STDOUT, cwd=str(REPO),
        )
        self.mocks.append(proc)
        self._wait_port(port, proc, log, "the Linear mock")
        return f"http://127.0.0.1:{port}/graphql"

    def start_mcp_mock(self) -> str:
        """The MCP mock (scripts/mocks/mcp, Streamable HTTP): it answers only with the bearer
        token MCP_MOCK_TOKEN, so a passing test proves the stored header reached it."""
        port = free_port()
        log = self.tmp / "mcp-mock.log"
        proc = subprocess.Popen(
            [str(REPO / ".venv" / "bin" / "python"), "-m", "scripts.mocks.mcp", "--port", str(port)],
            stdout=log.open("w"), stderr=subprocess.STDOUT, cwd=str(REPO),
            env=dict(os.environ, MCP_MOCK_TOKEN=MCP_MOCK_TOKEN),
        )
        self.mocks.append(proc)
        self._wait_port(port, proc, log, "the MCP mock")
        self.mcp_mock = f"http://127.0.0.1:{port}"
        return f"{self.mcp_mock}/mcp"

    def start_backend(self, env: dict) -> None:
        redis_url = self.start_redis()
        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self.ids = seed(self.redis)
        threading.Thread(target=self._heartbeat, daemon=True).start()
        aport = free_port()
        self.mcp_url = self.start_mcp_mock()
        api_env = dict(os.environ, REDIS_URL=redis_url, POD_NAMESPACE=NAMESPACE,
                       SYMPHONY_BOOTSTRAP_TOKEN=BOOTSTRAP, LINEAR_API_URL=self.start_linear_mock(),
                       SYMPHONY_TERN_SECRETS=str(self.secrets_file),
                       # MCP servers in a local 0600 file; the mock serves plain http on loopback.
                       SYMPHONY_TERN_MCP=str(self.tmp / "mcp.json"), SYMPHONY_MCP_ALLOW_HTTP_HOSTS="127.0.0.1",
                       # The image ships the repo's WORKFLOW.md as the default (orchestrator.Dockerfile).
                       SYMPHONY_DEFAULT_WORKFLOW=str(REPO / "WORKFLOW.md"))
        if OMP_CATALOG.exists():
            # The API reads the provider rules next to models.json, as the image installs them.
            staged = self.tmp / "omp-catalog"
            staged.mkdir(exist_ok=True)
            (staged / "models.json").write_bytes(OMP_CATALOG.read_bytes())
            (staged / "rules.json").write_bytes((OMP_CATALOG.parent / "compat" / "rules.json").read_bytes())
            api_env["SYMPHONY_OMP_CATALOG"] = str(staged / "models.json")
        self.api_log = self.tmp / "api.log"
        self.api = subprocess.Popen(
            [str(REPO / ".venv" / "bin" / "python"), str(HERE / "real_api.py"), str(aport)],
            stdout=self.api_log.open("w"), stderr=subprocess.STDOUT, env=api_env, cwd=str(REPO),
        )
        self.url = f"http://127.0.0.1:{aport}"
        self._wait_port(aport, self.api, self.api_log, "API")
        self.home.mkdir()
        venv_bin = REPO / ".venv" / "bin"
        env.pop("VIRTUAL_ENV", None)
        env.update(
            PATH=f"{venv_bin}:{env.get('PATH', '')}",
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.home / ".config"),
            # No keychain: the CLI keeps the token in its 0600 file under the isolated HOME.
            PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
            SYMPHONY_BIN=str(venv_bin / "symphony"),
        )
        self.cli_env = dict(env)
        if self.logged_in:
            self.login()
            # The ACP agent and MCP entries, as a person gets them (onboarding runs it itself).
            self.symphony("tern", "install", "--no-plugin", "--json")

    def _heartbeat(self) -> None:
        while not self._beat.wait(5):
            try:
                beat_daemon(self.redis)
            except redis.RedisError:
                return

    def symphony(self, *args: str, stdin: str | None = None) -> dict:
        proc = subprocess.run(
            [self.cli_env["SYMPHONY_BIN"], *args], input=stdin, capture_output=True, text=True,
            env=self.cli_env, timeout=60,
        )
        if proc.returncode != 0:
            raise TernError(f"symphony {' '.join(args)}: {proc.stdout}{proc.stderr}")
        return json.loads(proc.stdout or "{}")

    def login(self) -> None:
        self.symphony("login", "--url", self.url, "--token", "-", "--json", stdin=BOOTSTRAP + "\n")

    def api_get(self, path: str) -> dict:
        return self.symphony("api", "GET", path, "--json")

    def wait_until(self, check, timeout: float = 10, what: str = "condition"):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = check()
            if last:
                return last
            time.sleep(0.1)
        raise TernError(f"{what} never held; last: {last}")

    def leave_setup(self) -> None:
        """The backend starts unconfigured (only Redis and the API), so the plugin opens the
        Setup view for this admin by itself, once per panel; go back to the panel."""
        self.wait_text("Verify and save", timeout=20)
        self.run("plugin.symphony.dashboard")
        self.wait_gone("Verify and save")

    def stop(self) -> None:
        self._beat.set()
        for proc in self.mocks:
            proc.terminate()
            proc.wait(5)
        super().stop()
