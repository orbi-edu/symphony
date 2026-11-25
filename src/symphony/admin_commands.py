"""Durable, namespace-scoped administrative intents, reconciled by the lease holder.

One Redis HSET commits intent, inbox, idempotency and audit together. No administrative
record uses the core KV's sliding expiration. WATCH fences state transitions and lease
ownership; external effects are replayable and never run in an HTTP request.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime

from redis.exceptions import WatchError

from . import orchestrator, retention, state

# The default; the store applies WORKFLOW `retention.audit_days` (`symphony.retention`).
RETENTION_MS = retention.AUDIT_DAYS * retention.DAY_S * 1000
MAX_TERMINAL = 5000  # two audit events per command, at most 10000 terminal events
MAX_PENDING = 1000
ACTIONS = frozenset(
    {"pause", "resume", "refresh", "reevaluate", "interrupt", "workflow", "hold", "release"}
)
# Token names (`apitokens`) are the actors of audit events.
_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@-]{0,127}\Z")
STANDALONE_ACTIONS = frozenset({
    "token.create",
    "token.revoke",
    "teamconfig.publish",
    "merger.bypass",
    "merger.requeue",
    "merger.release",
    "setup.tracker",
    "setup.github_app",
    "setup.repositories",
    "setup.notifications",
    "setup.assistant",
    "setup.webhooks",
    "setup.skills",
    "admin.mcp.put",
    "admin.mcp.remove",
    "admin.mcp.headers",
    "admin.mcp.test",
    "mcp.viewer",
    "preview.opened",
    "assistant.tickets_created",
    "assistant.file_shared",
    "assistant.evidence_attached",
    "assistant.team_fact_pr",
    "assistant.memory_read",
    "assistant.memory_forget",
    "assistant.memory_wipe",
    "backup.export",
    "backup.restore",
    "backup.run",
    "backup.archive.get",
    "telemetry.backfill",
})
# Provider changes the API audits itself (`provider_audit`); only metadata is recorded. Every
# provider is configured by API key (internals §13), so saving one is the only provider action.
PROVIDER_ACTIONS = frozenset({"provider.save"})


class CommandError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _decode(value):
    return json.loads(value) if value is not None else None


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _now() -> int:
    return time.time_ns() // 1_000_000


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def public_command(record: dict) -> dict:
    return {
        key: record[key]
        for key in (
            "id",
            "action",
            "target",
            "state",
            "submittedAt",
            "finishedAt",
            "result",
            "revision",
        )
    }


def _event(record: dict, phase: str, now: int) -> dict:
    target = record["target"]
    return {
        "id": f"{record['id']}:{phase}",
        "time": _iso(now),
        "actor": record["actor"],
        "action": record["action"],
        "target": (
            "namespace" if not target
            else f"{target['issueId']}:{target['dispatchSeq']}" if "dispatchSeq" in target
            else target["issueId"]
        ),
        "result": record["state"],
        "reason": record["reason"],
        "commandId": record["id"],
    }


def _event_ms(event: dict) -> int:
    return int(datetime.fromisoformat(event["time"].replace("Z", "+00:00")).timestamp() * 1000)


class AdminStore:
    def __init__(self, client, namespace: str) -> None:
        self.client = client
        self.namespace = namespace
        self.key = f"admin:{namespace}:commands"
        self.admission_key = f"admin:{namespace}:admission"
        self.observation_key = f"admin:{namespace}:observations"

    def _identity(self, actor, action, idempotency_key, target, reason, payload):
        identity = hashlib.sha256(
            _json([actor, self.namespace, action, idempotency_key]).encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(
            _json(
                {
                    "actor": actor,
                    "namespace": self.namespace,
                    "action": action,
                    "target": target,
                    "reason": reason,
                    "payload": payload,
                }
            ).encode()
        ).hexdigest()
        return f"idem:{identity}", fingerprint

    def lookup(self, *, actor, action, idempotency_key, target, reason, payload=None):
        field, fingerprint = self._identity(
            actor, action, idempotency_key, target, reason.strip(), payload
        )
        mapping = _decode(self.client.hget(self.key, field))
        if mapping is None:
            return None
        if mapping["fingerprint"] != fingerprint:
            raise CommandError(
                "idempotency_conflict", "Idempotency key already names a different request."
            )
        record = self.get(mapping["id"])
        if record is None:
            raise CommandError("store_unavailable", "Command retention is inconsistent.", 503)
        return record

    def accept(
        self,
        *,
        actor,
        role,
        credential_fingerprint,
        action,
        target,
        reason,
        idempotency_key,
        payload=None,
    ):
        if (
            action not in ACTIONS
            or not isinstance(reason, str)
            or not 8 <= len(reason.strip()) <= 1000
        ):
            raise CommandError("invalid_command", "Action and reason are required.", 422)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise CommandError(
                "invalid_idempotency_key",
                "An idempotency key of at most 200 characters is required.",
                422,
            )
        reason = reason.strip()
        if action in {"reevaluate", "interrupt"}:
            if (
                not isinstance(target, dict)
                or set(target) != {"issueId", "dispatchSeq"}
                or not isinstance(target["issueId"], str)
                or not target["issueId"]
                or type(target["dispatchSeq"]) is not int
                or target["dispatchSeq"] < 1
            ):
                raise CommandError(
                    "invalid_target", "An exact issue and dispatch sequence are required.", 422
                )
        elif action in {"hold", "release"}:
            if (
                not isinstance(target, dict)
                or set(target) != {"issueId"}
                or not isinstance(target["issueId"], str)
                or not target["issueId"]
            ):
                raise CommandError("invalid_target", "An exact issue is required.", 422)
        elif target is not None:
            raise CommandError("invalid_target", "This action targets the namespace.", 422)
        field, fingerprint = self._identity(actor, action, idempotency_key, target, reason, payload)
        self.prune()
        for _ in range(8):
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(self.key, state.HEARTBEAT_KEY, state.LOCK_KEY)
                    mapping = _decode(pipe.hget(self.key, field))
                    if mapping:
                        if mapping["fingerprint"] != fingerprint:
                            raise CommandError(
                                "idempotency_conflict",
                                "Idempotency key already names a different request.",
                            )
                        record = _decode(pipe.hget(self.key, f"command:{mapping['id']}"))
                        if record is None:
                            raise CommandError(
                                "store_unavailable", "Command record is unavailable.", 503
                            )
                        return record
                    seconds, micros = pipe.time()
                    now = seconds * 1000 + micros // 1000
                    beat = _decode(pipe.get(state.HEARTBEAT_KEY))
                    lease = _decode(pipe.get(state.LOCK_KEY))
                    if (
                        not isinstance(beat, int)
                        or not 0 <= now - beat < 90_000
                        or not lease
                        or lease.get("lease_expires", 0) <= now
                    ):
                        raise CommandError(
                            "daemon_unavailable", "A fresh active daemon is required.", 503
                        )
                    pending = sum(1 for _ in pipe.hscan_iter(self.key, match="inbox:*", count=200))
                    if pending >= MAX_PENDING:
                        raise CommandError("queue_full", "Administrative queue is full.", 503)
                    order = int(pipe.hget(self.key, "nextOrder") or 0) + 1
                    command_id = uuid.uuid4().hex
                    record = {
                        "id": command_id,
                        "actor": actor,
                        "role": role,
                        "credentialFingerprint": credential_fingerprint,
                        "action": action,
                        "target": target,
                        "reason": reason,
                        "payload": payload,
                        "state": "accepted",
                        "submittedAt": _iso(now),
                        "submittedMs": now,
                        "finishedAt": None,
                        "result": "Awaiting daemon reconciliation.",
                        "revision": None,
                        "idempotencyField": field,
                        "claim": None,
                    }
                    pipe.multi()
                    pipe.hset(
                        self.key,
                        mapping={
                            f"command:{command_id}": _json(record),
                            field: _json({"id": command_id, "fingerprint": fingerprint}),
                            "nextOrder": str(order),
                            f"inbox:{command_id}": str(order),
                            f"audit:{command_id}:accepted": _json(_event(record, "accepted", now)),
                        },
                    )
                    pipe.execute()
                    return record
                except WatchError:
                    continue
        raise CommandError(
            "store_busy", "Command acceptance conflicted; retry with the same key.", 503
        )

    def get(self, command_id):
        return _decode(self.client.hget(self.key, f"command:{command_id}"))

    def paused(self) -> bool:
        raw = self.client.get(self.admission_key)
        if raw is None:
            return False
        if _text(raw) not in {"true", "false"}:
            raise CommandError("invalid_admission", "Admission state is unreadable.", 503)
        return _text(raw) == "true"

    def retention_ms(self) -> int:
        """How long audit entries and finished commands stay (WORKFLOW `retention`)."""
        return retention.audit_ms(self.client, self.namespace)

    def apply_retention(self, cfg) -> None:
        """Publish the WORKFLOW retention for every writer of this namespace. It runs on
        each tick, so a restored or deleted record comes back at once."""
        retention.publish(
            self.client, self.namespace, cfg.retention_sessions_days, cfg.retention_audit_days
        )

    def _expired_provider_audit(self, source, now, *, reserve=0):
        events = []
        for key, value in source.hscan_iter(self.key, match="audit:*", count=200):
            event = _decode(value)
            if event.get("commandId") is None:
                events.append((_event_ms(event), _text(key)))
        events.sort(reverse=True)
        cutoff = now - self.retention_ms()
        return [
            key
            for index, (timestamp, key) in enumerate(events)
            if index >= MAX_TERMINAL * 2 - reserve or timestamp < cutoff
        ]

    def provider_audit(self, *, actor, provider_id, outcome, operation_id, action="provider.save"):
        """Persist only allowlisted standalone metadata, never provider payloads/reasons."""
        if (
            not isinstance(actor, str)
            or not _ACTOR.fullmatch(actor)
            or not isinstance(provider_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", provider_id)
            or outcome not in {"attempted", "succeeded", "failed", "unknown"}
            or action not in PROVIDER_ACTIONS
            or not isinstance(operation_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", operation_id)
        ):
            raise CommandError("invalid_audit", "Invalid provider audit metadata.", 422)
        now = _now()
        event = {
            "id": f"{operation_id}:{outcome}",
            "time": _iso(now),
            "actor": actor,
            "action": action,
            "target": f"provider:{provider_id}",
            "result": outcome,
            # The action code says what changed; there is no human reason to show.
            "reason": None,
            "commandId": None,
        }
        # No credential-adjacent free text or provider payload reaches durable audit.
        self._persist_standalone(event, now)
        return event

    def audit_event(self, *, actor, action, target, reason):
        """A succeeded administrative change made directly by the API (no daemon command):
        token issue/revocation, team configuration publication, merger actions."""
        if (
            not isinstance(actor, str)
            or not _ACTOR.fullmatch(actor)
            or action not in STANDALONE_ACTIONS
            or not isinstance(target, str)
            or not 1 <= len(target) <= 200
            or not isinstance(reason, str)
        ):
            raise CommandError("invalid_audit", "Invalid audit metadata.", 422)
        now = _now()
        operation_id = uuid.uuid4().hex
        event = {
            "id": f"{operation_id}:succeeded",
            "time": _iso(now),
            "actor": actor,
            "action": action,
            "target": target,
            "result": "succeeded",
            "reason": reason.strip()[:500],
            "commandId": None,
        }
        self._persist_standalone(event, now)
        return event

    def _persist_standalone(self, event, now):
        try:
            with self.client.pipeline() as pipe:
                pipe.watch(self.key)
                expired = self._expired_provider_audit(pipe, now, reserve=1)
                pipe.multi()
                pipe.hset(self.key, f"audit:{event['id']}", _json(event))
                for start in range(0, len(expired), 400):
                    pipe.hdel(self.key, *expired[start : start + 400])
                pipe.execute()
        except Exception:
            raise CommandError(
                "audit_unavailable", "Audit persistence could not be confirmed.", 503
            ) from None

    def audit(self, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise CommandError("invalid_limit", "Audit limit must be between 1 and 200.", 422)
        if cursor is not None and (
            not isinstance(cursor, str)
            or not re.fullmatch(
                r"\d{13}:[0-9a-f]{32}:(?:accepted|finished|attempted|succeeded|failed|unknown)",
                cursor,
            )
        ):
            raise CommandError("invalid_cursor", "Invalid audit cursor.", 422)
        cutoff = _now() - self.retention_ms()
        events = [
            event
            for _, value in self.client.hscan_iter(self.key, match="audit:*", count=200)
            if (event := _decode(value))["commandId"] is not None or _event_ms(event) >= cutoff
        ]

        def position(event):
            return f"{_event_ms(event):013d}:{event['id']}"

        events.sort(key=position, reverse=True)
        if cursor:
            events = [event for event in events if position(event) < cursor]
        page = events[:limit]
        return {"items": page, "nextCursor": position(page[-1]) if len(events) > limit else None}

    def observe_workflow(self, consumer, document_hash, *, status="accepted", message=""):
        if status not in {"accepted", "rejected"} or not re.fullmatch(
            r"[0-9a-f]{64}", document_hash
        ):
            raise ValueError("Invalid workflow observation")
        now = _now()
        # Keep rejection separate: it must not replace the last accepted version.
        field = f"{consumer[:160]}:{status}"
        record = {
            "consumer": consumer[:160],
            "documentHash": document_hash,
            "observedAt": _iso(now),
            "status": status,
            "message": message[:300],
        }
        for _ in range(8):
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(self.observation_key)
                    records = {
                        _text(key): _decode(value)
                        for key, value in pipe.hscan_iter(self.observation_key, count=200)
                    }
                    records[field] = record
                    ordered = sorted(
                        records, key=lambda key: records[key]["observedAt"], reverse=True
                    )
                    expired = ordered[200:]
                    pipe.multi()
                    pipe.hset(self.observation_key, field, _json(record))
                    if expired:
                        pipe.hdel(self.observation_key, *expired)
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise CommandError("store_busy", "Workflow observation conflicted.", 503)

    def workflow_observations(self):
        return sorted(
            (
                _decode(value)
                for _, value in self.client.hscan_iter(self.observation_key, count=200)
            ),
            key=lambda item: item["observedAt"],
            reverse=True,
        )[:200]

    def run_history(self, issue_id, seq):
        return None  # Historical attempts are not retained by the core; never return a successor.

    def prune(self):
        for _ in range(8):
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(self.key)
                    now = _now()
                    keep_ms = self.retention_ms()
                    # Keep only compact retention metadata, never a history-sized
                    # collection of private workflow documents (including legacy ones).
                    terminal = []
                    for _, value in pipe.hscan_iter(self.key, match="command:*", count=20):
                        record = _decode(value)
                        if record["state"] in {"succeeded", "failed"}:
                            terminal.append(
                                (record["finishedMs"], record["id"], record["idempotencyField"])
                            )
                    terminal.sort(reverse=True)
                    fields = []
                    for index, (finished_ms, command_id, idempotency_field) in enumerate(terminal):
                        if index >= MAX_TERMINAL or now - finished_ms > keep_ms:
                            fields.extend(
                                [
                                    f"command:{command_id}",
                                    idempotency_field,
                                    f"audit:{command_id}:accepted",
                                    f"audit:{command_id}:finished",
                                ]
                            )
                    fields.extend(self._expired_provider_audit(pipe, now))
                    if not fields:
                        return
                    pipe.multi()
                    for start in range(0, len(fields), 400):
                        pipe.hdel(self.key, *fields[start : start + 400])
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise CommandError("store_busy", "Command retention conflicted.", 503)

    def pending(self, limit=20):
        entries = [
            (int(value), _text(key).split(":", 1)[1])
            for key, value in self.client.hscan_iter(self.key, match="inbox:*", count=200)
        ]
        return [command_id for _, command_id in sorted(entries)[:limit]]

    def transition(self, command_id, owner, change, *, expected_claim=None, issue_id=None):
        """Apply command, audit and optional lifecycle change under one watched lease.

        change(record, entry, pipe) only queues Redis writes after MULTI; it must not
        perform network effects. Redis TIME, not a captured tick timestamp, fences expiry.
        """
        for _ in range(8):
            with self.client.pipeline() as pipe:
                try:
                    keys = [self.key, state.LOCK_KEY]
                    if issue_id:
                        keys.append(state.issue_key(issue_id))
                    pipe.watch(*keys)
                    lease = _decode(pipe.get(state.LOCK_KEY))
                    seconds, micros = pipe.time()
                    now = seconds * 1000 + micros // 1000
                    if (
                        not lease
                        or lease.get("owner") != owner
                        or lease.get("lease_expires", 0) <= now
                    ):
                        raise CommandError("lease_lost", "Daemon lease was lost.", 503)
                    record = _decode(pipe.hget(self.key, f"command:{command_id}"))
                    if record is None or record["state"] in {"succeeded", "failed"}:
                        return record
                    if expected_claim is not None and record.get("claim") != expected_claim:
                        raise CommandError(
                            "claim_lost", "Command was claimed by another daemon.", 503
                        )
                    entry = _decode(pipe.get(state.issue_key(issue_id))) if issue_id else None
                    pipe.multi()
                    change(record, entry, pipe)
                    values = {}
                    if record["state"] in {"succeeded", "failed"}:
                        # The retained idempotency fingerprint is sufficient for replay;
                        # private patches must not accumulate in terminal history.
                        record.update(finishedAt=_iso(now), finishedMs=now, payload=None)
                        values[f"audit:{command_id}:finished"] = _json(
                            _event(record, "finished", now)
                        )
                        pipe.hdel(self.key, f"inbox:{command_id}")
                    values[f"command:{command_id}"] = _json(record)
                    pipe.hset(self.key, mapping=values)
                    pipe.execute()
                    return record
                except WatchError:
                    continue
        raise CommandError("store_busy", "Command reconciliation conflicted.", 503)


def _target_entry(record, entry):
    if entry is None or entry.get("dispatch_seq") != record["target"]["dispatchSeq"]:
        raise CommandError("stale_target", "The selected run is no longer current.")
    return entry


def process_commands(store, kv, tracker, cfg, fx, owner, now_ms, *, publish_workflow, authorize):
    # The lease holder is the one process that applies the WORKFLOW on each tick.
    store.apply_retention(cfg)
    completed = 0
    for command_id in store.pending():
        claim = uuid.uuid4().hex

        def start(record, entry, pipe, claim=claim):
            record.update(state="applying", claim=claim, result="Applying in daemon.")

        record = store.transition(command_id, owner, start)
        if record is None or record["state"] in {"succeeded", "failed"}:
            continue
        target = record["target"]
        issue_id = target["issueId"] if target else None
        try:
            action = record["action"]
            if action != "workflow" and not authorize(record) and not record.get("interrupted"):
                raise CommandError(
                    "authorization_revoked",
                    "The submitting principal no longer has this permission.",
                    403,
                )
            issue = None
            # A hold is the human's way out (takeover): it must apply during a tracker
            # outage, and it needs nothing from the tracker.
            if issue_id and action not in {"hold", "release"} and not record.get("interrupted"):
                found = tracker.fetch_issues_by_ids([issue_id])
                issue = next((item for item in found if item.id == issue_id), None)
            if action == "workflow":

                def before_write(command_id=command_id, claim=claim):
                    current = store.transition(
                        command_id, owner, lambda r, e, p: None, expected_claim=claim
                    )
                    if (
                        current is None
                        or current["state"] != "applying"
                        or current.get("claim") != claim
                    ):
                        raise CommandError(
                            "claim_lost", "Command was claimed by another daemon.", 503
                        )
                    if not authorize(current):
                        raise CommandError(
                            "authorization_revoked",
                            "The submitting principal no longer has this permission.",
                            403,
                        )

                # Replay may confirm a committed outcome without current credentials;
                # every fresh write must pass the guard after all remote preparation.
                revision = publish_workflow(
                    record["payload"], command_id, before_write=before_write
                )

                def published(r, e, p, revision=revision):
                    r.update(
                        state="succeeded",
                        revision=revision,
                        result="Workflow published; consumer observation is separate.",
                    )

                store.transition(command_id, owner, published, expected_claim=claim)
            elif action == "interrupt":

                def fence(r, entry, pipe, command_id=command_id, issue=issue, issue_id=issue_id):
                    entry = _target_entry(r, entry)
                    if entry.get("admin_interrupt") != command_id:
                        if entry.get("status") not in {"running", "claimed"}:
                            raise CommandError(
                                "not_running", "The selected run is not running or claimed."
                            )
                        if not authorize(r):
                            raise CommandError(
                                "authorization_revoked",
                                "The submitting principal no longer has this permission.",
                                403,
                            )
                        if (
                            issue is None
                            or issue.state_type != "active"
                            or issue.state not in cfg.active_states
                            or entry.get("mode")
                        ):
                            raise CommandError(
                                "human_gate",
                                "Human, terminal and review-answer work cannot be interrupted here.",
                            )
                        entry.update(
                            status="interrupting", bridge_token=None, admin_interrupt=command_id
                        )
                        pipe.set(state.issue_key(issue_id), _json(entry), ex=kv.ttl_s)
                    r["interrupted"] = True

                record = store.transition(
                    command_id, owner, fence, expected_claim=claim, issue_id=issue_id
                )
                entry = kv.get(state.issue_key(issue_id))
                if entry.get("admin_interrupt") != command_id:
                    raise CommandError(
                        "stale_target", "The selected run changed before cancellation."
                    )
                # Derive the deterministic Job even when the runner has not started yet.
                call_id = entry.get("runner_call_id")
                if not call_id:
                    from .kube import runner_job_name

                    call_id = runner_job_name(entry["identifier"], target["dispatchSeq"])
                store.transition(
                    command_id,
                    owner,
                    lambda r, e, p: _target_entry(r, e),
                    expected_claim=claim,
                    issue_id=issue_id,
                )
                fx.cancel_runner(call_id, issue_id=issue_id, seq=target["dispatchSeq"])
                store.transition(
                    command_id,
                    owner,
                    lambda r, e, p: _target_entry(r, e),
                    expected_claim=claim,
                    issue_id=issue_id,
                )
                fx.kill_agent_process(
                    entry.get("sandbox_name"), issue_id=issue_id, seq=target["dispatchSeq"]
                )

                def interrupted(r, current, pipe, command_id=command_id, issue_id=issue_id):
                    current = _target_entry(r, current)
                    if current.get("admin_interrupt") != command_id:
                        raise CommandError("stale_target", "The interrupted run changed.")
                    failed = current["attempt"]
                    current.update(
                        status="retry_queued",
                        attempt=failed + 1,
                        retry_at=_now()
                        + orchestrator.compute_backoff_ms(failed, cfg.max_retry_backoff_ms),
                        runner_call_id=None,
                        stall_strikes=0,
                        bridge_token=None,
                        failure_summary="Interrupted by an authorized operator.",
                        updated_at=_now(),
                    )
                    if not authorize(r):
                        # Stopping an already fenced process is mandatory safety cleanup,
                        # not a fresh privileged mutation. Revocation must never schedule work.
                        current.update(
                            status="blocked", retry_at=None, admin_authorization_revoked=True
                        )
                        r.update(
                            state="failed",
                            result="Authorization revoked; previously fenced run stopped and held.",
                        )
                    else:
                        r.update(
                            state="succeeded",
                            result="Run fenced and stopped; retry follows normal policy.",
                        )
                    pipe.set(state.issue_key(issue_id), _json(current), ex=kv.ttl_s)

                store.transition(
                    command_id, owner, interrupted, expected_claim=claim, issue_id=issue_id
                )
            elif action in {"hold", "release"}:
                # internals §12. Only the key is written here; the tick that follows in the
                # same daemon step stops a running attempt and parks it, workspace kept.

                def hold(r, entry, pipe, action=action, issue_id=issue_id):
                    if not authorize(r):
                        raise CommandError(
                            "authorization_revoked",
                            "The submitting principal no longer has this permission.",
                            403,
                        )
                    if action == "hold":
                        held = state.hold_record(str(r.get("actor") or ""), r["reason"], _now())
                        # No expiry (internals §12): held until released, however long the
                        # daemon is away — reads never add a TTL (kv.NO_TTL_PREFIXES).
                        pipe.set(state.hold_key(issue_id), _json(held))
                        result = "Ticket held: Symphony stops its run and stays out until released."
                    else:
                        pipe.delete(state.hold_key(issue_id))
                        result = "Ticket released: Symphony may dispatch it again."
                    r.update(state="succeeded", result=result)

                store.transition(command_id, owner, hold, expected_claim=claim)
            else:

                def apply(
                    r,
                    entry,
                    pipe,
                    action=action,
                    command_id=command_id,
                    issue=issue,
                    issue_id=issue_id,
                ):
                    if not authorize(r):
                        raise CommandError(
                            "authorization_revoked",
                            "The submitting principal no longer has this permission.",
                            403,
                        )
                    if action in {"pause", "resume"}:
                        pipe.set(store.admission_key, "true" if action == "pause" else "false")
                        result = (
                            "Admission paused; existing work continues."
                            if action == "pause"
                            else "Admission resumed."
                        )
                    elif action == "refresh":
                        pipe.set(state.REFRESH_KEY, _json({"commandId": command_id}), ex=kv.ttl_s)
                        result = "Reconciliation requested for the next tick."
                    else:
                        entry = _target_entry(r, entry)
                        if entry.get("admin_authorization_revoked"):
                            entry.pop("admin_authorization_revoked")
                        if entry.get("status") not in {"blocked", "retry_queued"} or entry.get(
                            "mode"
                        ):
                            raise CommandError(
                                "ineligible_target",
                                "Only blocked or queued implementation work can be reevaluated.",
                            )
                        if (
                            issue is None
                            or issue.state not in cfg.active_states
                            or not orchestrator.eligible(issue, cfg, set())
                            or (
                                entry.get("assignee") is not None
                                and issue.assignee != entry["assignee"]
                            )
                        ):
                            raise CommandError(
                                "human_gate", "Tracker prerequisites do not permit this work."
                            )
                        # A human looked: a ticket the retry budget had stopped gets a new one.
                        entry.update(
                            status="retry_queued",
                            retry_at=max(entry.get("retry_at") or 0, _now()),
                            updated_at=_now(),
                            failures=0,
                            blocked_reason=None,
                            exhaustion_reported_at=None,
                        )
                        pipe.set(state.issue_key(issue_id), _json(entry), ex=kv.ttl_s)
                        result = "Eligibility refreshed; admission, backoff and capacity still govern dispatch."
                    r.update(state="succeeded", result=result)

                store.transition(command_id, owner, apply, expected_claim=claim, issue_id=issue_id)
            completed += 1
        except Exception as exc:  # no raw provider/Redis messages in public commands
            code = getattr(exc, "code", "source_unavailable")
            status_code = getattr(exc, "status_code", 503)
            if status_code >= 500 or code in {"lease_lost", "claim_lost"}:
                # Keep the FIFO head pending: an unknown effect outcome is never success,
                # and a successor must not overtake unfinished interruption/publication.
                break

            def failed(r, entry, pipe, code=code):
                r.update(state="failed", result=f"Command rejected ({code}).")

            store.transition(command_id, owner, failed, expected_claim=claim)
            completed += 1
    store.prune()
    return {"completed": completed}
