"""Webhook deliveries as GitHub sends them: JSON body shaped by the event's documented payload
schema (`x-webhooks` in the REST description), `X-GitHub-Event`, `X-GitHub-Delivery` (a GUID),
`X-Hub-Signature-256: sha256=<HMAC-SHA256 hex of the body>` (and the legacy SHA-1 header), sent
in order from one background thread. Every delivery is kept for `/_mock/deliveries`."""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid

from .state import iso, node_id


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Hooks:
    def __init__(self, mock) -> None:
        self.m = mock
        self.deliveries: collections.deque = collections.deque(maxlen=1000)
        self._queue: queue.Queue = queue.Queue()
        self._seq = 0
        self._thread = threading.Thread(target=self._worker, name="mock-webhooks", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._queue.put(None)

    def emit(self, event: str, action: str | None, repo: dict | None, body: dict, sender: str | None,
             owner: str | None = None) -> dict:
        """One delivery; `repo` None is an account-level event (`projects_v2_item`) for `owner`."""
        m = self.m
        r = m.render()
        payload: dict = {"action": action} if action else {}
        payload.update(body)
        if repo is not None:
            payload["repository"] = r.repo(repo)
            owner = repo["owner"]
        owner = m.store.account(owner)
        if owner["type"] == "Organization":
            api = f"{r.api}/orgs/{owner['login']}"
            payload["organization"] = {
                "login": owner["login"], "id": owner["id"], "node_id": node_id("O", owner["id"]), "url": api,
                "repos_url": f"{api}/repos", "events_url": f"{api}/events", "hooks_url": f"{api}/hooks",
                "issues_url": f"{api}/issues", "members_url": f"{api}/members{{/member}}",
                "public_members_url": f"{api}/public_members{{/member}}", "avatar_url": f"{r.base}/avatars/{owner['id']}",
                "description": None}
        inst = m.store.installation_for(repo["full_name"]) if repo is not None else \
            m.store.installation_on(owner["login"])
        if inst is not None:
            payload["installation"] = {"id": inst["id"], "node_id": node_id("MDIz", inst["id"])}
        payload["sender"] = r.user(sender or owner["login"])
        name = event.replace("_", "-") + (f"-{action.replace('_', '-')}" if action else "")
        payload = m.spec.conform(m.spec.webhook_pointer(name), payload, r.base)
        with m.lock:
            self._seq += 1
            seq = self._seq
        delivery = {
            "id": seq, "guid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-github:{m.store.app['id']}:{seq}")),
            "event": event, "action": action, "schema": name,
            "repository": repo["full_name"] if repo is not None else None, "owner": owner["login"],
            "delivered_at": iso(), "status_code": None, "error": None, "payload": payload,
        }
        self.deliveries.append(delivery)
        self._queue.put(delivery)
        return delivery

    def _worker(self) -> None:
        while True:
            delivery = self._queue.get()
            try:
                if delivery is None:
                    return
                self._send(delivery)
            finally:
                self._queue.task_done()

    def _send(self, delivery: dict) -> None:
        hook = self.m.store.data.get("webhook") or {}
        url = hook.get("url")
        if not url:
            return
        body = json.dumps(delivery["payload"], separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json", "User-Agent": "GitHub-Hookshot/mock",
            "X-GitHub-Event": delivery["event"], "X-GitHub-Delivery": delivery["guid"],
            "X-GitHub-Hook-ID": "1", "X-GitHub-Hook-Installation-Target-Type": "integration",
            "X-GitHub-Hook-Installation-Target-ID": str(self.m.store.app["id"]),
        }
        secret = hook.get("secret")
        if secret:
            headers["X-Hub-Signature-256"] = sign(secret, body)
            headers["X-Hub-Signature"] = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                delivery["status_code"] = resp.status
        except urllib.error.HTTPError as exc:
            delivery["status_code"] = exc.code
        except Exception as exc:  # noqa: BLE001 — recorded, like GitHub's failed deliveries
            delivery["error"] = str(exc)[:300]
        delivery["duration_s"] = round(time.time() - started, 3)

    def flush(self, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        while self._queue.unfinished_tasks:
            if time.time() > deadline:
                return False
            time.sleep(0.02)
        return True
