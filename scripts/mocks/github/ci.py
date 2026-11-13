"""CI as GitHub Actions reports it: one workflow run per trigger (pushes to the default branch;
pull requests opened, synchronized, reopened or marked ready), a check suite for it, one job
and one check run per configured job name, and the webhooks along the way.

The result of a run comes, in order, from an override set through `/_mock/ci` for the head SHA,
the pull request or the branch; else the repository's mode: `success`/`failure`, or `auto`
(the default), which runs the repository's tests in a fresh checkout of the head (`command`
when configured, else `python -m pytest -q` when the tree has tests, else success).

Concurrency follows the workflow in `docs/install.md`: one group per pull request (separate when
it carries `train-ci`), cancel-in-progress for pull requests, one group per commit on main.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import gitrepo
from .state import iso

TRAIN_LABEL = "train-ci"
COMMAND_TIMEOUT_S = 900
STEP_NAMES = ("Set up job", "Run tests", "Complete job")


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.0000000Z", time.gmtime())


class CI:
    def __init__(self, mock) -> None:
        self.m = mock
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mock-ci")
        self._pending: set[tuple[str, int, int]] = set()
        self._cv = threading.Condition()
        self._closing = False

    @property
    def store(self):
        return self.m.store

    def close(self) -> None:
        self._closing = True
        with self._cv:
            self._cv.notify_all()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # -- reads -----------------------------------------------------------------------------
    def latest_checks(self, repo: dict, sha: str) -> list[dict]:
        latest: dict[str, dict] = {}
        for c in repo["check_runs"].values():
            if c["head_sha"] == sha and (c["name"] not in latest or c["id"] > latest[c["name"]]["id"]):
                latest[c["name"]] = c
        return list(latest.values())

    def latest_statuses(self, repo: dict, sha: str) -> list[dict]:
        latest: dict[str, dict] = {}
        for s in repo["statuses"]:
            if s["sha"] == sha:
                latest[s["context"]] = s
        return list(latest.values())

    # -- triggers ----------------------------------------------------------------------------
    def trigger(self, repo: dict, sha: str, branch: str, event: str, prs: list[int], actor: str, title: str,
                labels: list[str] = (), draft: bool = False) -> dict | None:
        cfg = repo["ci"]
        if cfg.get("mode") == "off":
            return None
        with self.m.lock:
            if event == "pull_request":
                group = f"{cfg['workflow']}-{prs[0]}{'-train' if TRAIN_LABEL in labels else ''}"
            else:
                group = f"{cfg['workflow']}-{sha}"
            if event == "pull_request":
                for other in list(repo["workflow_runs"].values()):
                    if other.get("group") == group and other["status"] != "completed":
                        self._cancel(repo, other, actor)
            now = iso()
            suite = {"id": self.store.next_id(), "head_sha": sha, "head_branch": branch, "status": "queued",
                     "conclusion": None, "created_at": now, "updated_at": now, "pull_requests": list(prs),
                     "check_runs": []}
            repo["check_suites"][str(suite["id"])] = suite
            if "workflow_id" not in cfg:
                cfg["workflow_id"] = self.store.next_id()
            repo["run_number"] += 1
            run = {"id": self.store.next_id(), "name": cfg["workflow"], "path": cfg["path"],
                   "workflow_id": cfg["workflow_id"], "run_number": repo["run_number"], "run_attempt": 1,
                   "event": event, "status": "queued", "conclusion": None, "head_sha": sha, "head_branch": branch,
                   "display_title": title, "actor": actor, "triggering_actor": actor, "created_at": now,
                   "updated_at": now, "run_started_at": now, "suite": suite["id"], "pull_requests": list(prs),
                   "jobs": [], "group": group, "draft": bool(draft and event == "pull_request")}
            repo["workflow_runs"][str(run["id"])] = run
            for name in cfg["jobs"]:
                run["jobs"].append(self._new_job(repo, run, suite, name))
            self.store.save()
            r = self.m.render()
            self.m.hooks.emit("workflow_run", "requested", repo,
                              {"workflow_run": r.workflow_run(repo, run), "workflow": self._workflow(repo)}, actor)
            self._schedule(repo, run)
            return run

    def _workflow(self, repo: dict) -> dict:
        cfg = repo["ci"]
        base = self.m.public_url
        fn = repo["full_name"]
        return {"id": cfg["workflow_id"], "node_id": f"W_{cfg['workflow_id']}", "name": cfg["workflow"],
                "path": cfg["path"], "state": "active", "created_at": repo["created_at"],
                "updated_at": repo["created_at"], "url": f"{base}/repos/{fn}/actions/workflows/{cfg['workflow_id']}",
                "html_url": f"{base}/{fn}/blob/{repo['default_branch']}/{cfg['path']}",
                "badge_url": f"{base}/{fn}/workflows/{cfg['workflow']}/badge.svg"}

    def _new_job(self, repo: dict, run: dict, suite: dict, name: str) -> int:
        now = iso()
        job = {"id": self.store.next_id(), "run_id": run["id"], "run_attempt": run["run_attempt"], "name": name,
               "status": "queued", "conclusion": None, "created_at": now, "started_at": None, "completed_at": None,
               "steps": [{"name": s, "status": "queued", "conclusion": None} for s in STEP_NAMES], "log": ""}
        check = {"id": self.store.next_id(), "name": name, "head_sha": run["head_sha"], "status": "queued",
                 "conclusion": None, "suite": suite["id"], "job": job["id"], "started_at": None,
                 "completed_at": None, "pull_requests": list(run["pull_requests"]), "title": None, "summary": None}
        job["check_run"] = check["id"]
        repo["jobs"][str(job["id"])] = job
        repo["check_runs"][str(check["id"])] = check
        suite["check_runs"].append(check["id"])
        return job["id"]

    def _schedule(self, repo: dict, run: dict) -> None:
        key = (repo["full_name"].lower(), run["id"], run["run_attempt"])
        with self._cv:
            self._pending.add(key)
        self.pool.submit(self._execute, key)

    # -- execution ---------------------------------------------------------------------------
    def override(self, repo: dict, run: dict) -> dict | None:
        res = repo["ci_results"]
        keys = [run["head_sha"]]
        keys += [f"pr:{n}" for n in run["pull_requests"]]
        keys += [f"branch:{run['head_branch']}", "*"]
        for key in keys:
            if key in res:
                return res[key]
        return None

    def _execute(self, key: tuple[str, int, int]) -> None:
        full, run_id, attempt = key
        try:
            self._run(full, run_id, attempt)
        finally:
            with self._cv:
                self._pending.discard(key)
                self._cv.notify_all()

    def _current(self, full: str, run_id: int, attempt: int):
        repo = self.store.data["repos"].get(full)
        run = repo["workflow_runs"].get(str(run_id)) if repo else None
        if run is None or run["run_attempt"] != attempt or run["status"] == "completed":
            return None, None
        return repo, run

    def _run(self, full: str, run_id: int, attempt: int) -> None:
        with self.m.lock:
            repo, run = self._current(full, run_id, attempt)
            if run is None:
                return
            now = iso()
            run.update(status="in_progress", updated_at=now, run_started_at=now)
            suite = repo["check_suites"][str(run["suite"])]
            suite.update(status="in_progress", updated_at=now)
            jobs = [repo["jobs"][str(j)] for j in run["jobs"] if repo["jobs"][str(j)]["status"] != "completed"]
            for job in jobs:
                job.update(status="in_progress", started_at=now)
                job["steps"][0].update(status="completed", conclusion="success", started_at=now, completed_at=now)
                job["steps"][1].update(status="in_progress", started_at=now)
                repo["check_runs"][str(job["check_run"])].update(status="in_progress", started_at=now)
            self.store.save()
            r = self.m.render()
            self.m.hooks.emit("workflow_run", "in_progress", repo,
                              {"workflow_run": r.workflow_run(repo, run), "workflow": self._workflow(repo)},
                              run["triggering_actor"])
            for job in jobs:
                self.m.hooks.emit("check_run", "created", repo,
                                  {"check_run": r.check_run(repo, repo["check_runs"][str(job["check_run"])])},
                                  run["triggering_actor"])
            sha, draft, cfg = run["head_sha"], run["draft"], dict(repo["ci"])
            path = self.store.repo_path(repo)
        # Decide outside the lock: a held result waits, and a test command can take minutes.
        while True:
            with self.m.lock:
                repo, run = self._current(full, run_id, attempt)
                if run is None:
                    return
                ov = self.override(repo, run)
            if not (ov and ov.get("hold")) or self._closing:
                break
            with self._cv:
                self._cv.wait(0.2)
        results = self._decide(path, sha, draft, cfg, ov, [repo["jobs"][str(j)]["name"] for j in run["jobs"]])
        delay = float((ov or {}).get("delay_s", cfg.get("delay_s") or 0))
        deadline = time.time() + delay
        while time.time() < deadline and not self._closing:
            with self.m.lock:
                if self._current(full, run_id, attempt)[1] is None:
                    return
            time.sleep(min(0.1, max(0.0, deadline - time.time())))
        with self.m.lock:
            repo, run = self._current(full, run_id, attempt)
            if run is None:
                return
            self._complete(repo, run, results)

    def _decide(self, path: str, sha: str, draft: bool, cfg: dict, ov: dict | None,
                names: list[str]) -> dict[str, dict]:
        """`{job name: {conclusion, log, failed_step}}`."""
        if draft and cfg.get("skip_drafts", True):
            return {n: {"conclusion": "skipped", "log": "", "step": None} for n in names}
        if ov is not None:
            out = {}
            for n in names:
                conclusion = (ov.get("jobs") or {}).get(n, ov.get("conclusion", "success"))
                log = ov.get("log") or f"Result set by the mock: {conclusion}"
                step = None if ov.get("interrupted") or conclusion != "failure" else 1
                out[n] = {"conclusion": conclusion, "log": log, "step": step}
            return out
        mode = cfg.get("mode", "auto")
        if mode in ("success", "failure"):
            return {n: {"conclusion": mode, "log": f"mode {mode}", "step": 1 if mode == "failure" else None}
                    for n in names}
        conclusion, log = self._run_command(path, sha, cfg.get("command"))
        return {n: {"conclusion": conclusion, "log": log, "step": 1 if conclusion == "failure" else None}
                for n in names}

    def _run_command(self, path: str, sha: str, command: str | None) -> tuple[str, str]:
        with tempfile.TemporaryDirectory(prefix="mock-ci-") as tmp:
            gitrepo.archive(path, sha, tmp)
            if command:
                argv, shown = ["bash", "-c", command], command
            elif _has_tests(tmp):
                argv, shown = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], "python -m pytest -q"
            else:
                return "success", f"{_ts()} No tests found in the tree; nothing to run.\n"
            env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_"))}
            try:
                proc = subprocess.run(argv, cwd=tmp, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S,
                                      env=env)
                code, text = proc.returncode, proc.stdout + proc.stderr
            except subprocess.TimeoutExpired as exc:
                code, text = 124, f"{exc.stdout or ''}{exc.stderr or ''}\ntimed out after {COMMAND_TIMEOUT_S}s"
        ts = _ts()
        lines = [f"{ts} ##[group]Run {shown}", f"{ts} ##[endgroup]"]
        lines += [f"{ts} {line}" for line in text.splitlines()]
        if code != 0:
            lines.append(f"{ts} ##[error]Process completed with exit code {code}.")
        return ("success" if code == 0 else "failure"), "\n".join(lines) + "\n"

    def _complete(self, repo: dict, run: dict, results: dict[str, dict]) -> None:
        now = iso()
        r = self.m.render()
        suite = repo["check_suites"][str(run["suite"])]
        for jid in run["jobs"]:
            job = repo["jobs"][str(jid)]
            if job["status"] == "completed":
                continue
            res = results.get(job["name"], {"conclusion": "success", "log": "", "step": None})
            conclusion = res["conclusion"]
            job.update(status="completed", conclusion=conclusion, completed_at=now,
                       log=f"{_ts()} Job {job['name']} on {run['head_sha'][:7]}\n{res['log']}")
            for i, step in enumerate(job["steps"]):
                if i == 0:
                    continue
                if conclusion == "skipped":
                    step.update(status="completed", conclusion="skipped")
                elif res["step"] is None and conclusion not in ("success", "neutral"):
                    # interrupted: the job failed but no step did (the runner went away)
                    step.update(status="completed", conclusion="cancelled" if i == 1 else "skipped")
                elif res["step"] is not None and i == res["step"]:
                    step.update(status="completed", conclusion=conclusion, completed_at=now)
                elif res["step"] is not None and i > res["step"]:
                    step.update(status="completed", conclusion="skipped")
                else:
                    step.update(status="completed", conclusion="success", started_at=step.get("started_at") or now,
                                completed_at=now)
            check = repo["check_runs"][str(job["check_run"])]
            check.update(status="completed", conclusion=conclusion, completed_at=now,
                         started_at=check.get("started_at") or now, title=f"{job['name']}: {conclusion}",
                         summary=res["log"][-1000:])
        conclusions = [repo["jobs"][str(j)]["conclusion"] for j in run["jobs"]]
        overall = _overall(conclusions)
        run.update(status="completed", conclusion=overall, updated_at=now)
        suite.update(status="completed", conclusion=overall, updated_at=now)
        self.store.save()
        actor = run["triggering_actor"]
        for jid in run["jobs"]:
            check = repo["check_runs"][str(repo["jobs"][str(jid)]["check_run"])]
            self.m.hooks.emit("check_run", "completed", repo, {"check_run": r.check_run(repo, check)}, actor)
        self.m.hooks.emit("check_suite", "completed", repo, {"check_suite": r.check_suite(repo, suite)}, actor)
        self.m.hooks.emit("workflow_run", "completed", repo,
                          {"workflow_run": r.workflow_run(repo, run), "workflow": self._workflow(repo)}, actor)

    # -- control --------------------------------------------------------------------------------
    def _cancel(self, repo: dict, run: dict, actor: str) -> None:
        now = iso()
        for jid in run["jobs"]:
            job = repo["jobs"][str(jid)]
            if job["status"] == "completed":
                continue
            job.update(status="completed", conclusion="cancelled", completed_at=now)
            for step in job["steps"]:
                if step["status"] != "completed":
                    step.update(status="completed", conclusion="cancelled" if step["status"] == "in_progress"
                                else "skipped")
            repo["check_runs"][str(job["check_run"])].update(status="completed", conclusion="cancelled",
                                                             completed_at=now)
        run.update(status="completed", conclusion="cancelled", updated_at=now)
        repo["check_suites"][str(run["suite"])].update(status="completed", conclusion="cancelled", updated_at=now)
        r = self.m.render()
        self.m.hooks.emit("workflow_run", "completed", repo,
                          {"workflow_run": r.workflow_run(repo, run), "workflow": self._workflow(repo)}, actor)

    def cancel(self, repo: dict, run: dict, actor: str) -> bool:
        with self.m.lock:
            if run["status"] == "completed":
                return False
            self._cancel(repo, run, actor)
            self.store.save()
        with self._cv:
            self._cv.notify_all()
        return True

    def rerun(self, repo: dict, run: dict, actor: str, failed_only: bool) -> bool:
        """A new attempt of a completed run: every job, or only those that did not succeed."""
        with self.m.lock:
            if run["status"] != "completed":
                return False
            suite = repo["check_suites"][str(run["suite"])]
            run["run_attempt"] += 1
            jobs = []
            for jid in run["jobs"]:
                job = repo["jobs"][str(jid)]
                if failed_only and job["conclusion"] in ("success", "neutral", "skipped"):
                    jobs.append(jid)
                    continue
                jobs.append(self._new_job(repo, run, suite, job["name"]))
            run.update(jobs=jobs, status="queued", conclusion=None, updated_at=iso(), triggering_actor=actor)
            suite.update(status="queued", conclusion=None, updated_at=iso())
            self.store.save()
            r = self.m.render()
            self.m.hooks.emit("workflow_run", "requested", repo,
                              {"workflow_run": r.workflow_run(repo, run), "workflow": self._workflow(repo)}, actor)
            self._schedule(repo, run)
            return True

    def set_result(self, repo: dict, key: str, result: dict | None) -> None:
        with self.m.lock:
            if result is None:
                repo["ci_results"].pop(key, None)
            else:
                repo["ci_results"][key] = result
            self.store.save()
        with self._cv:
            self._cv.notify_all()

    def drain(self, timeout_s: float = 60.0) -> bool:
        """Waits until no run is queued or in progress (held runs count as in progress)."""
        deadline = time.time() + timeout_s
        with self._cv:
            while self._pending:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 0.2))
        return True


def _overall(conclusions: list[str | None]) -> str:
    for c in ("failure", "timed_out", "startup_failure", "action_required", "cancelled"):
        if c in conclusions:
            return c
    if conclusions and all(c == "skipped" for c in conclusions):
        return "skipped"
    return "success"


def _has_tests(root: str) -> bool:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", ".venv", "venv")]
        if os.path.basename(dirpath) == "tests" and any(f.endswith(".py") for f in filenames):
            return True
        if any(f.startswith("test_") and f.endswith(".py") or f.endswith("_test.py") for f in filenames):
            return True
    return False
