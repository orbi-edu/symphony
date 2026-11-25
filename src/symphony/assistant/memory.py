"""Per-person memory: what the assistant remembers about each person mapped in `notify.users`.

`sym:<ns>:assistant:memory:<login>` holds a JSON list of items `{id, text, kind, at, thread}`
(`kind` is `explicit` or `inferred`), at most `MAX_ITEMS` of `MAX_TEXT` characters. Each write,
and each session that reads it, renews its TTL (`assistant.memory.ttl_days`).

The agent writes through two host tools, `remember(text, kind)` and `forget(id | query)`. Their
results go to a `TurnNotes` the engine reads after the turn, so every remembered item is shown
to the person ("I'll remember: …"). Text that looks like a credential is never stored.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import redis

from ..mcp_servers import _SECRET_ENV, _TOKENISH
from ..merger.agents import HostTool, _obj

MAX_ITEMS = 50
MAX_TEXT = 500
KINDS = ("explicit", "inferred")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
ITEM_ID = re.compile(r"[0-9a-f]{8}\Z")
NEEDS_MAPPING = ("Memory needs a mapped user: ask an admin to add you to notify.users in "
                 "WORKFLOW.md.")
REFUSED_SECRET = ("refused: this looks like a credential (a key, token, password or private key). "
                  "Symphony never stores secrets. Tell the person you did not remember it.")

# Token shapes no memory may hold, besides the MCP stdio env checks (`mcp_servers`).
_TOKEN_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-|sk_|xox[a-z]-|xapp-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|sj_|sym_|"
    r"glpat-|lin_api_|AKIA|ASIA)[A-Za-z0-9_\-]{6,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_BEARER = re.compile(r"\bbearer\s+\S{8,}", re.IGNORECASE)
_ASSIGNED = re.compile(r"([A-Za-z0-9_.-]+)\s*[:=]\s*(\S{4,})")


def _normalize(text: str) -> str:
    """Casefold, collapse whitespace, strip trailing punctuation and spaces — for dedupe."""
    return re.sub(r"[\s.!?,;:]+$", "", " ".join(text.casefold().split()))

def looks_like_credential(text: str) -> bool:
    """True when `text` holds something that looks like a secret value."""
    if _TOKEN_PREFIX.search(text) or _PRIVATE_KEY.search(text) or _BEARER.search(text):
        return True
    for run in _TOKENISH.findall(text):
        if re.search(r"\d", run) and re.search(r"[A-Za-z]", run):
            return True  # a long run of mixed token characters
    for name, value in _ASSIGNED.findall(text):
        if not _SECRET_ENV.search(name):
            continue
        if re.search(r"passw", name, re.IGNORECASE) or (len(value) >= 6 and re.search(r"\d", value)):
            return True  # `password: …`, `API_KEY=…`
    return False


def key_of(namespace: str, login: str) -> str:
    return f"sym:{namespace}:assistant:memory:{login.casefold()}"


class MemoryFull(ValueError):
    pass


class MemoryStore:
    def __init__(self, client, namespace: str, *, now_ms: Callable[[], int] | None = None) -> None:
        self.client = client
        self.namespace = namespace
        self.prefix = key_of(namespace, "")
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    def items(self, login: str) -> list[dict]:
        raw = self.client.get(key_of(self.namespace, login))
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            return []
        return items if isinstance(items, list) else []

    def renew(self, login: str, ttl_days: int) -> None:
        self.client.expire(key_of(self.namespace, login), ttl_days * 86400)

    def remember(self, login: str, text: str, kind: str, thread: str, ttl_days: int) -> tuple[dict, bool]:
        """Stores one item and returns ``(item, refreshed)``.  When the normalized text
        matches an existing item the timestamp is refreshed (and *kind* upgraded from
        ``inferred`` to ``explicit``, never downgraded).  Raises ``ValueError`` for text
        it must not store and ``MemoryFull`` at ``MAX_ITEMS``."""
        text = " ".join(text.split())
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"text must be 1-{MAX_TEXT} characters")
        if kind not in KINDS:
            raise ValueError("kind must be explicit or inferred")
        if looks_like_credential(text):
            raise ValueError("credential")
        norm = _normalize(text)

        # Returns (item, refreshed) from the attempt that commits: a WatchError retry runs it
        # again on fresh items, so the flag never leaks from an attempt that was discarded.
        def change(items: list[dict]) -> tuple[dict, bool]:
            for item in items:
                if _normalize(item.get("text", "")) == norm:
                    item["at"] = self.now_ms()
                    # Upgrade inferred → explicit, never downgrade.
                    if kind == "explicit" and item.get("kind") != "explicit":
                        item["kind"] = "explicit"
                    return item, True
            if len(items) >= MAX_ITEMS:
                raise MemoryFull(f"memory is full ({MAX_ITEMS} items): forget something first")
            item = {"id": secrets.token_hex(4), "text": text, "kind": kind, "at": self.now_ms(),
                    "thread": thread}
            items.append(item)
            return item, False

        return self._update(login, change, ttl_days)

    def forget(self, login: str, *, item_id: str | None = None, query: str | None = None) -> list[dict]:
        """Removes the item with this id, or every item whose text holds `query` (any case).
        Returns what it removed."""
        needle = (query or "").casefold().strip()

        def change(items: list[dict]) -> list[dict]:
            gone = [i for i in items if (item_id and i.get("id") == item_id)
                    or (needle and needle in i.get("text", "").casefold())]
            items[:] = [i for i in items if i not in gone]
            return gone

        return self._update(login, change, None)

    def wipe(self, login: str) -> int:
        n = len(self.items(login))
        self.client.delete(key_of(self.namespace, login))
        return n

    def people(self) -> list[dict]:
        """`[{login, count, updated_at}]` of everyone with memory, by login."""
        out = []
        for key in sorted(self.client.scan_iter(match=self.prefix + "*", count=500)):
            login = key[len(self.prefix):]
            items = self.items(login)
            if items:
                out.append({"login": login, "count": len(items),
                            "updated_at": max(int(i.get("at") or 0) for i in items)})
        return out

    def _update(self, login: str, change, ttl_days: int | None):
        key = key_of(self.namespace, login)
        with self.client.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    items = json.loads(raw) if raw else []
                    if not isinstance(items, list):
                        items = []
                    result = change(items)
                    ttl = pipe.ttl(key)
                    pipe.multi()
                    if items:
                        if ttl_days is not None:
                            pipe.set(key, json.dumps(items), ex=ttl_days * 86400)
                        elif ttl and ttl > 0:
                            pipe.set(key, json.dumps(items), ex=ttl)
                        else:
                            pipe.set(key, json.dumps(items))
                    else:
                        pipe.delete(key)
                    pipe.execute()
                    return result
                except redis.WatchError:
                    continue


def render_items(items: list[dict]) -> str:
    return "\n".join(f"- [{i['id']}] {i['text']} ({i.get('kind', 'explicit')})" for i in items)


def instructions(login: str | None, items: list[dict]) -> str:
    """The memory part of a turn's prompt: the rules, and what is known about the asker."""
    if login is None:
        return ("The person asking is not mapped in notify.users, so you cannot remember anything "
                "for them. If they ask you to remember or forget something, or what you remember, "
                f"tell them: {NEEDS_MAPPING}")
    lines = [
        f"You keep a memory about each mapped person. The person asking is {login}.",
        "Call remember(text, kind) when they ask you to remember something (kind explicit), or "
        "when you learn a lasting fact about how they work (kind inferred). Write the text in "
        "the third person, short. Never remember a credential, token, password or secret.",
        "Before remembering, check the items below. To update something already remembered, "
        "remember the new version first and forget the old item only after the new one was "
        "stored; if remember is refused or fails, keep the old item. Do not add near-duplicates. "
        "Prefer kind explicit. Store only what helps future answers.",
        "Never store sensitive personal data (health, religion, political opinion, sex life, "
        "ethnicity, biometrics, or union membership) or information about third parties beyond "
        "their role.",
        "Call forget with the id, or with a query, when they ask you to forget something. When "
        "they ask what you remember, list the items.",
        "Symphony adds an \"I'll remember: …\" line to your answer itself; do not write it.",
    ]
    if items:
        lines.append(f"What you remember about {login} (oldest first):")
        lines.append(render_items(items))
    else:
        lines.append(f"You remember nothing about {login} yet.")
    return "\n".join(lines)


@dataclass
class TurnNotes:
    """What one turn's memory, fact and plan-offer tools did, for the engine to show after the
    turn."""

    login: str | None = None
    thread: str = ""
    remembered: list[str] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    plan: str | None = None  # `offer_plan`'s summary: the answer gets a "Plan this" button

    def reset(self, login: str | None, thread: str) -> None:
        self.login, self.thread = login, thread
        self.remembered, self.forgotten, self.facts, self.plan = [], [], [], None


def _answer(fn: Callable[[dict], str]) -> Callable[[dict, int], str]:
    def answer(args: dict, _index: int) -> str:
        try:
            return fn(args)
        except Exception as exc:  # noqa: BLE001 — the agent gets the failure, the turn goes on
            return f"failed: {type(exc).__name__}: {str(exc)[:200]}"

    return answer


def memory_tools(store: MemoryStore, notes: TurnNotes, *, enabled: Callable[[], bool],
                 ttl_days: Callable[[], int]) -> list[HostTool]:
    """`remember` and `forget` for one session; they act for `notes.login`, the turn's asker."""

    def remember(args: dict) -> str:
        if not enabled():
            return "refused: memory is turned off (assistant.memory.enabled)."
        if notes.login is None:
            return "refused: " + NEEDS_MAPPING
        try:
            item, refreshed = store.remember(notes.login, args["text"], args.get("kind", "explicit"),
                                             notes.thread, ttl_days())
        except MemoryFull as exc:
            return f"refused: {exc}"
        except ValueError as exc:
            return REFUSED_SECRET if str(exc) == "credential" else f"refused: {exc}"
        if item["text"] not in notes.remembered:
            notes.remembered.append(item["text"])
        verb = "already remembered (refreshed)" if refreshed else f"remembered [{item['id']}]"
        return (f"{verb}. Now you remember about {notes.login}:\n"
                + render_items(store.items(notes.login)))

    def forget(args: dict) -> str:
        if not enabled():
            return "refused: memory is turned off (assistant.memory.enabled)."
        if notes.login is None:
            return "refused: " + NEEDS_MAPPING
        gone = store.forget(notes.login, item_id=args.get("id"), query=args.get("query"))
        if not gone:
            return "nothing matched: no item was removed."
        notes.forgotten += [i["text"] for i in gone if i["text"] not in notes.forgotten]
        left = store.items(notes.login)
        return (f"forgot {len(gone)} item(s). Now you remember about {notes.login}:\n"
                + (render_items(left) or "nothing"))

    def check_forget(args: dict) -> str | None:
        if bool((args.get("id") or "").strip()) == bool((args.get("query") or "").strip()):
            return "give exactly one of id or query"
        return None

    _s = {"type": "string"}
    return [
        HostTool(
            "remember",
            "Remember a short fact about the person asking, for later chats. kind is explicit "
            "(they asked) or inferred (you learned it). Never a credential or secret. At most "
            f"{MAX_TEXT} characters; {MAX_ITEMS} items per person.",
            _obj(["text", "kind"], {"text": _s, "kind": {"type": "string", "enum": list(KINDS)}}),
            answer=_answer(remember),
        ),
        HostTool(
            "forget",
            "Forget items about the person asking: by id, or every item whose text holds query.",
            _obj([], {"id": _s, "query": _s}),
            check=check_forget,
            answer=_answer(forget),
        ),
    ]
