"""`share_file`: a sandbox file posted in the thread (Slack and Discord mocks), the refusals, and
the confirmed attachment to a ticket."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading

import httpx
import pytest
from scripts.mocks.discord import server as discord
from scripts.mocks.slack import server as slack
from tests.test_assistant_engine import WORKFLOW, Clock, FakeAgents, FakeOutbound, eventually

from symphony.admin_commands import AdminStore
from symphony.assistant import Action, FileTooLarge, Inbound, Ref, share
from symphony.assistant.confirm import Confirmer, Policy, ProposalStore
from symphony.assistant.discord_gateway import DiscordGatewayTransport
from symphony.assistant.engine import SHARE_FILE_INSTRUCTIONS, Engine
from symphony.assistant.sandbox import NEEDS_MAPPING
from symphony.assistant.share import (
    EVIDENCE_AUDIT,
    ONLY_MAPPED,
    SHARE_AUDIT,
    Evidence,
    share_file_tool,
)
from symphony.assistant.slack_socket import SlackSocketTransport
from symphony.config import parse_workflow
from symphony.merger.agents import HostTool, HostToolError, _obj
from symphony.trackers.memory import MemoryTracker
from symphony.workspace import LocalSandbox

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00" + bytes(range(256)) * 64  # NULs and all
THREAD = "1700000001.000001"


class Out:
    """An Outbound that records uploads, posts and click answers."""

    name = "slack"
    message_limit = 4000

    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.posts: list[dict] = []
        self.updates: list[tuple[Ref, str, list]] = []
        self.acks: list[str | None] = []

    def upload(self, channel, thread, name, data, *, title, comment=""):
        self.uploads.append({"channel": channel, "thread": thread, "name": name, "data": data,
                             "title": title, "comment": comment})
        return "https://example.com/files/1"

    def post(self, channel, thread, text, buttons=()):
        self.posts.append({"channel": channel, "thread": thread, "text": text,
                           "buttons": list(buttons)})
        return Ref(channel, f"m{len(self.posts)}")

    def update(self, ref, text, buttons=()):
        self.updates.append((ref, text, list(buttons)))

    def ack_action(self, action, text=None):
        self.acks.append(text)


@pytest.fixture
def box(tmp_path):
    """A thread's sandbox: one clone, a scratch root (/tmp in the pod), a file outside both."""
    clone = tmp_path / "workspace" / "acme" / "app"
    (clone / "out").mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (tmp_path / "outside.txt").write_text("outside\n")
    sandbox = LocalSandbox(str(tmp_path))
    return {"clone": clone, "scratch": scratch, "root": tmp_path,
            "files": lambda: (sandbox, [str(clone)])}


def tool(box, out, *, login="alice", evidence=None, audit=None):
    who = {"login": login}
    t = share_file_tool(files=box["files"], login=lambda: who["login"], out=out, channel="C1",
                        thread=THREAD, audit=audit or (lambda *a: None), evidence=evidence,
                        scratch=[str(box["scratch"])])
    return t, who


def call(t, **args):
    problem = t.check(args)
    if problem:
        raise HostToolError(problem)
    return json.loads(t.answer(args, 0))


# -- Slack: the three calls, in the thread, the bytes as they are ----------------------------


@pytest.fixture
def slack_base(tmp_path):
    srv = slack.serve(0, tmp_path / "slack-state")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_an_mp4_from_the_sandbox_reaches_the_slack_thread(box, slack_base, redis_client):
    (box["clone"] / "out" / "anim.mp4").write_bytes(MP4)
    transport = SlackSocketTransport(slack.DEFAULT_APP_TOKEN, slack.DEFAULT_TOKEN,
                                     api_url=f"{slack_base}/api")
    admin = AdminStore(redis_client, "t")
    channel = slack.DEFAULT_CHANNEL["id"]
    t = share_file_tool(files=box["files"], login=lambda: "alice", out=transport, channel=channel,
                        thread=THREAD, scratch=[str(box["scratch"])],
                        audit=lambda actor, target, reason: admin.audit_event(
                            actor=actor, action=SHARE_AUDIT, target=target, reason=reason))

    result = call(t, path="out/anim.mp4", title="Animation", comment="The render you asked for")

    assert result["ok"] is True and result["name"] == "anim.mp4" and result["size"] == len(MP4)
    assert result["permalink"].endswith("/anim.mp4")
    state = httpx.get(f"{slack_base}/_mock/uploads").json()
    [upload] = state["uploads"]
    assert [c["method"] for c in state["calls"]] == [
        "files.getUploadURLExternal", "upload", "files.completeUploadExternal"]
    assert {c["file_id"] for c in state["calls"]} == {upload["file_id"]}
    assert upload | {"file_id": None} == {
        "file_id": None, "filename": "anim.mp4", "length": len(MP4), "received": len(MP4),
        "completed": True, "channel": channel, "thread_ts": THREAD, "title": "Animation",
        "initial_comment": "The render you asked for"}
    got = httpx.get(f"{slack_base}/files/{upload['file_id']}",
                    headers={"Authorization": f"Bearer {slack.DEFAULT_TOKEN}"})
    assert got.content == MP4
    [event] = [e for e in admin.audit()["items"] if e["action"] == SHARE_AUDIT]
    assert event["actor"] == "alice" and event["target"].startswith("anim.mp4 (")
    assert event["reason"] == "Animation"


def test_a_slack_error_is_a_tool_error(box, slack_base):
    (box["clone"] / "a.png").write_bytes(b"\x89PNG")
    transport = SlackSocketTransport(slack.DEFAULT_APP_TOKEN, slack.DEFAULT_TOKEN,
                                     api_url=f"{slack_base}/api")
    t = share_file_tool(files=box["files"], login=lambda: "alice", out=transport,
                        channel="C0NOSUCH", thread=THREAD, audit=lambda *a: None,
                        scratch=[str(box["scratch"])])
    with pytest.raises(HostToolError, match="channel_not_found"):
        call(t, path="a.png", title="Shot")


# -- refusals --------------------------------------------------------------------------------


def _secret() -> str:
    return "gh" + "p_" + "a1B2" * 9  # a GitHub token shape, built so no scanner trips on it


REFUSALS = {
    "outside": ("{root}/outside.txt", "resolves outside"),
    "symlink": ("link.txt", "resolves outside"),
    "git": (".git/config", "inside a .git directory"),
    "credentials": ("{scratch}/home/.git-credentials", "credential file"),
    "secret": ("notes.txt", "looks like a credential (GitHub token)"),
    "large": ("big.mp4", "over share_file's limit of 25 MB"),
    "missing": ("nope.mp4", "no such file"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_refused_files_are_never_posted(box, case):
    clone, scratch = box["clone"], box["scratch"]
    os.symlink(box["root"] / "outside.txt", clone / "link.txt")
    (clone / ".git").mkdir()
    (clone / ".git" / "config").write_text("[remote]\n")
    (scratch / "home").mkdir()
    (scratch / "home" / ".git-credentials").write_text("https://x:y@example.com\n")
    (clone / "notes.txt").write_text(f"token = {_secret()}\n")
    with open(clone / "big.mp4", "wb") as fh:
        fh.truncate(26 * 1024 * 1024)
    path, message = REFUSALS[case]
    out = Out()
    t, _ = tool(box, out)
    with pytest.raises(HostToolError, match=re.escape(message)) as info:
        call(t, path=path.format(root=box["root"], scratch=scratch), title="x")
    assert out.uploads == []
    if case == "large":
        assert "720p" in str(info.value) and "CRF" in str(info.value)


def test_a_relative_path_needs_a_single_clone(box):
    sandbox, clone = box["files"]()
    out = Out()
    t = share_file_tool(files=lambda: (sandbox, [*clone, "/workspace/acme/lib"]), login=lambda: "a",
                        out=out, channel="C1", thread=THREAD, audit=lambda *a: None)
    with pytest.raises(HostToolError, match="absolute path"):
        call(t, path="out/anim.mp4", title="x")


def test_a_file_over_the_chat_limit_says_how_to_shrink_it(box):
    (box["clone"] / "a.mp4").write_bytes(MP4)

    class Small(Out):
        def upload(self, *a, **kw):
            raise FileTooLarge(len(MP4), 1024)

    t, _ = tool(box, Small())
    with pytest.raises(HostToolError, match="720p"):
        call(t, path="a.mp4", title="x")


# -- who may use it --------------------------------------------------------------------------


def test_an_unmapped_asker_is_refused_like_sandbox_run(box):
    (box["clone"] / "a.png").write_bytes(b"\x89PNG")
    out = Out()
    t, who = tool(box, out)
    assert t.check({"path": "a.png", "title": "x"}) is None
    who["login"] = None  # another person speaks in the same thread
    assert t.check({"path": "a.png", "title": "x"}) == NEEDS_MAPPING
    with pytest.raises(HostToolError, match="notify.users"):
        t.answer({"path": "a.png", "title": "x"}, 0)
    assert out.uploads == []


@pytest.mark.parametrize("sandbox_on", [True, False])
def test_share_file_is_offered_only_with_the_sandbox(redis_client, box, sandbox_on):
    out, agents = FakeOutbound(), FakeAgents()
    sandbox_run = HostTool("sandbox_run", "run", _obj([], {}))
    engine = Engine(
        outbounds={out.name: out}, agents=agents, policy=lambda: parse_workflow(WORKFLOW % 3),
        confirmer=Confirmer(ProposalStore(redis_client, "t"), tracker=lambda: None,
                            audit=lambda *a: None, policy=lambda: Policy({}, "B", "T", (), ())),
        clock=Clock(),
        sandbox_tool=lambda key, notes: sandbox_run if sandbox_on else None,
        share_file_tool=lambda key, notes, o, channel, thread: share_file_tool(
            files=box["files"], login=lambda: notes.login, out=o, channel=channel,
            thread=thread, audit=lambda *a: None))
    engine.on_message(Inbound("slack", "C1", "t1", "in1", "U1", "make me a video"))
    eventually(lambda: agents.opened and agents.opened[0].prompts)
    agent = agents.opened[0]
    assert ("share_file" in agent.tools) is sandbox_on
    assert (SHARE_FILE_INSTRUCTIONS in agent.prompts[0]) is sandbox_on
    engine.stop()


# -- Discord ---------------------------------------------------------------------------------


@pytest.fixture
def discord_base(tmp_path):
    srv = discord.serve(0, tmp_path / "discord-state")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_discord_posts_the_file_as_multipart_without_mentions(discord_base):
    channel = discord.DEFAULT_CHANNEL["id"]
    transport = DiscordGatewayTransport(discord.DEFAULT_TOKEN, api_url=f"{discord_base}/api/v10")
    root = transport.post(channel, "", "Rendering…")  # a reply chain in a channel
    assert transport.upload(channel, root.message_id, "anim.mp4", MP4, title="Animation",
                            comment="Here it is @everyone") == ""
    posted = httpx.get(f"{discord_base}/_mock/messages").json()["messages"][-1]
    assert posted["allowed_mentions"] == {"parse": []} and posted["mention_everyone"] is False
    assert posted["files"] == [{"field": "files[0]", "filename": "anim.mp4", "size": len(MP4),
                                "sha256": hashlib.sha256(MP4).hexdigest()}]
    assert posted["body"]["attachments"] == [{"id": "0", "filename": "anim.mp4", "title": "Animation"}]
    assert posted["body"]["message_reference"]["message_id"] == root.message_id
    assert posted["content"] == "Here it is @everyone"
    [attachment] = posted["message"]["attachments"]
    assert attachment["size"] == len(MP4) and attachment["filename"] == "anim.mp4"


def test_discord_over_the_upload_limit_is_file_too_large(discord_base):
    httpx.post(f"{discord_base}/_mock/upload_limit", json={"bytes": 100})
    transport = DiscordGatewayTransport(discord.DEFAULT_TOKEN, api_url=f"{discord_base}/api/v10")
    with pytest.raises(FileTooLarge):
        transport.upload(discord.DEFAULT_CHANNEL["id"], "", "anim.mp4", MP4, title="Animation")
    assert httpx.get(f"{discord_base}/_mock/messages").json()["messages"] == []


# -- ticket attachments ----------------------------------------------------------------------


@pytest.fixture
def evidence(redis_client):
    tracker = MemoryTracker()
    ticket = tracker.create_issue("Login page", "b", labels=[])["identifier"]
    admin = AdminStore(redis_client, "t")
    store = ProposalStore(redis_client, "t", kind="evidence")
    ev = Evidence(store, tracker=lambda: tracker, notify_users=lambda: {"slack": {"alice": "U1"}},
                  audit=lambda actor, target, reason: admin.audit_event(
                      actor=actor, action=EVIDENCE_AUDIT, target=target, reason=reason))
    return {"ev": ev, "tracker": tracker, "ticket": ticket, "admin": admin, "store": store}


def attached(tracker) -> list[dict]:
    return [e for entries in tracker.evidence.values() for e in entries]


def _propose(box, evidence, out):
    (box["clone"] / "shot.png").write_bytes(b"\x89PNG\r\n" + bytes(range(256)))
    t, _ = tool(box, out, evidence=evidence["ev"])
    result = call(t, path="shot.png", title="Login page", ticket=evidence["ticket"])
    [post] = out.posts
    pid = post["buttons"][0].id.split(":")[1]
    return result, post, pid


def test_ticket_proposes_and_a_mapped_click_attaches_once(box, evidence):
    out = Out()
    result, post, pid = _propose(box, evidence, out)
    assert result["ok"] and result["ticket"].startswith("proposed")
    assert len(out.uploads) == 1  # the file is in the thread at once
    assert [b.id for b in post["buttons"]] == [f"evidence:{pid}:attach", f"evidence:{pid}:dismiss"]
    assert evidence["ticket"] in post["text"] and "Nothing is attached" in post["text"]
    assert attached(evidence["tracker"]) == []  # nothing before a click

    action = Action("slack", "C1", THREAD, "m1", "U1", f"evidence:{pid}:attach")
    assert evidence["ev"].handle(action, out) == "attached"
    [entry] = attached(evidence["tracker"])
    assert entry["title"] == "Login page" and entry["kind"] == "screenshot"
    ref, text, buttons = out.updates[-1]
    assert ref == Ref("C1", "m1") and buttons == [] and "Attached by alice" in text
    [event] = [e for e in evidence["admin"].audit()["items"] if e["action"] == EVIDENCE_AUDIT]
    assert event["actor"] == "alice" and event["target"] == evidence["ticket"]
    assert event["reason"].startswith("shot.png (")

    assert evidence["ev"].handle(action, out) == "already_attached"  # a double click
    assert len(attached(evidence["tracker"])) == 1
    assert out.acks[-1] == "This file is already attached."
    assert evidence["store"].client.get(evidence["store"].prefix + pid + ":data") is None


def test_a_click_while_another_attaches_is_busy(box, evidence):
    out = Out()
    _, _, pid = _propose(box, evidence, out)
    evidence["store"].claim(pid)
    action = Action("slack", "C1", THREAD, "m1", "U1", f"evidence:{pid}:attach")
    assert evidence["ev"].handle(action, out) == "busy"
    assert attached(evidence["tracker"]) == []


def test_an_unmapped_click_is_refused(box, evidence):
    out = Out()
    _, _, pid = _propose(box, evidence, out)
    action = Action("slack", "C1", THREAD, "m1", "U2", f"evidence:{pid}:attach")
    assert evidence["ev"].handle(action, out) == "unmapped"
    assert out.acks == [ONLY_MAPPED] and attached(evidence["tracker"]) == []
    assert evidence["store"].get(pid)["status"] == "open"


def test_dismiss_attaches_nothing(box, evidence):
    out = Out()
    _, _, pid = _propose(box, evidence, out)
    action = Action("slack", "C1", THREAD, "m1", "U1", f"evidence:{pid}:dismiss")
    assert evidence["ev"].handle(action, out) == "dismissed"
    assert attached(evidence["tracker"]) == []
    assert evidence["ev"].handle(
        Action("slack", "C1", THREAD, "m1", "U1", f"evidence:{pid}:attach"), out) == "already_dismissed"


def test_a_file_over_the_evidence_cap_is_shared_but_not_proposed(box, evidence, monkeypatch):
    monkeypatch.setattr(share, "EVIDENCE_MAX_BYTES", 100)
    (box["clone"] / "shot.png").write_bytes(b"\x89PNG" + bytes(200))
    out = Out()
    t, _ = tool(box, out, evidence=evidence["ev"])
    result = call(t, path="shot.png", title="Login page", ticket=evidence["ticket"])
    assert result["ok"] and len(out.uploads) == 1
    assert "only files up to" in result["ticket_error"] and "ticket" not in result
    assert out.posts == []


def test_an_unknown_ticket_is_not_proposed(box, evidence):
    (box["clone"] / "shot.png").write_bytes(b"\x89PNG")
    out = Out()
    t, _ = tool(box, out, evidence=evidence["ev"])
    result = call(t, path="shot.png", title="Login page", ticket="ENG-999")
    assert "no ticket ENG-999" in result["ticket_error"] and out.posts == []
