"""Operations shared by the REST routes and the MCP tools, so both enforce the same rules."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .. import state, wake
from ..admin_auth import Caller
from ..admin_commands import AdminStore, CommandError, public_command
from ..admin_read import LiveReader
from ..apitokens import TokenStore
from ..mcp_servers import session_leases
from ..merger.store import MergerStore, normalize_repo, registered_repos, wake_target
from ..sessions import STATES, SessionStore
from ..teamconfig import TeamConfig
from .catalog import Catalog
from .errors import ApiError, invalid, not_found

MODES = ("auto", "steer", "follow_up")
COMMANDS = ("pause", "resume", "refresh", "reevaluate", "hold", "release")
MERGER_ACTIONS = ("bypass", "requeue", "release")
MAX_TEXT = 20_000
_SID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


def etag_of(document: Any) -> str:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode()
    return '"' + hashlib.sha256(raw).hexdigest()[:32] + '"'


def check_sid(sid: str) -> str:
    if not _SID.fullmatch(sid or ""):
        raise invalid("invalid session id")
    return sid


class MergerFleet:
    """Every repository a merger serves in this namespace (one Deployment per repository)."""

    def __init__(self, client, namespace: str) -> None:
        self.client = client
        self.namespace = namespace

    def repos(self) -> list[str]:
        return registered_repos(self.client, self.namespace)

    def store(self, repo: str) -> MergerStore:
        return MergerStore(self.client, self.namespace, repo)


@dataclass
class Services:
    """Everything the API reads and writes. Optional parts are absent when not deployed."""

    client: Any
    namespace: str
    sessions: SessionStore
    tokens: TokenStore
    teamconfig: TeamConfig
    store: AdminStore
    kv: Any
    reader: LiveReader
    catalog: Catalog
    aclient: Any = None
    workflow: Any = None
    providers: Any = None
    merger: Any = None
    projection: Any = None
    secrets: Any = None  # setup_secrets.SecretStore
    mcp: Any = None  # mcp_servers.McpRepository
    previews: Any = None  # previews.PreviewManager: preview links (`api/previews.py`)
    # Webhook signing secrets; None reads LINEAR_/GITHUB_WEBHOOK_SECRET from the env per request.
    linear_secret: str | None = None
    github_secret: str | None = None
    ping_interval_s: float = 15.0
    now_ms: Any = field(default=_now_ms)


class Control:
    def __init__(self, services: Services) -> None:
        self.s = services
        self.setup = None  # api.setup.SetupService, set by build_app

    # -- sessions ------------------------------------------------------------
    def list_sessions(self, states: list[str] | None = None, limit: int = 50) -> list[dict]:
        if states and any(item not in STATES for item in states):
            raise invalid(f"state must be one of {', '.join(STATES)}")
        if not 1 <= limit <= 500:
            raise invalid("limit must be between 1 and 500")
        items = self.s.sessions.list(limit=limit, states=states or None)
        return self._with_leases(items)

    def _with_leases(self, items: list[dict]) -> list[dict]:
        """`mcp_leases`: what each session holds on MCP servers (one Redis pipeline)."""
        held = session_leases(self.s.client, self.s.namespace, [item["sid"] for item in items])
        return [{**item, "mcp_leases": held.get(item["sid"], [])} for item in items]

    def meta(self, sid: str) -> dict:
        meta = self.s.sessions.meta(check_sid(sid))
        if meta is None:
            raise not_found("session")
        return self._with_leases([meta])[0]

    def get_session(self, sid: str, transcript_limit: int = 4000) -> dict:
        if not 0 <= transcript_limit <= 4000:
            raise invalid("transcript_limit must be between 0 and 4000")
        meta = self.meta(sid)
        # Read the live cursor BEFORE the transcript: a viewer replays the transcript, then tails
        # events after this id, so nothing written in between is lost.
        last_event_id = self.s.sessions.last_event_id(sid)
        transcript = (
            self.s.sessions.transcript(sid, limit=transcript_limit) if transcript_limit else []
        )
        return {"meta": meta, "transcript": transcript, "last_event_id": last_event_id}

    def send_message(self, caller: Caller, sid: str, text: str, mode: str = "auto") -> dict:
        if mode not in MODES:
            raise invalid(f"mode must be one of {', '.join(MODES)}")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise invalid(f"text must have 1-{MAX_TEXT} characters")
        meta = self.meta(sid)
        if meta.get("state") == "ended":
            raise ApiError(409, "session_ended", "this session has ended")
        pending = meta.get("input_request")
        if mode == "auto":
            if isinstance(pending, dict) and pending.get("id"):
                kind = "answer"
            elif meta.get("state") == "running":
                kind = "steer"
            else:
                kind = "follow_up"
        else:
            kind = mode
        payload = {"text": text}
        if kind == "answer":
            payload["request_id"] = pending["id"]
        intent_id = self.s.sessions.send_intent(sid, kind, actor=caller.name, **payload)
        return {"intent_id": intent_id, "kind": kind}

    def interrupt(self, caller: Caller, sid: str, reason: str = "") -> dict:
        if not isinstance(reason, str) or len(reason) > 1000:
            raise invalid("reason must have at most 1000 characters")
        meta = self.meta(sid)
        if meta.get("state") == "ended":
            raise ApiError(409, "session_ended", "this session has ended")
        intent_id = self.s.sessions.send_intent(
            sid, "interrupt", actor=caller.name, reason=reason.strip() or "interrupted by operator"
        )
        return {"intent_id": intent_id, "kind": "interrupt"}

    def export_jsonl(self, sid: str) -> str:
        """The transcript as an omp session file: header line, then one entry per line."""
        meta = self.meta(sid)
        entries = self.s.sessions.transcript(sid, limit=4000)
        lines = []
        if not entries or entries[0].get("type") != "session":
            created = meta.get("created_at") or self.s.now_ms()
            lines.append({
                "type": "session",
                "version": 3,
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"symphony:{self.s.namespace}:{sid}")),
                "title": meta.get("title") or meta.get("identifier") or sid,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(created / 1000)),
                "cwd": ".",
            })
        lines.extend(entries)
        return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)

    # -- board ---------------------------------------------------------------
    def overview(self) -> dict:
        return self.s.reader.overview(self.s.now_ms())

    def issues(self) -> list[dict]:
        return self.s.reader.issues()

    def _resolve_target(self, target: Any) -> dict:
        if isinstance(target, dict):
            if set(target) != {"issueId", "dispatchSeq"}:
                raise invalid("target must be an issue id/identifier or {issueId, dispatchSeq}")
            issue_id, seq = target["issueId"], target["dispatchSeq"]
            if not isinstance(issue_id, str) or type(seq) is not int:
                raise invalid("target must be an issue id/identifier or {issueId, dispatchSeq}")
            raw = self.s.client.get(state.issue_key(issue_id))
            entry = json.loads(raw) if raw else None
            if not isinstance(entry, dict) or entry.get("dispatch_seq") != seq:
                raise ApiError(409, "stale_target", "this issue attempt is no longer current")
            return {"target": target, "entry": entry}
        if not isinstance(target, str) or not 1 <= len(target) <= 200:
            raise invalid("target must be an issue id/identifier or {issueId, dispatchSeq}")
        raw = self.s.client.get(state.issue_key(target))
        found = (target, json.loads(raw)) if raw else None
        if found is None:
            for issue_id, item in self.s.reader.issue_entries():
                if str(item.get("identifier", "")).casefold() == target.casefold():
                    found = (issue_id, item)
                    break
        if found is None or not isinstance(found[1].get("dispatch_seq"), int):
            raise not_found("tracked issue")
        issue_id, entry = found
        return {"target": {"issueId": issue_id, "dispatchSeq": entry["dispatch_seq"]}, "entry": entry}

    def _hold_target(self, action: str, target: Any) -> dict:
        """`{issueId}` as given (a ticket may be held before Symphony ever dispatched it), or an
        issue id / identifier Symphony tracks, plus whether it is held right now: holding twice
        or releasing what is not held is a 409, so a client learns at once its view was stale."""
        if isinstance(target, dict):
            if set(target) != {"issueId"} or not isinstance(target["issueId"], str) or not (
                1 <= len(target["issueId"]) <= 200
            ):
                raise invalid("target must be an issue id/identifier or {issueId}")
            issue_id = target["issueId"]
        elif isinstance(target, str) and 1 <= len(target) <= 200:
            issue_id = target if self.s.client.exists(state.issue_key(target)) else None
            if issue_id is None:
                for candidate, item in self.s.reader.issue_entries():
                    if str(item.get("identifier", "")).casefold() == target.casefold():
                        issue_id = candidate
                        break
            if issue_id is None:
                raise not_found("tracked issue")
        else:
            raise invalid(f"{action} needs a target issue")
        return {"target": {"issueId": issue_id},
                "held": bool(self.s.client.exists(state.hold_key(issue_id)))}

    def command(
        self, caller: Caller, action: str, reason: str, target: Any = None,
        idempotency_key: str | None = None,
    ) -> dict:
        if action not in COMMANDS:
            raise invalid(f"action must be one of {', '.join(COMMANDS)}")
        if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 1000:
            raise invalid("reason must have 8-1000 characters")
        if idempotency_key is not None and not _IDEMPOTENCY.fullmatch(idempotency_key):
            raise invalid("Idempotency-Key must have 8-128 letters, digits, - or _")
        key = idempotency_key or uuid.uuid4().hex
        resolved = None
        if action == "reevaluate":
            if target is None:
                raise invalid("reevaluate needs a target issue")
            resolved = self._resolve_target(target)
        elif action in ("hold", "release"):
            resolved = self._hold_target(action, target)
        elif target is not None:
            raise invalid("this action targets the whole namespace")
        normalized = resolved["target"] if resolved else None
        try:
            existing = self.s.store.lookup(
                actor=caller.name, action=action, idempotency_key=key,
                target=normalized, reason=reason,
            )
            if existing is not None:
                return public_command(existing)
            if (
                action == "reevaluate"
                and resolved["entry"].get("status") not in {"blocked", "retry_queued"}
            ):
                raise ApiError(409, "stale_target", "this issue is not eligible for reevaluation")
            if action == "hold" and resolved["held"]:
                raise ApiError(409, "already_held", "this ticket is already held by a person")
            if action == "release" and not resolved["held"]:
                raise ApiError(409, "not_held", "this ticket is not held")
            record = self.s.store.accept(
                actor=caller.name, role=caller.role, credential_fingerprint=caller.token_id,
                action=action, target=normalized, reason=reason, idempotency_key=key,
            )
        except CommandError as exc:
            raise ApiError(exc.status_code, exc.code, exc.message) from None
        wake.wake(self.s.client, self.s.namespace, wake.DAEMON, f"command:{action}")
        return public_command(record)

    def get_command(self, command_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", command_id or ""):
            raise invalid("invalid command id")
        record = self.s.store.get(command_id)
        if record is None:
            raise not_found("command")
        return public_command(record)

    # -- merger --------------------------------------------------------------
    # One merger Deployment per repository; `s.merger` is the fleet: `repos()` lists every
    # registered repository, `store(repo)` is that repository's MergerStore.
    def _merger(self):
        if self.s.merger is None:
            raise ApiError(503, "merger_unavailable", "the merger is not deployed")
        return self.s.merger

    def merger_status(self) -> dict:
        # Each snapshot already names its repository and says whether its merger is alive.
        fleet = self._merger()
        return {"repos": [fleet.store(repo).snapshot() for repo in sorted(fleet.repos())]}

    def _merger_repo(self, repo: str | None) -> str:
        known = self._merger().repos()
        if repo is None:
            if len(known) == 1:
                return next(iter(known))
            if not known:
                raise ApiError(404, "unknown_repo", "no merger has registered a repository")
            raise ApiError(
                422, "repo_required", "several repositories have a merger; name the repository"
            )
        try:
            normalized = normalize_repo(repo)
        except ValueError:
            raise ApiError(404, "unknown_repo", "the repository must be owner/name") from None
        if normalized not in known:
            raise ApiError(404, "unknown_repo", f"no merger serves {normalized}")
        return normalized

    def merger_action(
        self, caller: Caller, pr: int, action: str, reason: str, repo: str | None = None
    ) -> dict:
        if action not in MERGER_ACTIONS:
            raise not_found("merger action")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise invalid("reason must have 1-1000 characters")
        repo = self._merger_repo(repo)
        try:
            record = self._merger().store(repo).request_action(
                pr, action, actor=caller.name, reason=reason.strip()
            )
        except ValueError as exc:
            raise invalid(str(exc)) from None
        wake.wake(self.s.client, self.s.namespace, wake_target(repo), f"action:{action}:{pr}")
        self._audit(caller, f"merger.{action}", f"pr:{repo}#{pr}", reason)
        return {**record, "repo": repo}

    def _audit(self, caller: Caller, action: str, target: str, reason: str) -> None:
        try:
            self.s.store.audit_event(actor=caller.name, action=action, target=target, reason=reason)
        except CommandError as exc:
            raise ApiError(exc.status_code, exc.code, exc.message) from None
