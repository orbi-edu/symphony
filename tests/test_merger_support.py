"""Shared fakes for the merger tests: a local bare git remote with a push log, an in-memory
GitHub that merges into that remote for real, scripted omp agents speaking the RPC protocol
(host_tool_call frames), tracker and Slack recorders, and a ready-made `Merger`."""

from __future__ import annotations

import itertools
import json
import os
import queue
import re
import subprocess
import threading

from symphony.merger.agents import AgentRunner
from symphony.merger.context import Merger
from symphony.merger.git import Mirror
from symphony.merger.github import GitHub, GitHubError
from symphony.merger.store import MergerStore
from symphony.sessions import SessionStore
from symphony.teamconfig import TeamConfig
from symphony.trackers.linear import IDENTIFIER

NS = "test"
STATES = {"review": "In Review", "rework": "Rework", "merging": "Merging", "done": "Done"}
ENV = {**os.environ, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
       "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": "dev@example.com"}

POLICY_2_LANES = """---
lanes:
  - key: correctness
    audits: correctness and regressions
  - key: security
    audits: security and the ticket's acceptance criteria
---
Keep functions small.
"""


def sh(*args: str, cwd: str, check: bool = True) -> str:
    proc = subprocess.run(list(args), cwd=cwd, env=ENV, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {proc.stderr or proc.stdout}")
    return proc.stdout.strip()


class World:
    """A bare remote (`remote.git`) and a developer clone that authors commits on it."""

    def __init__(self, root: str, policy: str | None = POLICY_2_LANES) -> None:
        self.root = root
        self.remote = os.path.join(root, "remote.git")
        self.dev = os.path.join(root, "dev")
        self.pushlog = os.path.join(root, "pushes.log")
        sh("git", "init", "-q", "--bare", "-b", "main", self.remote, cwd=root)
        hook = os.path.join(self.remote, "hooks", "post-receive")
        with open(hook, "w") as fh:
            fh.write(f"#!/bin/sh\ncat >> {self.pushlog}\n")
        os.chmod(hook, 0o755)
        sh("git", "clone", "-q", self.remote, self.dev, cwd=root)
        files = {"README.md": "# app\n", "src/app.py": "def run():\n    return 1\n",
                 "lib/util.py": "def util():\n    return 2\n"}
        if policy:
            files[".symphony/merger.md"] = policy
        self.write(files)
        self.git("checkout", "-q", "-B", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.git("push", "-q", "origin", "main")

    def git(self, *args: str, check: bool = True) -> str:
        return sh("git", *args, cwd=self.dev, check=check)

    def write(self, files: dict) -> None:
        for path, text in files.items():
            full = os.path.join(self.dev, path)
            if text is None:
                if os.path.exists(full):
                    os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(text)

    def commit(self, branch: str, files: dict, message: str, base: str | None = None) -> str:
        """Commits on `branch` (created from origin/`base`, default its own tip or main) and
        pushes it; returns the new head."""
        self.git("fetch", "-q", "origin")
        start = f"origin/{base}" if base else (
            f"origin/{branch}" if self.git("rev-parse", "-q", "--verify", f"origin/{branch}", check=False)
            else "origin/main")
        self.git("checkout", "-q", "-B", branch, start)
        self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        self.git("push", "-q", "-f", "origin", f"{branch}:{branch}")
        return self.head(branch)

    def rebase(self, branch: str, files: dict | None = None, message: str = "fix") -> str:
        """The author rebases `branch` onto main (and optionally adds a commit)."""
        self.git("fetch", "-q", "origin")
        self.git("checkout", "-q", "-B", branch, f"origin/{branch}")
        self.git("rebase", "-q", "origin/main")
        if files:
            self.write(files)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", message)
        self.git("push", "-q", "-f", "origin", f"{branch}:{branch}")
        return self.head(branch)

    def head(self, ref: str) -> str:
        return sh("git", "rev-parse", "-q", "--verify", f"refs/heads/{ref}", cwd=self.remote, check=False)

    def show(self, ref: str, path: str) -> str | None:
        out = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=self.remote, capture_output=True, text=True)
        return out.stdout if out.returncode == 0 else None

    def log(self, ref: str = "main") -> list[str]:
        return sh("git", "log", "--format=%s", ref, cwd=self.remote).split("\n")


class FakeGitHub(GitHub):
    """GitHub over the World's remote: heads are read from the remote itself, merges really
    land on its main, CI runs start on every push (post-receive hook) and finish on `tick()`."""

    def __init__(self, world: World, repo: str = "acme/app") -> None:
        self.w = world
        self.repo = repo
        self.prs: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.ids = itertools.count(1000)
        self.runs: dict[str, dict] = {}
        self.main_runs: list[dict] = []
        self.plan = lambda sha, attempt: "success"
        self.reruns: list[int] = []  # rerun of the failed jobs (a red batch)
        self.restarts: list[int] = []  # a whole run again (a cancelled run, revived)
        self.cancelled: list[str] = []
        self.merges: list[tuple[int, str, str]] = []
        self.calls: list[str] = []
        self.fail_next: dict[str, Exception] = {}
        self.required_checks = False  # branch protection: merge only a head whose CI passed
        self.ticks = 0
        self._log_offset = 0
        self.lock = threading.RLock()
        self.pr_reviews: dict[int, list[dict]] = {}  # PR number → list of reviews
        self.artifacts: dict[int, list[dict]] = {}  # workflow run id → list of artifacts

    # -- helpers ---------------------------------------------------------------------
    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        exc = self.fail_next.pop(name, None)
        if exc is not None:
            raise exc

    def _sync(self) -> None:
        if not os.path.exists(self.w.pushlog):
            return
        with open(self.w.pushlog) as fh:
            fh.seek(self._log_offset)
            data = fh.read()
            self._log_offset = fh.tell()
        for line in data.splitlines():
            _old, new, ref = line.split()
            if ref.startswith("refs/heads/") and ref != "refs/heads/main" and set(new) != {"0"}:
                self._start(new, push=True)

    def _start(self, sha: str, push: bool = False) -> dict:
        """The head's latest run; a push starts a new one even for a head CI already saw (a
        branch pushed back to an earlier head runs again, as on GitHub)."""
        old = self.runs.get(sha)
        if old is None or (push and old["status"] == "completed"):
            self.runs[sha] = {"id": next(self.ids), "name": "CI", "sha": sha, "status": "in_progress",
                              "conclusion": "", "attempt": 1, "branch": "", "event": "pull_request",
                              "url": f"https://ci/{sha[:7]}", "title": sha[:7]}
        return self.runs[sha]

    def tick(self) -> None:
        """Every run in progress finishes, with the conclusion `plan` gives its head."""
        with self.lock:
            self.ticks += 1
            if self.ticks > 400:
                raise RuntimeError("the test never settled: too many CI waits")
            self._sync()
            for run in self.runs.values():
                if run["status"] != "completed":
                    run["status"] = "completed"
                    run["conclusion"] = self.plan(run["sha"], run["attempt"])

    def open_pr(self, n: int, branch: str, title: str, *, author: str = "alice", body: str = "Does a thing.",
                ready_at: str = "2026-11-01T10:00:00Z", labels: tuple = (), draft: bool = False,
                requested_reviewers: list[str] | None = None, author_type: str = "User") -> None:
        self.prs[n] = {"number": n, "state": "open", "draft": draft, "base": "main", "head_ref": branch,
                       "author": author, "author_type": author_type, "labels": list(labels),
                       "ready_at": ready_at, "title": title,
                       "body": body, "mergeable": None,
                       "requested_reviewers": list(requested_reviewers or [])}
        self.comments.setdefault(n, [])

    def by_url(self, url: str) -> dict:
        cid = int(url.rsplit("-", 1)[-1])
        return next(c for cs in self.comments.values() for c in cs if c["id"] == cid)

    # -- PRs ---------------------------------------------------------------------------
    def _view(self, p: dict) -> dict:
        head = self.w.head(p["head_ref"]) or p.get("last_head", "")
        if head:
            p["last_head"] = head
        mergeable = p["mergeable"]
        if mergeable is None:
            ok = subprocess.run(["git", "merge-tree", "--write-tree", "refs/heads/main", head],
                                cwd=self.w.remote, capture_output=True).returncode == 0
            mergeable = "MERGEABLE" if ok else "CONFLICTING"
        return {**p, "labels": list(p["labels"]), "head_sha": head, "mergeable": mergeable,
                "url": f"https://github.com/{self.repo}/pull/{p['number']}", "node_id": f"PR_{p['number']}"}

    def list_open(self) -> list[dict]:
        self._maybe_fail("list_open")
        return [self._view(p) for p in self.prs.values() if p["state"] == "open"]

    def pr(self, n: int) -> dict:
        self._maybe_fail("pr")
        if n not in self.prs:
            raise GitHubError(f"PR #{n} not found", 404)
        return self._view(self.prs[n])

    def add_labels(self, n: int, labels: list[str]) -> None:
        self._maybe_fail("add_labels")
        for label in labels:
            if label not in self.prs[n]["labels"]:
                self.prs[n]["labels"].append(label)

    def remove_label(self, n: int, label: str) -> None:
        self._maybe_fail("remove_label")
        if label in self.prs[n]["labels"]:
            self.prs[n]["labels"].remove(label)

    def comment(self, n: int, body: str) -> str:
        self._maybe_fail("comment")
        cid = next(self.ids)
        url = f"https://github.com/{self.repo}/pull/{n}#issuecomment-{cid}"
        self.comments[n].append({"id": cid, "url": url, "body": body, "edits": 0})
        return url

    def edit_comment(self, url: str, body: str) -> None:
        self._maybe_fail("edit_comment")
        c = self.by_url(url)
        c["body"] = body
        c["edits"] += 1

    def discussion(self, n: int) -> list[dict]:
        return [{"at": "2026-11-01T11:00:00Z", "who": "symphony-merger[bot]", "where": "PR", "body": c["body"]}
                for c in self.comments.get(n, [])]

    def set_draft(self, n: int, draft: bool) -> None:
        self._maybe_fail("set_draft")
        self.prs[n]["draft"] = draft

    def merge(self, n: int, sha: str, method: str) -> tuple[bool, str]:
        self._maybe_fail("merge")
        with self.lock:
            p = self._view(self.prs[n])
            if p["state"] != "open":
                return False, "Pull Request is not open"
            if p["head_sha"] != sha:
                return False, "Head branch was modified. Review and try the merge again."
            run = self.runs.get(sha)
            if self.required_checks and not (run and run["status"] == "completed" and run["conclusion"] == "success"):
                state = "expected" if run is None else run["conclusion"] or "in progress"
                return False, f'Required status check "test" is {state}.'
            work = os.path.join(self.w.root, "gh-merge")
            if not os.path.isdir(work):
                sh("git", "clone", "-q", self.w.remote, work, cwd=self.w.root)
            sh("git", "fetch", "-q", "origin", cwd=work)
            sh("git", "checkout", "-q", "-B", "tmp", "origin/main", cwd=work)
            base = sh("git", "merge-base", "origin/main", sha, cwd=work)
            if method == "rebase":
                for c in sh("git", "rev-list", "--reverse", f"{base}..{sha}", cwd=work).split():
                    if subprocess.run(["git", "cherry-pick", "--allow-empty", c], cwd=work, env=ENV,
                                      capture_output=True).returncode != 0:
                        sh("git", "cherry-pick", "--abort", cwd=work, check=False)
                        return False, "Merge conflict"
            elif method == "squash":
                sh("git", "merge", "-q", "--squash", sha, cwd=work)
                sh("git", "commit", "-q", "-m", p["title"], cwd=work)
            else:
                sh("git", "merge", "-q", "--no-ff", "-m", f"Merge #{n}", sha, cwd=work)
            sh("git", "push", "-q", "origin", "tmp:main", cwd=work)
            self.prs[n]["state"] = "merged"
            self.merges.append((n, sha, method))
            return True, ""

    # -- CI -----------------------------------------------------------------------------
    def check_runs(self, sha: str) -> list[dict]:
        self._maybe_fail("check_runs")
        with self.lock:
            self._sync()
            run = self._start(sha)
            return [{"name": "test", "status": run["status"],
                     "conclusion": run["conclusion"] if run["status"] == "completed" else "",
                     "started_at": f"{run['id']:010d}", "url": run["url"]}]

    def workflow_runs(self, *, sha=None, branch=None, event=None, status=None) -> list[dict]:
        with self.lock:
            self._sync()
            if sha:
                return [dict(self.runs[sha])] if sha in self.runs else []
            return [dict(r) for r in sorted(self.main_runs, key=lambda r: -r["id"])
                    if (status is None or r["status"] == status)]

    def _find(self, run_id: int) -> dict:
        for r in [*self.runs.values(), *self.main_runs]:
            if r["id"] == run_id:
                return r
        raise GitHubError(f"run {run_id} not found", 404)

    def run(self, run_id: int) -> dict:
        return dict(self._find(run_id))

    def run_jobs(self, run_id: int) -> list[dict]:
        r = self._find(run_id)
        if "jobs" in r:
            return r["jobs"]
        return [{"id": run_id * 10, "name": "test", "conclusion": r["conclusion"], "url": "",
                 "steps": [{"name": "pytest", "conclusion": r["conclusion"]}]}]

    def job_log(self, job_id: int) -> str:
        return "collected 3 items\nFAILED tests/test_app.py::test_run - assert 2 == 1\n"

    def rerun_failed(self, run_id: int) -> None:
        r = self._find(run_id)
        self.reruns.append(run_id)
        r["attempt"] += 1
        r["status"] = "in_progress"
        r["conclusion"] = ""

    def rerun(self, run_id: int) -> None:
        r = self._find(run_id)
        self.restarts.append(run_id)
        r["attempt"] += 1
        r["status"] = "in_progress"
        r["conclusion"] = ""

    def cancel(self, run_id: int) -> None:
        r = self._find(run_id)
        if r["status"] != "completed":
            r["status"] = "completed"
            r["conclusion"] = "cancelled"
            self.cancelled.append(r["sha"])

    def reviews(self, n: int) -> list[dict]:
        return list(self.pr_reviews.get(n, []))

    def artifact_download_url(self, run_id: int, name: str) -> str | None:
        for a in self.artifacts.get(run_id, []):
            if a.get("name") == name and not a.get("expired"):
                return f"https://artifacts/{run_id}/{name}"
        return None


class FakeOmp:
    """An `omp --mode rpc` that answers commands and plays a role's script as host tool calls."""

    def __init__(self, agents: ScriptedAgents, argv: list[str], cwd: str) -> None:
        self.a = agents
        self.argv = argv
        self.cwd = cwd
        self.out: queue.Queue = queue.Queue()
        self.role = "unknown"
        self.tools: list[dict] = []
        self.pending: list[tuple[str, dict]] = []
        self.prompt_id = None
        self.entries = 0
        self.results: list[dict] = []
        self.running = False

    def emit(self, frame: dict) -> None:
        self.out.put(json.dumps(frame))

    def _role(self) -> str:
        names = {t["name"] for t in self.tools}
        props = next((t["parameters"]["properties"] for t in self.tools if t["name"] == "submit_review"), {})
        if "submit_finding" in names:
            return "reviewer"
        if "submit_verdict" in names:
            return "oracle"
        if "submit_arbitration" in names:
            return "arbiter"
        if "submit_resolution" in names:
            return "developer"
        if "approved" in props:
            return "resolve_review"
        return "probe"

    def write_line(self, line: str) -> None:
        cmd = json.loads(line)
        kind = cmd.get("type")
        if kind == "host_tool_result":
            self.results.append(cmd)
            self.a.results.append((self.role, cmd))
            self._next()
            return
        if kind == "set_host_tools":
            self.tools = cmd["tools"]
            self.role = self._role()
        if kind == "get_entries":
            self.entries += 1
            self.emit({"id": cmd["id"], "type": "response", "command": kind, "success": True,
                       "data": {"entries": [{"id": f"e{self.entries}", "type": "message"}], "leafId": None}})
            return
        if kind == "get_state":
            self.emit({"id": cmd["id"], "type": "response", "command": kind, "success": True,
                       "data": {"sessionId": "s", **self.a.state}})
            return
        if kind == "abort":
            self.emit({"id": cmd["id"], "type": "response", "command": kind, "success": True})
            if self.running:
                self.pending = []
                self._end("aborted")
            return
        if kind in ("steer", "follow_up"):
            self.a.steers.append((self.role, kind, cmd["message"]))
        self.emit({"id": cmd["id"], "type": "response", "command": kind, "success": True})
        if kind == "prompt":
            self.prompt_id = cmd["id"]
            self.running = True
            self.a.prompts.append((self.role, cmd["message"], self.argv))
            script = self.a.scripts.get(self.role) or self.a.default
            self.pending = list(script(self.role, cmd["message"], self.cwd))
            self.emit({"type": "agent_start"})
            for error in self.a.retry_errors:
                self.emit({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 2000,
                           "errorMessage": error})
            self.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "working"}})
            if self.a.hold is not None and self.role in self.a.hold_roles:
                threading.Thread(target=self._held, daemon=True).start()
                return
            self._next()

    def _held(self) -> None:
        self.a.hold.wait(10)
        if self.running and self.pending:
            self._next()

    def _next(self) -> None:
        if not self.running:
            return
        if self.pending:
            name, args = self.pending.pop(0)
            self.emit({"type": "host_tool_call", "id": f"h{len(self.results)}-{name}", "toolCallId": "t",
                       "toolName": name, "arguments": args})
            return
        self._end("stop")

    def _end(self, reason: str) -> None:
        self.running = False
        msg = {"role": "assistant", "content": [{"type": "text", "text": f"{self.role} done"}], "stopReason": reason}
        self.emit({"type": "message_end", "message": msg})
        self.emit({"type": "agent_end", "messages": [msg], "isTerminal": True})

    def read_line(self, timeout_s: float) -> str | None:
        try:
            return self.out.get(timeout=min(timeout_s, 0.2))
        except queue.Empty:
            return None

    def close(self) -> None:
        self.running = False


def probe_script(role: str, prompt: str, cwd: str):
    m = re.search(r"Read the file (\S+) with", prompt)
    if m:
        with open(m.group(1)) as fh:
            return [("submit_review", {"summary": fh.read()})]
    return [("submit_review", {"summary": "nothing"})]


class ScriptedAgents:
    def __init__(self) -> None:
        self.scripts: dict = {"probe": probe_script}
        self.prompts: list = []
        self.results: list = []
        self.steers: list = []
        self.spawned: list = []
        self.hold: threading.Event | None = None
        self.hold_roles: set[str] = set()
        self.state: dict = {}  # what get_state answers (e.g. usageLimit)
        self.retry_errors: list[str] = []  # auto_retry_start frames each turn opens with

    @staticmethod
    def default(role: str, prompt: str, cwd: str):
        if role == "reviewer":
            return [("submit_review", {"strengths": [], "verified": "read the diff"})]
        if role == "oracle":
            return [("submit_review", {"summary": "done"})]
        if role == "resolve_review":
            return [("submit_review", {"approved": True, "issues": ""})]
        return probe_script(role, prompt, cwd)

    def spawn(self, argv, cwd, env, stderr_path):
        proc = FakeOmp(self, argv, cwd)
        proc.env = env
        agent_dir = env.get("PI_CODING_AGENT_DIR")
        models = os.path.join(agent_dir, "models.yml") if agent_dir else None
        # what omp would read at start: its private agent dir's models.yml, as staged then
        proc.models_yml = open(models).read() if models and os.path.exists(models) else None  # noqa: SIM115
        self.spawned.append(proc)
        return proc

    def roles(self) -> list[str]:
        return [r for r, _, _ in self.prompts if r != "probe"]


class FakeTracker:
    """The merger's view of a tracker, recording writes; identifiers are Linear-style."""

    kind = "fake"

    def __init__(self) -> None:
        self.moves: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.created: list[dict] = []
        self.states: dict[str, dict] = {}
        self.attached: dict[str, list[dict]] = {}

    def identifier_in(self, text: str, repo: str | None = None) -> str | None:
        m = IDENTIFIER.search(text or "")
        return m.group(1).upper() if m else None

    def move(self, identifier: str, state: str) -> bool:
        self.moves.append((identifier, state))
        self.states[identifier] = {"name": state, "type": "terminal" if state == "Done" else "active"}
        return True

    def comment(self, identifier: str, body: str) -> str:
        self.comments.append((identifier, body))
        return f"comment-{len(self.comments)}"

    def state_of(self, identifier: str) -> dict | None:
        return self.states.get(identifier, {"name": "Todo", "type": "active"})

    def attachments(self, identifier: str) -> list[dict]:
        return self.attached.get(identifier, [])

    def create_issue(self, title: str, body: str, *, labels, state=None, repo=None) -> dict:
        identifier = f"ENG-{100 + len(self.created)}"
        self.created.append({"repo": repo, "title": title, "body": body, "state": state, "labels": labels,
                             "identifier": identifier})
        return {"identifier": identifier, "url": f"https://linear.app/x/issue/{identifier}"}


class Clock:
    def __init__(self, start: int = 1_800_000_000_000) -> None:
        self.ms = start
        self.lock = threading.Lock()

    def __call__(self) -> int:
        with self.lock:
            self.ms += 1
            return self.ms


def make_merger(redis_client, tmp_path, world: World, gh: FakeGitHub, agents: ScriptedAgents,
                *, tracker: FakeTracker | None = None, notes: list | None = None, pause=None) -> Merger:
    clock = Clock()
    git = Mirror(str(tmp_path / "work"), world.remote)
    git.ensure()
    git.fetch()
    notes = notes if notes is not None else []
    m = Merger(
        store=MergerStore(redis_client, NS, gh.repo, now_ms=clock),
        gh=gh,
        git=git,
        agents=AgentRunner(SessionStore(redis_client, NS), session_root=str(tmp_path / "sessions"),
                           spawn=agents.spawn, silence_s=10, command_timeout_s=10),
        team=TeamConfig(redis_client, NS),
        repo=gh.repo,
        states=STATES,
        tracker=tracker if tracker is not None else FakeTracker(),
        notify_post=lambda event, text, link=None, github_logins=(): notes.append((event, text, link, list(github_logins))),
        pause=pause or (lambda s: gh.tick()),
        clock=clock,
        ticket_labels=("symphony",),
        label_filter_active=False,  # existing tests run without the filter
    )
    m.notes = notes
    return m


def finding(location: str, claim: str, cls: str = "bug", severity: str = "blocking", **extra) -> tuple:
    return ("submit_finding", {"severity": severity, "class": cls, "audit": "correctness", "location": location,
                               "claim": claim, "fix": "- [ ] fix it", **extra})


def verdict(fid: str, verdict_: str = "confirmed", cls: str = "bug", severity: str = "blocking", **extra) -> tuple:
    return ("submit_verdict", {"id": fid, "verdict": verdict_, "severity": severity, "class": cls,
                               "evidence": "opened the line", "fix": "- [ ] fix it" if verdict_ != "refuted" else "",
                               **extra})


def done_review() -> tuple:
    return ("submit_review", {"strengths": ["small"], "verified": "the whole diff"})


def done_verify() -> tuple:
    return ("submit_review", {"summary": "verified"})


def ids_in(prompt: str) -> list[str]:
    """The finding ids an oracle prompt asks about."""
    m = re.search(r"FINDINGS \(JSON\): (\[.*?\n\])", prompt, re.S)
    return [f["id"] for f in json.loads(m.group(1))] if m else []
