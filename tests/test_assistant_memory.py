"""Per-person memory: the store's rules, credential refusal, and the engine flow (fake agent)."""

from __future__ import annotations

import pytest
from tests.test_assistant_engine import FakeAgents, FakeOutbound, eventually

from symphony.assistant import Inbound
from symphony.assistant.confirm import Confirmer, Policy, ProposalStore
from symphony.assistant.engine import Engine, TurnResult
from symphony.assistant.memory import (
    MAX_ITEMS,
    NEEDS_MAPPING,
    REFUSED_SECRET,
    MemoryFull,
    MemoryStore,
    key_of,
    looks_like_credential,
)
from symphony.config import parse_workflow
from symphony.merger.agents import validate

WORKFLOW = """---
tracker: {kind: memory}
notify:
  users:
    slack: {alice: U1}
assistant:
  enabled: true
  slack: {channels: [C1]}
  memory: {enabled: %s, ttl_days: 30}
---
x
"""


def call(agent, name: str, args: dict) -> str:
    """What omp's host tool call gets back: the schema, the check, then the answer."""
    tool = agent.tools[name]
    err = validate(tool.parameters, args) or (tool.check(args) if tool.check else None)
    return f"rejected: {err}" if err else tool.answer(args, 0)


def build(redis_client, *, enabled=True):
    cfg = parse_workflow(WORKFLOW % ("true" if enabled else "false"))
    out, agents = FakeOutbound(), FakeAgents()
    confirmer = Confirmer(ProposalStore(redis_client, "t"), tracker=lambda: None, audit=lambda *a: None,
                          policy=lambda: Policy({}, "Backlog", "Todo", (), ()))
    store = MemoryStore(redis_client, "t")
    engine = Engine(outbounds={"slack": out}, agents=agents, policy=lambda: cfg, confirmer=confirmer,
                    memory=store)
    return engine, out, agents, store


def msg(text, thread, user="U1"):
    return Inbound("slack", "C1", thread, f"in-{thread}-{text[:8]}", user, text)


def final(out, thread):
    texts = [t for t in out.texts(thread) if t not in ("Working on it…",)]
    return texts[-1] if texts else None


def test_the_store_keeps_at_most_50_items_of_500_characters_with_a_renewable_ttl(redis_client):
    store = MemoryStore(redis_client, "t")
    first, refreshed_first = store.remember("Alice", "deploys on Fridays", "explicit", "t1", 30)
    assert first["kind"] == "explicit" and first["thread"] == "t1" and len(first["id"]) == 8
    assert 29 * 86400 < redis_client.ttl(key_of("t", "alice")) <= 30 * 86400
    assert not refreshed_first
    again, refreshed_again = store.remember("alice", "Deploys  on  Fridays", "explicit", "t2", 30)
    assert again["id"] == first["id"] and len(store.items("alice")) == 1  # the same text again
    assert refreshed_again
    with pytest.raises(ValueError):
        store.remember("alice", "x" * 501, "explicit", "t1", 30)
    with pytest.raises(ValueError):
        store.remember("alice", "fine", "guessed", "t1", 30)
    for n in range(MAX_ITEMS - 1):
        store.remember("alice", f"fact number {n}", "inferred", "t1", 30)
    with pytest.raises(MemoryFull):
        store.remember("alice", "one too many", "explicit", "t1", 30)
    redis_client.expire(key_of("t", "alice"), 100)
    store.renew("alice", 90)
    assert redis_client.ttl(key_of("t", "alice")) > 89 * 86400
    gone = store.forget("alice", query="FACT NUMBER 1")  # 1, 10-19
    assert len(gone) == 11 and len(store.items("alice")) == MAX_ITEMS - 11
    assert store.forget("alice", item_id=first["id"])[0]["text"] == "deploys on Fridays"
    assert store.people() == [{"login": "alice", "count": MAX_ITEMS - 12,
                               "updated_at": max(i["at"] for i in store.items("alice"))}]
    assert store.wipe("alice") == MAX_ITEMS - 12 and store.people() == []


@pytest.mark.parametrize("text", [
    "my key is sk-ant-api03-abcdefghijklmnop",
    "the slack token xoxb-1234-5678-abcdef",
    "use ghp_A1b2C3d4E5f6G7h8I9j0 for the bot",
    "Seiji token sj_live_abc123def",
    "symphony token sym_k2p9_Qm9vYmFyLXRlcm4",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjE=",
    "password: hunter",
    "API_KEY=abc123xyz",
    "Authorization: Bearer abcdefghijklmnop",
    "the secret is Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA",
])
def test_credential_like_text_is_detected(text):
    assert looks_like_credential(text)


@pytest.mark.parametrize("text", [
    "deploys on Fridays",
    "prefers short answers in Portuguese",
    "owns the internationalization-pipeline service",
    "the auth review happens on Tuesdays",
    "works on the session store",
])
def test_ordinary_text_is_not_a_credential(text):
    assert not looks_like_credential(text)


def test_remember_is_shown_and_a_new_thread_starts_with_the_memory(redis_client):
    engine, out, agents, store = build(redis_client)
    agents.scripts = [lambda a, p, t: (call(a, "remember", {"text": "deploys on Fridays", "kind": "explicit"}),
                                       TurnResult("completed", "Noted."))[1]]
    engine.on_message(msg("remember that I deploy on Fridays", "a"))
    eventually(lambda: final(out, "a") and "I'll remember" in final(out, "a"))
    assert final(out, "a") == "Noted.\n\nI'll remember: deploys on Fridays"
    first = agents.opened[0].prompts[0]
    assert "You remember nothing about alice yet." in first and "remember(text, kind)" in first
    assert [i["text"] for i in store.items("alice")] == ["deploys on Fridays"]

    redis_client.expire(key_of("t", "alice"), 60)
    engine.on_message(msg("when do I deploy?", "b"))
    eventually(lambda: len(agents.opened) == 2 and agents.opened[1].prompts)
    prompt = agents.opened[1].prompts[0]
    assert "What you remember about alice" in prompt and "deploys on Fridays (explicit)" in prompt
    assert redis_client.ttl(key_of("t", "alice")) > 29 * 86400  # a session that reads it renews it
    engine.on_message(msg("and on holidays?", "b"))
    eventually(lambda: len(agents.opened[1].prompts) == 2)
    assert "What you remember" not in agents.opened[1].prompts[1]  # once per session


def test_forget_removes_the_items_and_says_so(redis_client):
    engine, out, agents, store = build(redis_client)
    store.remember("alice", "deploys on Fridays", "explicit", "a", 30)
    store.remember("alice", "likes tables", "inferred", "a", 30)
    answers = []
    agents.scripts = [lambda a, p, t: (answers.append(call(a, "forget", {"query": "fridays"})),
                                       TurnResult("completed", "Done."))[1]]
    engine.on_message(msg("forget that I deploy on Fridays", "c"))
    eventually(lambda: final(out, "c") and "I forgot" in final(out, "c"))
    assert final(out, "c") == "Done.\n\nI forgot: deploys on Fridays"
    assert "likes tables" in answers[0] and [i["text"] for i in store.items("alice")] == ["likes tables"]
    assert call(agents.opened[0], "forget", {}).startswith("rejected: give exactly one of id or query")


def test_a_credential_is_refused_and_never_stored(redis_client):
    engine, out, agents, store = build(redis_client)
    answers = []
    agents.scripts = [lambda a, p, t: (answers.append(call(a, "remember", {
        "text": "the deploy token is ghp_A1b2C3d4E5f6G7h8I9j0", "kind": "explicit"})),
        TurnResult("completed", "I will not keep secrets."))[1]]
    engine.on_message(msg("remember my token ghp_A1b2C3d4E5f6G7h8I9j0", "d"))
    eventually(lambda: final(out, "d") == "I will not keep secrets.")
    assert answers == [REFUSED_SECRET]
    assert store.items("alice") == [] and redis_client.get(key_of("t", "alice")) is None


def test_an_unmapped_person_is_told_that_memory_needs_a_mapped_user(redis_client):
    engine, out, agents, store = build(redis_client)
    answers = []
    agents.scripts = [lambda a, p, t: (answers.append(call(a, "remember", {"text": "likes tea", "kind": "explicit"})),
                                       TurnResult("completed", "Sorry."))[1]]
    engine.on_message(msg("remember that I like tea", "e", user="U9"))
    eventually(lambda: final(out, "e") == "Sorry.")
    assert NEEDS_MAPPING in agents.opened[0].prompts[0] and "not mapped" in agents.opened[0].prompts[0]
    assert answers == ["refused: " + NEEDS_MAPPING]
    assert store.people() == []


def test_memory_turned_off_offers_no_tools_and_no_memory(redis_client):
    engine, out, agents, store = build(redis_client, enabled=False)
    store.remember("alice", "deploys on Fridays", "explicit", "a", 30)
    engine.on_message(msg("when do I deploy?", "f"))
    eventually(lambda: agents.opened and agents.opened[0].prompts)
    assert agents.opened[0].tools == {} and "Fridays" not in agents.opened[0].prompts[0]
