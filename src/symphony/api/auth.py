"""Bearer authentication with `sym_` API tokens, role gates and per-token rate limits.

Every request verifies its token against Redis (`TokenStore.verify`), so revocation and expiry
apply on the next request. Limits are fixed one-minute windows in Redis, shared by every API
replica: per token for authenticated calls, per client address for failed authentications.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from fastapi import Request

from ..admin_auth import Caller
from ..apitokens import ROLES, TokenStore
from .errors import ApiError

WINDOW_S = 60
_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="symphony"'}


class Authenticator:
    def __init__(
        self,
        client,
        namespace: str,
        tokens: TokenStore,
        *,
        per_minute: int = 600,
        failures_per_minute: int = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.tokens = tokens
        self.prefix = f"sym:{namespace}:ratelimit"
        self.per_minute = per_minute
        self.failures_per_minute = failures_per_minute
        self._clock = clock

    def _hit(self, bucket: str, limit: int) -> int | None:
        """Count one hit; returns the seconds to wait when the window is exhausted."""
        now = self._clock()
        window = int(now // WINDOW_S)
        key = f"{self.prefix}:{bucket}:{window}"
        pipe = self.client.pipeline()
        pipe.incr(key)
        pipe.expire(key, WINDOW_S + 5)
        count, _ = pipe.execute()
        if count > limit:
            return max(1, int((window + 1) * WINDOW_S - now))
        return None

    def _reject(self, request: Request, code: str, message: str) -> ApiError:
        address = request.client.host if request.client else "unknown"
        wait = self._hit(f"fail:{address}", self.failures_per_minute)
        if wait is not None:
            return ApiError(
                429, "rate_limited", "too many failed authentications", {"Retry-After": str(wait)}
            )
        return ApiError(401, code, message, _CHALLENGE)

    def caller(self, request: Request) -> Caller:
        cached = getattr(request.state, "caller", None)
        if cached is not None:
            return cached
        header = request.headers.get("authorization")
        if not header:
            raise ApiError(401, "unauthorized", "a bearer token is required", _CHALLENGE)
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise self._reject(request, "unauthorized", "a bearer token is required")
        record = self.tokens.verify(token.strip())
        if record is None:
            raise self._reject(request, "invalid_token", "the token is invalid, revoked or expired")
        wait = self._hit(f"token:{record['id']}", self.per_minute)
        if wait is not None:
            raise ApiError(429, "rate_limited", "rate limit exceeded", {"Retry-After": str(wait)})
        caller = Caller(token_id=record["id"], name=record["name"], role=record["role"])
        request.state.caller = caller
        return caller

    def require(self, request: Request, role: str) -> Caller:
        caller = self.caller(request)
        if not caller.allows(role):
            raise ApiError(403, "forbidden", f"this action requires the {role} role")
        return caller

    def dependency(self, role: str) -> Callable[[Request], Caller]:
        if role not in ROLES:
            raise ValueError(role)

        def dependency(request: Request) -> Caller:
            return self.require(request, role)

        return dependency
