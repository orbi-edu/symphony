"""The assistant proposes tickets, carries sandbox files to them, and hands a thread to the
planner with "Plan this" (fake transport, scripted fake agent, memory tracker, local sandbox)."""

from __future__ import annotations

import re

import pytest
from tests.test_assistant_engine import WORKFLOW, Clock, FakeAgents, FakeOutbound, eventually

from symphony.assistant import Action, Inbound, share
from symphony.assistant.confirm import (
    ATTACHED_LINE,
    Confirmer,
    Policy,
    ProposalFiles,
    ProposalStore,
)
from symphony.assistant.engine import (
    ASSISTANT_PROPOSE_INSTRUCTIONS,
    ONLY_MAPPED_PLAN,
    PLAN_OFFER_INSTRUCTIONS,
    PROPOSAL_FILES_INSTRUCTIONS,
    Engine,
    TurnResult,
)
from symphony.assistant.share import proposal_attacher
from symphony.assistant.tools import MAX_ATTACHMENTS, NO_ATTACHMENTS, propose_tool
from symphony.config import parse_workflow
from symphony.merger.agents import HostTool, HostToolError, _obj
from symphony.trackers.memory import MemoryTracker
from symphony.workspace import LocalSandbox

SKILL = b"---\nname: brand\n---\nUse the brand colours.\n"
ARCHIVE = b"\x1f\x8b\x08\x00" + bytes(range(256)) * 4  # a .tgz: binary, NULs and all


def _secret() -> str:
    return "gh" + "p_" + "a1B2" * 9  # a GitHub token shape, built so no scanner trips on it


@pytest.fixture
def box(tmp_path):
    """A thread's sandbox: one clone and a scratch root (/tmp in the pod)."""
    clone = tmp_path / "workspace" / "acme" / "skills"
    (clone / "brand").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    sandbox = LocalSandbox(str(tmp_path))
    return {"clone": clone, "scratch": scratch, "files": lambda: (sandbox, [str(clone)])}


@pytest.fixture
def world(redis_client, box):
    """An engine whose assistant proposes with sandbox files, and a memory tracker."""
    tracker = MemoryTracker()
    files = ProposalFiles(redis_client, "t")
    policy = Policy(notify_users={"slack": {"alice": "U1"}}, ticket_state="Backlog",
                    start_state="Todo", ticket_labels=(), required_labels=())
    confirmer = Confirmer(ProposalStore(redis_client, "t"), tracker=lambda: tracker,
                          policy=lambda: policy, audit=lambda *a: None, files=files)
    out, agents = FakeOutbound(), FakeAgents()
    ended: list[str] = []
    sandbox_run = HostTool("sandbox_run", "run", _obj([], {}))

    def propose(key, notes):
        return propose_tool((), proposal_attacher(files=box["files"], login=lambda: notes.login,
                                                  store=files, scratch=[str(box["scratch"])]))

    engine = Engine(outbounds={out.name: out}, agents=agents,
                    policy=lambda: parse_workflow(WORKFLOW % 3), confirmer=confirmer,
                    clock=Clock(), sandbox_tool=lambda key, notes: sandbox_run,
                    propose_tool=propose, plans=ProposalStore(redis_client, "t", kind="plan"),
                    on_session_end=ended.append)
    yield {"engine": engine, "out": out, "agents": agents, "tracker": tracker, "files": files,
           "confirmer": confirmer, "ended": ended, "redis": redis_client}
    engine.stop()


def call(tool: HostTool, args: dict) -> str:
    """A host tool call as the runtime makes it: the check, then the answer."""
    problem = tool.check(args) if tool.check else None
    if problem:
        raise HostToolError(problem)
    return tool.answer(args, 0)


def message(text, thread="t1", user="U1"):
    return Inbound("slack", "C1", thread, f"in-{thread}-{text[:8]}", user, text)


def buttons_of(out, prefix):
    return [(mid, b) for mid in out.order for b in out.messages[mid]["buttons"]
            if b.id.startswith(prefix)]


def click(out, prefix, user="U1"):
    """A click on the first button whose id starts with `prefix` (Create, for a proposal)."""
    mid, button = buttons_of(out, prefix)[0]
    return Action("slack", "C1", "t1", mid, user, button.id)


def file_keys(world) -> list[str]:
    return list(world["redis"].scan_iter(match=world["files"].prefix + "*"))


SKILL_TICKET = {"key": "skill", "title": "Add the brand skill", "repo": "acme/skills",
                "body": "Problem: no brand skill.\nAcceptance: the skill loads.\nVerify: run it."}


def proposing(args: dict):
    """A script: the agent calls propose_tickets with `args`, then the turn ends."""
    def script(agent, prompt, on_text):
        call(agent.tools["propose_tickets"], args)
        return TurnResult("completed", "I drafted the skill; here is the ticket.", args)
    return script


# -- the assistant proposes ------------------------------------------------------------------


def test_ask_mode_proposes_and_only_a_mapped_click_creates(world):
    engine, out, agents, tracker = world["engine"], world["out"], world["agents"], world["tracker"]
    agents.scripts.append(proposing({"summary": "Add a brand skill", "tickets": [dict(SKILL_TICKET)]}))
    engine.on_message(message("create a skill for our brand in acme/skills"))
    eventually(lambda: buttons_of(out, "proposal:"))
    agent = agents.opened[0]
    assert agent.role == "assistant" and "propose_tickets" in agent.tools
    assert ASSISTANT_PROPOSE_INSTRUCTIONS in agent.prompts[0]
    assert PROPOSAL_FILES_INSTRUCTIONS in agent.prompts[0]
    ids = [b.id.rsplit(":", 1)[1] for _, b in buttons_of(out, "proposal:")]
    assert ids == ["create", "start", "dismiss"]
    assert tracker._issues == {}  # nothing before a click

    engine.on_action(click(out, "proposal:", user="U2"))
    eventually(lambda: out.acks)
    assert out.acks[-1][1] == "Only people mapped in notify.users can create tickets."
    assert tracker._issues == {}

    engine.on_action(click(out, "proposal:"))
    eventually(lambda: tracker._issues)
    [issue] = tracker._issues.values()
    assert issue.title == "Add the brand skill" and "repo:acme/skills" in issue.labels
    assert ATTACHED_LINE not in issue.description  # no files, no line


# -- sandbox files ride with the proposal ----------------------------------------------------


def test_two_sandbox_files_are_attached_once_on_create(world, box):
    (box["clone"] / "brand" / "SKILL.md").write_bytes(SKILL)
    (box["scratch"] / "brand.tgz").write_bytes(ARCHIVE)
    engine, out, agents, tracker = world["engine"], world["out"], world["agents"], world["tracker"]
    ticket = {**SKILL_TICKET, "attachments": [
        {"path": "brand/SKILL.md", "title": "The skill"},
        {"path": str(box["scratch"] / "brand.tgz"), "title": "The skill directory"}]}
    agents.scripts.append(proposing({"summary": "Add a brand skill", "tickets": [ticket]}))
    engine.on_message(message("make our brand skill"))
    eventually(lambda: buttons_of(out, "proposal:"))
    proposal_text = out.messages[buttons_of(out, "proposal:")[0][0]]["text"]
    assert "with SKILL.md, brand.tgz" in proposal_text
    assert len(file_keys(world)) == 2 and tracker._issues == {}

    # The first Create fails on the second file: the people retry.
    real = tracker.attach_evidence
    failures = {"left": 1}

    def flaky(issue, **kw):
        if kw["filename"] == "brand.tgz" and failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("upload refused")
        return real(issue, **kw)

    tracker.attach_evidence = flaky
    pid = buttons_of(out, "proposal:")[0][1].id.split(":")[1]
    action = click(out, "proposal:")
    assert world["confirmer"].handle(action, out) == "failed"
    assert world["confirmer"].handle(action, out) == "created"
    assert world["confirmer"].handle(action, out) == "already_created"  # a double click

    [issue] = tracker._issues.values()
    assert issue.description.endswith("\n\n" + ATTACHED_LINE)
    entries = tracker.evidence[issue.id]
    assert [(e["title"], e["kind"]) for e in entries] == [
        ("The skill", "report"), ("The skill directory", "report")]
    assert sorted(f["data"] for f in tracker.files.values()) == sorted([SKILL, ARCHIVE])
    assert file_keys(world) == []  # dropped once the tickets exist
    assert world["confirmer"].store.get(pid)["status"] == "created"


REFUSED = {
    "git": (".git/config", "inside a .git directory"),
    "netrc": ("{scratch}/home/.netrc", "credential file"),
    "token": ("brand/notes.md", "looks like a credential (GitHub token)"),
    "outside": ("{root}/outside.txt", "propose_tickets reads only files under"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_refused_file_fails_the_call_and_stores_nothing(world, box, case):
    clone, scratch = box["clone"], box["scratch"]
    (clone / ".git").mkdir()
    (clone / ".git" / "config").write_text("[remote]\n")
    (scratch / "home").mkdir()
    (scratch / "home" / ".netrc").write_text("machine example.com login a password b\n")
    (clone / "brand" / "notes.md").write_text(f"token = {_secret()}\n")
    (clone.parent.parent.parent / "outside.txt").write_text("outside\n")
    (clone / "brand" / "SKILL.md").write_bytes(SKILL)
    path, reason = REFUSED[case]
    from symphony.assistant.engine import _Thread

    th = _Thread("slack", "C1", "t1", False)
    th.notes.reset("alice", "t1")
    tool = world["engine"]._tools(th, parse_workflow(WORKFLOW % 3), "assistant")
    [propose] = [t for t in tool if t.name == "propose_tickets"]
    ticket = {**SKILL_TICKET, "attachments": [
        {"path": "brand/SKILL.md", "title": "The skill"},
        {"path": path.format(scratch=scratch, root=clone.parent.parent.parent), "title": "x"}]}
    with pytest.raises(HostToolError, match=re.escape(reason)) as info:
        call(propose, {"summary": "s", "tickets": [ticket]})
    assert "Nothing was proposed" in str(info.value)
    assert file_keys(world) == [] and "file" not in ticket["attachments"][0]


def test_attachments_over_the_byte_cap_are_refused(world, box, monkeypatch):
    monkeypatch.setattr(share, "MAX_ATTACHMENT_BYTES", 100)
    (box["clone"] / "a.md").write_bytes(b"a" * 60)
    (box["clone"] / "b.md").write_bytes(b"b" * 60)
    attach = proposal_attacher(files=box["files"], login=lambda: "alice", store=world["files"])
    tool = propose_tool((), attach)
    ticket = {**SKILL_TICKET, "attachments": [{"path": "a.md", "title": "a"},
                                              {"path": "b.md", "title": "b"}]}
    with pytest.raises(HostToolError, match="add up to more than"):
        call(tool, {"summary": "s", "tickets": [ticket]})
    assert file_keys(world) == []


def test_attachment_limits_and_who_may_attach(world, box):
    (box["clone"] / "a.md").write_bytes(b"a")
    who = {"login": "alice"}
    tool = propose_tool((), proposal_attacher(files=box["files"], login=lambda: who["login"],
                                              store=world["files"]))
    many = {**SKILL_TICKET, "attachments": [{"path": "a.md", "title": "a"}] * (MAX_ATTACHMENTS + 1)}
    assert "at most 10 files" in tool.check({"summary": "s", "tickets": [many]})
    one = {**SKILL_TICKET, "attachments": [{"path": "a.md", "title": "a"}]}
    who["login"] = None  # an unmapped person asks in the thread
    with pytest.raises(HostToolError, match="notify.users"):
        call(tool, {"summary": "s", "tickets": [dict(one)]})
    without_sandbox = propose_tool(())
    assert without_sandbox.check({"summary": "s", "tickets": [one]}) == NO_ATTACHMENTS
    assert without_sandbox.check({"summary": "s", "tickets": [dict(SKILL_TICKET)]}) is None


# -- Plan this -------------------------------------------------------------------------------


def offering(summary: str):
    def script(agent, prompt, on_text):
        call(agent.tools["offer_plan"], {"summary": summary})
        return TurnResult("completed", "This is several tickets of work: a plan fits.")
    return script


def test_plan_this_hands_the_thread_to_the_planner(world):
    engine, out, agents = world["engine"], world["out"], world["agents"]
    agents.scripts.append(offering("Rate limits for the public API of acme/app"))
    engine.on_message(message("add rate limits to the public API"))
    eventually(lambda: buttons_of(out, "plan:"))
    assistant = agents.opened[0]
    assert "offer_plan" in assistant.tools and PLAN_OFFER_INSTRUCTIONS in assistant.prompts[0]
    [(mid, button)] = buttons_of(out, "plan:")
    assert button.label == "Plan this"
    assert out.messages[mid]["text"] == "This is several tickets of work: a plan fits."

    engine.on_action(click(out, "plan:", user="U2"))
    eventually(lambda: out.acks)
    assert out.acks[-1][1] == ONLY_MAPPED_PLAN
    assert len(agents.opened) == 1 and buttons_of(out, "plan:")

    action = click(out, "plan:")
    engine.on_action(action)
    eventually(lambda: len(agents.opened) == 2 and agents.opened[1].prompts)
    planner = agents.opened[1]
    assert planner.role == "planner" and assistant.closed == "completed"
    prompt = planner.prompts[0]
    assert "Earlier messages in this thread" in prompt
    assert "add rate limits to the public API" in prompt  # the thread's context
    assert "This is several tickets of work" in prompt
    assert prompt.endswith("alice asks:\nPlan this: Rate limits for the public API of acme/app"
                           "\n\nThe request it came from: add rate limits to the public API")
    assert "propose_tickets" in planner.tools and "offer_plan" not in planner.tools
    assert out.messages[mid]["buttons"] == []  # the button is gone
    assert "alice handed this thread to the planner." in out.texts("t1")
    assert world["ended"] == []  # the sandbox stays with the thread

    engine.on_action(action)  # a double click
    eventually(lambda: out.acks[-1][1] == "alice already handed this to the planner.")
    engine.on_message(message("make the first ticket smaller"))
    eventually(lambda: len(planner.prompts) == 2)
    assert len(agents.opened) == 2  # a follow-up stays with the planner


def test_a_planner_or_researcher_gets_no_plan_offer(world):
    engine, agents = world["engine"], world["agents"]
    engine.on_message(message("plan the importer", thread="p"))
    engine.on_message(message("research retries", thread="r"))
    eventually(lambda: len(agents.opened) == 2 and all(a.prompts for a in agents.opened))
    for agent in agents.opened:
        assert "offer_plan" not in agent.tools
        assert PLAN_OFFER_INSTRUCTIONS not in agent.prompts[0]
        assert ASSISTANT_PROPOSE_INSTRUCTIONS not in agent.prompts[0]
    roles = {a.role: a for a in agents.opened}
    assert "propose_tickets" in roles["planner"].tools
    assert "propose_tickets" not in roles["researcher"].tools


def test_without_plans_the_button_is_never_offered(redis_client):
    out, agents = FakeOutbound(), FakeAgents()
    engine = Engine(outbounds={out.name: out}, agents=agents,
                    policy=lambda: parse_workflow(WORKFLOW % 3), clock=Clock(),
                    confirmer=Confirmer(ProposalStore(redis_client, "t"), tracker=lambda: None,
                                        audit=lambda *a: None,
                                        policy=lambda: Policy({}, "B", "T", (), ())))
    engine.on_message(message("hello"))
    eventually(lambda: agents.opened and agents.opened[0].prompts)
    assert "offer_plan" not in agents.opened[0].tools
    engine.stop()
