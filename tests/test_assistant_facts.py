"""Team facts: the soul file edit, the confirmation, the PR over a fake GitHub, the engine flow."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from tests.test_assistant_engine import FakeAgents, FakeOutbound, eventually

from symphony.assistant import Action, Inbound
from symphony.assistant.confirm import Confirmer, Policy, ProposalStore
from symphony.assistant.engine import Engine, TurnResult
from symphony.assistant.facts import (
    ONLY_MAPPED,
    FactConfirmer,
    FactError,
    FactPRs,
    append_fact,
    no_write_access,
)
from symphony.config import parse_workflow
from symphony.ghapp import NotAuthorizedForRepo
from symphony.merger.agents import validate

USERS = {"slack": {"alice": "U1"}, "discord": {}}


def test_append_fact_adds_the_section_once_and_keeps_the_rest():
    assert append_fact("", "Staging resets on Mondays.") == "## Team facts\n\n- Staging resets on Mondays.\n"
    doc = "---\nname: Maestro\n---\nSign replies.\n\n## Team facts\n\n- One.\n\n## Other\n\nKeep.\n"
    out = append_fact(doc, "Two.")
    assert out == "---\nname: Maestro\n---\nSign replies.\n\n## Team facts\n\n- One.\n- Two.\n\n## Other\n\nKeep.\n"
    out = append_fact(out, "Three.", "Deploys")
    assert "- Two.\n\n### Deploys\n\n- Three.\n\n## Other" in out
    out = append_fact(out, "Four.", "deploys")
    assert "### Deploys\n\n- Three.\n- Four.\n\n## Other" in out
    out = append_fact(out, "Five.")  # no section: before the first sub-heading
    assert "- Two.\n- Five.\n\n### Deploys" in out
    assert append_fact("# Soul\nBe kind.\n", "Six.") == "# Soul\nBe kind.\n\n## Team facts\n\n- Six.\n"


class GitHub:
    """The REST calls FactPRs makes, against one repository (`acme/app`, main at `base`)."""

    def __init__(self, *, deny_writes=False) -> None:
        self.files = {"main": {".symphony/assistant.md": "Sign replies.\n"}}
        self.refs = {"main": "base"}
        self.pulls: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.deny_writes = deny_writes

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        assert request.headers["Authorization"] == "Bearer app-token"
        if self.deny_writes and method in ("POST", "PUT"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if (method, path) == ("GET", "/repos/acme/app"):
            return httpx.Response(200, json={"default_branch": "main"})
        if (method, path) == ("GET", "/repos/acme/app/pulls"):
            head = request.url.params["head"].split(":")[1]
            return httpx.Response(200, json=[p for p in self.pulls if p["head"] == head])
        if (method, path) == ("GET", "/repos/acme/app/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": self.refs["main"]}})
        if (method, path) == ("POST", "/repos/acme/app/git/refs"):
            branch = json.loads(request.content)["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[branch] = "base"
            self.files[branch] = dict(self.files["main"])
            return httpx.Response(201, json={})
        if path == "/repos/acme/app/contents/.symphony/assistant.md":
            ref = request.url.params.get("ref")
            if method == "GET":
                text = self.files.get(ref, {}).get(".symphony/assistant.md")
                if text is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json={"sha": "blob1", "content": base64.b64encode(text.encode()).decode()})
            body = json.loads(request.content)
            assert body["sha"] == "blob1"
            self.files[body["branch"]][".symphony/assistant.md"] = base64.b64decode(body["content"]).decode()
            return httpx.Response(200, json={})
        if (method, path) == ("POST", "/repos/acme/app/pulls"):
            body = json.loads(request.content)
            pr = {"head": body["head"], "number": 7 + len(self.pulls),
                  "html_url": f"https://github.test/acme/app/pull/{7 + len(self.pulls)}"}
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})


DOC = {"text": "Staging resets on Mondays.", "section": None, "repo": "acme/app",
       "path": ".symphony/assistant.md", "asker": "alice", "transport": "slack"}


def test_the_pr_appends_the_fact_on_its_branch_and_a_retry_reuses_it():
    gh = GitHub()
    prs = FactPRs(lambda repo: "app-token", api_url="https://api.github.test",
                  transport=httpx.MockTransport(gh))
    pr = prs.open("f00d", DOC, "alice")
    assert pr == {"url": "https://github.test/acme/app/pull/7", "number": 7,
                  "branch": "symphony/assistant-fact-f00d"}
    assert gh.files["symphony/assistant-fact-f00d"][".symphony/assistant.md"] == (
        "Sign replies.\n\n## Team facts\n\n- Staging resets on Mondays.\n")
    assert gh.files["main"][".symphony/assistant.md"] == "Sign replies.\n"
    assert prs.open("f00d", DOC, "alice") == pr and len(gh.pulls) == 1


def test_a_missing_write_permission_is_reported():
    prs = FactPRs(lambda repo: "app-token", api_url="https://api.github.test",
                  transport=httpx.MockTransport(GitHub(deny_writes=True)))
    with pytest.raises(FactError, match="contents: write and pull requests: write on acme/app"):
        prs.open("f00d", DOC, "alice")

    def refused(repo):
        raise NotAuthorizedForRepo(repo)

    with pytest.raises(FactError, match="contents: write"):
        FactPRs(refused, api_url="https://api.github.test").open("f00d", DOC, "alice")


def click(fid, choice, user="U1"):
    return Action("slack", "C1", "t1", "m1", user, f"fact:{fid}:{choice}")


def confirmer(redis_client, opener, audits):
    return FactConfirmer(ProposalStore(redis_client, "t", kind="fact"), opener=opener,
                         notify_users=lambda: USERS, audit=lambda *a: audits.append(a))


def test_only_a_mapped_click_opens_the_pr_once_and_it_is_audited(redis_client):
    out, audits, opened = FakeOutbound(), [], []
    out.post("C1", "t1", "proposal")
    facts = confirmer(redis_client, lambda fid, doc, login: opened.append(fid) or
                      {"url": "https://github.test/acme/app/pull/7", "number": 7, "branch": "b"}, audits)
    fid = facts.save(DOC)
    assert facts.handle(click(fid, "pr", user="U9"), out) == "unmapped"
    assert out.acks[-1][1] == ONLY_MAPPED and opened == []
    assert facts.handle(click(fid, "pr"), out) == "opened"
    assert out.messages["m1"]["text"].startswith("PR opened by alice: https://github.test/acme/app/pull/7")
    assert out.messages["m1"]["buttons"] == []
    assert facts.handle(click(fid, "pr"), out) == "already_opened" and opened == [fid]
    assert audits == [("alice", f"fact:{fid}", "acme/app#7 https://github.test/acme/app/pull/7")]
    other = facts.save(DOC)
    assert facts.handle(click(other, "dismiss"), out) == "dismissed"
    assert facts.handle(click(other, "pr"), out) == "already_dismissed"


def test_a_failed_pr_keeps_the_buttons_for_a_retry(redis_client):
    out = FakeOutbound()
    out.post("C1", "t1", "proposal")

    def opener(fid, doc, login):
        raise no_write_access(doc["repo"])

    facts = confirmer(redis_client, opener, [])
    fid = facts.save(DOC)
    assert facts.handle(click(fid, "pr"), out) == "failed"
    text = out.messages["m1"]["text"]
    assert "pull requests: write on acme/app" in text and "Click again to retry." in text
    assert [b.id for b in out.messages["m1"]["buttons"]] == [f"fact:{fid}:pr", f"fact:{fid}:dismiss"]
    assert facts.store.get(fid)["status"] == "open"


def test_the_engine_posts_a_proposed_fact_with_its_buttons_and_routes_the_click(redis_client):
    cfg = parse_workflow("---\ntracker: {kind: memory}\nnotify: {users: {slack: {alice: U1}}}\n"
                         "assistant: {enabled: true, slack: {channels: [C1]}, repositories: [acme/app]}\n---\nx")
    out, agents, opened = FakeOutbound(), FakeAgents(), []
    facts = confirmer(redis_client, lambda fid, doc, login: opened.append(doc) or
                      {"url": "https://github.test/acme/app/pull/7", "number": 7, "branch": "b"}, [])
    engine = Engine(outbounds={"slack": out}, agents=agents, policy=lambda: cfg, facts=facts,
                    confirmer=Confirmer(ProposalStore(redis_client, "t"), tracker=lambda: None,
                                        audit=lambda *a: None, policy=lambda: Policy({}, "B", "T", (), ())))

    def script(agent, prompt, on_text):
        tool = agent.tools["propose_team_fact"]
        args = {"text": "Staging resets on Mondays.", "section": "Environments"}
        assert validate(tool.parameters, args) is None and tool.check(args) is None
        assert tool.answer(args, 0).startswith("Proposed.")
        assert tool.answer({"text": "the token is ghp_A1b2C3d4E5f6G7h8I9j0"}, 0).startswith("refused")
        return TurnResult("completed", "I proposed it.")

    agents.scripts = [script]
    engine.on_message(Inbound("slack", "C1", "t1", "m0", "U1", "propose a team fact about staging"))
    eventually(lambda: any(m["buttons"] for m in out.messages.values()))
    mid, posted = next((k, m) for k, m in out.messages.items() if m["buttons"])
    assert "Proposed team fact for acme/app:.symphony/assistant.md (Team facts › Environments)" in posted["text"]
    assert [b.label for b in posted["buttons"]] == ["Open PR", "Dismiss"]
    engine.on_action(Action("slack", "C1", "t1", mid, "U1", posted["buttons"][0].id))
    eventually(lambda: opened)
    assert opened[0]["section"] == "Environments" and opened[0]["asker"] == "alice"
