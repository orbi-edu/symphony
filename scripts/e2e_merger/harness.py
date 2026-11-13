"""Real-world certification of symphony-merger against a sandbox GitHub repository.

Runs the production merger code (`symphony.entry.merger.build` + `start_loops`) against a real
private repository, real GitHub Actions CI and real Bedrock agents. What differs from the
Deployment: the GitHub token is the operator's `gh auth token` (no GitHub App), Redis is a local
disposable server under $E2E_ROOT, agents take their model credentials from this process's env
(no `symphony-providers` Secret: `build(providers=None)`), Linear writes and Slack posts are recorded to JSONL files
(no keys), and no webhook wakes the merger (the safety poll runs every --poll seconds).

From the Symphony repository root, with the sandbox repository in --repo or $E2E_REPO
(`owner/name`; `setup` creates it as a private repository):

    uv run python scripts/e2e_merger/harness.py setup            # repo, seed, labels (idempotent)
    uv run python scripts/e2e_merger/harness.py open a           # a .. f: open that scenario's PRs (see SCENARIOS)
    uv run python scripts/e2e_merger/harness.py run --until merged:1 drafted:2 [--no-train] [--timeout 2400]
    uv run python scripts/e2e_merger/harness.py evidence         # timings per PR, as JSON
    uv run python scripts/e2e_merger/harness.py stop             # stop the local Redis
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import redis

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("E2E_REPO", "")  # set from --repo in main()
ROOT = os.environ.get("E2E_ROOT", "/tmp/e2e-merger")
OMP = os.environ.get("SYMPHONY_OMP_BIN", os.path.expanduser("~/Library/Application Support/Tern/omp/bin/omp"))
NS = "e2e"
LABELS = {
    "reviewed": "0e8a16", "wait-ci": "fbca04", "train-ci": "5319e7", "review-requested": "d93f0b",
    "in-audit": "1d76db", "review-bypass": "b60205", "needs-maintainer": "e99695",
}
MODELS = {
    "reviewer": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-opus-5-5", "thinking": "medium"},
    # opus-5, sonnet-5* and gpt-6* answer 403 (no Marketplace subscription) on this account:
    # the oracle and arbiter run on other families the account has.
    "oracle": {"provider": "amazon-bedrock", "model": "global.moonshotai.kimi-k3.1", "thinking": "medium"},
    "arbiter": {"provider": "amazon-bedrock", "model": "deepseek.v3.2", "thinking": "medium"},
    "developer": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-opus-5-5", "thinking": "medium"},
    "probe": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-opus-5-5", "thinking": "low"},
}
STATES = {"review": "In Review", "rework": "Rework", "merging": "Merging", "done": "Done"}

OPS = open(os.path.join(HERE, "sandbox", "calc", "ops.py")).read()
SCENARIOS = {
    # a: a clean PR, merged by the train on green CI.
    "a": [("sbx-1-clamp", "SBX-1 add clamp", "Adds `clamp(x, lo, hi)`, which limits x to [lo, hi].", {
        "calc/clamp.py": '"""Clamping."""\n\n\ndef clamp(x: float, lo: float, hi: float) -> float:\n'
                         '    """Return x limited to the closed range [lo, hi]."""\n    return max(lo, min(x, hi))\n',
        "tests/test_clamp.py": "from calc.clamp import clamp\n\n\ndef test_clamp():\n    assert clamp(5, 0, 3) == 3\n"
                               "    assert clamp(-1, 0, 3) == 0\n    assert clamp(2, 0, 3) == 2\n",
    })],
    # b: a real bug the tests don't catch: the docstring says even, the code returns odd.
    "b": [("sbx-2-is-even", "SBX-2 add is_even", "Adds `is_even(n)` for the parity checks of the billing job.", {
        "calc/parity.py": '"""Parity."""\n\n\ndef is_even(n: int) -> bool:\n    """Return True when n is even."""\n'
                          "    return n % 2 == 1\n",
        "tests/test_parity.py": "from calc.parity import is_even\n\n\ndef test_is_even_returns_a_bool():\n"
                                "    assert isinstance(is_even(3), bool)\n",
    })],
    # c: two PRs that append a function at the same place of calc/ops.py.
    "c": [
        ("sbx-3-mul", "SBX-3 add mul", "Adds `mul(a, b)`.", {
            "calc/ops.py": OPS + '\n\ndef mul(a: float, b: float) -> float:\n    """Return a times b."""\n    return a * b\n',
            "tests/test_mul.py": "from calc.ops import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
        }),
        ("sbx-4-neg", "SBX-4 add neg", "Adds `neg(a)`.", {
            "calc/ops.py": OPS + '\n\ndef neg(a: float) -> float:\n    """Return -a."""\n    return -a\n',
            "tests/test_neg.py": "from calc.ops import neg\n\n\ndef test_neg():\n    assert neg(2) == -2\n",
        }),
    ],
    # d: three independent clean PRs, merged as one batch on the top's CI.
    "d": [
        (f"sbx-{n}-{name}", f"SBX-{n} add {name}", f"Adds `{name}(xs)`.", {
            f"calc/{name}.py": f'"""{doc}."""\n\n\ndef {name}(xs: list[float]) -> float:\n'
                               f'    """Return the {doc.lower()} of a non-empty list."""\n    return {body}\n',
            f"tests/test_{name}.py": f"from calc.{name} import {name}\n\n\ndef test_{name}():\n"
                                     f"    assert {name}([1, 2, 3]) == {want}\n",
        })
        for n, name, doc, body, want in (
            (5, "largest", "Largest value", "max(xs)", 3),
            (6, "smallest", "Smallest value", "min(xs)", 1),
            (7, "total", "Total", "sum(xs)", 6),
        )
    ],
    # e: a batch of two with branch protection requiring the `test` check (admins included).
    "e": [
        (f"sbx-{n}-{name}", f"SBX-{n} add {name}", f"Adds `{name}(x)`.", {
            f"calc/{name}.py": f'"""{doc}."""\n\n\ndef {name}(x: float) -> float:\n'
                               f'    """Return {doc.lower()} x."""\n    return {body}\n',
            f"tests/test_{name}.py": f"from calc.{name} import {name}\n\n\ndef test_{name}():\n"
                                     f"    assert {name}(4) == {want}\n",
        })
        for n, name, doc, body, want in ((8, "double", "Twice", "2 * x", 8), (9, "half", "Half of", "x / 2", 2))
    ],
    # f: the same as e after the fix, three members: each waits for its own required checks.
    "f": [
        (f"sbx-{n}-{name}", f"SBX-{n} add {name}", f"Adds `{name}(x)`.", {
            f"calc/{name}.py": f'"""{doc}."""\n\n\ndef {name}(x: float) -> float:\n'
                               f'    """Return {doc.lower()} x."""\n    return {body}\n',
            f"tests/test_{name}.py": f"from calc.{name} import {name}\n\n\ndef test_{name}():\n"
                                     f"    assert {name}(4) == {want}\n",
        })
        for n, name, doc, body, want in ((10, "triple", "Three times", "3 * x", 12),
                                         (11, "square", "The square of", "x * x", 16),
                                         (12, "succ", "One more than", "x + 1", 5))
    ],
}


def sh(*args: str, cwd: str | None = None, check: bool = True, env: dict | None = None) -> str:
    proc = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})})
    if check and proc.returncode != 0:
        raise SystemExit(f"{' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


_token = {"value": "", "at": 0.0}


def gh_token() -> str:
    """The operator's gh token, re-read every 10 minutes (gh refreshes it)."""
    if not _token["value"] or time.time() - _token["at"] > 600:
        _token.update(value=sh("gh", "auth", "token"), at=time.time())
    return _token["value"]


# -- recorders -----------------------------------------------------------------------------
def _record(name: str, doc: dict) -> None:
    os.makedirs(ROOT, exist_ok=True)
    with open(os.path.join(ROOT, name), "a") as fh:
        fh.write(json.dumps({"at": dt.datetime.now(dt.UTC).isoformat(), **doc}) + "\n")


def notify_post(event, text, *, link=None, github_login=None):
    _record("notify.jsonl", {"event": event, "text": text, "link": link, "github_login": github_login})
    print(f"[slack] {event}: {text} {link or ''}", flush=True)


class RecordingTracker:
    """The tracker stays unverified here (no key): every write is recorded instead."""

    kind = "recording"

    def identifier_in(self, text):
        from symphony.trackers.linear import IDENTIFIER

        m = IDENTIFIER.search(text or "")
        return m.group(1).upper() if m else None

    def move(self, identifier, state):
        _record("tracker.jsonl", {"op": "move", "issue": identifier, "state": state})
        return True

    def comment(self, identifier, body):
        _record("tracker.jsonl", {"op": "comment", "issue": identifier, "body": body[:2000]})
        return "comment"

    def state_of(self, identifier):
        return {"name": "Todo", "type": "active"}

    def attachments(self, identifier):
        return []

    def create_issue(self, title, body, *, labels, state=None, repo=None):
        _record("tracker.jsonl", {"op": "create", "repo": repo, "title": title, "state": state})
        return {"identifier": "MAIN-0", "url": ""}


# -- local Redis ---------------------------------------------------------------------------------
def redis_client() -> redis.Redis:
    sock = os.path.join(ROOT, "redis.sock")
    client = redis.Redis(unix_socket_path=sock, decode_responses=True, socket_timeout=60)
    try:
        client.ping()
        return client
    except (redis.ConnectionError, FileNotFoundError):
        pass
    os.makedirs(ROOT, exist_ok=True)
    sh("redis-server", "--port", "0", "--unixsocket", sock, "--dir", ROOT, "--dbfilename", "e2e.rdb",
       "--save", "5", "1", "--appendonly", "yes", "--daemonize", "yes", "--pidfile", os.path.join(ROOT, "redis.pid"))
    for _ in range(50):
        try:
            client.ping()
            return client
        except (redis.ConnectionError, FileNotFoundError):
            time.sleep(0.1)
    raise SystemExit("local Redis did not start")


# -- commands -------------------------------------------------------------------------------------
def setup(_args) -> None:
    exists = subprocess.run(["gh", "repo", "view", REPO], capture_output=True).returncode == 0
    if not exists:
        sh("gh", "repo", "create", REPO, "--private", "--description", "symphony-merger certification sandbox")
    dev = os.path.join(ROOT, "dev")
    if not os.path.isdir(os.path.join(dev, ".git")):
        os.makedirs(ROOT, exist_ok=True)
        sh("git", "clone", "-q", f"git@github.com:{REPO}.git", dev, check=False)
        os.makedirs(dev, exist_ok=True)
        if not os.path.isdir(os.path.join(dev, ".git")):
            sh("git", "init", "-q", "-b", "main", cwd=dev)
            sh("git", "remote", "add", "origin", f"git@github.com:{REPO}.git", cwd=dev)
    if not sh("git", "ls-remote", "origin", "refs/heads/main", cwd=dev):
        shutil.copytree(os.path.join(HERE, "sandbox"), dev, dirs_exist_ok=True)
        sh("git", "checkout", "-q", "-B", "main", cwd=dev)
        sh("git", "add", "-A", cwd=dev)
        sh("git", "commit", "-q", "-m", "calc: seed the sandbox", cwd=dev)
        # SSH: pushing .github/workflows needs the workflow scope over HTTPS, not over SSH.
        sh("git", "push", "-q", "origin", "main", cwd=dev)
    for name, color in LABELS.items():
        sh("gh", "label", "create", name, "--repo", REPO, "--color", color, "--force")
    print(f"https://github.com/{REPO} ready")


def open_scenario(args) -> None:
    dev = os.path.join(ROOT, "dev")
    sh("git", "fetch", "-q", "origin", cwd=dev)
    for branch, title, body, files in SCENARIOS[args.scenario]:
        sh("git", "checkout", "-q", "-B", branch, "origin/main", cwd=dev)
        for path, text in files.items():
            full = os.path.join(dev, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as fh:
                fh.write(text)
        sh("git", "add", "-A", cwd=dev)
        sh("git", "commit", "-q", "-m", f"calc: {title.split(' ', 1)[1]}", cwd=dev)
        sh("git", "push", "-q", "-f", "origin", branch, cwd=dev)
        url = sh("gh", "pr", "create", "--repo", REPO, "--head", branch, "--base", "main", "--title", title,
                 "--body", body)
        _record("prs.jsonl", {"scenario": args.scenario, "branch": branch, "url": url})
        print(url)


def merger(client, pulse):
    from symphony.entry.merger import build
    from symphony.teamconfig import TeamConfig

    team = TeamConfig(client, NS)
    current = team.get()
    want = {role: MODELS[role] for role in MODELS}
    if current["generation"] == 0 or any(current["models"][r] != m for r, m in want.items()):
        team.publish({"models": want}, expected_generation=current["generation"], actor="e2e",
                     reason="certification models: reviewer opus-5-5, oracle kimi-k3.1, arbiter deepseek-v3.2")
    cfg = SimpleNamespace(default_repo=REPO, tracker_states=STATES, required_labels=("symphony",))
    env = {**os.environ, "SYMPHONY_MERGER_REPO": REPO, "SYMPHONY_MERGER_WORKDIR": os.path.join(ROOT, "work"),
           "SYMPHONY_OMP_BIN": OMP}
    return build(client, NS, cfg, env, pulse, token=gh_token, notify_post=notify_post,
                 tracker=RecordingTracker())


def _met(m, goal: str) -> bool:
    from symphony.merger.github import GitHubError

    kind, _, prs = goal.partition(":")
    for n in (int(x) for x in prs.split(",") if x):
        try:
            pr = m.gh.pr(n)
        except GitHubError as exc:
            if exc.status == 404:  # not opened yet
                return False
            raise
        # merged and finalized: the train's outcome is applied once its record is gone
        if kind == "merged" and (pr["state"] != "merged" or m.store.rec(n) is not None):
            return False
        if kind == "drafted" and not (pr["draft"] and m.store.rec(n) is None and m.store.reviewing().get(n) is None):
            return False
        if kind == "escalated" and "needs-maintainer" not in pr["labels"]:
            return False
        if kind == "reviewed" and (m.store.rec(n) or {}).get("state") != "reviewed":
            return False
    return True


def run(args) -> None:
    from symphony.entry.merger import Pulse, start_loops

    client = redis_client()
    pulse = Pulse()
    m = merger(client, pulse)
    stop = threading.Event()
    start_loops(m, client, NS, pulse, stop, reviewers=args.reviewers, train=not args.no_train, watch=True,
                poll_s=args.poll)
    started = time.monotonic()
    last = None
    try:
        while True:
            snap = m.store.snapshot()
            view = json.dumps({"reviewing": [(r["pr"], r["phase"]) for r in snap["reviewing"]],
                               "parked": [p["pr"] for p in snap["parked"]],
                               "train": [(r["pr"], r["state"]) for r in snap["train"]["records"]],
                               "batch": [x["pr"] for x in snap["batch"].get("members", [])]})
            if view != last:
                print(f"[{time.monotonic() - started:6.0f}s] {view}", flush=True)
                _record("timeline.jsonl", {"view": json.loads(view)})
                last = view
            if args.until and all(_met(m, g) for g in args.until):
                print(f"goals met after {time.monotonic() - started:.0f}s", flush=True)
                return
            if time.monotonic() - started > args.timeout:
                raise SystemExit(f"timeout: goals {args.until} not met")
            time.sleep(10)
    finally:
        stop.set()
        pulse.fire()


def _iso(s: str | None):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def evidence(_args) -> None:
    """Per PR: ready → first merger comment, green CI on the merged head → merged."""
    out = []
    prs = json.loads(sh("gh", "pr", "list", "--repo", REPO, "--state", "all", "--limit", "50", "--json",
                        "number,title,state,isDraft,createdAt,mergedAt,url,headRefOid"))
    for p in sorted(prs, key=lambda p: p["number"]):
        n = p["number"]
        comments = json.loads(sh("gh", "api", f"repos/{REPO}/issues/{n}/comments", "--paginate"))
        ours = [c for c in comments if "**symphony-merger**" in c["body"]]
        events = json.loads(sh("gh", "api", f"repos/{REPO}/issues/{n}/events", "--paginate"))
        readies = [p["createdAt"]] + [e["created_at"] for e in events if e["event"] == "ready_for_review"]
        # The latest ready (or the creation) before the first merger comment: the PR's wait.
        first = ours[0] if ours else None
        ready = max((r for r in readies if not first or r <= first["created_at"]), default=p["createdAt"])
        row = {"pr": n, "title": p["title"], "url": p["url"], "state": p["state"], "draft": p["isDraft"],
               "ready_at": ready, "first_report_at": first["created_at"] if first else None,
               "first_report": first["body"].split("\n", 3)[2][:300] if first and first["body"].count("\n") > 2 else None,
               "comments": [(c["created_at"], c["body"].split("\n", 1)[0]) for c in ours], "merged_at": p["mergedAt"]}
        if first:
            row["ready_to_report_s"] = round((_iso(first["created_at"]) - _iso(ready)).total_seconds())
        if p["mergedAt"]:
            merged = json.loads(sh("gh", "api", f"repos/{REPO}/pulls/{n}", "--jq", "{sha: .head.sha}"))
            runs = json.loads(sh("gh", "api", f"repos/{REPO}/commits/{merged['sha']}/check-runs", "--jq",
                                 "[.check_runs[] | {name, conclusion, completed_at}]"))
            # The green run the merge waited for: on the merged head, finished before the merge
            # (a batch member merges on its top's run instead, so it has none of its own).
            green = max((r["completed_at"] for r in runs if r["conclusion"] == "success" and r["completed_at"]
                         and r["completed_at"] <= p["mergedAt"]), default=None)
            row["merged_head"] = merged["sha"]
            row["green_at"] = green
            if green:
                row["green_to_merged_s"] = round((_iso(p["mergedAt"]) - _iso(green)).total_seconds())
        out.append(row)
    print(json.dumps(out, indent=1))


def stop_redis(_args) -> None:
    redis_client().shutdown(nosave=False)


def main() -> None:
    sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
    global REPO
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=REPO, help="sandbox repository, owner/name (default: $E2E_REPO)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=setup)
    o = sub.add_parser("open")
    o.add_argument("scenario", choices=sorted(SCENARIOS))
    o.set_defaults(fn=open_scenario)
    r = sub.add_parser("run")
    r.add_argument("--until", nargs="*", default=[], help="merged:1,2 drafted:3 reviewed:4 escalated:5")
    r.add_argument("--no-train", action="store_true")
    r.add_argument("--reviewers", type=int, default=2)
    r.add_argument("--poll", type=float, default=10.0)
    r.add_argument("--timeout", type=float, default=2400.0)
    r.set_defaults(fn=run)
    sub.add_parser("evidence").set_defaults(fn=evidence)
    sub.add_parser("stop").set_defaults(fn=stop_redis)
    args = ap.parse_args()
    if not args.repo:
        ap.error("set the sandbox repository with --repo or $E2E_REPO")
    REPO = args.repo
    args.fn(args)


if __name__ == "__main__":
    main()
