"""Who is calling: the identity behind a `sym_` API token, and the daemon's command fence.

Every person and integration authenticates with an API token (`apitokens.TokenStore`). A token
is revocable and may expire; revocation takes effect on the next request because verification
always reads Redis. Durable work accepted on someone's behalf (admin commands) keeps the token
id, and the daemon that later executes it re-checks that the token is still active with a
sufficient role, so a queued intent never outlives the privilege that created it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import cache

from .apitokens import TokenStore, role_allows

# The role each durable admin action needs when the daemon executes it.
COMMAND_ROLES = {
    "workflow": "admin",
    "pause": "operator",
    "resume": "operator",
    "refresh": "operator",
    "reevaluate": "operator",
    "interrupt": "operator",
    "hold": "operator",
    "release": "operator",
}


@dataclass(frozen=True)
class Caller:
    """An authenticated token. `name` is the actor recorded in intents and audit."""

    token_id: str
    name: str
    role: str

    def allows(self, required: str) -> bool:
        return role_allows(self.role, required)

    def public(self) -> dict:
        return {"name": self.name, "role": self.role, "token_id": self.token_id}


def token_record(tokens: TokenStore, token_id: str) -> dict | None:
    """The public record of a token by id (no secret, no hash)."""
    if not isinstance(token_id, str) or not token_id:
        return None
    raw = tokens.client.hget(tokens.key, token_id)
    if raw is None:
        return None
    record = json.loads(raw)
    record.pop("hash", None)
    return record


def token_active(tokens: TokenStore, token_id: str, required: str, *, now_ms=None) -> bool:
    """True when the token exists, is neither revoked nor expired, and has `required`."""
    record = token_record(tokens, token_id)
    if record is None or record.get("revoked_at"):
        return False
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if record.get("expires_at") and record["expires_at"] <= now:
        return False
    return role_allows(record.get("role", ""), required)


def authorize_command(command: dict, namespace: str, tokens: TokenStore | None = None) -> bool:
    """Daemon fence: execute a queued admin command only while its token is still entitled."""
    required = COMMAND_ROLES.get(command.get("action"))
    if required is None:
        return False
    try:
        store = tokens if tokens is not None else _default_tokens(namespace)
        return token_active(store, command.get("credentialFingerprint"), required)
    except Exception:  # noqa: BLE001 — an unreadable fence never authorizes
        return False


@cache
def _default_tokens(namespace: str) -> TokenStore:
    from .kv import RedisKV

    return TokenStore(RedisKV().client, namespace)
