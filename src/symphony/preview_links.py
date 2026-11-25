"""Preview links: a signed, durable URL per PR build that opens it in Seiji from the PR.

The merger signs one link per (repository, PR, head sha) whose preview artifact it found and
lists the newest `KEPT` on the PR (`merger.approval.check_preview`); the API serves it
(`api/previews.py`). The signature is HMAC-SHA256 over the three values with
`SYMPHONY_PREVIEW_LINK_SECRET`. A link has no expiry of its own: the artifact's retention on
GitHub bounds it. It authorizes nothing alone unless `previews.public_links` is on; behind
Cloudflare Access (`api.access`) the person must also be mapped in `notify.users`.

The builds the merger listed are kept in Redis (`builds_key`) with their links, so an answer
run can hand them out (`runner`) without holding the secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import urlencode

SECRET_ENV = "SYMPHONY_PREVIEW_LINK_SECRET"
OPEN_PATH = "/v1/previews/open"
KEPT = 5
_TTL_S = 90 * 86400  # GitHub's longest artifact retention
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
SHA12 = re.compile(r"[0-9a-f]{12}\Z")


def secret() -> str | None:
    value = (os.environ.get(SECRET_ENV) or "").strip()
    return value or None


def _message(repo: str, pr: int, sha12: str) -> bytes:
    return f"{repo.lower()}\n{int(pr)}\n{sha12.lower()}".encode()


def sign(key: str, repo: str, pr: int, sha12: str) -> str:
    digest = hmac.new(key.encode(), _message(repo, pr, sha12), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def verify(key: str, repo: str, pr: int, sha12: str, sig: str) -> bool:
    return hmac.compare_digest(sign(key, repo, pr, sha12), sig or "")


def url(base: str, key: str, repo: str, pr: int, sha12: str) -> str:
    query = urlencode({"repo": repo, "pr": int(pr), "sha": sha12,
                       "sig": sign(key, repo, pr, sha12)})
    return f"{base.rstrip('/')}{OPEN_PATH}?{query}"


def builds_key(namespace: str, repo: str, pr: int) -> str:
    return f"sym:{namespace}:preview:builds:{repo.lower()}:{int(pr)}"


def document(client, namespace: str, repo: str, pr: int) -> dict:
    """`{comment: url | None, builds: [{sha, sha12, url, at}]}`, newest build first."""
    raw = client.get(builds_key(namespace, repo, pr))
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    builds = [b for b in doc.get("builds") or [] if isinstance(b, dict) and b.get("url")]
    return {"comment": doc.get("comment"), "builds": builds}


def builds(client, namespace: str, repo: str, pr: int) -> list[dict]:
    return document(client, namespace, repo, pr)["builds"]


def with_build(doc: dict, head_sha: str, link: str, now: float | None = None) -> list[dict]:
    """The build list with `head_sha` first; the oldest beyond `KEPT` drop off."""
    sha12 = head_sha[:12]
    rest = [b for b in doc["builds"] if b.get("sha12") != sha12]
    entry = {"sha": head_sha, "sha12": sha12, "url": link,
             "at": int(now if now is not None else time.time())}
    return [entry, *rest][:KEPT]


def save(client, namespace: str, repo: str, pr: int, comment: str | None,
         build_list: list[dict]) -> None:
    client.set(builds_key(namespace, repo, pr),
               json.dumps({"comment": comment, "builds": build_list}, separators=(",", ":")),
               ex=_TTL_S)
