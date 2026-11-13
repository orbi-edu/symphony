"""API tokens: simple, per person, revocable, hashed at rest.

A token is `sym_<id>_<secret>`. The server stores only the id, a SHA-256 of the secret (a
32-byte random secret needs no slow hash), the role, and timestamps. `create` returns the full
token exactly once. Revocation and expiry take effect on the next request: verification reads
Redis every time, with no cache to invalidate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time

ROLES = ("viewer", "operator", "admin")
_RANK = {role: i for i, role in enumerate(ROLES)}
_TOKEN = re.compile(r"sym_([0-9a-f]{8})_([A-Za-z0-9_-]{43})\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._@-]{0,63}\Z")
LAST_USED_RESOLUTION_MS = 60_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def role_allows(role: str, required: str) -> bool:
    return _RANK.get(role, -1) >= _RANK[required]


class TokenError(Exception):
    pass


class TokenStore:
    def __init__(self, client, namespace: str, *, now_ms=_now_ms) -> None:
        self.client = client
        self.key = f"sym:{namespace}:tokens"
        self._now = now_ms

    def create(
        self, name: str, role: str, *, actor: str, expires_in_s: int | None = None
    ) -> tuple[str, dict]:
        if role not in ROLES:
            raise TokenError(f"role must be one of {', '.join(ROLES)}")
        if not _NAME.fullmatch(name):
            raise TokenError("name: 1-64 letters, digits, spaces or ._@-")
        if expires_in_s is not None and expires_in_s <= 0:
            raise TokenError("expiry must be positive")
        token_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        now = self._now()
        record = {
            "id": token_id,
            "name": name,
            "role": role,
            "hash": _digest(secret),
            "created_at": now,
            "created_by": actor,
            "expires_at": now + expires_in_s * 1000 if expires_in_s else None,
            "last_used_at": None,
            "revoked_at": None,
        }
        if not self.client.hsetnx(self.key, token_id, json.dumps(record)):
            return self.create(name, role, actor=actor, expires_in_s=expires_in_s)
        return f"sym_{token_id}_{secret}", _public(record)

    def ensure_bootstrap(self, token: str) -> None:
        """Install the installer's admin token (from a Secret) when it is not present yet."""
        match = _TOKEN.fullmatch(token)
        if not match:
            raise TokenError("bootstrap token must look like sym_<8 hex>_<43 chars>")
        token_id, secret = match.groups()
        record = {
            "id": token_id, "name": "bootstrap", "role": "admin", "hash": _digest(secret),
            "created_at": self._now(), "created_by": "install", "expires_at": None,
            "last_used_at": None, "revoked_at": None,
        }
        self.client.hsetnx(self.key, token_id, json.dumps(record))

    def verify(self, token: str) -> dict | None:
        match = _TOKEN.fullmatch(token or "")
        if not match:
            return None
        token_id, secret = match.groups()
        raw = self.client.hget(self.key, token_id)
        if raw is None:
            return None
        record = json.loads(raw)
        if not hmac.compare_digest(record["hash"], _digest(secret)):
            return None
        now = self._now()
        if record.get("revoked_at") or (record.get("expires_at") and record["expires_at"] <= now):
            return None
        last = record.get("last_used_at") or 0
        if now - last >= LAST_USED_RESOLUTION_MS:
            record["last_used_at"] = now
            self.client.hset(self.key, token_id, json.dumps(record))
        return _public(record)

    def list(self) -> list[dict]:
        rows = [json.loads(v) for v in self.client.hvals(self.key)]
        return sorted((_public(r) for r in rows), key=lambda r: r["created_at"])

    def revoke(self, token_id: str, *, actor: str) -> dict:
        raw = self.client.hget(self.key, token_id)
        if raw is None:
            raise TokenError("unknown token")
        record = json.loads(raw)
        if not record.get("revoked_at"):
            record.update(revoked_at=self._now(), revoked_by=actor)
            self.client.hset(self.key, token_id, json.dumps(record))
        return _public(record)


def _public(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "hash"}
