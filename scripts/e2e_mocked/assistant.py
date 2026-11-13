"""Scenarios 9-12 and 14: the chat assistant and MCP through the gateway (`run.py --assistant`).

- 9: ask on Slack (Socket Mode): ack, "Working on it…" then the answer with a code link, an
  `assist` session, a thread follow-up in the same session.
- 10: plan on Slack + Linear: the proposal's buttons, an unmapped click refused, a mapped
  click creating the tree once, the message edited, the audit entry.
- 11: 9 and 10 through the Discord Gateway with `--tracker github` (issues on the board).
- 12: MCP through the gateway: an implementer and the assistant call the mock MCP server with
  the stored headers; the repository's own `.omp/mcp.json` is ignored.
- 14: the soul and memory on Slack: a persona rule seeded in `.symphony/assistant.md` is followed;
  "remember …" is kept and used in a new thread; "forget …" removes it; a secret is refused; an
  unmapped person gets no memory; a team fact PR opens on the GitHub mock after a mapped click.

The assistant (`python -m symphony.entry.assistant`) runs next to the local mode on the same
Redis, with both transports pointed at the mocks. Each check reads the mocks' recorded state.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time

from scripts.e2e_mocked.run import (
    AGENT_TIMEOUT_S,
    APP,
    DISCORD_CHANNEL,
    DISCORD_PERSON,
    DISCORD_TOKEN,
    NAMESPACE,
    PERSON,
    SLACK_CHANNEL,
    SLACK_TOKEN,
    Failure,
    Scenario,
    until,
)

SCENARIOS = ("9", "10", "11", "12", "14")
LINEAR_SCENARIOS = ("9", "10", "12", "14")
GITHUB_SCENARIOS = ("11",)
SLACK_APP_TOKEN = "xapp-mock-symphony"  # the Slack mock's seeded app-level token
SLACK_BOT_USER, SLACK_PERSON, SLACK_STRANGER = "U0MOCKBOT1", "U0OCTOCAT", "U0STRANGER"
DISCORD_BOT_USER, DISCORD_STRANGER = "1300000000000000001", "1500000000000000999"
DISCORD_GUILD = "1200000000000000001"
WORKING = "Working on it…"
ONLY_MAPPED = "Only people mapped in notify.users can create tickets."
FACT_PATH, FACT_LINE, FACT = "docs/FACTS.md", 3, "HERON-4127"
FACT_FILE = f"# Facts\n\nThe release codename of acme/app is {FACT}.\n"
ASK = ("What is the release codename of acme/app? Look it up in the repository, and cite the "
       "file and line where it is written.")
FOLLOW_UP = "Which file was that in? Reply with the file path only."
PLAN = ("plan Add a shout(name) function to app/greet.py that returns the greeting in upper case, "
        "and document it in README.md. Propose exactly three tickets: a parent ticket titled "
        "'Shout support', with two sub-tickets: 'Add shout() to app/greet.py' and "
        "'Document shout() in README.md'. The docs sub-ticket is blocked by the code sub-ticket.")
MCP_TOOL = "lookup_ticket_context"
MCP_HEADER = "X-Acme-Tenant"
INTENT_MESSAGE_CONTENT = 1 << 15
SOUL_PATH = ".symphony/assistant.md"
SIGN = "— Maestro"
SOUL_FILE = ("---\nname: Maestro\nlanguage: English\ntone: friendly and brief\n---\n"
             "You are Maestro, the team's assistant.\n\n"
             f"Team rule: end every reply with a last line that says exactly \"{SIGN}\".\n")
# A made-up GitHub token shape (it opens nothing): the memory must refuse it.
FAKE_SECRET = "ghp_" + "E2eNotARealToken0000000000000000"
ONLY_MAPPED_FACT = "Only people mapped in notify.users can open team fact PRs."
FACT_TEXT = "The staging database is reset every Monday at 06:00 UTC."


def workflow_section(tracker: str) -> str:
    """WORKFLOW.md's `assistant:` section for the harness (both transports, one repository)."""
    state = "Backlog" if tracker == "linear" else "Todo"  # the board has no Backlog option
    return ("assistant:\n  enabled: true\n"
            f'  slack: {{channels: ["{SLACK_CHANNEL}"]}}\n'
            f'  discord: {{channels: ["{DISCORD_CHANNEL}"]}}\n'
            f'  repositories: ["{APP}"]\n  max_concurrent: 2\n  turn_timeout_s: 600\n'
            f'  ticket_state: "{state}"\n')


# ======================================================================================
# processes
# ======================================================================================
def start_assistant(s) -> None:
    if getattr(s, "assistant_proc", None) is not None and s.assistant_proc.poll() is None:
        return
    env = s.common_env()
    env.update(
        SLACK_BOT_TOKEN=SLACK_TOKEN, SLACK_APP_TOKEN=SLACK_APP_TOKEN, SLACK_API_URL=f"{s.slack_url}/api",
        DISCORD_BOT_TOKEN=DISCORD_TOKEN, DISCORD_API_URL=f"{s.discord_url}/api/v10",
        SYMPHONY_ASSISTANT_WORKDIR=str(s.tmp / "assistant"),
        SYMPHONY_HEALTH_PORT=str(s.ports["assistant_health"]),
        SYMPHONY_MCP_FILE=str(s.tmp / "root" / "mcp.json"),
        SYMPHONY_GATEWAY_URL=f"http://127.0.0.1:{s.ports['gateway']}",
    )
    env.pop("SYMPHONY_BOOTSTRAP_TOKEN", None)
    (s.tmp / "assistant").mkdir(exist_ok=True)
    started = s.log_text("assistant").count("assistant_started")
    gateway = len(s.http.get(f"{s.discord_url}/_control/gateway").json()["identifies"])
    s.assistant_proc = s.spawn("assistant", [sys.executable, "-m", "symphony.entry.assistant"], env=env)
    until(lambda: s.assistant_proc.poll() is not None
          or s.log_text("assistant").count("assistant_started") > started, 60, "the assistant")
    if s.assistant_proc.poll() is not None:
        raise Failure("the assistant exited:\n" + s.log_text("assistant")[-3000:])
    until(lambda: s.http.get(f"{s.slack_url}/_control/socket").json()["connections"] >= 1, 30,
          "the assistant's Slack socket")
    until(lambda: len(s.http.get(f"{s.discord_url}/_control/gateway").json()["identifies"]) > gateway, 30,
          "the assistant's Discord Identify")


def restart_assistant(s) -> None:
    """A fresh assistant: its mirrors sync at start, so it reads what main has now."""
    proc = getattr(s, "assistant_proc", None)
    if proc is not None and proc.poll() is None:
        s.stop(proc)
        until(lambda: s.http.get(f"{s.slack_url}/_control/socket").json()["connections"] == 0, 30,
              "the old Slack socket to close")
    start_assistant(s)


def start_mcp(s) -> None:
    """Two MCP mocks: `mock` (Admin › MCP) and `project` (only in the repository's own
    `.omp/mcp.json`, which must be ignored). Each wants its own random bearer."""
    if getattr(s, "mcp_token", None):
        return
    s.mcp_token, s.project_mcp_token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    for name, port, token in (("mock-mcp", s.ports["mcp"], s.mcp_token),
                              ("mock-mcp-project", s.ports["mcp_project"], s.project_mcp_token)):
        s.spawn(name, [sys.executable, "-m", "scripts.mocks.mcp", "--port", str(port)],
                env={**os.environ, "MCP_MOCK_TOKEN": token})
        until(lambda p=port: s._healthy(f"http://127.0.0.1:{p}/_control/requests"), 30, name)


def mcp_requests(s, key: str = "mcp") -> list[dict]:
    return s.http.get(f"http://127.0.0.1:{s.ports[key]}/_control/requests").json()["requests"]


def push_main_files(s, message: str, files: dict[str, str]) -> str:
    """A person's commit on acme/app main with these files."""
    clone = s.clone_as(APP, s.person_token, PERSON)
    for rel, body in files.items():
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        s.git("add", rel, cwd=clone)
    s.git("commit", "-q", "-m", message, cwd=clone)
    s.git("push", "-q", "origin", "HEAD:main", cwd=clone)
    return s.git("rev-parse", "HEAD", cwd=clone).stdout.strip()


def seed_fact(s) -> str:
    if not getattr(s, "fact_sha", None):
        s.fact_sha = push_main_files(s, "docs: record the release codename", {FACT_PATH: FACT_FILE})
    return s.fact_sha


def assist_sessions(s, transport: str, thread: str) -> list[dict]:
    return [m for m in s.sessions() if m.get("kind") == "assist" and m.get("transport") == transport
            and m.get("thread") == thread]


def running_turns(s, sid: str) -> int:
    """How many turns the session ran: `running` state events in its event stream."""
    from symphony.sessions import EV_STATE

    rows = s.redis.xrange(f"sym:{NAMESPACE}:session:{sid}:events")
    return sum(1 for _, f in rows if f.get("kind") == EV_STATE
               and json.loads(f.get("data") or "{}").get("state") == "running")


# ======================================================================================
# chat adapters: one shape for Slack and Discord
# ======================================================================================
class SlackChat:
    transport = "slack"
    person, stranger = SLACK_PERSON, SLACK_STRANGER

    def __init__(self, s) -> None:
        self.s = s

    def _get(self, route: str) -> dict:
        return self.s.http.get(f"{self.s.slack_url}{route}").json()

    def push(self, user: str, text: str, thread: str | None = None) -> dict:
        event = {"type": "app_mention" if thread is None else "message", "user": user,
                 "channel": SLACK_CHANNEL, "text": text if thread else f"<@{SLACK_BOT_USER}> {text}"}
        if thread:
            event.update(thread_ts=thread, channel_type="channel")
        r = self.s.http.post(f"{self.s.slack_url}/_control/socket/event", json={"event": event})
        if r.status_code != 200:
            raise Failure(f"slack socket event: {r.status_code} {r.text[:300]}")
        body = r.json()
        return {"root": thread or body["ts"], "id": body["ts"], "envelope": body["envelope_id"]}

    def mention(self, text: str, user: str | None = None) -> dict:
        return self.push(user or self.person, text)

    def follow_up(self, root: str, text: str, reply_to: str) -> dict:
        return self.push(self.person, text, thread=root)

    def ack_delay(self, sent: dict) -> float | None:
        acks = self._get("/_control/socket")["acks"]
        return next((a["delay_s"] for a in acks if a["envelope_id"] == sent["envelope"]), None)

    def replies(self, root: str) -> list[dict]:
        out = []
        for m in self._get("/_mock/messages")["messages"]:
            if m.get("via") != "chat.postMessage" or m.get("thread_ts") != root:
                continue
            buttons = [(e["action_id"], e["text"]["text"]) for b in m.get("blocks") or []
                       if b.get("type") == "actions" for e in b.get("elements") or []]
            out.append({"id": m["ts"], "first": m["args"].get("text"), "text": m["text"], "buttons": buttons})
        return out

    def edits(self, message_id: str) -> list[dict]:
        return [u for u in self._get("/_mock/updates")["updates"] if u["ts"] == message_id]

    def click(self, message_id: str, action_id: str, user: str) -> tuple[int, dict]:
        r = self.s.http.post(f"{self.s.slack_url}/_control/socket/interactive", json={
            "user": user, "channel": SLACK_CHANNEL, "message_ts": message_id, "action_id": action_id})
        return r.status_code, r.json()

    def private_notes(self, user: str, clicks: list[dict]) -> list[str]:
        """What only that person saw: `chat.postEphemeral`."""
        return [e["text"] for e in self._get("/_mock/ephemerals")["ephemerals"] if e["user"] == user]

    def identify(self) -> dict | None:
        return None

    @staticmethod
    def links(text: str) -> list[tuple[str, str, str]]:
        """(url, path, line) of every `<url|acme/app:path:line>` link."""
        return [(m.group(1), m.group(2), m.group(3)) for m in re.finditer(
            r"<(https?://[^|>]+/acme/app/blob/[0-9a-f]{40}/([^#|>]+)#L(\d+))\|acme/app:\2:\3>", text)]


class DiscordChat:
    transport = "discord"
    person, stranger = DISCORD_PERSON, DISCORD_STRANGER

    def __init__(self, s) -> None:
        self.s = s

    def _get(self, route: str) -> dict:
        return self.s.http.get(f"{self.s.discord_url}{route}").json()

    def _post(self, route: str, body: dict) -> tuple[int, dict]:
        r = self.s.http.post(f"{self.s.discord_url}{route}", json=body)
        return r.status_code, r.json()

    def mention(self, text: str, user: str | None = None) -> dict:
        status, body = self._post("/_control/gateway/message", {
            "channel_id": DISCORD_CHANNEL, "author_id": user or self.person, "guild_id": DISCORD_GUILD,
            "content": f"<@{DISCORD_BOT_USER}> {text}"})
        if status != 200:
            raise Failure(f"discord gateway message: {status} {body}")
        return {"root": body["id"], "id": body["id"]}

    def follow_up(self, root: str, text: str, reply_to: str) -> dict:
        """A reply to the bot's answer (no mention): the conversation continues."""
        status, body = self._post("/_control/gateway/message", {
            "channel_id": DISCORD_CHANNEL, "author_id": self.person, "guild_id": DISCORD_GUILD,
            "content": text, "reference": reply_to})
        if status != 200:
            raise Failure(f"discord gateway reply: {status} {body}")
        return {"root": root, "id": body["id"]}

    def ack_delay(self, sent: dict) -> float | None:
        return None

    def replies(self, root: str) -> list[dict]:
        out = []
        for m in self._get("/_mock/messages")["messages"]:
            ref = (m["body"].get("message_reference") or {}).get("message_id")
            if m.get("via") != "bot" or m["channel_id"] != DISCORD_CHANNEL or ref != root:
                continue
            buttons = [(c["custom_id"], c.get("label")) for row in m["message"].get("components") or []
                       for c in row.get("components") or [] if c.get("type") == 2]
            out.append({"id": m["id"], "first": m["body"].get("content"), "text": m["content"],
                        "buttons": buttons})
        return out

    def edits(self, message_id: str) -> list[dict]:
        return [e for e in self._get("/_mock/edits")["edits"] if e["id"] == message_id]

    def click(self, message_id: str, action_id: str, user: str) -> tuple[int, dict]:
        return self._post("/_control/gateway/interaction", {
            "custom_id": action_id, "user_id": user, "channel_id": DISCORD_CHANNEL,
            "message_id": message_id, "guild_id": DISCORD_GUILD})

    def callbacks(self, clicks: list[dict]) -> list[dict]:
        ids = {c.get("id") for c in clicks}
        return [c for c in self._get("/_mock/callbacks")["callbacks"] if c["interaction_id"] in ids]

    def private_notes(self, user: str, clicks: list[dict]) -> list[str]:
        """Ephemeral interaction responses (type 4, flag 64) to these clicks."""
        return [c["data"].get("content") or "" for c in self.callbacks(clicks) if c["type"] == 4 and c["ephemeral"]]

    def identify(self) -> dict | None:
        found = self._get("/_control/gateway")["identifies"]
        return found[-1] if found else None

    @staticmethod
    def links(text: str) -> list[tuple[str, str, str]]:
        """(url, path, line) of every `[acme/app:path:line](<url>)` link."""
        return [(m.group(3), m.group(1), m.group(2)) for m in re.finditer(
            r"\[acme/app:([^:\]]+):(\d+)\]\(<(https?://[^>]+/acme/app/blob/[0-9a-f]{40}/\1#L\2)>\)", text)]


# ======================================================================================
# tracker adapters
# ======================================================================================
class LinearTickets:
    def __init__(self, run) -> None:
        self.run, self.s = run, run.s
        self.state = "Backlog"

    def snapshot(self) -> set[str]:
        return {i["identifier"] for i in self.s.lin("GET", "issues")["issues"]}

    def new(self, before: set[str]) -> list[dict]:
        views = [self.s.issue(i) for i in sorted(self.snapshot() - before)]
        blocks = {(r["issue"], r["relatedIssue"]) for v in views for r in v["relations"] if r["type"] == "blocks"}
        return [{"id": v["identifier"], "ident": v["identifier"], "token": v["identifier"], "title": v["title"],
                 "state": v["state"],
                 "labels": sorted(v["labels"]), "parent": v["parent"], "children": sorted(v["children"]),
                 "blocked_by": sorted(a for a, b in blocks if b == v["identifier"])} for v in views]

    def close(self, before: set[str]) -> None:
        self.run.cancel_open(self.snapshot() - before)


class BoardTickets:
    def __init__(self, run) -> None:
        from scripts.e2e_mocked.github_tracker import Board

        self.run, self.s, self.board = run, run.s, Board(run)
        self.state = "Todo"

    def snapshot(self) -> set[int]:
        return self.board.numbers()

    def new(self, before: set[int]) -> list[dict]:
        views = [self.board.view(n) for n in sorted(self.snapshot() - before)]
        parent = {c: v["number"] for v in views for c in v["children"]}
        # The GitHub tracker's identifier is `<repo name>-<number>` (`app-5`).
        return [{"id": v["number"], "ident": f"app-{v['number']}", "token": f"/issues/{v['number']}",
                 "title": v["title"], "state": v["status"],
                 "labels": v["labels"], "parent": parent.get(v["number"]), "children": sorted(v["children"]),
                 "blocked_by": sorted(v["blocked_by"])} for v in views]

    def close(self, before: set[int]) -> None:
        self.board.close_open(self.snapshot() - before)


# ======================================================================================
# shared steps
# ======================================================================================
def settled(chat, root: str, after: set[str], needle, timeout: float = AGENT_TIMEOUT_S) -> dict:
    """The bot's first new message in the thread once `needle(text)` holds and the text has
    stopped changing for 4 s (the stream edits at most every 1.5 s)."""
    last: dict = {}

    def done():
        msgs = [m for m in chat.replies(root) if m["id"] not in after]
        if not msgs or not needle(msgs[0]["text"]):
            last.clear()
            return None
        m = msgs[0]
        if last.get("text") != m["text"]:
            last.update(text=m["text"], at=time.monotonic())
            return None
        return m if time.monotonic() - last["at"] >= 4 else None

    return until(done, timeout, "the assistant's answer", interval=1)


def ask(run, sc: Scenario, chat) -> None:
    """Scenario 9 (and the first half of 11): a question, its answer with a code link, the
    session, and a follow-up in the same session."""
    s = run.s
    sha = seed_fact(s)
    sc.check("a distinctive fact seeded in acme/app main", bool(sha),
             {"file": FACT_PATH, "line": FACT_LINE, "commit": sha[:12]})
    t0 = time.monotonic()
    sent = chat.mention(ASK)
    root = sent["root"]
    if chat.transport == "slack":
        delay = until(lambda: (d := chat.ack_delay(sent)) is not None and (d,), 5, "the envelope's ack")[0]
        sc.check("the app_mention envelope was acked by envelope_id within 3 s", delay < 3,
                 {"envelope_id": sent["envelope"], "delay_s": delay})
    working = until(lambda: next((m for m in chat.replies(root) if m["first"] == WORKING), None), 30,
                    "the Working on it message")
    sc.time("question → Working on it posted", time.monotonic() - t0)
    sc.check(f"{chat.transport}: \"Working on it…\" posted in the thread at once", True,
             {"message": working["id"], "first_text": working["first"]})
    answer = settled(chat, root, set(), lambda text: FACT in text)
    sc.time("question → final answer", time.monotonic() - t0)
    edits = chat.edits(working["id"])
    sc.check("the same message was then updated (chat.update / edit message) with the answer",
             answer["id"] == working["id"] and len(edits) >= 1 and FACT in answer["text"],
             {"edits": len(edits), "answer": answer["text"][:600]})
    links = chat.links(answer["text"])
    sc.check("the answer has an acme/app:path:line link to the mirror's commit", bool(links),
             {"links": links[:3], "answer": answer["text"][:600]})
    sc.check(f"the link points at {FACT_PATH}:{FACT_LINE}",
             any(p == FACT_PATH and int(n) == FACT_LINE for _, p, n in links), links[:3], fatal=False)
    found = until(lambda: assist_sessions(s, chat.transport, root), 30, "the assist session")
    sid = found[0]["sid"]
    sc.check("an `assist` session in the session store, meta {transport, channel, thread, role, asker}",
             len(found) == 1 and found[0].get("role") == "assistant" and found[0].get("asker") == PERSON,
             {k: found[0].get(k) for k in ("sid", "kind", "transport", "channel", "thread", "role", "asker")})

    t1 = time.monotonic()
    before = {m["id"] for m in chat.replies(root)}
    chat.follow_up(root, FOLLOW_UP, reply_to=answer["id"])
    second = settled(chat, root, before, lambda text: "FACTS.md" in text and text != WORKING)
    sc.time("follow-up → answer", time.monotonic() - t1)
    again = assist_sessions(s, chat.transport, root)
    turns = until(lambda: running_turns(s, sid) >= 2 and running_turns(s, sid), 30, "the second turn")
    sc.check("the thread follow-up ran as the next turn of the same session",
             [m["sid"] for m in again] == [sid] and turns >= 2 and "FACTS.md" in second["text"],
             {"sessions": [m["sid"] for m in again], "running_turns": turns, "answer": second["text"][:300]})


def plan(run, sc: Scenario, chat, tickets) -> None:
    """Scenario 10 (and the second half of 11)."""
    s = run.s
    before = tickets.snapshot()
    run.cleanups.append(lambda: tickets.close(before))
    t0 = time.monotonic()
    root = chat.mention(PLAN)["root"]

    def proposal():
        return next((m for m in chat.replies(root) if any(b[0].startswith("proposal:") for b in m["buttons"])), None)

    msg = until(proposal, AGENT_TIMEOUT_S, "the planner's proposal", interval=2)
    sc.time("plan request → proposal with buttons", time.monotonic() - t0)
    labels = [b[1] for b in msg["buttons"]]
    pid = msg["buttons"][0][0].split(":")[1]
    sc.check("the proposal has the Create, Create and start and Dismiss buttons",
             labels == ["Create", "Create and start", "Dismiss"]
             and [b[0] for b in msg["buttons"]] == [f"proposal:{pid}:{c}" for c in ("create", "start", "dismiss")],
             {"buttons": msg["buttons"], "text": msg["text"][:600]})
    create = f"proposal:{pid}:create"

    status, out = chat.click(msg["id"], create, chat.stranger)
    sc.check("the mock delivered an unmapped person's click", status == 200, out)
    refused = until(lambda: [t for t in chat.private_notes(chat.stranger, [out]) if t == ONLY_MAPPED], 30,
                    "the refusal")
    time.sleep(5)
    sc.check("unmapped click → ephemeral refusal, and no ticket created",
             bool(refused) and tickets.snapshot() == before,
             {"note": refused[0], "new_tickets": sorted(map(str, tickets.snapshot() - before))})
    if chat.transport == "discord":
        cb = chat.callbacks([out])
        sc.check("the refusal is the interaction's callback (type 4, ephemeral flag), within 3 s",
                 bool(cb) and cb[0]["type"] == 4 and cb[0]["ephemeral"] and cb[0]["delay_s"] < 3, cb)

    t1 = time.monotonic()
    first = chat.click(msg["id"], create, chat.person)
    second = chat.click(msg["id"], create, chat.person)  # a double click
    sc.check("the mock delivered the mapped person's click", first[0] == 200, first[1])
    edited = until(lambda: next((m for m in chat.replies(root) if m["id"] == msg["id"]
                                 and m["text"].startswith(f"Created by {PERSON}")), None),
                   120, "the message to list the created tickets")
    sc.time("mapped click → tickets created and message edited", time.monotonic() - t1)
    made = tickets.new(before)
    by = {("parent" if "Shout support" in t["title"] else "docs" if "Document" in t["title"] else "code"): t
          for t in made}
    excerpt = [{k: t[k] for k in ("id", "title", "state", "labels", "parent", "children", "blocked_by")} for t in made]
    sc.check(f"three tickets in the tracker, in ticket_state ({tickets.state}), without the symphony label",
             len(made) == 3 and len(by) == 3 and all(t["state"] == tickets.state and "symphony" not in t["labels"]
                                                     for t in made), excerpt)
    sc.check("parent with the two sub-tickets as children",
             len(by) == 3 and by["parent"]["children"] == sorted([by["code"]["id"], by["docs"]["id"]])
             and by["code"]["parent"] == by["parent"]["id"] and by["docs"]["parent"] == by["parent"]["id"], excerpt)
    sc.check("the docs ticket is blocked by the code ticket (blocks / blocked_by relation)",
             len(by) == 3 and by["docs"]["blocked_by"] == [by["code"]["id"]], excerpt)
    sc.check("the parent carries repo:acme/app", len(by) == 3 and f"repo:{APP}" in by["parent"]["labels"],
             by.get("parent", {}).get("labels"))
    sc.check("the message lists the created tickets and the buttons are gone",
             all(t["token"] in edited["text"] for t in made) and not edited["buttons"],
             {"text": edited["text"], "buttons": edited["buttons"]})
    if chat.transport == "discord":
        cb = chat.callbacks([first[1]])
        sc.check("the mapped click's interaction was answered (type 6, deferred update) within 3 s",
                 bool(cb) and cb[0]["type"] == 6 and cb[0]["delay_s"] < 3, cb)

    later = chat.click(msg["id"], create, chat.person)
    time.sleep(5)
    notes = chat.private_notes(chat.person, [second[1]])
    sc.check("a second click creates nothing (double click and a later click)",
             len(tickets.new(before)) == 3 and later[0] >= 400
             and (second[0] >= 400 or any("already created" in n or "being created" in n for n in notes)),
             {"double_click": {"status": second[0], "notes": notes},
              "later_click": {"status": later[0], "body": later[1]}, "tickets": len(tickets.new(before))})
    audit = [e for e in s.api("GET", "/v1/audit?limit=200").json()["items"]
             if e["action"] == "assistant.tickets_created" and e["target"] == f"proposal:{pid}"]
    sc.check("audit: one assistant.tickets_created by the mapped login, with the identifiers",
             len(audit) == 1 and audit[0]["actor"] == PERSON
             and sorted(audit[0]["reason"].split(", ")) == sorted(t["ident"] for t in made),
             audit)


# ======================================================================================
# scenarios
# ======================================================================================
def s9_ask(run) -> None:
    sc = run.results["9"] = Scenario("9", "Assistant: ask on Slack (Socket Mode)", "B3, B4")
    start_assistant(run.s)
    ask(run, sc, SlackChat(run.s))


def s10_plan(run) -> None:
    sc = run.results["10"] = Scenario("10", "Assistant: plan + confirm on Slack, tickets in Linear", "B3, B4")
    start_assistant(run.s)
    plan(run, sc, SlackChat(run.s), LinearTickets(run))


def s11_discord(run) -> None:
    s = run.s
    sc = run.results["11"] = Scenario("11", "Assistant on the Discord Gateway, GitHub tracker", "B3, B4")
    start_assistant(s)
    chat = DiscordChat(s)
    ident = chat.identify()
    want = (1 << 0) | (1 << 9) | (1 << 12) | INTENT_MESSAGE_CONTENT
    sc.check("Identify with GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT",
             ident is not None and ident["intents"] & want == want and ident["intents"] & INTENT_MESSAGE_CONTENT,
             ident)
    ask(run, sc, chat)
    plan(run, sc, chat, BoardTickets(run))


def s12_mcp(run) -> None:
    s = run.s
    sc = run.results["12"] = Scenario("12", "MCP through the gateway (implementer and assistant)", "A1-A6")
    start_mcp(s)
    mock_url = f"http://127.0.0.1:{s.ports['mcp']}/mcp"
    r = s.api("PUT", "/v1/admin/mcp/mock", {"transport": "http", "url": mock_url,
                                            "roles": ["implementer", "assistant"], "enabled": True})
    sc.check("PUT /v1/admin/mcp/mock (http, roles implementer + assistant)", r.status_code == 200,
             {"status": r.status_code, "body": r.json()})
    r = s.api("PUT", "/v1/admin/mcp/mock/headers",
              {"headers": {"Authorization": f"Bearer {s.mcp_token}", MCP_HEADER: "acme-e2e"}})
    sc.check("PUT .../headers → headers_set names only", r.status_code == 200
             and sorted(r.json()["headers_set"]) == sorted(["Authorization", MCP_HEADER])
             and s.mcp_token not in r.text, r.json())
    listed = s.api("GET", "/v1/admin/mcp")
    sc.check("GET /v1/admin/mcp shows the server and never a header value",
             listed.status_code == 200 and s.mcp_token not in listed.text
             and any(x["name"] == "mock" for x in listed.json()["servers"]),
             [{k: x.get(k) for k in ("name", "transport", "roles", "headers_set", "enabled")}
              for x in listed.json()["servers"]])
    tested = s.api("POST", "/v1/admin/mcp/mock/test").json()
    sc.check("POST .../test → ok, the mock's tools", tested.get("ok") is True and MCP_TOOL in tested.get("tools", []),
             tested)
    s.http.post(f"http://127.0.0.1:{s.ports['mcp']}/_control/reset")

    project_url = f"http://127.0.0.1:{s.ports['mcp_project']}/mcp"
    sha = push_main_files(s, "chore: a project MCP config", {".omp/mcp.json": json.dumps(
        {"mcpServers": {"project": {"type": "http", "url": project_url,
                                    "headers": {"Authorization": f"Bearer {s.project_mcp_token}"}}}}, indent=2)})
    sc.check("acme/app main carries its own .omp/mcp.json (a second server)", bool(sha), {"commit": sha[:12]})

    t0 = time.monotonic()
    issue = s.create_ticket(
        "Look up the ticket context",
        f"Call the MCP tool `{MCP_TOOL}` (offered to you as `mcp__symphony_mock_{MCP_TOOL}`) with this "
        "ticket's identifier. Write the tool's summary, verbatim, in the Workpad under Notes and in "
        "your report. Do not read or change code for this.",
        ["symphony", "investigation", f"repo:{APP}"])
    run.cleanups.append(lambda: run.cancel_open([issue["identifier"]]))
    meta = s.wait_session(sc, issue)
    from symphony.workspace import sandbox_name

    # The local mode stages a Claude-format plugin (`--plugin-dir .../mcp/symphony`).
    plugin = s.tmp / "root" / "credentials" / sandbox_name(issue["identifier"]) / "mcp" / "symphony" / ".mcp.json"
    staged = until(lambda: plugin.exists() and json.loads(plugin.read_text() or "{}").get("mcpServers"), 120,
                   "the staged MCP config")
    entry = staged.get("mock") or {}
    bearer = (entry.get("headers") or {}).get("Authorization", "")
    sc.check("the staged MCP config points at the gateway with a minted bearer, never the stored header",
             set(staged) == {"mock"} and entry.get("url") == f"http://127.0.0.1:{s.ports['gateway']}/v1/mcp/mock"
             and bearer.startswith("Bearer ") and s.mcp_token not in plugin.read_text()
             and project_url not in plugin.read_text() and oct(plugin.stat().st_mode & 0o777) == "0o600",
             {"file": str(plugin.relative_to(s.tmp)), "servers": sorted(staged), "url": entry.get("url"),
              "headers": sorted(entry.get("headers") or {}), "mode": oct(plugin.stat().st_mode & 0o777)})
    summary = f"{issue['identifier']}: no linked incidents; owner team ENG."
    lin = until(lambda: (lambda i: i if i["state"] == "Done" else None)(s.issue(issue["identifier"])),
                AGENT_TIMEOUT_S, "the MCP ticket Done", interval=3)
    sc.time("ticket created → Done", time.monotonic() - t0)
    workpad = [c for c in lin["comments"] if summary in c["body"]]
    sc.check("the tool's result reached the ticket (Workpad or report)", bool(workpad),
             [c["body"][:300] for c in workpad[:2]] or [c["body"][:300] for c in lin["comments"]])
    s.wait_session_end(meta["sid"])
    calls = mcp_requests(s)
    tool_calls = [c for c in calls if c["rpc_method"] == "tools/call"]
    sc.check("the mock recorded the tools/call, every request with has_auth and the stored header",
             bool(tool_calls) and all(c["has_auth"] and MCP_HEADER.lower() in c["headers"] for c in calls),
             {"requests": len(calls), "tools_call": tool_calls[:2]})
    leaked = [str(p.relative_to(s.tmp)) for p in (s.tmp / "root").rglob("*")
              if p.is_file() and p != s.tmp / "root" / "mcp.json" and p.stat().st_size < 4 << 20
              and s.mcp_token in p.read_text(errors="replace")]
    sc.check("the agent's bearer never reached the mock (it accepts only the stored token, and the call "
             "succeeded); the stored token is in no agent file",
             bool(workpad) and not leaked, {"files_with_the_token": leaked})
    sc.check("the repository's own .omp/mcp.json server was never contacted",
             mcp_requests(s, "mcp_project") == [], mcp_requests(s, "mcp_project")[:3])

    # The assistant role, through the same gateway route.
    start_assistant(s)
    chat = SlackChat(s)
    n_before = len(tool_calls)
    t1 = time.monotonic()
    ident = "ENG-4242"
    root = chat.mention(f"Call the MCP tool {MCP_TOOL} of the `mock` MCP server with the identifier "
                        f"{ident} and reply with its summary, verbatim.")["root"]
    want = f"{ident}: no linked incidents; owner team ENG."
    answer = settled(chat, root, set(), lambda text: want in text)
    sc.time("assistant question → answer with the MCP result", time.monotonic() - t1)
    calls = mcp_requests(s)
    new = [c for c in calls if c["rpc_method"] == "tools/call"][n_before:]
    sc.check("assistant role: the answer carries the MCP result; the mock saw the call with the stored header",
             bool(new) and all(c["has_auth"] and MCP_HEADER.lower() in c["headers"] for c in calls),
             {"answer": answer["text"][:300], "tools_call": new[:1]})
    sc.check("still no request to the repository's own server", mcp_requests(s, "mcp_project") == [],
             mcp_requests(s, "mcp_project")[:3])

    # The agent's bearer itself: refused by the mock, and revoked at the gateway once the
    # implementer's session ended. Last, since the mock records this probe.
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    accept = "application/json, text/event-stream"
    via_gateway = s.http.post(f"http://127.0.0.1:{s.ports['gateway']}/v1/mcp/mock", json=rpc,
                              headers={"Authorization": bearer, "Accept": accept})
    direct = s.http.post(mock_url, json=rpc, headers={"Authorization": bearer, "Accept": accept})
    sc.check("the agent's bearer: 403 at the gateway after its session ended, 401 at the mock",
             via_gateway.status_code == 403 and direct.status_code == 401,
             {"gateway": [via_gateway.status_code, via_gateway.text[:80]], "mock": direct.status_code})


def memory_items(s, login: str = PERSON) -> list[dict]:
    r = s.api("GET", f"/v1/assistant/memory/{login}")
    if r.status_code != 200:
        raise Failure(f"GET /v1/assistant/memory/{login}: {r.status_code} {r.text[:300]}")
    return r.json()["items"]


def ask_new_thread(chat, text: str, needle, user: str | None = None) -> dict:
    """A mention that starts a new thread; the bot's settled answer there."""
    root = chat.mention(text, user=user)["root"]
    answer = settled(chat, root, set(), lambda t: t != WORKING and needle(t))
    return {**answer, "root": root}


def s14_soul_memory(run) -> None:
    import hashlib

    s = run.s
    sc = run.results["14"] = Scenario("14", "Assistant soul and memory: persona, remember, forget, team fact PR",
                                      "B3, B4")
    chat = SlackChat(s)
    body = SOUL_FILE.encode()
    blob = hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()
    commit = push_main_files(s, "docs: the assistant's soul", {SOUL_PATH: SOUL_FILE})
    sc.check(f"a persona rule seeded in {APP}:{SOUL_PATH}", bool(commit),
             {"commit": commit[:12], "blob": blob[:12], "rule": SIGN})
    restart_assistant(s)

    def soul_in(doc):
        soul = (doc or {}).get("soul") or {}
        return soul if soul.get("sha") == blob else None

    beat = until(lambda: soul_in(json.loads(s.redis.get(f"sym:{NAMESPACE}:assistant:heartbeat") or "{}")),
                 120, "the heartbeat to name the soul")
    listed = s.api("GET", "/v1/assistant/memory").json()
    sc.check("the heartbeat and GET /v1/assistant/memory carry {soul: {repo, path, sha}} = the blob sha",
             beat == {"repo": APP, "path": SOUL_PATH, "sha": blob} and soul_in(listed) == beat,
             {"heartbeat": beat, "api": listed.get("soul")})

    t0 = time.monotonic()
    first = ask_new_thread(chat, "Remember that I deploy on Fridays.", lambda t: "I'll remember" in t)
    sc.time("remember → answer", time.monotonic() - t0)
    sc.check(f"the reply follows the soul's persona rule (signed \"{SIGN}\")", SIGN in first["text"],
             {"answer": first["text"][-400:]})
    items = memory_items(s)
    sc.check("the reply ends with \"I'll remember: …\" and the item is stored for the mapped login",
             first["text"].rstrip().splitlines()[-1].startswith("I'll remember: ")
             and any("friday" in i["text"].lower() for i in items),
             {"last_line": first["text"].rstrip().splitlines()[-1], "items": items})
    sid_a = assist_sessions(s, "slack", first["root"])[0]["sid"]

    t1 = time.monotonic()
    second = ask_new_thread(chat, "When do I deploy? Answer in one short sentence.",
                            lambda t: "friday" in t.lower())
    sc.time("new thread → answer from memory", time.monotonic() - t1)
    sid_b = assist_sessions(s, "slack", second["root"])[0]["sid"]
    sc.check("a new thread (a new session) answers from the memory", sid_b != sid_a
             and "friday" in second["text"].lower(), {"sessions": [sid_a, sid_b], "answer": second["text"][:400]})

    third = ask_new_thread(chat, "Forget that I deploy on Fridays.", lambda t: "I forgot" in t)
    items = memory_items(s)
    sc.check("\"forget …\" removes the item and the reply says \"I forgot: …\"",
             not any("friday" in i["text"].lower() for i in items) and "I forgot: " in third["text"],
             {"answer": third["text"][-300:], "items": items})

    fourth = ask_new_thread(chat, f"Remember my GitHub token for later: {FAKE_SECRET}", lambda t: bool(t.strip()))
    stored = [s.redis.get(k) or "" for k in s.redis.scan_iter(match=f"sym:{NAMESPACE}:assistant:memory:*")]
    sc.check("a secret is refused: no memory key holds it and the reply does not say it was kept",
             not any(FAKE_SECRET in v for v in stored) and "I'll remember: " not in fourth["text"],
             {"answer": fourth["text"][:300].replace(FAKE_SECRET, "<the fake token>"),
              "memory_keys": len(stored)})

    fifth = ask_new_thread(chat, "Remember that I like green tea.", lambda t: bool(t.strip()),
                           user=chat.stranger)
    people = s.api("GET", "/v1/assistant/memory").json()["people"]
    sc.check("an unmapped person gets no memory", all(p["login"] == PERSON for p in people)
             and "I'll remember" not in fifth["text"], {"people": people, "answer": fifth["text"][:300]})
    sc.check("the unmapped person is told that memory needs a mapped user",
             "mapped" in fifth["text"].lower() or "notify.users" in fifth["text"],
             {"answer": fifth["text"][:300]}, fatal=False)

    t2 = time.monotonic()
    root = chat.mention(f"Propose this as a team fact for your soul file, with propose_team_fact: {FACT_TEXT}")["root"]

    def proposal():
        return next((m for m in chat.replies(root) if any(b[0].startswith("fact:") for b in m["buttons"])), None)

    msg = until(proposal, AGENT_TIMEOUT_S, "the team fact proposal", interval=2)
    sc.time("team fact request → proposal with buttons", time.monotonic() - t2)
    fid = msg["buttons"][0][0].split(":")[1]
    sc.check("the proposal has the Open PR and Dismiss buttons",
             [b[1] for b in msg["buttons"]] == ["Open PR", "Dismiss"]
             and [b[0] for b in msg["buttons"]] == [f"fact:{fid}:pr", f"fact:{fid}:dismiss"],
             {"buttons": msg["buttons"], "text": msg["text"][:400]})
    branch = f"symphony/assistant-fact-{fid}"

    def prs() -> list[dict]:
        r = s.rest("GET", f"/repos/{APP}/pulls?head=acme:{branch}&state=all")
        return r.json() if r.status_code == 200 else []

    status, out = chat.click(msg["id"], f"fact:{fid}:pr", chat.stranger)
    refused = until(lambda: [t for t in chat.private_notes(chat.stranger, [out]) if t == ONLY_MAPPED_FACT], 30,
                    "the refusal")
    time.sleep(3)
    sc.check("an unmapped click is refused privately and opens no PR", status == 200 and bool(refused)
             and prs() == [], {"note": refused[0], "prs": len(prs())})
    t3 = time.monotonic()
    chat.click(msg["id"], f"fact:{fid}:pr", chat.person)
    chat.click(msg["id"], f"fact:{fid}:pr", chat.person)  # a double click
    edited = until(lambda: next((m for m in chat.replies(root) if m["id"] == msg["id"]
                                 and m["text"].startswith(f"PR opened by {PERSON}")), None),
                   120, "the message to name the PR")
    sc.time("mapped click → PR opened", time.monotonic() - t3)
    found = prs()
    if found:
        run.cleanups.append(lambda: s.rest("PATCH", f"/repos/{APP}/pulls/{found[0]['number']}", {"state": "closed"}))
    raw = s.rest("GET", f"/repos/{APP}/contents/{SOUL_PATH}?ref={branch}", accept="application/vnd.github.raw")
    sc.check(f"one PR on the GitHub mock from {branch}, by the App, appending under ## Team facts",
             len(found) == 1 and found[0]["base"]["ref"] == "main"
             and found[0]["user"]["login"].endswith("[bot]") and raw.status_code == 200
             and raw.text.startswith(SOUL_FILE.rstrip("\n")) and "## Team facts" in raw.text
             and "staging database" in raw.text.lower().split("## team facts", 1)[-1]
             and found[0]["html_url"] in edited["text"] and not edited["buttons"],
             {"prs": [{k: p.get(k) for k in ("number", "html_url", "title")} for p in found],
              "file": raw.text[-300:], "message": edited["text"][:300]})
    audit = [e for e in s.api("GET", "/v1/audit?limit=200").json()["items"]
             if e["action"] == "assistant.team_fact_pr" and e["target"] == f"fact:{fid}"]
    sc.check("audit: one assistant.team_fact_pr by the mapped login", len(audit) == 1
             and audit[0]["actor"] == PERSON, audit)
    wiped = s.api("DELETE", f"/v1/assistant/memory/{PERSON}")
    sc.check("DELETE /v1/assistant/memory/{login} wipes the person's memory", wiped.status_code == 200
             and memory_items(s) == [], wiped.json())


RUNS = (("9", s9_ask), ("10", s10_plan), ("11", s11_discord), ("12", s12_mcp), ("14", s14_soul_memory))


def execute(run, wanted: set[str]) -> None:
    import traceback

    for key, fn in RUNS:
        if key not in wanted:
            continue
        print(f"== scenario {key} ({fn.__name__})", flush=True)
        try:
            fn(run)
        except Exception as exc:  # noqa: BLE001 — one scenario's failure is its own result
            sc = run.results.setdefault(key, Scenario(key, fn.__name__, ""))
            sc.error = f"{type(exc).__name__}: {exc}"
            (run.s.tmp / "logs" / f"scenario-{key}.trace").write_text(traceback.format_exc())
            print(f"  [ERROR] {key}: {sc.error[:500]}", flush=True)
        finally:
            run.after_scenario()

