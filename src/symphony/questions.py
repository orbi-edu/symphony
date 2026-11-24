"""Questions an agent asks a person (`symphony_ask`), kept until the answer reaches the work.

The run that asks waits only `agent.input_timeout_ms`, and may end or die long before the
person answers, so the question outlives it here, keyed by the ask's call id:

- The runner records it (`ask`) and goes on waiting as before.
- The daemon (`tick`) posts it on the ticket, as a comment that names who decides and
  numbers the options, and in Slack with one button per option; reminds that person once,
  `agent.ask_reminder_ms` after the question; and delivers the answer.
- An answer is a reply to that comment (the Linear webhook queues it: `queue_reply`), a
  button or a thread reply in Slack (the assistant), or Tern and the CLI (the waiting run
  itself). The first one wins (`submit`); the ticket and Slack then say who answered.
- Delivery: to the run still waiting on it (an `answer` intent); to a run of the ticket that
  went on without it (`steer`, or `follow_up` while it waits on something else); with no
  run, the ticket goes back to Rework and its next attempt reads the answer on the ticket. A
  closed ticket only records it.

External calls are best effort and retried at the next tick: posting, reminding, delivering
and announcing each record a field only once they happened.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

import structlog

from . import notify, state, wake
from .sessions import SessionStore, attempt_session_id

log = structlog.get_logger()

RETAIN_S = 14 * 24 * 3600  # how long a question (and the refs to it) stays readable
EXPIRE_MS = 7 * 24 * 3600 * 1000  # an open question closes, unanswered, after this
INBOX_MAX = 500
BATCH = 200  # questions advanced per tick
VIA = {"linear": "on the ticket", "slack": "in Slack", "symphony": "in Symphony"}
_JSON_FIELDS = frozenset({"options", "own", "slack", "slack_reminder", "stakeholder", "assignee"})
_INT_FIELDS = frozenset({"asked_at", "answered_at", "reminded_at"})
# "4", "4.", "#4", "option 4", "opção 4": the 4th option.
_NUMBER = re.compile(r"\s*(?:#|op(?:tion|ção|cao)?\.?\s*)?(\d{1,2})\s*[.):]?\s*", re.IGNORECASE)
DELIVERY = {
    "answer": "The agent was waiting for it and goes on with it.",
    "steer": "The agent had gone on with its own judgment; it got the answer while still working.",
    "follow_up": "The agent gets it as soon as it is done with what it is waiting for.",
    "queued": "The ticket is waiting for its next attempt, which starts from this answer.",
    "rework": "No run was working on the ticket, so it goes back to Rework: the next attempt "
              "starts from this answer.",
    "none": "The ticket is closed; the answer is recorded here.",
}


def _key(ns: str, qid: str) -> str:
    return f"sym:{ns}:question:{qid}"


def _claim(ns: str, qid: str) -> str:
    return f"sym:{ns}:question:{qid}:answered"


def _index(ns: str) -> str:
    return f"sym:{ns}:questions"  # zset: questions not finished yet, by when they were asked


def _ref(ns: str, kind: str, ref: str) -> str:
    return f"sym:{ns}:question-ref:{kind}:{ref}"


def _inbox(ns: str) -> str:
    return f"sym:{ns}:questions:inbox"


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


# -- the record -----------------------------------------------------------------------------

def load(client, ns: str, qid: str) -> dict | None:
    raw = client.hgetall(_key(ns, qid))
    if not raw:
        return None
    rec: dict = {}
    for field, value in raw.items():
        name, value = _text(field), _text(value)
        rec[name] = (json.loads(value) if name in _JSON_FIELDS
                     else int(value) if name in _INT_FIELDS else value)
    return rec


def _set(client, ns: str, qid: str, **fields) -> None:
    mapping = {k: json.dumps(v) if k in _JSON_FIELDS else str(v) for k, v in fields.items()}
    pipe = client.pipeline()
    pipe.hset(_key(ns, qid), mapping=mapping)
    pipe.expire(_key(ns, qid), RETAIN_S)
    pipe.execute()


def _remember(client, ns: str, kind: str, ref: str, qid: str) -> None:
    client.set(_ref(ns, kind, ref), qid, ex=RETAIN_S)


def by_ref(client, ns: str, kind: str, ref: str) -> str | None:
    """The question a Slack message (`slack`, `<channel>:<ts>`) or ticket comment (`linear`,
    its id) belongs to."""
    found = client.get(_ref(ns, kind, ref))
    return _text(found) if found else None


def choice(text: str, options: Sequence[str]) -> str:
    """The answer `text` gives: an option when it is just that option's number."""
    match = _NUMBER.fullmatch(text)
    if match and 1 <= int(match.group(1)) <= len(options):
        return options[int(match.group(1)) - 1]
    return text.strip()


# -- writers: the runner, the assistant, the webhook -----------------------------------------

def ask(client, ns: str, *, qid: str, issue_id: str, identifier: str, sid: str, url: str | None,
        question: str, options: Sequence[str], to: str | None, now_ms: int) -> None:
    """Record a question the agent just asked; the daemon posts it."""
    _set(client, ns, qid, id=qid, issue_id=issue_id, identifier=identifier, sid=sid,
         url=url or "", question=question, options=list(options), to=(to or "").strip(),
         asked_at=now_ms, state="open", own=[])
    client.zadd(_index(ns), {qid: now_ms})
    wake.wake(client, ns, wake.DAEMON, f"question:{qid}")


def submit(client, ns: str, qid: str, text: str, *, by: str, via: str, now_ms: int,
           delivered: str | None = None) -> tuple[bool, dict | None]:
    """Record `text` as the answer, unless the question has one already or closed. Returns
    (whether this answer won, the question). `delivered` marks an answer that already
    reached the run (Tern and the CLI answer the waiting run directly)."""
    rec = load(client, ns, qid)
    if rec is None or rec.get("state") != "open" or not text.strip():
        return False, rec
    if not client.set(_claim(ns, qid), by or "?", nx=True, ex=RETAIN_S):
        return False, load(client, ns, qid)
    fields = {"state": "answered", "answer": choice(text, rec.get("options") or []),
              "by": by or "someone", "via": via, "answered_at": now_ms}
    if delivered:
        fields["delivered"] = delivered
    _set(client, ns, qid, **fields)
    client.zadd(_index(ns), {qid: rec["asked_at"]})
    wake.wake(client, ns, wake.DAEMON, f"question:{qid}:answered")
    rec.update(fields)
    return True, rec


def queue_reply(client, ns: str, *, comment_id: str, parent_id: str, body: str,
                author: str) -> bool:
    """A reply on the ticket (Linear webhook), queued for the daemon when it answers one of
    the questions. The daemon reads it after it has recorded its own replies, so those are
    never taken for an answer."""
    if not parent_id or by_ref(client, ns, "linear", parent_id) is None:
        return False
    pipe = client.pipeline()
    pipe.lpush(_inbox(ns), json.dumps({"id": comment_id, "parent": parent_id, "body": body,
                                       "author": author}))
    pipe.ltrim(_inbox(ns), 0, INBOX_MAX - 1)
    pipe.execute()
    return True


# -- the daemon ----------------------------------------------------------------------------

def tick(client, ns: str, *, kv, tracker, cfg, now_ms: int) -> None:
    """Take queued ticket replies, then move every unfinished question one step on."""
    _drain(client, ns, now_ms)
    sessions = SessionStore(client, ns)
    for raw in client.zrange(_index(ns), 0, BATCH - 1):
        qid = _text(raw)
        try:
            finished = _advance(client, ns, qid, kv=kv, tracker=tracker, cfg=cfg,
                                sessions=sessions, now_ms=now_ms)
        except Exception as exc:  # noqa: BLE001 — the next tick tries this question again
            log.warning("question_step_failed", question=qid, error=str(exc)[:300])
            continue
        if finished:
            client.zrem(_index(ns), qid)


def _drain(client, ns: str, now_ms: int) -> None:
    for _ in range(INBOX_MAX):
        raw = client.rpop(_inbox(ns))
        if raw is None:
            return
        try:
            reply = json.loads(_text(raw))
        except ValueError:
            continue
        qid = by_ref(client, ns, "linear", reply.get("parent") or "")
        rec = load(client, ns, qid) if qid else None
        if rec is None or reply.get("id") in (rec.get("own") or []):
            continue
        won, _ = submit(client, ns, qid, reply.get("body") or "", by=reply.get("author") or "",
                        via="linear", now_ms=now_ms)
        if won:
            log.info("question_answered", question=qid, via="linear")


def _advance(client, ns: str, qid: str, *, kv, tracker, cfg, sessions: SessionStore,
             now_ms: int) -> bool:
    """One step; True once nothing is left to do."""
    rec = load(client, ns, qid)
    if rec is None or rec.get("state") == "closed":
        return True
    if rec["state"] == "answered":
        if not rec.get("delivered"):
            kind = _deliver(rec, kv=kv, tracker=tracker, cfg=cfg, sessions=sessions)
            _set(client, ns, qid, delivered=kind)
            rec["delivered"] = kind
            log.info("question_delivered", question=qid, issue_id=rec["issue_id"], kind=kind)
        if not rec.get("announced"):
            _announce(client, ns, rec, tracker)
            _set(client, ns, qid, announced="1")
        return True
    if "stakeholder" not in rec:
        rec["stakeholder"] = tracker.person(rec["issue_id"], rec.get("to") or None) or {}
        rec["assignee"] = tracker.assignee(rec["issue_id"]) or {}
        _set(client, ns, qid, stakeholder=rec["stakeholder"], assignee=rec["assignee"])
    if not rec.get("comment"):
        comment = tracker.comment(rec["issue_id"], _ticket_question(rec, cfg))
        _own(client, ns, rec, comment)
        _set(client, ns, qid, comment=comment)
        _remember(client, ns, "linear", comment, qid)
        rec["comment"] = comment
    if not rec.get("posted"):
        where = notify.post_question(_slack_question(rec), qid=qid, options=rec["options"],
                                     mention_keys=_mention_keys(rec), direct_keys=_direct_keys(rec),
                                     link=rec.get("url") or None)
        _set(client, ns, qid, posted="1", slack=where or {})
        _remember_slack(client, ns, where, qid)
        log.info("question_posted", question=qid, issue_id=rec["issue_id"], slack=bool(where))
        return False
    age = now_ms - rec["asked_at"]
    if age >= EXPIRE_MS:
        _set(client, ns, qid, state="closed")
        for where in (rec.get("slack"), rec.get("slack_reminder")):
            if where:
                notify.edit_question(where, ":hourglass: *Closed without an answer*",
                                     f"{rec['identifier']}: {rec['question']}")
        return True
    if cfg.ask_reminder_ms and not rec.get("reminded_at") and age >= cfg.ask_reminder_ms:
        _remind(client, ns, rec, tracker, age)
        _set(client, ns, qid, reminded_at=now_ms)
    return False


def _own(client, ns: str, rec: dict, comment_id: str) -> None:
    """Remember a comment Symphony wrote in the question's thread: never an answer."""
    rec["own"] = [*(rec.get("own") or []), comment_id]
    _set(client, ns, rec["id"], own=rec["own"])


def _mention_keys(rec: dict) -> list[str]:
    who = rec.get("stakeholder") or {}
    return [k for k in (who.get("email"), who.get("name")) if k]


def _direct_keys(rec: dict) -> list[str]:
    """Who gets the question as a DM: who decides, and the ticket's assignee."""
    who = rec.get("assignee") or {}
    return [*_mention_keys(rec), *(k for k in (who.get("email"), who.get("name")) if k)]


def _remember_slack(client, ns: str, where: dict | None, qid: str) -> None:
    """A reply in the thread of the channel message or of a DM answers the question."""
    for ref in ([where] if where and where.get("ts") else []) + list((where or {}).get("dms") or []):
        _remember(client, ns, "slack", f"{ref['channel']}:{ref['ts']}", qid)


def _who(rec: dict) -> str:
    who = rec.get("stakeholder") or {}
    return who.get("mention") or who.get("name") or ""


def _options(rec: dict) -> str:
    return "\n".join(f"{i}. {option}" for i, option in enumerate(rec.get("options") or [], 1))


def _ticket_question(rec: dict, cfg) -> str:
    who = _who(rec)
    minutes = max(1, round(cfg.input_timeout_ms / 60_000))
    parts = [f"**Question from the agent**{f' for {who}' if who else ''}", rec["question"]]
    if rec.get("options"):
        parts.append(_options(rec))
    parts.append(
        "Reply to this comment with an option's number or in your own words (Slack works too). "
        f"After {minutes} minutes the agent goes on with its best judgment and records that "
        "assumption in the Workpad; an answer that comes later still reaches the work.")
    return "\n\n".join(parts)


def _slack_question(rec: dict) -> str:
    name = (rec.get("stakeholder") or {}).get("name")
    text = f"{rec['identifier']} needs an answer{f' from {name}' if name else ''}: {rec['question']}"
    return f"{text}\n\n{_options(rec)}" if rec.get("options") else text


def _remind(client, ns: str, rec: dict, tracker, age_ms: int) -> None:
    hours = max(1, round(age_ms / 3_600_000))
    who = _who(rec)
    if rec.get("comment"):
        body = (f"**Still waiting for an answer**{f', {who}' if who else ''}: this question has "
                f"been open for {hours} h.\n\n{_quote(rec['question'])}\n\nReply in this thread "
                "with an option's number or in your own words.")
        _own(client, ns, rec, tracker.comment(rec["issue_id"], body, parent=rec["comment"]))
    if rec.get("slack"):
        name = (rec.get("stakeholder") or {}).get("name")
        where = notify.post_question(
            f"Still waiting for an answer{f' from {name}' if name else ''} ({hours} h): "
            f"{rec['identifier']}: {rec['question']}",
            qid=rec["id"], options=rec["options"], mention_keys=_mention_keys(rec),
            direct_keys=_direct_keys(rec), link=rec.get("url") or None, thread=rec["slack"])
        if where:
            _set(client, ns, rec["id"], slack_reminder=where)
            _remember_slack(client, ns, {"dms": where.get("dms")}, rec["id"])
    log.info("question_reminded", question=rec["id"], issue_id=rec["issue_id"], hours=hours)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def _deliver(rec: dict, *, kv, tracker, cfg, sessions: SessionStore) -> str:
    """Hand the answer to whoever is working the ticket now; the kind of delivery."""
    entry = kv.get(state.issue_key(rec["issue_id"])) or {}
    seq = entry.get("dispatch_seq")
    if entry.get("status") == "running" and seq:
        sid = attempt_session_id(rec["issue_id"], seq)
        meta = sessions.meta(sid) or {}
        if meta.get("state") not in (None, "ended"):
            if (meta.get("input_request") or {}).get("id") == rec["id"]:
                sessions.send_intent(sid, "answer", actor=rec["by"], request_id=rec["id"],
                                     text=rec["answer"])
                return "answer"
            kind = "steer" if meta.get("state") == "running" else "follow_up"
            sessions.send_intent(sid, kind, actor=rec["by"], text=(
                f"{rec['by']} answered your earlier question {VIA.get(rec['via'], '')}.\n"
                f"Question: {rec['question']}\nAnswer: {rec['answer']}\n"
                "If it changes what you did on your own judgment, adjust the work and the "
                "Workpad."))
            return kind
    found = tracker.state_of(rec["issue_id"]) or {}
    if found.get("type") == "terminal":
        return "none"
    if found.get("type") == "active":
        return "queued"
    tracker.move(rec["issue_id"], cfg.tracker_states["rework"])
    return "rework"


def _announce(client, ns: str, rec: dict, tracker) -> None:
    via = VIA.get(rec["via"], "")
    for where in (rec.get("slack"), rec.get("slack_reminder")):
        if where:
            notify.edit_question(where, ":white_check_mark: *Answered*",
                                 f"{rec['identifier']}: {rec['question']}\n"
                                 f"{rec['by']}, {via}: {rec['answer']}")
    if rec.get("comment"):
        said = (f"Answer received from {rec['by']}. " if rec["via"] == "linear" else
                f"**Answered** by {rec['by']} {via}: {rec['answer']}\n\n")
        body = said + DELIVERY.get(rec["delivered"], "")
        _own(client, ns, rec, tracker.comment(rec["issue_id"], body, parent=rec["comment"]))
