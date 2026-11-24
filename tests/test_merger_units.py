"""The merger's parts on their own: store reads and actions, leases, policy, CI verdicts, the
precheck rules, the agent runtime's host-tool contract and intents, and the main CI watch."""

from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from tests.test_merger_support import (
    ENV,
    NS,
    FakeGitHub,
    FakeTracker,
    ScriptedAgents,
    World,
    make_merger,
)

from symphony.merger import agents as ag
from symphony.merger.ci import ci_state
from symphony.merger.mainwatch import watch_once
from symphony.merger.policy import Policy, PolicyError, parse
from symphony.merger.rules import near_hunks, parse_diff, rule_checks
from symphony.merger.store import LeaseLost, MergerStore
from symphony.sessions import EV_INTENT_APPLIED, SessionStore
from symphony.teamconfig import DEFAULT


# -- store -------------------------------------------------------------------------------
def test_snapshot_and_counts_serve_what_the_api_shows(redis_client):
    s = MergerStore(redis_client, NS, "Acme/App")
    s.put_rec({"pr": 7, "state": "train", "head_sha": "a" * 40, "branch": "b", "author": "x", "reviewed_at": 2,
               "pushing": "c" * 40})
    s.put_rec({"pr": 8, "state": "reviewed", "head_sha": "b" * 40, "branch": "c", "author": "y", "reviewed_at": 1})
    s.park({"pr": 9, "head_sha": "d" * 40, "kind": "clean", "parked_at": 5, "comment": "u"})
    s.set_reviewing(10, head_sha="e" * 40, worker="1", phase="review")
    s.add_session(10, "merger:acme:app:10:reviewer:1")
    s.set_queue([{"pr": 11, "kind": "first"}])
    s.save_batch({"members": [{"pr": 7, "reviewed": "a" * 40}]})
    assert s.acquire("train", "train:pod", 60_000)
    snap = s.snapshot()
    assert (snap["repo"], snap["alive"]) == ("acme/app", False)
    assert [r["pr"] for r in snap["train"]["records"]] == [8, 7]
    assert snap["train"]["lease"]["owner"] == "train:pod" and snap["train"]["lease"]["expires_in_ms"] > 0
    assert snap["reviewing"][0]["sessions"] == ["merger:acme:app:10:reviewer:1"]
    assert snap["parked"][0]["pr"] == 9 and snap["queue"] == [{"pr": 11, "kind": "first"}]
    assert snap["batch"]["members"][0]["pr"] == 7
    assert s.counts() == {"reviewing": 1, "train": 1, "parked": 1}
    s.clear_reviewing(10)
    assert s.snapshot()["reviewing"] == [] and not redis_client.exists(f"sym:{NS}:merger:acme/app:reviewing:10:sessions")


def test_two_repositories_in_one_namespace_never_see_each_others_state(redis_client):
    """One merger per repository can share a namespace: the train lease, the PR records (same
    PR numbers), the claims and the operator actions are each repository's own."""
    from symphony.merger.store import registered_repos, wake_target

    app, lib = MergerStore(redis_client, NS, "acme/app"), MergerStore(redis_client, NS, "acme/lib")
    app.register(ttl_ms=60_000)
    lib.register(ttl_ms=60_000)
    assert registered_repos(redis_client, NS) == ["acme/app", "acme/lib"]
    assert app.acquire("train", "train:a", 60_000) and lib.acquire("train", "train:b", 60_000)
    assert (app.holder("train"), lib.holder("train")) == ("train:a", "train:b")
    app.put_rec({"pr": 7, "state": "reviewed", "head_sha": "a" * 40, "branch": "x", "author": "u", "reviewed_at": 1})
    lib.put_rec({"pr": 7, "state": "train", "head_sha": "b" * 40, "branch": "y", "author": "v", "reviewed_at": 2})
    assert app.rec(7)["head_sha"] == "a" * 40 and lib.rec(7)["head_sha"] == "b" * 40
    assert app.claim(7, "review:a:1") and lib.claim(7, "review:b:1")
    app.request_action(7, "bypass", actor="op", reason="hotfix")
    assert [a["action"] for a in app.pending_actions()] == ["bypass"] and lib.pending_actions() == []
    assert app.next_session_id(7, "reviewer") == "merger:acme:app:7:reviewer:1"
    assert lib.next_session_id(7, "reviewer") == "merger:acme:lib:7:reviewer:1"
    assert (app.snapshot()["repo"], lib.snapshot()["train"]["records"][0]["head_sha"]) == ("acme/app", "b" * 40)
    assert app.counts() == {"reviewing": 0, "train": 0, "parked": 0}
    assert lib.counts() == {"reviewing": 0, "train": 1, "parked": 0}
    assert (wake_target("Acme/App"), wake_target("acme/lib")) == ("merger:acme/app", "merger:acme/lib")
    assert app.alive() and lib.alive()
    redis_client.delete(f"sym:{NS}:merger:acme/lib:heartbeat")  # its merger stopped
    assert app.alive() and not lib.alive()
    with pytest.raises(ValueError):
        MergerStore(redis_client, NS, "not-a-repo")


def test_actions_are_durable_and_applied_once(redis_client):
    s = MergerStore(redis_client, NS, "acme/app")
    a = s.request_action(7, "bypass", actor="maintainer", reason="hotfix")
    assert a["action"] == "bypass" and a["id"]
    with pytest.raises(ValueError):
        s.request_action(7, "merge", actor="x")
    with pytest.raises(ValueError):
        s.request_action(0, "requeue", actor="x")
    (pending,) = s.pending_actions()
    assert pending["pr"] == 7 and pending["reason"] == "hotfix"
    s.ack_action(pending["id"])
    assert s.pending_actions() == []
    s.request_action(8, "release", actor="x")
    assert [p["pr"] for p in s.pending_actions()] == [8]


def test_a_lease_has_one_holder_expires_and_fences(redis_client):
    s = MergerStore(redis_client, NS, "acme/app")
    assert s.acquire("train", "a", 100) and not s.acquire("train", "b", 100)
    assert not s.renew("train", "b", 100) and s.renew("train", "a", 100)
    assert not s.release("train", "b") and s.holder("train") == "a"
    time.sleep(0.15)
    assert s.acquire("train", "b", 1000)  # an expired holder frees it
    with s.lease("x", "a", 60_000) as lease:
        lease.check()
        s.force_release("x")
        s.acquire("x", "intruder", 60_000)
        assert not s.renew("x", "a", 60_000)
        lease._lost.set()
        with pytest.raises(LeaseLost):
            lease.check()
    assert s.holder("x") == "intruder"  # releasing never deletes another owner's lease


def test_a_held_claim_is_renewed_and_a_dead_holders_claim_expires(redis_client):
    """Regression (sandbox certification): a killed merger left `pr:3` claimed for 6 hours.
    Claims are now short leases renewed while held."""
    s = MergerStore(redis_client, NS, "acme/app")
    assert s.claim(7, "review:podA:1", ttl_ms=300)
    time.sleep(0.9)  # three TTLs: only the heartbeat keeps it
    assert s.holder("pr:7") == "review:podA:1"
    s.unclaim(7, "review:podA:1")
    assert s.holder("pr:7") is None
    assert s.acquire("pr:8", "review:dead:1", 200)  # a process that died: nobody renews it
    assert not s.claim(8, "review:podB:1")
    time.sleep(0.3)
    assert s.claim(8, "review:podB:1", ttl_ms=300)


def test_a_malformed_record_fails_closed(redis_client):
    s = MergerStore(redis_client, NS, "acme/app")
    redis_client.hset(f"sym:{NS}:merger:acme/app:rec", "7", "{nope")
    with pytest.raises(Exception, match=r"rec\[7\] is malformed"):
        s.recs()
    with pytest.raises(Exception, match="malformed train record"):
        s.put_rec({"pr": 7, "state": "merging"})


# -- policy --------------------------------------------------------------------------------
def test_the_policy_defaults_and_validates():
    p = parse(None)
    assert len(p.lanes) == 3 and p.merge_method == "rebase" and p.batch_max == 4 and p.fix_slots == 3
    assert set(p.blocking_classes) == {"bug", "regression", "security", "spec", "duplicate-owner"}
    p = parse("---\nblocking_classes: [stand-in]\nmerge_method: squash\nbatch_max: 2\nci: {checks: [test]}\n---\nRules.")
    assert p.blocking_classes[-1] == "stand-in" and p.merge_method == "squash" and p.ci_checks == ("test",)
    assert p.rules == "Rules."
    for bad in ("---\nmerge_method: ff\n---\n", "---\nbatch_max: 0\n---\n", "---\nlanes: []\n---\n",
                "---\nmodels: {oracle: {provider: a, model: m}, reviewer: {provider: a, model: m}}\n---\n",
                "---\nblocking_classes: [nit]\n---\n"):
        with pytest.raises(PolicyError):
            parse(bad)


def test_a_published_team_config_wins_over_the_policy_models():
    p = parse("---\nmodels: {reviewer: {provider: openai, model: gpt-x}}\n---\n")
    unpublished = {**DEFAULT, "generation": 0}
    assert p.model("reviewer", unpublished)["model"] == "gpt-x"
    published = {**DEFAULT, "generation": 3}
    assert p.model("reviewer", published) == DEFAULT["models"]["reviewer"]
    assert p.model("oracle", unpublished) == DEFAULT["models"]["oracle"]


# -- CI ---------------------------------------------------------------------------------------
def _c(name, status="completed", conclusion="success", started="1"):
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started, "url": ""}


def test_ci_state_reads_the_latest_run_of_each_check():
    assert ci_state([]) == ("none", [])
    assert ci_state([_c("test"), _c("lint")])[0] == "passed"
    assert ci_state([_c("test", conclusion="failure", started="1"), _c("test", started="2")])[0] == "passed"
    assert ci_state([_c("test"), _c("lint", conclusion="failure")])[0] == "failed"
    assert ci_state([_c("test", status="in_progress", conclusion="")])[0] == "running"
    assert ci_state([_c("test", conclusion="cancelled")])[0] == "none"
    assert ci_state([_c("test", conclusion="skipped", started="9"), _c("test", started="1")])[0] == "passed"
    # with required names, only they decide, and a missing one is still to come
    assert ci_state([_c("lint", conclusion="failure"), _c("test")], ("test",))[0] == "passed"
    assert ci_state([_c("lint")], ("test", "lint"))[0] == "running"


# -- rules ------------------------------------------------------------------------------------
def test_near_hunks_matches_lines_within_reach():
    hunks = parse_diff("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -10,0 +11,2 @@\n+a\n+b\n").hunks
    assert near_hunks("x.py:15", hunks) and near_hunks("./x.py:22", hunks) and near_hunks("x.py", hunks)
    assert not near_hunks("x.py:40", hunks) and not near_hunks("y.py:11", hunks)


def test_cancelling_a_just_pushed_head_waits_for_the_run_github_starts():
    """Regression (sandbox certification): a batch member pushed onto the merged main had its
    CI cancelled before GitHub created the run, so the run burned its minutes to the end."""
    from symphony.merger.ci import cancel_ci

    class Gh:
        polls = 0
        cancelled: list[int] = []

        def workflow_runs(self, sha=None, **_):
            self.polls += 1
            return [{"id": 9, "name": "CI", "status": "queued"}] if self.polls >= 3 else []

        def cancel(self, run_id):
            self.cancelled.append(run_id)

    gh, waits = Gh(), []
    assert cancel_ci(gh, "abc", "CI", wait=waits.append) == [9]
    assert gh.polls == 3 and waits == [5, 5]
    assert cancel_ci(Gh(), "abc", "CI") == []  # without wait: one look, as for an old head


def test_the_precheck_finds_generic_rule_breaks(tmp_path):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"), policy=None)
    head = world.commit("eng-1", {
        "src/keys.py": "KEY = 'AKIAABCDEFGHIJKLMNOP'\nPATH = '/Users/maintainer/x'\n# TODO tidy\n",
        "docs/a.md": "see [b](b.md) and [x](missing.md) and [web](https://x.y)\n",
        "docs/b.md": "b\n",
        "big.bin": "x" * 3000,
    }, "fixup! keys")
    from symphony.merger.git import Mirror

    git = Mirror(str(tmp_path / "m"), world.remote)
    git.ensure()
    git.fetch()
    res = rule_checks(git, Policy(max_file_kib=2, rule_checks=({"name": "lint", "run": "exit 3"},)),
                      str(tmp_path), "origin/main", head, {"body": ""})
    text = "\n".join(res.breaks)
    for expected in ("commit messages", "fixup", "large files", "big.bin", "machine-local paths",
                     "src/keys.py:2", "secrets", "AWS access key", "doc links", "missing.md", "lint: `exit 3` exited 3"):
        assert expected in text
    assert "b.md" not in text.split("doc links", 1)[1].replace("missing.md", "")
    assert res.body and "empty" in res.body[0]
    assert "TODO" in res.leads[0]


def test_the_mirror_follows_a_default_branch_that_is_not_main(tmp_path):
    import subprocess

    from symphony.merger.git import Mirror

    def sh(*args, cwd):
        return subprocess.run(list(args), cwd=cwd, env=ENV, capture_output=True, text=True,
                              check=True).stdout.strip()

    remote, dev = str(tmp_path / "remote.git"), str(tmp_path / "dev")
    sh("git", "init", "-q", "--bare", "-b", "trunk", remote, cwd=str(tmp_path))
    sh("git", "clone", "-q", remote, dev, cwd=str(tmp_path))
    sh("git", "checkout", "-q", "-B", "trunk", cwd=dev)
    sh("git", "commit", "-q", "--allow-empty", "-m", "init", cwd=dev)
    sh("git", "push", "-q", "origin", "trunk", cwd=dev)
    tip = sh("git", "rev-parse", "HEAD", cwd=dev)

    git = Mirror(str(tmp_path / "m"), remote)
    git.ensure()
    git.fetch()
    assert (git.main, git.origin_main, git.main_sha()) == ("trunk", "origin/trunk", tip)

    # A mirror cloned before origin/HEAD was read (or one that lost it) asks the remote.
    git.run("symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    again = Mirror(str(tmp_path / "m"), remote)
    assert again.main == "trunk"
    assert Mirror(str(tmp_path / "m"), remote, main="release").main == "release"


# -- the agent runtime ----------------------------------------------------------------------------
@pytest.fixture
def runner(redis_client, tmp_path):
    agents = ScriptedAgents()
    sessions = SessionStore(redis_client, NS)
    return SimpleNamespace(agents=agents, sessions=sessions, run=ag.AgentRunner(
        sessions, session_root=str(tmp_path), spawn=agents.spawn, silence_s=5, command_timeout_s=5))


def _spec(sid="merger:7:reviewer:1"):
    return ag.AgentSpec(sid=sid, kind="review", role="reviewer", model=DEFAULT["models"]["reviewer"],
                        prompt="review it", cwd="/tmp", tools=("read", "grep"),
                        host_tools=ag.reviewer_tools(("bug", "regression"), False),
                        meta={"identifier": "ENG-1", "title": "t", "repo": "acme/app"})


def test_invalid_tool_calls_are_rejected_and_only_valid_ones_count(runner):
    runner.agents.scripts["reviewer"] = lambda r, p, c: [
        ("submit_finding", {"severity": "fatal", "class": "bug"}),
        ("submit_finding", {"severity": "blocking", "class": "bug", "audit": "a", "location": "x.py:1",
                            "claim": "c", "fix": "f"}),
        ("submit_review", {"strengths": [], "verified": "v"}),
        ("submit_review", {"strengths": [], "verified": "again"}),
    ]
    res = runner.run.run(_spec())
    assert res.status == "completed" and len(res.calls["submit_finding"]) == 1
    assert res.last("submit_review")["verified"] == "v"
    answers = [cmd["isError"] for _, cmd in runner.agents.results]
    # the second submit_review is no error: the review is recorded and the run stops there
    assert answers == [True, False, False, False]
    assert runner.agents.results[3][1]["result"]["content"][0]["text"] == "already recorded: you are done"
    assert "rejected" in runner.agents.results[0][1]["result"]["content"][0]["text"]
    assert runner.agents.results[1][1]["result"]["content"][0]["text"] == "recorded as finding 1"
    meta = runner.sessions.meta("merger:7:reviewer:1")
    assert meta["state"] == "ended" and meta["outcome"] == "completed" and meta["kind"] == "review"
    assert meta["model"] == "amazon-bedrock/global.anthropic.claude-opus-5-5"


def test_a_model_that_keeps_calling_the_terminal_tool_is_stopped_once_it_delivered(runner):
    """Regression (mocked e2e, oracle on kimi-k3.1): told that a second submit_review was an
    error, the model called it again and again, 5000 events, until the silence timeout. Once
    the terminal tool is recorded the run is aborted and counts as completed."""
    runner.agents.scripts["oracle"] = lambda r, p, c: [("submit_review", {"summary": "done"})] * 50
    spec = _spec("merger:acme:app:7:oracle:1")
    spec.kind, spec.role, spec.host_tools = "oracle", "oracle", ag.oracle_tools(("bug",), ["A"], False)
    res = runner.run.run(spec)
    assert res.status == "completed" and res.calls["submit_review"] == [{"summary": "done"}]
    assert len(runner.agents.results) <= 2  # the abort cut the loop off
    assert runner.sessions.meta("merger:acme:app:7:oracle:1")["outcome"] == "completed"


def test_a_turn_without_the_terminal_tool_gets_one_reminder(runner):
    calls = []

    def reviewer(role, prompt, cwd):
        calls.append(prompt)
        return [] if len(calls) == 1 else [("submit_review", {"strengths": [], "verified": "v"})]

    runner.agents.scripts["reviewer"] = reviewer
    assert runner.run.run(_spec()).status == "completed"
    assert "You ended without calling submit_review" in calls[1]
    runner.agents.scripts["reviewer"] = lambda r, p, c: []
    res = runner.run.run(_spec("merger:7:reviewer:2"))
    assert res.status == "failed" and "without calling submit_review" in res.error


def test_steer_and_interrupt_intents_reach_omp_at_once(runner):
    runner.agents.hold = threading.Event()
    runner.agents.hold_roles = {"reviewer"}
    runner.agents.scripts["reviewer"] = lambda r, p, c: [("submit_review", {"strengths": [], "verified": "v"})]
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("res", runner.run.run(_spec())))
    t.start()
    deadline = time.monotonic() + 5
    while (runner.sessions.meta("merger:7:reviewer:1") or {}).get("state") != "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    runner.sessions.send_intent("merger:7:reviewer:1", "steer", actor="op", text="look at x.py")
    runner.sessions.send_intent("merger:7:reviewer:1", "answer", actor="op", request_id="r", text="y")
    runner.sessions.send_intent("merger:7:reviewer:1", "interrupt", actor="op", reason="stop")
    t.join(10)
    assert out["res"].status == "aborted"
    assert runner.agents.steers == [("reviewer", "steer", "look at x.py")]
    applied = [d for _, k, d in runner.sessions.read_events("merger:7:reviewer:1") if k == EV_INTENT_APPLIED]
    assert [(a["kind"], a.get("ignored", False)) for a in applied] == [("steer", False), ("answer", True),
                                                                       ("interrupt", False)]


def test_agents_record_provider_rate_limits(runner, redis_client):
    """internals §10: the usage-limit stage after each turn, and a throttling retry."""
    runner.agents.state = {"usageLimit": {"stage": "low_priority", "resetsAtSec": 1_800_000_000,
                                          "allowanceLeftPercent": 12}}
    runner.agents.retry_errors = ["Bedrock HTTP 429: ThrottlingException: Too many tokens"]
    runner.agents.scripts["reviewer"] = lambda r, p, c: [("submit_review", {"strengths": [], "verified": "v"})]
    before = int(time.time() * 1000)
    assert runner.run.run(_spec()).status == "completed"
    doc = json.loads(redis_client.hget(f"sym:{NS}:ratelimits", "amazon-bedrock/global.anthropic.claude-opus-5-5"))
    assert doc["stage"] == "low_priority" and doc["source"] == "usage_limit"
    assert doc["reset_at"] == 1_800_000_000_000 and (doc["remaining"], doc["limit"]) == (12, 100)
    assert doc["observed_at"] >= before
    runner.agents.state = {}
    runner.run.run(_spec("merger:7:reviewer:2"))
    doc = json.loads(redis_client.hget(f"sym:{NS}:ratelimits", "amazon-bedrock/global.anthropic.claude-opus-5-5"))
    assert (doc["stage"], doc["source"], doc["retry_after_ms"]) == ("throttled", "retry", 2000)


def _linear_tracker(sent: list, **scope):
    import httpx

    from symphony.trackers.linear import LinearTracker

    team = {"id": "T", "key": "ENG", "states": {"nodes": [{"id": "S", "name": "Todo"}]}}

    def answer(request):
        body = json.loads(request.content)
        query, variables = body["query"], body["variables"]
        sent.append((query.split("(", 1)[0].split()[-1], variables))
        if "teams(" in query:
            data = {"teams": {"nodes": [team]}}
        elif "project(" in query:
            data = {"project": {"id": "P", "teams": {"nodes": [team]}}}
        elif "issueLabels(" in query:
            data = {"issueLabels": {"nodes": [{"id": "L1", "name": "symphony", "team": None},
                                              {"id": "LX", "name": "ci", "team": {"id": "OTHER"}},
                                              {"id": "L2", "name": "ci", "team": {"id": "T"}}]}}
        elif "issueLabelCreate" in query:
            data = {"issueLabelCreate": {"success": True, "issueLabel": {"id": "L3", "name": variables["input"]["name"]}}}
        else:
            data = {"issueCreate": {"success": True, "issue": {"id": "I", "identifier": "ENG-9", "url": "u"}}}
        return httpx.Response(200, json={"data": data})

    return LinearTracker("k", active_states=["Todo"], terminal_states=["Done"],
                         client=httpx.Client(transport=httpx.MockTransport(answer)), **scope)


def test_linear_create_reuses_existing_labels_and_creates_the_missing_routing_one():
    sent: list = []
    out = _linear_tracker(sent, project_slug="", team_key="ENG").create_issue(
        "Main CI red", "body", state="Todo", labels=["repo:acme/app", "symphony", "ci"], repo="acme/app")
    assert out == {"identifier": "ENG-9", "url": "u"}
    created = next(v for _, v in sent if "input" in v and "name" in v["input"])
    assert created["input"] == {"name": "repo:acme/app", "teamId": "T"}
    issue = sent[-1][1]["input"]
    assert issue["labelIds"] == ["L3", "L1", "L2"] and issue["stateId"] == "S" and issue["teamId"] == "T"
    assert "projectId" not in issue


def test_linear_create_on_a_project_board_puts_the_ticket_in_the_project():
    sent: list = []
    _linear_tracker(sent, project_slug="proj").create_issue("Main CI red", "body", labels=[])
    issue = sent[-1][1]["input"]
    assert (issue["projectId"], issue["teamId"]) == ("P", "T")


def test_main_watch_refuses_unknown_keys():
    with pytest.raises(PolicyError, match="unknown keys team"):
        parse("---\nmain_watch: {team: ENG}\n---\n")


class _ProvidersCore:
    """The `symphony-providers` Secret as the Kubernetes client returns it (internals §13)."""

    def __init__(self, providers: dict) -> None:
        self.providers = providers
        self.version = 1

    def read_namespaced_secret(self, name, namespace, _request_timeout=None):
        import base64

        doc = json.dumps({"schemaVersion": 1, "providers": self.providers}).encode()
        return SimpleNamespace(data={"providers.json": base64.b64encode(doc).decode()},
                               metadata=SimpleNamespace(resource_version=str(self.version), annotations={}))


def _role_spec(sid: str, provider: str, model: str):
    spec = _spec(sid)
    spec.model = {"provider": provider, "model": model, "thinking": "low"}
    return spec


def test_a_role_with_a_managed_builtin_key_runs_omp_with_it_and_rotation_applies_next_run(
        redis_client, tmp_path):
    """internals §13: each agent run reads `symphony-providers`; a built-in provider's key
    reaches the spawned omp through its env and a private models.yml that names only the env
    variable; a key saved in Admin applies to the next run; the key is never stored elsewhere."""
    from symphony.providers import ProviderRepository

    agents = ScriptedAgents()
    agents.scripts["reviewer"] = lambda r, p, c: [("submit_review", {"strengths": [], "verified": "v"})]
    core = _ProvidersCore({"anthropic": {"builtin": True, "apiKey": "sk-ant-first"}})
    sessions = SessionStore(redis_client, NS)
    runner = ag.AgentRunner(sessions, session_root=str(tmp_path), spawn=agents.spawn, silence_s=5,
                            command_timeout_s=5, providers=ProviderRepository(core, NS).runtime)
    assert runner.run(_role_spec("merger:7:reviewer:1", "anthropic", "claude-opus-5-5")).status == "completed"
    first = agents.spawned[-1]
    assert first.env["SYMPHONY_PROVIDER_API_KEY"] == "sk-ant-first"
    agent_dir = first.env["PI_CODING_AGENT_DIR"]
    assert agent_dir.startswith(str(tmp_path)) and oct(os.stat(agent_dir).st_mode & 0o777) == "0o700"
    assert json.loads(first.models_yml) == {"providers": {"anthropic": {"apiKey": "SYMPHONY_PROVIDER_API_KEY"}}}
    assert "sk-ant-first" not in first.models_yml and "sk-ant-first" not in " ".join(first.argv)
    assert not any("sk-ant-first" in str(v) for k in redis_client.keys("*") if redis_client.type(k) == "string"
                   for v in [redis_client.get(k)])
    core.providers["anthropic"]["apiKey"] = "sk-ant-rotated"  # saved in Admin between two runs
    core.version = 2
    runner.run(_role_spec("merger:7:reviewer:2", "anthropic", "claude-opus-5-5"))
    assert agents.spawned[-1].env["SYMPHONY_PROVIDER_API_KEY"] == "sk-ant-rotated"


def test_a_role_with_no_managed_provider_keeps_the_pods_env(redis_client, tmp_path):
    from symphony.providers import ProviderRepository

    agents = ScriptedAgents()
    agents.scripts["reviewer"] = lambda r, p, c: [("submit_review", {"strengths": [], "verified": "v"})]
    core = _ProvidersCore({"anthropic": {"builtin": True, "apiKey": "sk-ant-first"}})
    runner = ag.AgentRunner(SessionStore(redis_client, NS), session_root=str(tmp_path), spawn=agents.spawn,
                            silence_s=5, command_timeout_s=5, providers=ProviderRepository(core, NS).runtime)
    runner.run(_role_spec("merger:7:reviewer:1", "amazon-bedrock", "global.anthropic.claude-opus-5-5"))
    env = agents.spawned[-1].env
    assert "SYMPHONY_PROVIDER_API_KEY" not in env and env.get("PI_CODING_AGENT_DIR") == os.environ.get(
        "PI_CODING_AGENT_DIR")
    assert agents.spawned[-1].models_yml is None


def test_a_provider_store_outage_fails_the_run_without_spawning_omp(redis_client, tmp_path):
    from symphony.providers import ProviderRepository

    class Down:
        def read_namespaced_secret(self, **_):
            raise RuntimeError("apiserver unreachable")

    agents = ScriptedAgents()
    runner = ag.AgentRunner(SessionStore(redis_client, NS), session_root=str(tmp_path), spawn=agents.spawn,
                            providers=ProviderRepository(Down(), NS).runtime)
    with pytest.raises(ag.AgentFailed, match="no credential for anthropic/claude-opus-5-5: providers_unavailable"):
        runner.run(_role_spec("merger:7:reviewer:1", "anthropic", "claude-opus-5-5"))
    assert agents.spawned == []
    assert SessionStore(redis_client, NS).meta("merger:7:reviewer:1")["state"] == "ended"


def test_validate_covers_the_schema_subset():
    schema = ag.arbiter_tools(["A"])[0].parameters
    assert ag.validate(schema, {"rulings": [{"id": "A", "ruling": "uphold", "reason": "r"}], "summary": "s"}) is None
    assert "must be one of" in ag.validate(schema, {"rulings": [{"id": "B", "ruling": "uphold", "reason": "r"}],
                                                    "summary": "s"})
    assert "unknown fields" in ag.validate(schema, {"rulings": [], "summary": "s", "x": 1})
    assert "must be an array" in ag.validate(schema, {"rulings": {}, "summary": "s"})
    check = ag.arbiter_tools(["A", "B"])[0].check
    assert "missing for: B" in check({"rulings": [{"id": "A", "ruling": "uphold", "reason": "r"}]})


# -- the main CI watch ------------------------------------------------------------------------------
@pytest.fixture
def watch(redis_client, tmp_path):
    os.makedirs(tmp_path / "w")
    world = World(str(tmp_path / "w"), policy="---\nmain_watch: {ticket: true, labels: [ci]}\n---\n")
    gh = FakeGitHub(world)
    tracker = FakeTracker()
    notes: list = []
    m = make_merger(redis_client, tmp_path, world, gh, ScriptedAgents(), tracker=tracker, notes=notes)
    return SimpleNamespace(m=m, gh=gh, tracker=tracker, notes=notes)


def _main_run(gh, rid, conclusion, attempt=1, failed_step=True):
    run = {"id": rid, "name": "CI", "status": "completed", "conclusion": conclusion, "attempt": attempt,
           "sha": f"{rid:040x}", "branch": "main", "event": "push", "url": f"https://ci/{rid}", "title": f"commit {rid}"}
    if conclusion == "failure":
        run["jobs"] = [{"id": rid * 10, "name": "test", "conclusion": "failure", "url": "",
                        "steps": [{"name": "pytest", "conclusion": "failure" if failed_step else "success"}]}]
    gh.main_runs.append(run)
    return run


def test_red_main_opens_one_ticket_with_the_log_and_green_closes_it(watch):
    m, gh = watch.m, watch.gh
    _main_run(gh, 1, "failure")
    _main_run(gh, 2, "success")
    assert watch_once(m, m.policy()) == []  # first pass: only the newest finished run, no replay
    _main_run(gh, 3, "failure")
    _main_run(gh, 4, "failure")
    assert watch_once(m, m.policy()) == ["red 3", "red 4"]
    (ticket,) = watch.tracker.created
    assert ticket["repo"] == "acme/app" and ticket["state"] == "Todo"
    # routable: the daemon dispatches it on `repo:` and the tracker's required labels
    assert ticket["labels"] == ["repo:acme/app", "symphony", "ci"]
    assert "FAILED tests/test_app.py::test_run" in ticket["body"] and "repo: acme/app" in ticket["body"]
    assert [e for e, *_ in watch.notes] == ["main_red", "main_red"]
    assert "ENG-100" in watch.notes[0][1]
    _main_run(gh, 5, "success")
    assert watch_once(m, m.policy()) == ["green 5"]
    assert ("ENG-100", "Done") in watch.tracker.moves and "green again" in watch.tracker.comments[-1][1]
    assert watch.notes[-1][0] == "main_green"
    assert watch_once(m, m.policy()) == []


def test_an_interrupted_main_run_is_held_until_its_rerun(watch):
    m, gh = watch.m, watch.gh
    _main_run(gh, 1, "success")
    watch_once(m, m.policy())
    run = _main_run(gh, 2, "failure", failed_step=False)
    assert watch_once(m, m.policy()) == []
    assert m.store.mainci()["held"][0]["id"] == 2 and watch.tracker.created == []
    run.update(attempt=2, conclusion="success")
    run["jobs"][0]["conclusion"] = "success"
    assert watch_once(m, m.policy()) == []  # rerun green: main was never red
    assert m.store.mainci()["held"] == [] and watch.notes == []


def test_a_wake_reaches_every_idle_loop_at_once(redis_client):
    """The webhook → merger path: one blocking wait on the wake list, fanned out to every loop,
    so no loop sits out its 30 s safety poll."""
    from symphony import wake
    from symphony.entry.merger import Pulse

    pulse = Pulse()
    woke: list[float] = []

    def loop():
        start = time.monotonic()
        pulse.wait(30)
        woke.append(time.monotonic() - start)

    def coordinator():
        if wake.wait(redis_client, NS, wake.MERGER, 30):
            pulse.fire()

    threads = [threading.Thread(target=loop) for _ in range(3)] + [threading.Thread(target=coordinator)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    wake.wake(redis_client, NS, wake.MERGER, "github:check_suite")
    for t in threads:
        t.join(5)
    assert len(woke) == 3 and max(woke) < 1


class _Resp:
    def __init__(self, status: int, body) -> None:
        import json as _json

        self.status_code = status
        self._body = body
        self.content = _json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        return self._body


class _Http:
    """Answers RestGitHub's requests from a script, recording them."""

    def __init__(self, answers: list) -> None:
        self.answers = answers
        self.sent: list[tuple] = []

    def request(self, method, url, json=None, params=None, headers=None):
        self.sent.append((method, url, json, headers["Authorization"]))
        return self.answers.pop(0)


def test_rest_github_maps_graphql_not_found_and_merges_on_the_expected_head():
    from symphony.merger.github import GitHubError, RestGitHub

    http = _Http([
        _Resp(200, {"data": {"repository": {"pullRequest": None}},
                    "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a PullRequest"}]}),
        _Resp(200, {"errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}),
        _Resp(405, {"message": "Pull Request is not mergeable"}),
        _Resp(200, {"merged": True, "sha": "m"}),
    ])
    gh = RestGitHub("acme/app", lambda: "tok", http=http)
    with pytest.raises(GitHubError) as missing:
        gh.pr(3)
    assert missing.value.status == 404  # a PR not opened yet, not an outage
    with pytest.raises(GitHubError) as limited:
        gh.pr(3)
    assert limited.value.status == 0
    assert gh.merge(7, "abc", "rebase") == (False, "Pull Request is not mergeable")
    assert gh.merge(7, "abc", "squash") == (True, "")
    method, url, body, auth = http.sent[-1]
    assert (method, url, body, auth) == ("PUT", "https://api.github.com/repos/acme/app/pulls/7/merge",
                                         {"sha": "abc", "merge_method": "squash"}, "Bearer tok")


# The built-in names omp 18.20.2 accepts in --tools (from its own "Unknown tool" error).
OMP_TOOLS = {
    "read", "bash", "edit", "ast_grep", "ast_edit", "ask", "debug", "ida", "eval", "github", "glob", "grep",
    "find", "lsp", "checkpoint", "rewind", "context_notes", "new_context", "security_scan", "task", "wait",
    "todo", "web_search", "write", "memory_edit", "retain", "recall", "reflect", "learn", "manage_skill",
}


def test_every_role_asks_omp_only_for_tools_it_has():
    """Regression (sandbox certification): the developer asked for `ls`, which omp doesn't have,
    so omp exited 2 before the first frame, and every conflict resolution failed."""
    from symphony.merger.review import REVIEW_TOOLS

    assert set(REVIEW_TOOLS) == {"read", "grep"}  # reviewer, oracle, arbiter: read-only
    assert set(ag.EDIT_TOOLS) <= OMP_TOOLS and "find" not in ag.EDIT_TOOLS


def test_omp_exiting_at_once_reports_its_own_error(tmp_path):
    proc = ag.OmpProcess(["sh", "-c", "echo 'Error: Unknown tool in --tools: ls.' >&2; exit 2"], str(tmp_path),
                         dict(os.environ), str(tmp_path / "s" / "omp.stderr"))
    from symphony.omp_rpc import OmpError

    with pytest.raises(OmpError, match=r"omp exited with 2: Error: Unknown tool in --tools: ls\."):
        proc.read_line(5)
    proc.close()


def test_a_merger_outside_a_cluster_never_reaches_for_a_kubeconfig(monkeypatch):
    """On a host (next to the local mode) the agents use the host env: the user's kubeconfig
    may point at any cluster, and the merger must not read a Secret from it."""
    from symphony import kube
    from symphony.entry.merger import managed_providers

    def no_cluster():
        raise AssertionError("kube.get_clients called outside a cluster")

    monkeypatch.setattr(kube, "get_clients", no_cluster)
    assert managed_providers("ns", {}) is None

    seen = []
    monkeypatch.setattr(kube, "get_clients", lambda: (seen.append("core") or "core", None))
    runtime = managed_providers("ns", {"KUBERNETES_SERVICE_HOST": "10.0.0.1"})
    assert seen == ["core"] and runtime.__self__.core == "core" and runtime.__self__.namespace == "ns"


def test_a_pr_notification_goes_by_dm_to_its_tickets_assignee():
    from symphony.merger import comments as cm
    from symphony.merger.context import Merger

    sent = []
    tracker = FakeTracker()
    tracker.assignees["ENG-12"] = {"name": "Alice", "email": "alice@example.com", "mention": ""}
    m = Merger(store=None, gh=None, git=None, agents=None, team=None, repo="acme/app", states={},
               tracker=tracker, notify_post=lambda *a, **kw: sent.append((a, kw)))
    cm.say(m, "back_to_author", 12, "Fix the export", "bob", "https://example.com/pr/12",
           ticket="ENG-12")
    [((event, _text), kw)] = sent
    assert event == "back_to_author" and kw["github_logins"] == ["bob"]
    assert kw["direct_to"] == ["alice@example.com", "Alice"]
    # No ticket, or a tracker that cannot answer: the channel message still goes, no DM.
    tracker.assignee = lambda identifier: (_ for _ in ()).throw(RuntimeError("tracker down"))
    m.notify("run_error", "boom", tickets=["ENG-12", None])
    assert sent[-1][1]["direct_to"] == []
