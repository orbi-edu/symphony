"""Image attachments: Slack and Discord files reach the agent as image blocks."""

from __future__ import annotations

import base64
import threading
import time

import httpx
import pytest
from scripts.mocks.slack import server as slack

from symphony.assistant import File, Inbound
from symphony.assistant.engine import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_TURN,
    TurnResult,
)
from symphony.config import parse_workflow

# ---------------------------------------------------------------------------
# Helpers (reused from test_assistant_engine.py pattern)
# ---------------------------------------------------------------------------

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGNgYPgPAAEDAQAIicLsAAAABElFTkSuQmCC"
)


def eventually(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition never held")


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeOutbound:
    def __init__(self, name="slack", message_limit=40_000) -> None:
        self.name = name
        self.message_limit = message_limit
        self.messages: dict[str, dict] = {}
        self.updates: list[tuple[str, str]] = []
        self._n = 0

    def post(self, channel, thread, text, buttons=()):
        self._n += 1
        mid = f"out{self._n}"
        self.messages[mid] = {"channel": channel, "thread": thread, "text": text}
        from symphony.assistant import Ref
        return Ref(channel, mid)

    def update(self, ref, text, buttons=()):
        self.updates.append((ref.message_id, text))
        self.messages[ref.message_id]["text"] = text

    def ephemeral(self, channel, thread, user_id, text):
        pass

    def ack_action(self, action, text=None):
        pass

    def history(self, channel, thread, limit):
        return []

    def texts(self, thread=None):
        return [m["text"] for m in self.messages.values()
                if thread is None or m["thread"] == thread]

    def download(self, file):
        """Always returns a 1-px PNG for testing."""
        return PNG_1PX


class FakeAgent:
    def __init__(self, factory, sid, role) -> None:
        self.factory, self.sid, self.role = factory, sid, role
        self.prompts: list[str] = []
        self.last_images: tuple[dict, ...] = ()
        self.closed: str | None = None

    def ask(self, prompt, on_text, timeout_s, images=()):
        self.prompts.append(prompt)
        self.last_images = images
        script = self.factory.scripts.pop(0) if self.factory.scripts else None
        if script is None:
            return TurnResult("completed", f"answer to: {prompt.splitlines()[-1]}")
        return script(self, prompt, on_text)

    def close(self, outcome):
        self.closed = outcome


class FakeAgents:
    def __init__(self) -> None:
        self.opened: list[FakeAgent] = []
        self.metas: list[dict] = []
        self.intents: list = []
        self.scripts: list = []
        self.lock = threading.Lock()

    def open(self, sid, role, meta, on_intent, tools=()):
        agent = FakeAgent(self, sid, role)
        agent.tools = {t.name: t for t in tools}
        with self.lock:
            self.opened.append(agent)
            self.metas.append(meta)
            self.intents.append(on_intent)
        return agent

    def commits(self):
        return {"acme/app": "abc123"}


WORKFLOW = """---
tracker: {kind: memory}
assistant:
  enabled: true
  max_concurrent: 3
  slack: {channels: [C1]}
  discord: {channels: ["1100000000000000001"]}
---
x
"""


def build(redis_client, *, out=None, clock=None):
    from symphony.assistant.confirm import Confirmer, Policy, ProposalStore
    from symphony.assistant.engine import Engine

    cfg = parse_workflow(WORKFLOW)
    out = out or FakeOutbound()
    clock = clock or Clock()
    agents = FakeAgents()
    store = ProposalStore(redis_client, "t")
    confirmer = Confirmer(store, tracker=lambda: None, audit=lambda *a: None,
                          policy=lambda: Policy({}, "Backlog", "Todo", (), ()))
    engine = Engine(outbounds={out.name: out}, agents=agents, policy=lambda: cfg,
                    confirmer=confirmer, clock=clock, idle_s=1800, stream_interval_s=1.5)
    return engine, out, agents


def msg(text, files=(), thread="t1", channel="C1", user="U1", n=[0], **kw):  # noqa: B006
    n[0] += 1
    return Inbound("slack", channel, thread, f"in{n[0]}", user, text, files=files, **kw)


def answered(out, thread, count=1):
    return lambda: sum(1 for t in out.texts(thread) if t.startswith("answer to")) >= count


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_a_png_reaches_the_agent_as_an_image_block(redis_client):
    """A Slack message with a PNG file is downloaded and sent as an image block."""
    engine, out, agents = build(redis_client)
    files = (File(name="screen.png", url="https://files.slack.com/files/F1/screen.png",
                  mimetype="image/png", size=len(PNG_1PX)),)
    engine.on_message(msg("look at this", files=files))
    eventually(answered(out, "t1"))

    agent = agents.opened[0]
    assert len(agent.last_images) == 1
    img = agent.last_images[0]
    assert img["type"] == "image"
    assert img["mimeType"] == "image/png"
    # The data decodes to the PNG bytes
    assert base64.b64decode(img["data"]) == PNG_1PX
    # The prompt names the attached file
    assert "Attached images: screen.png" in agent.prompts[-1]


def test_missing_scope_gives_a_note_and_no_crash(redis_client):
    """When the transport download fails (e.g. missing_scope), the file goes to
    'not read' and the agent gets no image."""
    from symphony.assistant.slack_socket import SlackError

    class FailOutbound(FakeOutbound):
        def download(self, file):
            raise SlackError("files.download", "missing_scope")

    engine, out, agents = build(redis_client, out=FailOutbound())
    files = (File(name="shot.png", url="https://files.slack.com/files/F2/shot.png",
                  mimetype="image/png", size=100),)
    engine.on_message(msg("check this", files=files))
    eventually(answered(out, "t1"))

    agent = agents.opened[0]
    assert len(agent.last_images) == 0
    assert "Attached files, not read: shot.png" in agent.prompts[-1]


def test_oversize_and_non_image_files_are_not_sent(redis_client):
    """Files that are too big or not images go to 'not read' with no download."""
    engine, out, agents = build(redis_client)
    files = (
        File(name="big.png", url="https://x/big.png",
             mimetype="image/png", size=MAX_IMAGE_BYTES + 1),
        File(name="data.csv", url="https://x/data.csv",
             mimetype="text/csv", size=100),
        File(name="ok.jpeg", url="https://x/ok.jpeg",
             mimetype="image/jpeg", size=200),
    )
    engine.on_message(msg("here are files", files=files))
    eventually(answered(out, "t1"))

    agent = agents.opened[0]
    assert len(agent.last_images) == 1  # only ok.jpeg
    assert agent.last_images[0]["mimeType"] == "image/jpeg"
    prompt = agent.prompts[-1]
    assert "Attached images: ok.jpeg" in prompt
    assert "Attached files, not read: big.png, data.csv" in prompt


def test_per_turn_cap_holds(redis_client):
    """At most MAX_IMAGES_PER_TURN images are sent; the rest go to 'not read'."""
    engine, out, agents = build(redis_client)
    files = tuple(
        File(name=f"img{i}.png", url=f"https://x/img{i}.png",
             mimetype="image/png", size=100)
        for i in range(MAX_IMAGES_PER_TURN + 2)
    )
    engine.on_message(msg("many images", files=files))
    eventually(answered(out, "t1"))

    agent = agents.opened[0]
    assert len(agent.last_images) == MAX_IMAGES_PER_TURN
    prompt = agent.prompts[-1]
    # The excess files are listed as not read
    for i in range(MAX_IMAGES_PER_TURN, MAX_IMAGES_PER_TURN + 2):
        assert f"img{i}.png" in prompt


def test_discord_attachments_are_downloaded(redis_client):
    """Discord attachments with CDN urls are downloaded the same way."""
    discord_out = FakeOutbound("discord")
    engine = build(redis_client, out=discord_out)[0]
    # Replace outbounds to include discord
    engine.outbounds = {"discord": discord_out}
    files = (File(name="capture.webp",
                  url="https://cdn.discordapp.com/attachments/1/2/capture.webp",
                  mimetype="image/webp", size=500),)
    inbound = Inbound("discord", "D1", "dt1", "din1", "U1", "see this",
                      files=files, is_dm=True)
    engine.on_message(inbound)
    eventually(lambda: any(t.startswith("answer to") for t in discord_out.texts("dt1")))

    agent = engine.agents.opened[0]
    assert len(agent.last_images) == 1
    assert agent.last_images[0]["mimeType"] == "image/webp"
    assert "Attached images: capture.webp" in agent.prompts[-1]


# ---------------------------------------------------------------------------
# Slack mock integration: url_private served with token check
# ---------------------------------------------------------------------------

@pytest.fixture
def mock(tmp_path):
    srv = slack.serve(0, tmp_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_slack_mock_serves_files_with_token(mock):
    """The mock serves /files/<id> with Bearer auth and rejects without."""
    file_b64 = base64.b64encode(PNG_1PX).decode()
    httpx.post(f"{mock}/_mock/files", json={"id": "F123", "content": file_b64})

    # With the right token
    resp = httpx.get(f"{mock}/files/F123",
                     headers={"Authorization": f"Bearer {slack.DEFAULT_TOKEN}"})
    assert resp.status_code == 200
    assert resp.content == PNG_1PX

    # Without a token
    resp = httpx.get(f"{mock}/files/F123")
    assert resp.status_code == 401

    # With scope blocked
    httpx.post(f"{mock}/_mock/files_scope_blocked", json={"blocked": True})
    resp = httpx.get(f"{mock}/files/F123",
                     headers={"Authorization": f"Bearer {slack.DEFAULT_TOKEN}"})
    assert resp.status_code == 403


def test_slack_transport_download_with_mock(mock):
    """SlackSocketTransport.download fetches a file from the mock."""
    from symphony.assistant.slack_socket import SlackError, SlackSocketTransport

    file_b64 = base64.b64encode(PNG_1PX).decode()
    httpx.post(f"{mock}/_mock/files", json={"id": "F456", "content": file_b64})

    transport = SlackSocketTransport(slack.DEFAULT_APP_TOKEN, slack.DEFAULT_TOKEN,
                                     api_url=mock)
    f = File(name="img.png", url=f"{mock}/files/F456", mimetype="image/png", size=len(PNG_1PX))
    data = transport.download(f)
    assert data == PNG_1PX

    # Scope blocked -> SlackError
    httpx.post(f"{mock}/_mock/files_scope_blocked", json={"blocked": True})
    with pytest.raises(SlackError, match="missing_scope"):
        transport.download(f)
