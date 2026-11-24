"""Live agent sessions: metadata, a bounded event stream, a replayable transcript, human intents.

One session is one agent conversation the people watching Symphony can follow and steer: an
implementation attempt (`<issue_id>:<dispatch_seq>`), a merger agent
(`merger:<owner>:<name>:<pr>:<role>:<n>`) or a chat thread of the assistant
(`assist:<transport>:<channel>:<thread>:<n>`, kind `assist`). The runner (or the merger, or the
assistant) is the only writer of
events and transcript; intents come from the control API and, for a late answer to a
question, from the daemon (`questions.py`). Everything lives in Redis
so every read path — the API, SSE, the ACP bridge, MCP — is a Redis read with no Kubernetes call.

Keys (prefix `sym:<namespace>:session:<sid>`):

- `:meta`        JSON document (state, identifier, title, repo, model, kind, timestamps, ...)
- `:events`      Stream of `{kind, data}`; omp RPC frames in delta mode plus `symphony.*` kinds
- `:transcript`  List of omp session entries (JSON) for replay, tail-trimmed
- `:intents`     Stream of human intents `{kind, actor, payload}` the runner consumes at once
- `sym:<namespace>:sessions` sorted set of session ids by `updated_at`
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from . import metrics, retention

STATES = ("starting", "running", "waiting_input", "idle", "ended")
KINDS = ("implement", "answer", "review", "oracle", "arbiter", "resolve", "reflect", "assist")
INTENTS = ("steer", "follow_up", "interrupt", "answer")

# Symphony's own event kinds, alongside omp's RPC event types.
EV_STATE = "symphony.state"
EV_INTENT_APPLIED = "symphony.intent_applied"
EV_INPUT_REQUESTED = "symphony.input_requested"
EV_INPUT_RESOLVED = "symphony.input_resolved"

MAX_EVENT_BYTES = 64 * 1024


def _now_ms() -> int:
    return int(time.time() * 1000)


def attempt_session_id(issue_id: str, seq: int) -> str:
    return f"{issue_id}:{seq}"


def _cap(data: dict) -> str:
    """Serialize an event payload, replacing oversized ones by a marker. A viewer can always
    fetch the full content from the transcript; the stream only has to stay bounded."""
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    if len(raw.encode()) <= MAX_EVENT_BYTES:
        return raw
    return json.dumps({"truncated": True, "bytes": len(raw.encode()), "type": data.get("type")})


class SessionStore:
    def __init__(
        self,
        client,
        namespace: str,
        *,
        max_events: int = 5000,
        max_transcript: int = 4000,
        now_ms=_now_ms,
    ) -> None:
        self.client = client
        self.ns = namespace
        self.max_events = max_events
        self.max_transcript = max_transcript
        self._now = now_ms

    # -- keys --------------------------------------------------------------
    def _key(self, sid: str, part: str) -> str:
        return f"sym:{self.ns}:session:{sid}:{part}"

    @property
    def _index(self) -> str:
        return f"sym:{self.ns}:sessions"

    # -- metadata ----------------------------------------------------------
    def open(self, sid: str, **meta: Any) -> dict:
        """Start (or restart) a session. A reopened attempt keeps its events and transcript:
        a resumed omp session is the same conversation."""
        if meta.get("kind", "implement") not in KINDS:
            raise ValueError(f"unknown session kind {meta.get('kind')!r}")
        now = self._now()
        doc = self.meta(sid) or {"sid": sid, "created_at": now}
        doc.update(meta, sid=sid, state=meta.get("state", "starting"), updated_at=now)
        doc.setdefault("kind", "implement")
        self._write_meta(sid, doc)
        self.append_event(sid, EV_STATE, {"state": doc["state"]})
        return doc

    def update(self, sid: str, **fields: Any) -> dict | None:
        doc = self.meta(sid)
        if doc is None:
            return None
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError(f"unknown session state {fields['state']!r}")
        changed_state = "state" in fields and fields["state"] != doc.get("state")
        doc.update(fields, updated_at=self._now())
        self._write_meta(sid, doc)
        if changed_state:
            self.append_event(sid, EV_STATE, {"state": doc["state"]})
        return doc

    def close(self, sid: str, outcome: str) -> dict | None:
        doc = self.update(sid, state="ended", outcome=outcome, ended_at=self._now())
        ttl_s = retention.sessions_s(self.client, self.ns)
        for part in ("meta", "events", "transcript", "intents"):
            self.client.expire(self._key(sid, part), ttl_s)
        if doc is not None and doc.get("kind") in metrics.ATTEMPT_KINDS:
            metrics.count_attempt_finished(self.client, self.ns, outcome)
        return doc

    def end_orphans(self, live_seq: Callable[[str], int | None], *, idle_ms: int,
                    limit: int = 200) -> list[str]:
        """Close implementation attempts left open by a runner that died: no update for
        `idle_ms` and the ticket's live attempt (`live_seq(issue_id)`, None when no runner
        works on it) is another one. Without this, a run that died waiting for an answer
        shows `waiting_input` for good. Returns the ids closed."""
        closed = []
        cutoff = self._now() - idle_ms
        for doc in self.list(limit=limit, states=("starting", "running", "waiting_input", "idle")):
            sid = doc.get("sid") or ""
            issue_id, _, seq = sid.rpartition(":")
            if (doc.get("kind") not in metrics.ATTEMPT_KINDS or not issue_id or not seq.isdigit()
                    or doc.get("updated_at", 0) > cutoff or live_seq(issue_id) == int(seq)):
                continue
            if doc.get("input_request"):
                self.resolve_input(sid, doc["input_request"].get("id") or "", answer=None,
                                   by="abandoned")
            self.close(sid, "error")
            closed.append(sid)
        return closed

    def _write_meta(self, sid: str, doc: dict) -> None:
        pipe = self.client.pipeline()
        pipe.set(self._key(sid, "meta"), json.dumps(doc, separators=(",", ":")))
        pipe.zadd(self._index, {sid: doc["updated_at"]})
        pipe.execute()

    def meta(self, sid: str) -> dict | None:
        raw = self.client.get(self._key(sid, "meta"))
        return json.loads(raw) if raw else None

    def metas(self, sids: Iterable[str]) -> dict[str, dict]:
        """Metadata of many sessions in one round trip; unknown or expired ids are absent."""
        ids = list(sids)
        if not ids:
            return {}
        raws = self.client.mget([self._key(sid, "meta") for sid in ids])
        return {sid: json.loads(raw) for sid, raw in zip(ids, raws, strict=True) if raw}

    def updated_since(self, ms: int) -> list[str]:
        """Ids of sessions updated at or after `ms`, newest first."""
        return self.client.zrevrangebyscore(self._index, "+inf", ms)

    def events_key(self, sid: str) -> str:
        """The Redis Stream key of a session's events, for readers on another client (the
        API's asyncio SSE tail)."""
        return self._key(sid, "events")

    def list(self, *, limit: int = 50, states: Iterable[str] | None = None) -> list[dict]:
        """Newest first. Ids whose metadata expired are pruned from the index on the way."""
        wanted = set(states) if states else None
        out: list[dict] = []
        start = 0
        page = max(limit, 50)
        while len(out) < limit:
            ids = self.client.zrevrange(self._index, start, start + page - 1)
            if not ids:
                break
            start += page
            raws = self.client.mget([self._key(sid, "meta") for sid in ids])
            for sid, raw in zip(ids, raws, strict=True):
                if raw is None:
                    self.client.zrem(self._index, sid)
                    continue
                doc = json.loads(raw)
                if wanted is None or doc.get("state") in wanted:
                    out.append(doc)
                    if len(out) >= limit:
                        break
        return out

    # -- events ------------------------------------------------------------
    def append_event(self, sid: str, kind: str, data: dict) -> str:
        return self.client.xadd(
            self._key(sid, "events"),
            {"kind": kind, "data": _cap(data), "t": str(self._now())},
            maxlen=self.max_events,
            approximate=True,
        )

    def read_events(
        self, sid: str, after: str | None = None, *, block_ms: int | None = None, count: int = 500
    ) -> list[tuple[str, str, dict]]:
        """Events strictly after `after` (`None`: from the start). With `block_ms`, waits up to
        that long for the first new event — what SSE and the ACP bridge tail with."""
        key = self._key(sid, "events")
        if block_ms is None:
            rows = self.client.xrange(key, min=f"({after}" if after else "-", count=count)
        else:
            res = self.client.xread({key: after or "0-0"}, count=count, block=block_ms)
            rows = res[0][1] if res else []
        return [(eid, f["kind"], json.loads(f["data"])) for eid, f in rows]

    def last_event_id(self, sid: str) -> str | None:
        rows = self.client.xrevrange(self._key(sid, "events"), count=1)
        return rows[0][0] if rows else None

    # -- transcript ----------------------------------------------------------
    def append_transcript(self, sid: str, entries: Iterable[dict]) -> int:
        payload = [json.dumps(e, separators=(",", ":"), ensure_ascii=False) for e in entries]
        if not payload:
            return 0
        key = self._key(sid, "transcript")
        pipe = self.client.pipeline()
        pipe.rpush(key, *payload)
        pipe.ltrim(key, -self.max_transcript, -1)
        pipe.execute()
        return len(payload)

    def transcript(self, sid: str, *, start: int = 0, limit: int = 1000) -> list[dict]:
        raws = self.client.lrange(self._key(sid, "transcript"), start, start + limit - 1)
        return [json.loads(r) for r in raws]

    # -- intents -----------------------------------------------------------
    def send_intent(self, sid: str, kind: str, *, actor: str, **payload: Any) -> str:
        if kind not in INTENTS:
            raise ValueError(f"unknown intent {kind!r}")
        return self.client.xadd(
            self._key(sid, "intents"),
            {"kind": kind, "actor": actor, "payload": json.dumps(payload), "t": str(self._now())},
            maxlen=1000,
            approximate=True,
        )

    def read_intents(
        self, sid: str, after: str | None = None, *, block_ms: int | None = None
    ) -> list[tuple[str, str, str, dict]]:
        key = self._key(sid, "intents")
        if block_ms is None:
            rows = self.client.xrange(key, min=f"({after}" if after else "-")
        else:
            res = self.client.xread({key: after or "$"}, block=block_ms)
            rows = res[0][1] if res else []
        return [(iid, f["kind"], f["actor"], json.loads(f["payload"])) for iid, f in rows]

    def last_intent_id(self, sid: str) -> str:
        rows = self.client.xrevrange(self._key(sid, "intents"), count=1)
        return rows[0][0] if rows else "0-0"

    def intent_applied(self, sid: str, intent_id: str, kind: str, **detail: Any) -> None:
        self.append_event(sid, EV_INTENT_APPLIED, {"intent": intent_id, "kind": kind, **detail})

    # -- human input ---------------------------------------------------------
    def request_input(
        self, sid: str, request_id: str, question: str, options: list[str] | None = None
    ) -> None:
        self.update(sid, state="waiting_input", input_request={
            "id": request_id, "question": question, "options": options or [], "at": self._now(),
        })
        self.append_event(sid, EV_INPUT_REQUESTED, {
            "id": request_id, "question": question, "options": options or [],
        })

    def resolve_input(self, sid: str, request_id: str, *, answer: str | None, by: str) -> None:
        self.update(sid, state="running", input_request=None)
        self.append_event(sid, EV_INPUT_RESOLVED, {"id": request_id, "answer": answer, "by": by})
