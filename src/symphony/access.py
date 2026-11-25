"""Cloudflare Access identity for browser routes (`api.access` in WORKFLOW.md).

Behind Cloudflare Access every request carries `Cf-Access-Jwt-Assertion`: a JWT that Access
signs (RS256) with the team's keys, published at `https://<team_domain>/cdn-cgi/access/certs`.
`AccessVerifier.verify` checks its signature against those keys (cached), its `aud` (the
application's audience tag), `iss` (`https://<team_domain>`) and `exp`, and returns the
claims. The `Cf-Access-Authenticated-User-Email` header is never trusted: only the e-mail
inside a verified token is.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

import httpx
import jwt

HEADER = "cf-access-jwt-assertion"
JWKS_TTL_S = 3600
# An unknown `kid` refetches the keys (Access rotates them), at most this often.
REFETCH_MIN_S = 30
LEEWAY_S = 30


class AccessError(Exception):
    """The token is missing, malformed, signed by an unknown key, or has the wrong claims."""


def _default_http() -> httpx.Client:
    return httpx.Client(timeout=10)


class AccessVerifier:
    def __init__(self, team_domain: str, aud: str, *,
                 http: Callable[[], httpx.Client] = _default_http,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.issuer = f"https://{team_domain}"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.aud = aud
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, object] = {}
        self._fetched_at: float | None = None

    def _fetch(self) -> None:
        with self._http() as http:
            resp = http.get(self.certs_url)
        if resp.status_code != 200:
            raise AccessError(f"the Access keys could not be read (HTTP {resp.status_code})")
        keys: dict[str, object] = {}
        for jwk in resp.json().get("keys") or []:
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not jwk.get("kid"):
                continue
            keys[jwk["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        self._keys, self._fetched_at = keys, self._clock()

    def _key(self, kid: str) -> object:
        with self._lock:
            now = self._clock()
            age = None if self._fetched_at is None else now - self._fetched_at
            if age is None or age >= JWKS_TTL_S or (kid not in self._keys and age >= REFETCH_MIN_S):
                try:
                    self._fetch()
                except (AccessError, httpx.HTTPError, ValueError) as exc:
                    if not self._keys:  # no keys yet; stale keys still verify
                        raise AccessError("the Access keys could not be read") from exc
                    self._fetched_at = now
            key = self._keys.get(kid)
        if key is None:
            raise AccessError("the token is signed by an unknown key")
        return key

    def verify(self, token: str) -> dict:
        """The verified claims of an Access token. Raises AccessError."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AccessError("the token is malformed") from None
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise AccessError("the token is not an RS256 Access token")
        key = self._key(header["kid"])
        try:
            return jwt.decode(token, key, algorithms=["RS256"], audience=self.aud,
                              issuer=self.issuer, leeway=LEEWAY_S,
                              options={"require": ["exp", "iss", "aud"]})
        except jwt.PyJWTError as exc:
            raise AccessError(f"the token was refused ({type(exc).__name__})") from None
