"""Mocked certification: Symphony end to end against the doc-faithful vendor mocks.

    uv run python -m scripts.e2e_mocked.run [--scenario N ...] [--keep] [--out FILE]

Starts, all on 127.0.0.1 and free ports: a disposable Redis (unix socket); the Linear mock
(webhook → Symphony `/v1/webhooks/linear`, shared secret); the GitHub mock (an App with a key
generated here, installed on `acme/app` and `acme/lib` seeded from `seed/`; webhook → Symphony
`/v1/webhooks/github`); the Slack mock; Symphony's local mode (`python -m symphony.entry.local`,
`tracker.kind: linear`) and, from the first scenario that needs it on, the merger
(`python -m symphony.entry.merger`) for `acme/app`. Both get the GitHub App, Slack bot and
endpoint variables (internals §14). Agents are the real omp (pinned: `SYMPHONY_OMP_BIN`, default
the Tern-managed 18.20.2) on Bedrock (`AWS_REGION` + `AWS_BEARER_TOKEN_BEDROCK` from the env).

Scenarios (each asserts on mock state, never on logs alone; `--scenario` takes their numbers):

1. implement → merge: Linear ticket → webhook → dispatch → App token (repositories and
   permissions checked) → clone and push over git smart HTTP → ready PR with `symphony`,
   ticket In Review, Workpad, a `symphony_ask` answered from the API (Slack notice) → CI green →
   merger review + rebase merge → Linear Merging → Done (the Linear↔GitHub integration, played
   through `/_mock`);
2. multi-repo: one ticket, `repo:acme/app` + `repo:acme/lib` → a token for both, one PR each;
3. investigation: read-only token, a push with it refused by the mock, report comment, Done,
   no PR;
4. sub-issues + evidence: `symphony_create_issues` children (parent, `blocks`) and
   `symphony_attach_evidence` (upload bytes and attachment);
5. review comment answer: a person's inline comment → webhook → answer run → reply from the App;
6. learning PR: the retrospective of scenario 1's ticket opens a learnings PR;
7. main red → one Linear ticket with routing labels and the log → green closes it;
8. rejection: a PR with a real bug goes back (draft, Rework, report).

5 and 6 build on 1, which runs first when they are selected.

Assistant scenarios (`scripts/e2e_mocked/assistant.py`; `--assistant` selects those of the
tracker, `--scenario` names them too; the default stays 1-8): the assistant
(`python -m symphony.entry.assistant`) runs next to the local mode, with Slack Socket Mode and
the Discord Gateway pointed at the mocks.

9. ask on Slack: ack < 3 s, "Working on it…" then updated with the answer and a code link, an
   `assist` session, a follow-up in the same session;
10. plan + confirm on Slack: buttons, an unmapped click refused, a mapped click creating the
    tree once in Linear, the message edited, the audit entry;
11. 9 and 10 on the Discord Gateway with `--tracker github` (issues on the board);
12. MCP through the gateway: an implementer and the assistant call the mock MCP server with the
    stored headers; the repository's own `.omp/mcp.json` is ignored.
14. soul and memory on Slack: a persona rule seeded in `acme/app:.symphony/assistant.md` is
    followed and its blob sha is in the heartbeat; "remember …" is kept and used in a new
    thread; "forget …" removes it; a secret and an unmapped person get no memory; a team fact
    PR opens on the GitHub mock after a mapped click (`assistant.team_fact_pr`).

13. Seiji through the gateway (`scripts/e2e_mocked/seiji.py`, Linear only, by `--scenario 13`):
    an implementer acquires an iPhone from the Seiji mock, uploads a build through `$SEIJI_URL`,
    puts `export_evidence` in the PR body and attaches `report.md`; it keeps the lease, and the
    session cleanup releases it. omp runs behind a wrapper that records only the SHA-256 and
    prefix of `SEIJI_TOKEN`.

With assistant scenarios and scenario 13 every process gets a private HOME, so a developer's own omp config
(`~/.omp/agent/mcp.json`) cannot leak in.

Prints (and with `--out` writes) one JSON summary: per scenario its checks, timings and
mock-state excerpts. No credential value is printed. `--keep` keeps the temporary directory
(logs, mock state, workspaces).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx
import redis

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_OMP = Path.home() / "Library/Application Support/Tern/omp/versions/18.20.2"
NAMESPACE = "e2e-mocked"
TEAM = "ENG"
APP, LIB = "acme/app", "acme/lib"
PERSON = "octocat"
SLACK_TOKEN, SLACK_CHANNEL = "xoxb-mock-symphony", "C0SYMPHONY"
DISCORD_TOKEN, DISCORD_CHANNEL = "mock-discord-bot-token", "1100000000000000001"
DISCORD_PERSON = "1500000000000000001"  # octocat in WORKFLOW.md.tmpl `notify.users.discord`
# The App's installation: what Symphony asks for anywhere — the workspace token (ghapp.WORKSPACE),
# the merger (actions: write to rerun and cancel, issues: write for labels and comments) and the
# GitHub tracker (issues, organization projects).
APP_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read", "checks": "read",
                   "actions": "write", "statuses": "read", "issues": "write", "organization_projects": "write"}
READ_ONLY = {"contents": "read", "pull_requests": "read", "metadata": "read"}
MERGER_LABELS = ("symphony", "reviewed", "wait-ci", "train-ci", "review-requested", "in-audit",
                 "review-bypass", "needs-maintainer", "symphony-learnings", "investigation", "ci")
EVIDENCE_BYTES = b"symphony evidence: pytest passed\n"
ALL = ("1", "2", "3", "4", "5", "6", "7", "8")
NEEDS_1 = {"5", "6"}
GITHUB_SCENARIOS = ("1", "3", "4", "7")  # `--tracker github` (scripts/e2e_mocked/github_tracker.py)
AGENT_TIMEOUT_S = float(os.environ.get("E2E_AGENT_TIMEOUT_S", "1200"))


class Failure(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def until(predicate, timeout: float, what: str, interval: float = 0.5):
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


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def first_added_line(patch: str) -> int | None:
    """The new-side line number of the first added line of a unified diff patch."""
    line = None
    for row in patch.splitlines():
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
        if m:
            line = int(m.group(1))
            continue
        if line is None:
            continue
        if row.startswith("+"):
            return line
        if not row.startswith("-"):
            line += 1
    return None


class Scenario:
    """One scenario's evidence: checks (assertion, ok, excerpt) and timings."""

    def __init__(self, key: str, title: str, features: str) -> None:
        self.key, self.title, self.features = key, title, features
        self.checks: list[dict] = []
        self.timings: dict[str, float] = {}
        self.started = time.monotonic()
        self.error: str | None = None

    def check(self, name: str, ok: bool, excerpt=None, *, fatal: bool = True) -> bool:
        self.checks.append({"assertion": name, "ok": bool(ok), "excerpt": excerpt})
        mark = "ok " if ok else "FAIL"
        print(f"  [{mark}] {self.key}: {name}", flush=True)
        if not ok and fatal:
            raise Failure(f"{name}: {json.dumps(excerpt, ensure_ascii=False)[:600]}")
        return bool(ok)

    def time(self, name: str, seconds: float) -> None:
        self.timings[name] = round(seconds, 2)
        print(f"  [time] {self.key}: {name} = {seconds:.2f}s", flush=True)

    @property
    def ok(self) -> bool:
        return self.error is None and all(c["ok"] for c in self.checks)

    def summary(self) -> dict:
        return {"scenario": self.key, "title": self.title, "features": self.features, "ok": self.ok,
                "error": self.error, "checks": self.checks, "timings_s": self.timings,
                "elapsed_s": round(time.monotonic() - self.started, 1)}


class Stack:
    def __init__(self, tmp: Path, omp: Path, provider: str, model: str, thinking: str,
                 max_turns: int, tracker: str = "linear", notify: str = "slack",
                 assistant: bool = False, private_home: bool = False, env_probe: bool = False) -> None:
        self.tmp = tmp
        self.omp, self.provider, self.model, self.thinking = omp, provider, model, thinking
        self.max_turns = max_turns
        self.tracker, self.notify, self.assistant = tracker, notify, assistant
        self.private_home = private_home or assistant
        self.env_probe = env_probe
        self.project_number: int | None = None
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.merger: subprocess.Popen | None = None
        self.ports = {name: free_port() for name in ("linear", "github", "slack", "discord", "api",
                                                      "gateway", "merger_health", "mcp", "mcp_project",
                                                      "assistant_health", "seiji")}
        self.discord_url = f"http://127.0.0.1:{self.ports['discord']}"
        self.linear_url = f"http://127.0.0.1:{self.ports['linear']}"
        self.github_url = f"http://127.0.0.1:{self.ports['github']}"
        self.slack_url = f"http://127.0.0.1:{self.ports['slack']}"
        self.api_url = f"http://127.0.0.1:{self.ports['api']}"
        self.admin_token = f"sym_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"
        self.linear_secret = secrets.token_hex(16)
        self.github_secret = secrets.token_hex(16)
        self.http = httpx.Client(timeout=30)
        self.issues: dict[str, dict] = {}  # scenario key → the Linear issue view at creation
        self.prs: dict[str, dict] = {}

    # -- processes -----------------------------------------------------------------
    def spawn(self, name: str, argv: list[str], env: dict | None = None) -> subprocess.Popen:
        log = self.tmp / "logs" / f"{name}.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        proc = subprocess.Popen(argv, env=env, cwd=str(REPO), stdout=fd, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)
        os.close(fd)
        self.procs.append((name, proc))
        return proc

    def log_text(self, name: str) -> str:
        path = self.tmp / "logs" / f"{name}.log"
        return path.read_text(errors="replace") if path.exists() else ""

    def stop(self, proc: subprocess.Popen, wait_s: float = 40) -> None:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(wait_s)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self) -> None:
        for _, proc in reversed(self.procs):
            self.stop(proc)

    # -- mocks -----------------------------------------------------------------------
    def lin(self, method: str, route: str, body: dict | None = None):
        r = self.http.request(method, f"{self.linear_url}/_mock/{route}", json=body)
        if r.status_code >= 400:
            raise Failure(f"linear mock {method} {route}: {r.status_code} {r.text[:300]}")
        return r.json()

    def gh(self, method: str, route: str, body: dict | None = None, params: dict | None = None):
        r = self.http.request(method, f"{self.github_url}/_mock/{route}", json=body, params=params)
        if r.status_code >= 400:
            raise Failure(f"github mock {method} {route}: {r.status_code} {r.text[:300]}")
        return r.json()

    def rest(self, method: str, path: str, body: dict | None = None, token: str | None = None,
             accept: str = "application/vnd.github+json") -> httpx.Response:
        return self.http.request(method, f"{self.github_url}{path}", json=body, headers={
            "Authorization": f"Bearer {token or self.person_token}", "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28"})

    def notices(self, event: str, needle: str = "") -> list[dict]:
        """Messages of one `notify` event on the configured channel (Slack or Discord): the
        event's emoji and title lead the text. Each item carries `text` (Discord's `content`)."""
        from symphony.notify import DISCORD_EMOJI, EVENTS

        emoji, title = EVENTS[event]
        if self.notify == "slack":
            body = self.http.get(f"{self.slack_url}/_mock/messages").json()
            msgs = body.get("messages", body) if isinstance(body, dict) else body
            head = f"{emoji} *{title}*"
        else:
            body = self.http.get(f"{self.discord_url}/_mock/messages").json()
            msgs = [{**m, "text": m.get("content") or ""}
                    for m in (body.get("messages", body) if isinstance(body, dict) else body)]
            head = f"{DISCORD_EMOJI[event]} **{title}**"
        return [m for m in msgs if (m.get("text") or "").startswith(head) and needle in m["text"]]

    @property
    def mention(self) -> str:
        return "<@U0OCTOCAT>" if self.notify == "slack" else f"<@{DISCORD_PERSON}>"

    def repo_state(self, repo: str) -> dict:
        return self.gh("GET", "state", params={"repo": repo})

    def issue(self, ref: str) -> dict:
        return self.lin("GET", f"issues/{ref}")

    def gh_tokens(self) -> dict:
        """Minted tokens as the mock recorded them (its persisted state)."""
        return json.loads((self.tmp / "github" / "state.json").read_text())["tokens"]

    # -- symphony API ----------------------------------------------------------------
    def api(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        return self.http.request(method, f"{self.api_url}{path}", json=body,
                                 headers={"Authorization": f"Bearer {self.admin_token}"})

    def sessions(self) -> list[dict]:
        r = self.api("GET", "/v1/sessions?limit=200")
        return r.json().get("items", []) if r.status_code == 200 else []

    def session_for(self, identifier: str, kind: str = "implement", after_ms: int = 0) -> dict | None:
        found = [m for m in self.sessions() if m.get("identifier") == identifier
                 and m.get("kind") == kind and int(m.get("created_at") or 0) >= after_ms]
        return min(found, key=lambda m: m["created_at"]) if found else None

    def command(self, action: str, reason: str) -> None:
        r = self.api("POST", "/v1/commands", {"action": action, "reason": reason})
        if r.status_code != 202:
            raise Failure(f"command {action}: {r.status_code} {r.text[:300]}")
        want = action == "pause"
        until(lambda: self.api("GET", "/v1/overview").json()["daemon"].get("paused") is want, 30,
              f"the daemon to {action}")

    # -- git as a person ---------------------------------------------------------------
    def git_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0",
                   GIT_AUTHOR_NAME="Octo Cat", GIT_AUTHOR_EMAIL="octocat@example.invalid",
                   GIT_COMMITTER_NAME="Octo Cat", GIT_COMMITTER_EMAIL="octocat@example.invalid")
        return env

    def git(self, *args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run(["git", *args], cwd=str(cwd), env=self.git_env(), capture_output=True,
                              text=True, timeout=120, check=False)
        if check and done.returncode != 0:
            raise Failure(f"git {args[0]}: {done.stderr[-500:]}")
        return done

    def clone_as(self, repo: str, token: str, user: str = "x-access-token") -> Path:
        dest = Path(tempfile.mkdtemp(prefix="clone-", dir=self.tmp / "clones"))
        url = self.github_url.replace("http://", f"http://{user}:{token}@") + f"/{repo}.git"
        self.git("clone", "-q", url, str(dest), cwd=self.tmp)
        return dest

    def push_main_commit(self, message: str) -> str:
        clone = self.clone_as(APP, self.person_token, PERSON)
        notes = clone / "NOTES.md"
        notes.write_text((notes.read_text() if notes.exists() else "") + f"- {message}\n")
        self.git("add", "NOTES.md", cwd=clone)
        self.git("commit", "-q", "-m", message, cwd=clone)
        self.git("push", "-q", "origin", "HEAD:main", cwd=clone)
        return self.git("rev-parse", "HEAD", cwd=clone).stdout.strip()

    # -- setup -------------------------------------------------------------------------
    def common_env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("SYMPHONY_URL", "SYMPHONY_TOKEN", "SYMPHONY_GATEWAY_URL",
                            "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT")}
        env.update(
            REDIS_URL=f"unix://{self.tmp / 'redis.sock'}?db=0",
            POD_NAMESPACE=NAMESPACE,
            PYTHONUNBUFFERED="1",
            KUBECONFIG=str(self.tmp / "no-kubeconfig"),
            SYMPHONY_WORKFLOW_MOUNT=str(self.tmp / "workflow"),
            SYMPHONY_BOOTSTRAP_TOKEN=self.admin_token,
            LINEAR_API_KEY=self.linear_key,
            LINEAR_WEBHOOK_SECRET=self.linear_secret,
            GITHUB_WEBHOOK_SECRET=self.github_secret,
            GITHUB_APP_ID=self.app_env["GITHUB_APP_ID"],
            GITHUB_APP_INSTALLATION_ID=str(self.app_env["GITHUB_APP_INSTALLATION_ID"]),
            GITHUB_APP_PRIVATE_KEY=self.private_key,
            **({"SLACK_BOT_TOKEN": SLACK_TOKEN, "SLACK_CHANNEL": SLACK_CHANNEL} if self.notify == "slack" else {
                "DISCORD_BOT_TOKEN": DISCORD_TOKEN, "DISCORD_CHANNEL_ID": DISCORD_CHANNEL,
                "DISCORD_API_URL": f"{self.discord_url}/api/v10"}),
            LINEAR_API_URL=f"{self.linear_url}/graphql",
            GITHUB_API_URL=self.github_url,
            GITHUB_GRAPHQL_URL=f"{self.github_url}/graphql",
            GITHUB_SERVER_URL=self.github_url,
            SLACK_API_URL=f"{self.slack_url}/api",
            SYMPHONY_OMP_BIN=str(self.omp_link),
            SYMPHONY_MCP_ALLOW_HTTP_HOSTS="127.0.0.1",
        )
        if self.private_home:
            env["HOME"] = str(self.tmp / "home")
        return env

    def start(self) -> None:
        for sub in ("logs", "linear", "github", "slack", "discord", "root", "workflow", "bin", "clones",
                    "merger", "home"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        py = sys.executable

        sock = self.tmp / "redis.sock"
        self.spawn("redis", ["redis-server", "--port", "0", "--unixsocket", str(sock), "--save", "",
                             "--appendonly", "no", "--dir", str(self.tmp)])
        self.redis = redis.Redis(unix_socket_path=str(sock), decode_responses=True)
        until(lambda: sock.exists() and self.redis.ping(), 10, "redis")

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()).decode()
        private_file(self.tmp / "app-key.pem", self.private_key)

        from scripts.mocks.linear.store import DEFAULT_API_KEY

        self.linear_key = DEFAULT_API_KEY
        self.spawn("mock-linear", [py, "-m", "scripts.mocks.linear", "--port", str(self.ports["linear"]),
                                   "--state", str(self.tmp / "linear"), "--team", f"{TEAM}:Engineering",
                                   "--webhook-url", f"{self.api_url}/v1/webhooks/linear",
                                   "--webhook-secret", self.linear_secret])
        self.spawn("mock-github", [py, "-m", "scripts.mocks.github", "--port", str(self.ports["github"]),
                                   "--state", str(self.tmp / "github"), "--app-key", str(self.tmp / "app-key.pem"),
                                   "--webhook-url", f"{self.api_url}/v1/webhooks/github",
                                   "--webhook-secret", self.github_secret])
        self.spawn("mock-slack", [py, "-m", "scripts.mocks.slack", "--port", str(self.ports["slack"]),
                                  "--state", str(self.tmp / "slack")])
        self.spawn("mock-discord", [py, "-m", "scripts.mocks.discord", "--port", str(self.ports["discord"]),
                                    "--state", str(self.tmp / "discord")])
        for url in (f"{self.linear_url}/_mock/health", f"{self.github_url}/_mock/health",
                    f"{self.slack_url}/_mock/state", f"{self.discord_url}/_mock/state"):
            until(lambda u=url: self._healthy(u), 30, url)

        # GitHub: a person, the two repositories, the App installed on both.
        self.person_token = self.gh("POST", "users", {"login": PERSON, "name": "Octo Cat"})["token"]
        for repo, seed in ((APP, "app"), (LIB, "lib")):
            owner, name = repo.split("/")
            self.gh("POST", "repos", {"owner": owner, "name": name, "seed": str(HERE / "seed" / seed),
                                      "labels": list(MERGER_LABELS)})
        self.gh("POST", "installations", {"account": "acme", "repositories": ["app", "lib"],
                                          "permissions": APP_PERMISSIONS})
        self.app_env = self.gh("GET", "app")["env"]
        self.app_slug = self.gh("GET", "app")["slug"]
        self.bot_login = f"{self.app_slug}[bot]"
        if self.tracker == "github":
            # The board: GitHub's default Status options include every state the workflow names.
            self.project_number = self.gh("POST", "projects", {"owner": "acme", "title": "Symphony"})["number"]

        # Linear: routing labels; who the API key is.
        for name in (f"repo:{APP}", f"repo:{LIB}"):
            self.lin("POST", "labels", {"name": name, "team": TEAM})
        data = self.lin("GET", "state")
        bot_id = data["botId"]
        users = data["tables"]["User"] if "tables" in data else data.get("User", {})
        bot = users.get(bot_id) if isinstance(users, dict) else None
        self.linear_bot_email = (bot or {}).get("email")

        # The pinned omp through a path without spaces (engine.command is a word list).
        self.omp_link = self.tmp / "bin" / "omp"
        if self.env_probe:
            from scripts.e2e_mocked.seiji import ENV_LOG, omp_wrapper

            private_file(self.omp_link, omp_wrapper(str(self.omp), str(self.tmp / ENV_LOG)))
            os.chmod(self.omp_link, 0o700)
        else:
            self.omp_link.symlink_to(self.omp)
        self.omp_version = subprocess.run([str(self.omp_link), "--version"], capture_output=True, text=True,
                                          timeout=30, check=False).stdout.strip()
        workflow = (HERE / "WORKFLOW.md.tmpl").read_text()
        if self.tracker == "github":
            provider = (f"  kind: github\n  provider:\n    owner: acme\n    project_number: {self.project_number}\n"
                        "    status_field: Status")
            terminal = "Done, Canceled"
        else:
            provider = f'  kind: linear\n  provider:\n    team_key: "{TEAM}"\n    api_key: $LINEAR_API_KEY'
            terminal = "Done, Canceled, Duplicate"
        assistant = ""
        if self.assistant:
            from scripts.e2e_mocked.assistant import workflow_section

            assistant = workflow_section(self.tracker)
        for key, value in {"@OMP@": str(self.omp_link), "@PROVIDER@": self.provider, "@MODEL@": self.model,
                           "@THINKING@": self.thinking, "@MAX_TURNS@": str(self.max_turns),
                           "@TRACKER_PROVIDER@": provider, "@TERMINAL@": terminal,
                           "@ASSISTANT@\n": assistant}.items():
            workflow = workflow.replace(key, value)
        (self.tmp / "workflow" / "WORKFLOW.md").write_text(workflow)

        env = self.common_env()
        stack = self.spawn("symphony-local", [
            py, "-m", "symphony.entry.local", "--root", str(self.tmp / "root"),
            "--workflow", str(self.tmp / "workflow" / "WORKFLOW.md"), "--port", str(self.ports["api"]),
            "--gateway-port", str(self.ports["gateway"])], env=env)
        until(lambda: stack.poll() is not None or "Symphony local at" in self.log_text("symphony-local"),
              60, "the local stack")
        if stack.poll() is not None:
            raise Failure("the local stack exited:\n" + self.log_text("symphony-local")[-3000:])
        until(lambda: self.api("GET", "/v1/me").status_code == 200, 20, "the API")

    @staticmethod
    def _healthy(url: str) -> bool:
        try:
            return httpx.get(url, timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def start_merger(self) -> None:
        if self.merger is not None and self.merger.poll() is None:
            return
        env = self.common_env()
        env.update(SYMPHONY_MERGER_REPO=APP, SYMPHONY_MERGER_WORKDIR=str(self.tmp / "merger"),
                   SYMPHONY_MERGER_REVIEWERS="1", SYMPHONY_HEALTH_PORT=str(self.ports["merger_health"]))
        self.merger = self.spawn("merger", [sys.executable, "-m", "symphony.entry.merger"], env=env)
        until(lambda: self.merger.poll() is not None or "merger_started" in self.log_text("merger"), 90,
              "the merger")
        if self.merger.poll() is not None:
            raise Failure("the merger exited:\n" + self.log_text("merger")[-3000:])

    # -- shared waits -------------------------------------------------------------------
    def create_ticket(self, title: str, description: str, labels: list[str], state: str = "Todo") -> dict:
        issue = self.lin("POST", "issues", {"title": title, "team": TEAM, "state": state,
                                            "labels": labels, "description": description})
        issue["created_ms"] = now_ms()
        return issue

    def wait_session(self, sc: Scenario, issue: dict, kind: str = "implement", after_ms: int | None = None,
                     timeout: float = 90) -> dict:
        after = issue["created_ms"] if after_ms is None else after_ms
        meta = until(lambda: self.session_for(issue["identifier"], kind, after), timeout,
                     f"a {kind} session for {issue['identifier']}")
        sc.time(f"{'ticket created' if after_ms is None else 'trigger'} → {kind} session (dispatch)",
                (meta["created_at"] - after) / 1000)
        return meta

    def wait_credential(self, sc: Scenario, identifier: str, timeout: float = 120) -> tuple[str, dict]:
        """The attempt's token, read from its credential file while it runs, and the mock's record
        of that token (what the runner asked for at `POST /app/installations/{id}/access_tokens`)."""
        from symphony.workspace import sandbox_name

        path = self.tmp / "root" / "credentials" / sandbox_name(identifier) / "gh-token"
        token = until(lambda: path.exists() and path.read_text().strip(), timeout,
                      f"the credential file of {identifier}")
        record = until(lambda: self.gh_tokens().get(token), 10, "the token in the mock's state")
        helper = path.parent / "git-credential-symphony"
        sc.check("the token file and its helper are private (0600 / 0700)",
                 oct(path.stat().st_mode & 0o777) == "0o600" and oct(helper.stat().st_mode & 0o777) == "0o700",
                 {"token": oct(path.stat().st_mode & 0o777), "helper": oct(helper.stat().st_mode & 0o777)})
        return token, record

    def wait_session_end(self, sid: str, timeout: float = AGENT_TIMEOUT_S) -> dict:
        def ended():
            r = self.api("GET", f"/v1/sessions/{sid}")
            return r.status_code == 200 and r.json()["meta"].get("state") == "ended" and r.json()["meta"]

        return until(ended, timeout, f"session {sid} to end", interval=2)

    def pulls(self, repo: str, identifier: str) -> list[dict]:
        ident = identifier.lower()
        return [p for p in self.repo_state(repo)["pulls"]
                if ident in p["title"].lower() or ident in p["head_ref"].lower()]

    def ci_of(self, repo: str, sha: str) -> list[dict]:
        return [r for r in self.repo_state(repo)["workflow_runs"] if r["head_sha"] == sha]

    def git_requests(self, repo: str, since: float) -> list[dict]:
        return [r for r in self.gh("GET", "requests") if r["at"] >= since and f"/{repo}.git/" in r["path"]]


# ======================================================================================
# scenarios
# ======================================================================================
class Run:
    def __init__(self, stack: Stack) -> None:
        self.s = stack
        self.results: dict[str, Scenario] = {}
        # Run after each scenario, passed or failed, before admission reopens: what the
        # scenario created must not become work for the daemon.
        self.cleanups: list = []

    def cancel_open(self, identifiers) -> None:
        for ident in identifiers:
            if self.s.issue(ident)["stateType"] not in ("completed", "canceled"):
                self.s.lin("POST", f"issues/{ident}/state", {"state": "Canceled"})

    # -- 1a: implement ----------------------------------------------------------------
    def s1_implement(self) -> None:
        s = self.s
        sc = Scenario("1", "Implement → PR → review and merge → Merging → Done",
                      "A1, A7, A8, B10, B11, B14, B15, O3, X1, X3, X4, R1, R2, R4, R5, R7, R12, T1, T3")
        self.results["1"] = sc
        t0 = time.monotonic()
        since = time.time()
        issue = s.create_ticket(
            "Add farewell() to app/greet.py",
            "Add a function `farewell(name: str) -> str` to `app/greet.py`, next to `greet`, and a "
            "pytest test for it in `tests/test_greet.py`.\n\n"
            "Before you write any code, ask with `symphony_ask` which word the farewell starts with, "
            "offering exactly the options `Goodbye` and `Farewell`, and use the answer: "
            "`farewell(\"Ana\")` returns `\"<Word>, Ana!\"`.\n\n"
            "Acceptance: `python -m pytest -q` passes and the new test covers `farewell`.",
            ["symphony", f"repo:{APP}"])
        s.issues["1"] = issue
        sc.check("ticket created in mock Linear (Todo, symphony + repo:acme/app)",
                 issue["state"] == "Todo" and set(issue["labels"]) == {"symphony", f"repo:{APP}"},
                 {k: issue[k] for k in ("identifier", "state", "labels")})
        meta = s.wait_session(sc, issue)
        sid = meta["sid"]
        hooks = [d for d in s.lin("GET", "webhooks")["deliveries"]
                 if (d.get("payload") or {}).get("data", {}).get("id") == issue["id"]]
        sc.check("Linear webhook delivered to /v1/webhooks/linear, signature accepted (202)",
                 bool(hooks) and hooks[0]["status"] == 202,
                 [{k: d.get(k) for k in ("type", "action", "status", "sentAt")} for d in hooks[:2]])
        sc.check("dispatch < 3 s after the ticket (internals §6, webhook path)",
                 (meta["created_at"] - issue["created_ms"]) < 3000, {"ms": meta["created_at"] - issue["created_ms"]},
                 fatal=False)
        from symphony.ghapp import WORKSPACE

        token, record = s.wait_credential(sc, issue["identifier"])
        sc.check("runner minted an App token scoped to repositories [acme/app] with exactly ghapp.WORKSPACE",
                 record["repos"] == [APP] and record["permissions"] == WORKSPACE and record["kind"] == "installation",
                 {k: record[k] for k in ("repos", "permissions", "login")})

        # The agent asks; Slack says so; the answer goes through the API.
        def asked():
            msgs = s.notices("input_requested", issue["identifier"])
            return msgs or (s.pulls(APP, issue["identifier"]) and "no-ask")

        asked_msgs = until(asked, AGENT_TIMEOUT_S, "the agent's symphony_ask (or a PR)", interval=1)
        if asked_msgs == "no-ask":
            sc.check("agent asked with symphony_ask (Slack input_requested)", False, "PR opened without asking",
                     fatal=False)
        else:
            sc.check(f"{s.notify} got the input_requested message",
                     True, {k: asked_msgs[0].get(k) for k in ("channel", "text")})
            pending = until(lambda: (s.api("GET", f"/v1/sessions/{sid}").json()["meta"].get("input_request")),
                            30, "the session's pending input request")
            t_answer = time.monotonic()
            r = s.api("POST", f"/v1/sessions/{sid}/messages", {"text": "Goodbye", "mode": "auto"})
            sc.check("answer accepted by POST /v1/sessions/{sid}/messages (auto → answer)", r.status_code == 202,
                     {"status": r.status_code, "question": (pending or {}).get("question")})
            until(lambda: not s.api("GET", f"/v1/sessions/{sid}").json()["meta"].get("input_request"), 30,
                  "the answer to be applied")
            sc.time("answer posted → input resolved", time.monotonic() - t_answer)

        pr = until(lambda: next((p for p in s.pulls(APP, issue["identifier"]) if p["state"] == "open"), None),
                   AGENT_TIMEOUT_S, "the implementer's PR", interval=3)
        sc.time("ticket created → PR opened", time.monotonic() - t0)
        def handed_over():
            p = next((x for x in s.pulls(APP, issue["identifier"]) if x["number"] == pr["number"]), None)
            i = s.issue(issue["identifier"])
            return (p and not p["draft"] and "symphony" in p["labels"] and i["state"] == "In Review") and (p, i)

        pr, lin = until(handed_over, AGENT_TIMEOUT_S, "a ready, labelled PR and the ticket In Review", interval=3)
        s.prs["1"] = pr
        sc.time("ticket created → In Review", time.monotonic() - t0)
        sc.check("PR opened by the App, ready (not draft), label `symphony`",
                 pr["user"] == s.bot_login and not pr["draft"] and "symphony" in pr["labels"],
                 {k: pr[k] for k in ("number", "title", "user", "draft", "labels", "head_ref")})
        reqs = s.git_requests(APP, since)
        sc.check("clone and push went over git smart HTTP (upload-pack and receive-pack, 200)",
                 any(r["path"].endswith("git-upload-pack") and r["status"] == 200 for r in reqs)
                 and any(r["path"].endswith("git-receive-pack") and r["status"] == 200 for r in reqs),
                 sorted({f"{r['method']} {r['path'].split('?')[0]} {r['status']}" for r in reqs}))
        workpad = [c for c in lin["comments"] if c["body"].lstrip().startswith("## Workpad")]
        sc.check("Workpad comment written through linear_graphql (by the API key's user)",
                 bool(workpad) and workpad[0]["user"] == s.linear_bot_email,
                 {"user": workpad[0]["user"], "body": workpad[0]["body"][:200]} if workpad else lin["comments"])
        sc.check("ticket moved to In Review", lin["state"] == "In Review", {"state": lin["state"]})
        diff = s.rest("GET", f"/repos/{APP}/pulls/{pr['number']}", accept="application/vnd.github.diff").text
        sc.check("the diff adds farewell() with the answered word and a test",
                 "def farewell" in diff and "Goodbye" in diff and "tests/test_greet.py" in diff,
                 [ln for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")][:12])
        run = until(lambda: next((r for r in s.ci_of(APP, pr["head_sha"]) if r["status"] == "completed"), None),
                    300, "CI on the PR head")
        sc.check("mock CI (Actions) ran pytest on the head: success", run["conclusion"] == "success",
                 {k: run[k] for k in ("name", "event", "status", "conclusion", "head_sha")})
        end = s.wait_session_end(sid)
        sc.check("implement session ended", end.get("state") == "ended",
                 {k: end.get(k) for k in ("outcome", "turn", "tokens", "model")})

    # -- 5: review comment answer ---------------------------------------------------------
    def s5_answer(self) -> None:
        s = self.s
        sc = Scenario("5", "Review comment → agent answer", "C17")
        self.results["5"] = sc
        issue, pr = s.issues["1"], s.prs["1"]
        files = s.rest("GET", f"/repos/{APP}/pulls/{pr['number']}/files").json()
        target = next((f for f in files if f["filename"].startswith("tests/") and f.get("patch")), None) or \
            next(f for f in files if f.get("patch"))
        line = first_added_line(target["patch"])
        body = ("Did you actually run the whole suite before opening this? Reply with the exact command "
                "and its output. Next time, please paste that output in the PR description.")
        t0 = time.monotonic()
        t0_ms = now_ms()
        comment = s.gh("POST", "comment", {"repo": APP, "number": pr["number"], "user": PERSON,
                                            "kind": "review_comment", "path": target["filename"],
                                            "line": line, "body": body})
        sc.check("a person's inline review comment in mock GitHub",
                 comment["user"]["login"] == PERSON, {"path": target["filename"], "line": line, "id": comment["id"]})
        meta = s.wait_session(sc, issue, kind="answer", after_ms=t0_ms, timeout=120)
        hooks = [d for d in s.gh("GET", "deliveries", params={"event": "pull_request_review_comment", "brief": "1"})]
        sc.check("GitHub webhook pull_request_review_comment delivered to /v1/webhooks/github (202/200)",
                 any(d["status_code"] in (200, 202) for d in hooks),
                 [{k: d.get(k) for k in ("event", "status_code", "delivered_at")} for d in hooks[-2:]])

        def replied():
            st = s.repo_state(APP)
            replies = [c for c in st["review_comments"] if c["pr"] == pr["number"] and c["user"] == s.bot_login
                       and iso_ms(c["created_at"]) >= t0_ms - 1000]
            convo = [c for c in st["comments"] if c["issue"] == pr["number"] and c["user"] == s.bot_login
                     and iso_ms(c["created_at"]) >= t0_ms - 1000]
            return replies + convo

        reply = until(replied, AGENT_TIMEOUT_S, "the App's reply on the PR", interval=3)
        sc.time("person's comment → reply on the PR", time.monotonic() - t0)
        sc.check("reply posted on the PR by the App (in the comment's thread or the conversation)",
                 bool(reply), [{k: c.get(k) for k in ("user", "in_reply_to_id", "body")} | {"body": c["body"][:300]}
                               for c in reply[:2]])
        sc.check("the reply is in the comment's thread", any(c.get("in_reply_to_id") == comment["id"] for c in reply),
                 [c.get("in_reply_to_id") for c in reply], fatal=False)
        end = s.wait_session_end(meta["sid"])
        lin = s.issue(issue["identifier"])
        sc.check("answer run left the ticket in In Review", lin["state"] == "In Review" and end["state"] == "ended",
                 {"state": lin["state"], "outcome": end.get("outcome")})

    # -- 1b: merge -------------------------------------------------------------------------
    def s1_merge(self) -> None:
        s = self.s
        sc = self.results["1"]
        issue, pr = s.issues["1"], s.prs["1"]
        t0 = time.monotonic()
        s.start_merger()
        sc.time("merger process up (mirror cloned over git smart HTTP)", time.monotonic() - t0)

        def report():
            st = s.repo_state(APP)
            return [c for c in st["comments"] if c["issue"] == pr["number"] and c["user"] == s.bot_login
                    and "**symphony-merger**" in c["body"]]

        rep = until(report, AGENT_TIMEOUT_S, "the merger's review report", interval=3)
        sc.time("merger up → review report", time.monotonic() - t0)
        sc.check("merger review report on the PR (by the App)", True, rep[0]["body"][:500])

        def merged():
            p = next((x for x in s.repo_state(APP)["pulls"] if x["number"] == pr["number"]), None)
            return p if p and p.get("merged") else None

        done = until(merged, AGENT_TIMEOUT_S, "the train to merge the PR", interval=3)
        sc.time("merger up → merged", time.monotonic() - t0)
        st = s.repo_state(APP)
        main_sha = st["branches"]["main"]
        clone = s.clone_as(APP, s.person_token, PERSON)
        parents = s.git("rev-list", "--parents", "-n", "1", "HEAD", cwd=clone).stdout.split()[1:]
        log = s.git("log", "--format=%h %s", "-4", cwd=clone).stdout.splitlines()
        touched = s.git("log", "-1", "--format=%H", "--", "app/greet.py", cwd=clone).stdout.strip()
        sc.check("merged with rebase: no merge commit on main, the PR's change is on main",
                 done["merged"] and len(parents) == 1 and touched != "" and "def farewell" in
                 s.git("show", "HEAD:app/greet.py", cwd=clone).stdout,
                 {"main": main_sha[:12], "parents_of_head": len(parents), "log": log,
                  "merged_by": done.get("merged_by")})
        lin = until(lambda: (lambda i: i if i["state"] == "Merging" else None)(s.issue(issue["identifier"])), 120,
                    "Linear Merging")
        sc.check("merger moved the Linear ticket to Merging", lin["state"] == "Merging", {"state": lin["state"]})
        errors = s.notices("run_error")
        sc.check("no run_error on Slack", not errors, [m.get("text") for m in errors])
        # The Linear↔GitHub integration closes the ticket when the PR lands.
        s.lin("POST", f"issues/{issue['identifier']}/state", {"state": "Done", "as": "ada@example.com"})
        from symphony.workspace import workspace_key

        ws = s.tmp / "root" / "workspaces" / workspace_key(issue["identifier"])
        until(lambda: not ws.exists(), 60, "terminal cleanup of the workspace")
        sc.check("Done (integration simulated via /_mock) → terminal cleanup removed the workspace",
                 not ws.exists(), {"workspace": str(ws.name), "state": s.issue(issue["identifier"])["state"]})

    # -- 2: multi-repo ---------------------------------------------------------------------
    def s2_multi(self) -> None:
        s = self.s
        sc = Scenario("2", "Multi-repo: one ticket, two repositories", "O1, A2, B15")
        self.results["2"] = sc
        t0 = time.monotonic()
        issue = s.create_ticket(
            "Add VERSION to app and lib",
            "Add a module-level constant `VERSION = \"1.1.0\"` to `app/__init__.py` in acme/app and to "
            "`lib/__init__.py` in acme/lib, each with a pytest test asserting its value. One pull request "
            "per repository, linked to each other.\n\nAcceptance: `python -m pytest -q` passes in both.",
            ["symphony", f"repo:{APP}", f"repo:{LIB}"])
        s.issues["2"] = issue
        self.cleanups.append(lambda: (self.cancel_open([issue["identifier"]]), [
            s.rest("PATCH", f"/repos/{repo}/pulls/{p['number']}", {"state": "closed"})
            for repo in (APP, LIB) for p in s.pulls(repo, issue["identifier"]) if p["state"] == "open"]))
        meta = s.wait_session(sc, issue)
        _, record = s.wait_credential(sc, issue["identifier"])
        sc.check("one token scoped to both repositories", record["repos"] == sorted([APP, LIB]),
                 {k: record[k] for k in ("repos", "permissions")})

        def both():
            a = [p for p in s.pulls(APP, issue["identifier"]) if p["state"] == "open" and not p["draft"]]
            b = [p for p in s.pulls(LIB, issue["identifier"]) if p["state"] == "open" and not p["draft"]]
            return a and b and (a, b)

        app_prs, lib_prs = until(both, AGENT_TIMEOUT_S, "one ready PR per repository", interval=3)
        sc.time("ticket created → both PRs", time.monotonic() - t0)
        sc.check("exactly one PR in acme/app and one in acme/lib, by the App",
                 len(app_prs) == 1 and len(lib_prs) == 1 and all(p["user"] == s.bot_login for p in app_prs + lib_prs),
                 {"acme/app": [(p["number"], p["title"], p["labels"]) for p in app_prs],
                  "acme/lib": [(p["number"], p["title"], p["labels"]) for p in lib_prs]})
        until(lambda: s.issue(issue["identifier"])["state"] == "In Review", AGENT_TIMEOUT_S, "In Review", interval=3)
        sc.check("ticket In Review", True, {"state": "In Review"})
        s.wait_session_end(meta["sid"])
        # Out of the merger's way: the PRs close and the ticket is cancelled.
        for repo, p in ((APP, app_prs[0]), (LIB, lib_prs[0])):
            s.rest("PATCH", f"/repos/{repo}/pulls/{p['number']}", {"state": "closed"})
        s.lin("POST", f"issues/{issue['identifier']}/state", {"state": "Canceled"})

    # -- 3: investigation ------------------------------------------------------------------
    def s3_investigation(self) -> None:
        s = self.s
        sc = Scenario("3", "Investigation with a read-only token", "O6, B15")
        self.results["3"] = sc
        t0 = time.monotonic()
        before = {p["number"] for p in s.repo_state(APP)["pulls"]}
        issue = s.create_ticket(
            "Which functions in app/greet.py lack a test?",
            "Investigate which public functions in `app/greet.py` have no pytest test under `tests/`. "
            "Answer with evidence: the commands you ran and what they printed.",
            ["symphony", "investigation", f"repo:{APP}"])
        s.issues["3"] = issue
        meta = s.wait_session(sc, issue)
        token, record = s.wait_credential(sc, issue["identifier"])
        sc.check("investigation token minted with ghapp.READ_ONLY, scoped to acme/app",
                 record["permissions"] == READ_ONLY and record["repos"] == [APP],
                 {k: record[k] for k in ("repos", "permissions")})
        clone = s.clone_as(APP, token)
        (clone / "PROBE.md").write_text("must not land\n")
        s.git("checkout", "-q", "-b", "probe-read-only", cwd=clone)
        s.git("add", "PROBE.md", cwd=clone)
        s.git("commit", "-q", "-m", "probe", cwd=clone)
        push = s.git("push", "origin", "probe-read-only", cwd=clone, check=False)
        sc.check("a push with the attempt's token is refused by the mock (403)",
                 push.returncode != 0 and "403" in push.stderr, push.stderr.strip().splitlines()[-3:])
        pr_try = s.rest("POST", f"/repos/{APP}/pulls", {"title": "probe", "head": "main", "base": "main"}, token=token)
        sc.check("opening a PR with it is refused (403 Resource not accessible by integration)",
                 pr_try.status_code == 403, {"status": pr_try.status_code, "body": pr_try.json()})
        lin = until(lambda: (lambda i: i if i["state"] == "Done" else None)(s.issue(issue["identifier"])),
                    AGENT_TIMEOUT_S, "the investigation ticket Done", interval=3)
        sc.time("ticket created → Done", time.monotonic() - t0)
        reports = [c for c in lin["comments"] if c["user"] == s.linear_bot_email
                   and not c["body"].lstrip().startswith("## Workpad") and "greet" in c["body"]]
        sc.check("report posted as a Linear comment", bool(reports), reports[0]["body"][:600] if reports else lin["comments"])
        st = s.repo_state(APP)
        new = [p for p in st["pulls"] if p["number"] not in before]
        sc.check("no PR and no branch for the investigation",
                 not new and not any(issue["identifier"].lower() in b.lower() for b in st["branches"])
                 and "probe-read-only" not in st["branches"],
                 {"new_prs": [p["title"] for p in new], "branches": sorted(st["branches"])})
        s.wait_session_end(meta["sid"])

    # -- 4: sub-issues + evidence -------------------------------------------------------------
    def s4_tree(self) -> None:
        s = self.s
        sc = Scenario("4", "Sub-issues and evidence", "O5, O2")
        self.results["4"] = sc
        t0 = time.monotonic()
        issue = s.create_ticket(
            "Plan the shout() follow-up",
            "This is a planning ticket: the deliverable is the plan, not code. Open no pull request.\n\n"
            "1. Run `printf 'symphony evidence: pytest passed\\n' > evidence.txt` in the workspace and attach "
            "`evidence.txt` to this ticket with `symphony_attach_evidence` (title `pytest evidence`, kind `log`).\n"
            "2. With ONE call to `symphony_create_issues`, create two sub-issues: key `code`, title "
            "`Add shout() to app/greet.py`; and key `docs`, title `Document shout() in README.md`, "
            "`blocked_by: [\"code\"]`. Give each a one-line description with its acceptance criterion.\n"
            "3. Record the plan in the Workpad and move this ticket to Done.",
            ["symphony", f"repo:{APP}"])
        s.issues["4"] = issue
        meta = s.wait_session(sc, issue)
        # Children carry `symphony`: hold admission so this run does not pay for their attempts.
        s.command("pause", "e2e: keep the sub-issues from being dispatched")
        self.cleanups.append(lambda: self.cancel_open(s.issue(issue["identifier"])["children"]))
        lin = until(lambda: (lambda i: i if i["state"] == "Done" else None)(s.issue(issue["identifier"])),
                    AGENT_TIMEOUT_S, "the planning ticket Done", interval=3)
        sc.time("ticket created → Done", time.monotonic() - t0)
        children = [s.issue(c) for c in lin["children"]]
        sc.check("two sub-issues with parentId = the ticket, inheriting symphony + repo:acme/app",
                 len(children) == 2 and all(c["parent"] == issue["identifier"] and "symphony" in c["labels"]
                                            and f"repo:{APP}" in c["labels"] for c in children),
                 [{k: c[k] for k in ("identifier", "title", "parent", "labels", "state")} for c in children])
        by_title = {("docs" if "Document" in c["title"] else "code"): c for c in children}
        rel = [r for r in lin["relations"] if r["type"] == "blocks"] + [
            r for c in children for r in c["relations"] if r["type"] == "blocks"]
        sc.check("`code` blocks `docs` (IssueRelation type blocks)",
                 len(by_title) == 2 and any(r["issue"] == by_title["code"]["identifier"]
                                            and r["relatedIssue"] == by_title["docs"]["identifier"] for r in rel),
                 rel[:2])
        uploads = s.lin("GET", "uploads")["uploads"]
        up = [u for u in uploads if u.get("uploaded")]
        sizes = [u.get("size") for u in up]
        assets = []
        for u in up:
            url = u.get("assetUrl")
            if url:
                got = s.http.get(url, headers={"Authorization": s.linear_key})
                assets.append(got.content)
        sc.check("fileUpload + PUT to the signed URL: the uploaded bytes match the workspace file",
                 EVIDENCE_BYTES in assets, {"uploads": [{k: u.get(k) for k in ("filename", "contentType", "size")}
                                                         for u in up], "expected_size": len(EVIDENCE_BYTES),
                                             "sizes": sizes})
        sc.check("attachmentCreate on the ticket", any("evidence" in (a["title"] or "").lower() for a in lin["attachments"]),
                 lin["attachments"])
        end = s.wait_session_end(meta["sid"])
        sc.check("meta.evidence[] lists the attachment", bool(end.get("evidence")), end.get("evidence"))

    # -- 6: learnings PR ---------------------------------------------------------------------
    def s6_learning(self) -> None:
        s = self.s
        sc = Scenario("6", "Learnings PR (retrospective)", "C18")
        self.results["6"] = sc
        issue = s.issues["1"]
        until(lambda: "reflector_skipped_local_mode" in s.log_text("symphony-local"), 60,
              "the daemon's terminal path to reach the reflector")
        t0 = time.monotonic()
        env = s.common_env()
        env.pop("SYMPHONY_BOOTSTRAP_TOKEN", None)
        out = subprocess.run([sys.executable, "-m", "scripts.e2e_mocked.reflect", issue["id"], issue["identifier"], APP],
                             env=env, cwd=str(REPO), capture_output=True, text=True, timeout=900, check=False)
        (s.tmp / "logs" / "reflect.log").write_text(out.stdout + out.stderr)
        result = json.loads((out.stdout.strip().splitlines() or ["{}"])[-1])
        sc.time("retrospective (harvest + distill + curation + PR)", time.monotonic() - t0)
        sc.check("run_retrospective → learnings-pr-opened", result.get("outcome") == "learnings-pr-opened",
                 result)
        prs = [p for p in s.repo_state(APP)["pulls"] if p["title"] == f"learnings: {issue['identifier']}"]
        sc.check("learnings PR in mock GitHub by the App, label symphony-learnings",
                 len(prs) == 1 and prs[0]["user"] == s.bot_login and "symphony-learnings" in prs[0]["labels"],
                 [{k: p[k] for k in ("number", "title", "user", "labels", "head_ref")} for p in prs])
        if prs:
            files = s.rest("GET", f"/repos/{APP}/pulls/{prs[0]['number']}/files").json()
            names = [f["filename"] for f in files]
            sc.check("it targets AGENTS.md (and skills, when proposed)", "AGENTS.md" in names,
                     {"files": names, "AGENTS.md": next((f.get("patch", "")[:600] for f in files
                                                         if f["filename"] == "AGENTS.md"), None)})
            s.rest("PATCH", f"/repos/{APP}/pulls/{prs[0]['number']}", {"state": "closed"})
        token = result.get("token")
        sc.check("retro token done with the PR URL", bool(token) and token.get("status") == "done"
                 and token.get("learnings_pr"), token)

    # -- 7: main red -------------------------------------------------------------------------
    def s7_main_red(self) -> None:
        s = self.s
        sc = Scenario("7", "Red main → ticket; green → closed", "T8, X3")
        self.results["7"] = sc
        s.start_merger()
        s.command("pause", "e2e: the main-red ticket must not be dispatched")
        before = {i["identifier"] for i in s.lin("GET", "issues")["issues"]}
        self.cleanups.append(lambda: (
            s.gh("POST", "ci", {"repo": APP, "branch": "main", "clear": True}),
            self.cancel_open(i["identifier"] for i in s.lin("GET", "issues")["issues"]
                             if i["identifier"] not in before)))
        log_line = "FAILED tests/test_greet.py::test_greet - AssertionError: mocked red main"
        s.gh("POST", "ci", {"repo": APP, "branch": "main", "conclusion": "failure", "log": log_line})
        t0 = time.monotonic()
        red_sha = s.push_main_commit("e2e: red main")

        def opened():
            new = [i for i in s.lin("GET", "issues")["issues"] if i["identifier"] not in before]
            return new

        tickets = until(opened, 300, "the main-watch ticket", interval=2)
        sc.time("red run on main → Linear ticket", time.monotonic() - t0)
        t = tickets[0]
        sc.check("ticket opened in ENG/Todo with repo:acme/app + symphony + ci",
                 t["state"] == "Todo" and {f"repo:{APP}", "symphony", "ci"} <= set(t["labels"]),
                 {k: t[k] for k in ("identifier", "title", "state", "labels")})
        sc.check("the description carries the failing job's log", log_line in (t["description"] or ""),
                 (t["description"] or "")[:600])
        red = until(lambda: s.notices("main_red"), 60, "main_red notice")
        sc.check(f"{s.notify}: main_red", True, red[-1].get("text"))
        s.push_main_commit("e2e: still red")
        s.gh("POST", "ci/drain", {"timeout_s": 120})
        time.sleep(35)  # one more safety poll of the watch
        again = [i for i in s.lin("GET", "issues")["issues"] if i["identifier"] not in before]
        sc.check("a second red run opens no second ticket (exactly one)", len(again) == 1,
                 [i["identifier"] for i in again])
        s.gh("POST", "ci", {"repo": APP, "branch": "main", "conclusion": "success"})
        t1 = time.monotonic()
        s.push_main_commit("e2e: green main")
        closed = until(lambda: (lambda i: i if i["stateType"] == "completed" else None)(s.issue(t["identifier"])),
                       300, "the main-watch ticket to close", interval=2)
        sc.time("green run on main → ticket Done", time.monotonic() - t1)
        sc.check("green: comment on the ticket and moved to Done",
                 closed["state"] == "Done" and len(closed["comments"]) >= 1,
                 {"state": closed["state"], "comment": closed["comments"][-1]["body"][:300] if closed["comments"] else None})
        green = until(lambda: s.notices("main_green"), 60, "main_green notice")
        sc.check(f"{s.notify}: main_green", True, green[-1].get("text"))
        s.gh("POST", "ci", {"repo": APP, "branch": "main", "clear": True})
        sc.check("red sha recorded", bool(red_sha), red_sha[:12])

    # -- 8: rejection ----------------------------------------------------------------------
    def s8_rejection(self) -> None:
        s = self.s
        sc = Scenario("8", "Rejection: a real bug goes back to the author", "R6, R8, R12, X3")
        self.results["8"] = sc
        s.start_merger()
        if not s.api("GET", "/v1/overview").json()["daemon"].get("paused"):
            s.command("pause", "e2e: the Rework must not dispatch an implementer")
        issue = s.create_ticket("Add is_even()", "Add `is_even(n)` to `app/parity.py`.",
                                ["symphony", f"repo:{APP}"], state="In Review")
        s.issues["8"] = issue
        self.cleanups.append(lambda: (self.cancel_open([issue["identifier"]]), [
            s.rest("PATCH", f"/repos/{APP}/pulls/{p['number']}", {"state": "closed"})
            for p in s.pulls(APP, issue["identifier"]) if p["state"] == "open"]))
        ident = issue["identifier"]
        clone = s.clone_as(APP, s.person_token, PERSON)
        branch = f"{ident.lower()}-is-even"
        s.git("checkout", "-q", "-b", branch, cwd=clone)
        (clone / "app" / "parity.py").write_text(
            '"""Parity."""\n\n\ndef is_even(n: int) -> bool:\n    """Return True when n is even."""\n'
            "    return n % 2 == 1\n")
        s.git("add", "app/parity.py", cwd=clone)
        s.git("commit", "-q", "-m", f"{ident}: add is_even", cwd=clone)
        s.git("push", "-q", "origin", branch, cwd=clone)
        r = s.rest("POST", f"/repos/{APP}/pulls", {"title": f"{ident}: add is_even", "head": branch, "base": "main",
                                                  "body": f"Adds is_even for {ident}."})
        pr = r.json()
        sc.check("a person's PR with a real bug (is_even returns the odd check)", r.status_code == 201,
                 {"number": pr.get("number"), "title": pr.get("title")})
        t0 = time.monotonic()

        def drafted():
            p = next((x for x in s.repo_state(APP)["pulls"] if x["number"] == pr["number"]), None)
            return p if p and p["draft"] else None

        p = until(drafted, AGENT_TIMEOUT_S, "the merger to send the PR back (draft)", interval=3)
        sc.time("PR opened → back to draft", time.monotonic() - t0)
        st = s.repo_state(APP)
        rep = [c for c in st["comments"] if c["issue"] == pr["number"] and c["user"] == s.bot_login
               and "symphony-merger" in c["body"]]
        sc.check("report comment with a blocking finding", bool(rep) and "blocking" in rep[-1]["body"],
                 rep[-1]["body"][:700] if rep else None)
        sc.check("PR is draft", p["draft"], {k: p[k] for k in ("number", "draft", "labels")})
        lin = until(lambda: (lambda i: i if i["state"] == "Rework" else None)(s.issue(ident)), 120, "Linear Rework")
        sc.check("Linear moved to Rework with a comment", lin["state"] == "Rework" and bool(lin["comments"]),
                 {"state": lin["state"], "comment": lin["comments"][-1]["body"][:300] if lin["comments"] else None})
        back = until(lambda: s.notices("back_to_author", f"#{pr['number']}"), 60, "back_to_author notice")
        sc.check(f"{s.notify}: back_to_author, mentioning the author from notify.users", s.mention in back[-1]["text"],
                 back[-1].get("text"))
        s.lin("POST", f"issues/{ident}/state", {"state": "Canceled"})
        s.rest("PATCH", f"/repos/{APP}/pulls/{pr['number']}", {"state": "closed"})

    # -- driver -----------------------------------------------------------------------------
    def execute(self, wanted: set[str]) -> None:
        from scripts.e2e_mocked import assistant, seiji

        self.execute_core(wanted - set(assistant.SCENARIOS) - set(seiji.SCENARIOS))
        assistant.execute(self, wanted & set(assistant.SCENARIOS))
        seiji.execute(self, wanted & set(seiji.SCENARIOS))

    def execute_core(self, wanted: set[str]) -> None:
        if self.s.tracker == "github":
            from scripts.e2e_mocked import github_tracker

            return github_tracker.execute(self, wanted)
        plan = []
        if wanted & ({"1"} | NEEDS_1):
            plan.append(("1", self.s1_implement))
        if "5" in wanted:
            plan.append(("5", self.s5_answer))
        for key, fn in (("2", self.s2_multi), ("3", self.s3_investigation), ("4", self.s4_tree)):
            if key in wanted:
                plan.append((key, fn))
        if wanted & {"1", "6"}:
            plan.append(("1", self.s1_merge))
        for key, fn in (("6", self.s6_learning), ("7", self.s7_main_red), ("8", self.s8_rejection)):
            if key in wanted:
                plan.append((key, fn))
        failed: set[str] = set()
        for key, fn in plan:
            if key in NEEDS_1 and "1" in failed:
                self.results[key] = sc = Scenario(key, fn.__name__, "")
                sc.error = "skipped: scenario 1 failed"
                continue
            if fn == self.s1_merge and "1" in failed:
                continue
            print(f"== scenario {key} ({fn.__name__})", flush=True)
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — one scenario's failure is its own result
                sc = self.results.get(key)
                if sc is None:
                    self.results[key] = sc = Scenario(key, fn.__name__, "")
                sc.error = f"{type(exc).__name__}: {exc}"
                (self.s.tmp / "logs" / f"scenario-{key}.trace").write_text(traceback.format_exc())
                print(f"  [ERROR] {key}: {sc.error[:500]}", flush=True)
                failed.add(key)
            else:
                if not self.results[key].ok:
                    failed.add(key)
            finally:
                self.after_scenario()

    def after_scenario(self) -> None:
        while self.cleanups:
            try:
                self.cleanups.pop()()
            except Exception as exc:  # noqa: BLE001 — the next scenario reports its own state
                print(f"  [warn] cleanup failed: {exc}", flush=True)
        if self.s.api("GET", "/v1/overview").json()["daemon"].get("paused"):
            self.s.command("resume", "e2e: scenario over")


def mock_hygiene(s: Stack) -> dict:
    """What the mocks saw outside the published specifications."""
    out = {}
    try:
        out["github_violations"] = s.gh("GET", "violations")["violations"][:20]
    except Exception as exc:  # noqa: BLE001
        out["github_violations"] = str(exc)
    reqs = s.lin("GET", "requests")["requests"]
    out["linear_requests"] = len(reqs)
    out["linear_errors"] = [{k: r.get(k) for k in ("status", "operation", "errors", "query")}
                            for r in reqs if (r.get("status") or 200) >= 400][:20]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Certify Symphony end to end against the vendor mocks.")
    parser.add_argument("--scenario", "--scenarios", action="append", default=[],
                        help="scenario number(s), repeatable or comma-separated (default: 1-8, or "
                             "1, 3, 4, 7 with --tracker github)")
    parser.add_argument("--assistant", action="store_true",
                        help="add the assistant scenarios of the tracker (9, 10, 12, 14; 11 with github)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--out", help="also write the JSON summary to this file")
    parser.add_argument("--tracker", choices=("linear", "github"), default="linear",
                        help="tickets in Linear (default) or as GitHub issues on a Projects board")
    parser.add_argument("--notify", choices=("slack", "discord"), default="slack",
                        help="the notification channel (default: slack)")
    args = parser.parse_args()
    from scripts.e2e_mocked import assistant, seiji

    wanted = {x.strip() for item in args.scenario for x in item.split(",") if x.strip()}
    supported = set(ALL) if args.tracker == "linear" else set(GITHUB_SCENARIOS)
    extra = set(assistant.LINEAR_SCENARIOS if args.tracker == "linear" else assistant.GITHUB_SCENARIOS)
    devices = set(seiji.SCENARIOS) if args.tracker == "linear" else set()
    if args.assistant:
        wanted |= extra
    wanted = wanted or supported
    if wanted - supported - extra - devices:
        parser.error(f"scenario(s) {sorted(wanted - supported - extra - devices)} are not available with "
                     f"--tracker {args.tracker}")
    omp = Path(os.environ.get("SYMPHONY_OMP_BIN") or DEFAULT_OMP)
    if not omp.exists():
        print(f"omp not found at {omp} (set SYMPHONY_OMP_BIN)", file=sys.stderr)
        return 2
    if not (os.environ.get("AWS_REGION") and (os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
                                              or os.environ.get("AWS_ACCESS_KEY_ID"))):
        print("Bedrock credentials missing: AWS_REGION + AWS_BEARER_TOKEN_BEDROCK", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="sym-e2e-mocked-", dir="/tmp"))
    stack = Stack(tmp, omp, os.environ.get("E2E_PROVIDER", "amazon-bedrock"),
                  os.environ.get("E2E_MODEL", "global.anthropic.claude-opus-5-5"),
                  os.environ.get("E2E_THINKING", "low"), int(os.environ.get("E2E_MAX_TURNS", "3")),
                  tracker=args.tracker, notify=args.notify, assistant=bool(wanted & extra),
                  private_home=bool(wanted & devices), env_probe=bool(wanted & devices))
    run = Run(stack)
    summary: dict = {"tmp": str(tmp), "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                     "scenarios": []}
    code = 0
    t0 = time.monotonic()
    try:
        stack.start()
        summary["omp_version"] = stack.omp_version
        summary["model"] = f"{stack.provider}/{stack.model} ({stack.thinking})"
        summary["tracker"], summary["notify"] = stack.tracker, stack.notify
        summary["setup_s"] = round(time.monotonic() - t0, 1)
        run.execute(wanted)
        summary["mock_hygiene"] = mock_hygiene(stack)
    except Exception as exc:  # noqa: BLE001
        summary["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        code = 1
    finally:
        stack.stop_all()
        summary["elapsed_s"] = round(time.monotonic() - t0, 1)
        summary["scenarios"] = [run.results[k].summary() for k in sorted(run.results)]
        summary["ok"] = code == 0 and all(r["ok"] for r in summary["scenarios"]) and \
            {r["scenario"] for r in summary["scenarios"]} >= wanted
        if not summary["ok"]:
            code = 1
        text = json.dumps(summary, indent=2, ensure_ascii=False, default=str)
        (tmp / "summary.json").write_text(text)
        if args.out:
            Path(args.out).write_text(text)
        print(text)
        if not args.keep and code == 0:
            shutil.rmtree(tmp, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
