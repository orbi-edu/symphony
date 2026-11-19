"""PR preview devices: click a button, get a simulator running the PR's build.

The merger posts a ``preview:<owner>/<name>:<pr>:<sha12>`` button on the review
notification.  A click is handled here by the assistant process, which:

1. Verifies the clicker is a mapped reviewer of the PR.
2. Downloads the ``ios-simulator-preview`` artifact from the PR's current head.
3. Uploads the app to Seiji, acquires a device, installs and launches the app.
4. Records the lease and sends a viewer link to the clicker.

One preview device per (repo, pr).  A second click reuses the live lease and
creates a fresh viewer link.  A background loop releases stale devices.
"""

from __future__ import annotations

import calendar
import io
import json
import re
import secrets
import threading
import time
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from . import mcp_servers
from .assistant import Action, Outbound
from .assistant.confirm import mapped_login
from .endpoints import github_api_url

_log = structlog.get_logger()

BUTTON_PREFIX = "preview:"
ARTIFACT_NAME = "ios-simulator-preview"
# 55 min: leave 5 min before Seiji's 60 min max lease.
LEASE_MAX_AGE_S = 55 * 60
CHECK_INTERVAL_S = 60
# Redis key prefix for preview leases.
_KEY_PREFIX = "sym:{ns}:preview:"
_KEY_TTL_S = 60 * 60
_ACTION_RE = re.compile(
    r"^preview:(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)"
    r":(?P<pr>\d+):(?P<sha12>[0-9a-f]{12})$"
)
_DOWNLOAD_TIMEOUT = httpx.Timeout(10, read=120)
RPC_TIMEOUT_S = 300


class PreviewError(Exception):
    """A failure the clicker can read: no secret, url or token in the message."""


@dataclass
class _LivePreview:
    """An active preview device for one (repo, pr)."""

    repo: str
    pr: int
    head_sha: str
    lease_id: str
    server: str
    mcp_token: str  # synthetic token used with record_held
    started_at: float
    bundle_id: str = ""


def parse_action(action_id: str) -> dict | None:
    """Parse ``preview:<owner>/<name>:<pr>:<sha12>`` into components, or None."""
    m = _ACTION_RE.fullmatch(action_id)
    if m is None:
        return None
    return {
        "repo": f"{m['owner']}/{m['name']}",
        "pr": int(m["pr"]),
        "sha12": m["sha12"],
    }


# ---------------------------------------------------------------------------
# GitHub helpers (REST, using the App's installation token)
# ---------------------------------------------------------------------------

def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _pr_reviewers(http: httpx.Client, token: str, repo: str, pr: int) -> set[str]:
    """GitHub logins who are requested_reviewers or have submitted a review."""
    api = github_api_url()
    headers = _github_headers(token)
    logins: set[str] = set()
    # requested_reviewers
    resp = http.get(f"{api}/repos/{repo}/pulls/{pr}", headers=headers, timeout=10)
    if resp.status_code == 200:
        data = resp.json()
        for u in data.get("requested_reviewers") or []:
            if u.get("login"):
                logins.add(u["login"].lower())
    # submitted reviews
    page = 1
    while True:
        resp = http.get(
            f"{api}/repos/{repo}/pulls/{pr}/reviews",
            headers=headers,
            params={"per_page": 100, "page": page},
            timeout=10,
        )
        if resp.status_code != 200:
            break
        reviews = resp.json()
        for r in reviews:
            if r.get("user", {}).get("login"):
                logins.add(r["user"]["login"].lower())
        if len(reviews) < 100:
            break
        page += 1
    return logins


def _pr_head_sha(http: httpx.Client, token: str, repo: str, pr: int) -> str | None:
    """The current head SHA of a PR, or None on failure."""
    api = github_api_url()
    resp = http.get(
        f"{api}/repos/{repo}/pulls/{pr}",
        headers=_github_headers(token),
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    return resp.json().get("head", {}).get("sha")


def _pr_author(http: httpx.Client, token: str, repo: str, pr: int) -> str | None:
    """The author login of a PR."""
    api = github_api_url()
    resp = http.get(
        f"{api}/repos/{repo}/pulls/{pr}",
        headers=_github_headers(token),
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    return (resp.json().get("user") or {}).get("login", "").lower() or None


def _pr_state(http: httpx.Client, token: str, repo: str, pr: int) -> str:
    """``open``, ``closed``, or ``merged`` (closed with merged_at)."""
    api = github_api_url()
    resp = http.get(
        f"{api}/repos/{repo}/pulls/{pr}",
        headers=_github_headers(token),
        timeout=10,
    )
    if resp.status_code != 200:
        return "unknown"
    data = resp.json()
    if data.get("merged_at"):
        return "merged"
    return data.get("state", "unknown")


def _find_artifact(
    http: httpx.Client, token: str, repo: str, head_sha: str,
) -> dict | None:
    """Find the ``ios-simulator-preview`` artifact for *head_sha*.

    Returns ``{archive_download_url, ...}`` or None.
    """
    api = github_api_url()
    headers = _github_headers(token)
    # List workflow runs for this commit
    resp = http.get(
        f"{api}/repos/{repo}/actions/runs",
        headers=headers,
        params={"head_sha": head_sha, "per_page": 20},
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    runs = resp.json().get("workflow_runs", [])
    for run in runs:
        run_id = run["id"]
        aresp = http.get(
            f"{api}/repos/{repo}/actions/runs/{run_id}/artifacts",
            headers=headers,
            params={"per_page": 50},
            timeout=10,
        )
        if aresp.status_code != 200:
            continue
        for artifact in aresp.json().get("artifacts", []):
            if artifact.get("name") == ARTIFACT_NAME and not artifact.get("expired"):
                return artifact
    return None


def _download_artifact_zip(
    http: httpx.Client, token: str, download_url: str,
) -> bytes:
    """Download the artifact zip (GitHub redirects to a short-lived URL)."""
    headers = _github_headers(token)
    resp = http.get(download_url, headers=headers, follow_redirects=True,
                    timeout=_DOWNLOAD_TIMEOUT)
    if resp.status_code != 200:
        raise PreviewError(f"the artifact download failed (HTTP {resp.status_code})")
    return resp.content


def _extract_preview(zip_bytes: bytes) -> tuple[bytes, dict]:
    """Extract ``app.zip`` and ``preview.json`` from the artifact zip.

    Returns ``(app_zip_bytes, preview_meta)``.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        if "app.zip" not in names or "preview.json" not in names:
            raise PreviewError("the artifact has no app.zip or preview.json")
        app_zip = zf.read("app.zip")
        meta = json.loads(zf.read("preview.json"))
    return app_zip, meta


# ---------------------------------------------------------------------------
# Seiji helpers (JSON-RPC via the stored headers, not through the gateway)
# ---------------------------------------------------------------------------

def _upload_app(
    http: httpx.Client,
    definition: Mapping[str, Any],
    stored: Mapping[str, str],
    app_zip: bytes,
) -> str:
    """Upload *app_zip* to ``POST {http_base}/v1/apps``, return the ``app_id``."""
    base = definition["http_base"]
    headers = dict(stored)
    headers["X-Filename"] = "app.zip"
    headers["Content-Type"] = "application/octet-stream"
    resp = http.post(
        f"{base}/v1/apps", content=app_zip, headers=headers,
        timeout=httpx.Timeout(10, read=120),
    )
    if resp.status_code != 200:
        raise PreviewError(f"the build upload was refused (HTTP {resp.status_code})")
    return resp.json()["app_id"]


def _rpc_tool_call(
    http: httpx.Client,
    definition: Mapping[str, Any],
    stored: Mapping[str, str],
    tool: str,
    arguments: dict,
) -> dict:
    """A single JSON-RPC ``tools/call`` against the MCP server, with initialize handshake.

    Returns the result dict.  Raises on protocol or tool errors.
    """
    base_headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
    }

    def post(message: dict, extra: Mapping[str, str]) -> httpx.Response:
        # acquire_device boots a simulator: up to a few minutes on a cold host.
        return http.post(
            definition["url"],
            content=json.dumps(message),
            headers=mcp_servers.upstream_headers({**base_headers, **extra}, stored),
            timeout=httpx.Timeout(RPC_TIMEOUT_S, connect=10),
        )

    # Initialize
    init_resp = post(mcp_servers._initialize(1), {})
    if init_resp.status_code >= 300:
        raise PreviewError(f"the device lab refused the session (HTTP {init_resp.status_code})")
    init_result = mcp_servers._sync_result(init_resp, 1) or {}
    extra: dict[str, str] = {
        "mcp-protocol-version": str(
            init_result.get("protocolVersion") or mcp_servers.PROTOCOL_VERSION
        ),
    }
    if init_resp.headers.get("mcp-session-id"):
        extra["mcp-session-id"] = init_resp.headers["mcp-session-id"]
    # Notify initialized
    post(mcp_servers._INITIALIZED, extra)
    # tools/call
    call_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    call_resp = post(call_msg, extra)
    result = (
        mcp_servers._sync_result(call_resp, 2)
        if call_resp.status_code < 300
        else None
    )
    # Terminate session if needed
    if "mcp-session-id" in extra:
        import contextlib

        with contextlib.suppress(httpx.HTTPError):
            http.delete(
                definition["url"],
                headers=mcp_servers.upstream_headers(extra, stored),
            )
    if result is None:
        raise PreviewError(f"the device lab did not answer {tool} (HTTP {call_resp.status_code})")
    if result.get("isError"):
        text = ""
        for c in result.get("content", []):
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                break
        raise PreviewError(f"{tool}: {text[:300] or 'failed'}")
    return result


def _tool_text(result: dict) -> str:
    """Extract the text from a tools/call result."""
    for c in result.get("content", []):
        if isinstance(c, dict) and c.get("type") == "text":
            return c.get("text", "")
    return ""


# ---------------------------------------------------------------------------
# The preview manager: handles clicks, manages leases
# ---------------------------------------------------------------------------

@dataclass
class PreviewManager:
    """Preview devices of the assistant process: one per (repo, pr), recorded in Redis
    so that a restart can release what the previous process held."""

    client: Any  # Redis client
    namespace: str
    repository: Any  # McpRepository
    github_token: Callable[[str], Callable[[], str]]  # repo → token factory
    notify_users: Callable[[], dict]  # () → cfg.notify_users
    server_name: str = "seiji"
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _previews: dict[str, _LivePreview] = field(default_factory=dict)
    _starting: set[str] = field(default_factory=set)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def _key(self, repo: str, pr: int) -> str:
        return f"{repo}:{pr}"

    def _index(self) -> str:
        return _KEY_PREFIX.format(ns=self.namespace) + "live"

    def _save(self, preview: _LivePreview) -> None:
        self.client.hset(self._index(), self._key(preview.repo, preview.pr),
                         json.dumps(preview.__dict__, separators=(",", ":")))
        self.client.expire(self._index(), _KEY_TTL_S)

    def start(self) -> None:
        """Release what a previous process left, then start the sweep loop."""
        if self._thread is not None:
            return
        try:
            self.release_leftovers()
        except Exception as exc:  # noqa: BLE001 — Seiji's idle reaper is the backstop
            _log.warning("preview_leftovers_failed", error=type(exc).__name__)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._sweep_loop, name="preview-sweep", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=10)
        self._thread = None

    def release_leftovers(self) -> int:
        """Release every preview recorded in Redis that this process does not know: the
        previews of a previous process. Returns how many were released."""
        released = 0
        for field_name, raw in (self.client.hgetall(self._index()) or {}).items():
            name = field_name.decode() if isinstance(field_name, bytes) else field_name
            with self._lock:
                if name in self._previews:
                    continue
            try:
                preview = _LivePreview(**json.loads(raw))
            except (TypeError, ValueError):
                self.client.hdel(self._index(), name)
                continue
            self._release(preview)
            released += 1
        if released:
            _log.info("preview_leftovers_released", previews=released)
        return released

    def handle(self, action: Action, out: Outbound) -> str:
        """Handle a preview button click.  Returns an outcome string for logging."""
        parsed = parse_action(action.action_id)
        if parsed is None:
            out.ack_action(action)
            return "unknown_preview_action"
        return self._handle_preview(action, out, parsed)

    def _handle_preview(self, action: Action, out: Outbound, parsed: dict) -> str:
        repo, pr, sha12 = parsed["repo"], parsed["pr"], parsed["sha12"]

        # -- authorization: mapped user who is a reviewer ----
        users = self.notify_users()
        login = mapped_login(users, action.transport, action.user_id)
        if login is None:
            out.ack_action(action, "Only people mapped in the configuration can open previews.")
            return "unmapped"

        key = self._key(repo, pr)
        http = httpx.Client(timeout=30, follow_redirects=True)
        claimed = False
        lease_id = None
        definition: Mapping[str, Any] | None = None
        stored: Mapping[str, str] = {}
        try:
            token = self.github_token(repo)()
            reviewers = _pr_reviewers(http, token, repo, pr)
            if not reviewers:  # contract §4: the author when the PR has no reviewer
                author = _pr_author(http, token, repo, pr)
                reviewers = {author} if author else set()
            if login.lower() not in {r.lower() for r in reviewers}:
                out.ack_action(action, "Only the PR's reviewers can open a preview.")
                return "not_reviewer"

            # -- a live device is reused; one being started is not started twice --
            with self._lock:
                live = self._previews.get(key)
            if live is not None:
                if mcp_servers.held_by_live_session(self.client, self.namespace,
                                                    live.server, live.lease_id):
                    return self._send_viewer(action, out, live)
                self._forget(live)
            with self._lock:
                busy = key in self._starting
                if not busy:
                    self._starting.add(key)
                    claimed = True
            if busy:
                out.ack_action(action, f"A simulator for #{pr} is starting. Click again in a minute.")
                return "starting"

            out.ack_action(action, f"Starting a simulator for #{pr}\u2026 (about a minute)")

            # -- validate head SHA ----------
            current_head = _pr_head_sha(http, token, repo, pr)
            if current_head is None:
                raise PreviewError(f"could not read PR #{pr}")
            if not current_head.startswith(sha12):
                out.ack_action(action, f"The PR head has moved past {sha12[:8]}\u2026; "
                                       "click the button on the new notification.")
                return "stale_head"

            # -- find and download the artifact --
            artifact = _find_artifact(http, token, repo, current_head)
            if artifact is None:
                raise PreviewError(f"no preview build for #{pr} at {current_head[:12]} yet")
            app_zip, meta = _extract_preview(
                _download_artifact_zip(http, token, artifact["archive_download_url"]))
            if meta.get("head_sha") != current_head:
                raise PreviewError("the preview build is not of the PR's current head")
            bundle_id = meta.get("bundle_id", "")

            # -- Seiji: upload, acquire, install, launch --
            definition = self.repository.get(self.server_name)
            if definition is None:
                raise PreviewError("no device lab is configured")
            stored = self.repository.headers(self.server_name)
            app_id = _upload_app(http, definition, stored, app_zip)
            lease = json.loads(_tool_text(_rpc_tool_call(
                http, definition, stored, "acquire_device", {"platform": "ios"})) or "{}")
            lease_id = lease.get("lease_id")
            if not lease_id:
                raise PreviewError("the device lab gave no device")
            _rpc_tool_call(http, definition, stored, "install_app",
                           {"lease_id": lease_id, "app_id": app_id})
            launch = {"lease_id": lease_id, **({"bundle_id": bundle_id} if bundle_id else {})}
            _rpc_tool_call(http, definition, stored, "launch_app", launch)

            # A preview has no agent session. A token record of its own keeps the lease
            # visible to `held_by_live_session` and to the gateway's sweep; its role is not
            # a team role, so the gateway serves it no MCP server.
            session = f"preview:{repo}:{pr}"
            synth_token = mcp_servers.TOKEN_PREFIX + "preview_" + secrets.token_urlsafe(24)
            self.client.set(
                mcp_servers.token_key(self.namespace, synth_token),
                json.dumps({"role": "preview", "session": session,
                            "exp": int(time.time()) + LEASE_MAX_AGE_S}),
                ex=LEASE_MAX_AGE_S,
            )
            mcp_servers.record_held(self.client, self.namespace, synth_token, session,
                                    self.server_name, lease_id, extra={"platform": "ios"})
            preview = _LivePreview(repo=repo, pr=pr, head_sha=current_head, lease_id=lease_id,
                                   server=self.server_name, mcp_token=synth_token,
                                   started_at=time.time(), bundle_id=bundle_id)
            with self._lock:
                self._previews[key] = preview
            self._save(preview)
            lease_id = None  # recorded: the sweep owns it now
            return self._send_viewer(action, out, preview)
        except PreviewError as exc:
            _log.warning("preview_failed", error="PreviewError", pr=pr)
            out.ack_action(action, f"Could not start the preview: {exc}")
            return "error"
        except Exception as exc:  # noqa: BLE001
            _log.warning("preview_failed", error=type(exc).__name__, pr=pr)
            out.ack_action(action, f"Could not start the preview ({type(exc).__name__}).")
            return "error"
        finally:
            if lease_id is not None and definition is not None:
                # Acquired but not recorded: give the device back now.
                self._release_lease(definition, stored, lease_id, pr)
            if claimed:
                with self._lock:
                    self._starting.discard(key)
            http.close()

    def _send_viewer(self, action: Action, out: Outbound, preview: _LivePreview) -> str:
        """Request a control viewer and send it privately to the clicker."""
        import asyncio

        try:
            definition = self.repository.get(preview.server)
            stored = self.repository.headers(preview.server)
            link = asyncio.run(mcp_servers.request_viewer(definition, stored, preview.lease_id, True))
        except mcp_servers.McpError as exc:
            _log.warning("preview_viewer_failed", error=exc.code, pr=preview.pr)
            out.ack_action(action, f"The simulator is running, but the viewer link failed: {exc.message}")
            return "viewer_failed"
        except Exception as exc:  # noqa: BLE001
            _log.warning("preview_viewer_failed", error=type(exc).__name__, pr=preview.pr)
            out.ack_action(action, "The simulator is running, but the viewer link failed.")
            return "viewer_failed"
        out.ack_action(action, f"Preview of #{preview.pr}: {link['url']}\n"
                               "The link opens once, within 60 s. Click the button again for a new one.")
        return "viewer_sent"

    def _forget(self, preview: _LivePreview) -> None:
        """Remove a preview record and clean up Redis."""
        key = self._key(preview.repo, preview.pr)
        with self._lock:
            if self._previews.get(key) is preview:
                self._previews.pop(key, None)
        self.client.hdel(self._index(), key)
        mcp_servers.forget_held(self.client, self.namespace, preview.mcp_token,
                                preview.server, preview.lease_id)
        mcp_servers.revoke_token(self.client, self.namespace, preview.mcp_token)

    def _release_lease(self, definition: Mapping[str, Any], stored: Mapping[str, str],
                       lease_id: str, pr: int) -> None:
        cleanup = definition.get("cleanup")
        if not cleanup:
            return
        try:
            with httpx.Client(timeout=60) as http:
                mcp_servers._release_one(http, definition, stored, cleanup, lease_id)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 — Seiji's idle reaper is the backstop
            _log.warning("preview_release_failed", error=type(exc).__name__, pr=pr)

    def _release(self, preview: _LivePreview) -> None:
        """Release a preview device and forget its record."""
        definition = self.repository.get(preview.server)
        if definition is not None:
            self._release_lease(definition, self.repository.headers(preview.server),
                                preview.lease_id, preview.pr)
        self._forget(preview)

    # -- background sweep -------------------------------------------------------

    def sweep(self) -> int:
        """Check all live previews and release stale ones.  Returns count released."""
        released = 0
        with self._lock:
            previews = list(self._previews.values())
        for preview in previews:
            reason = self._release_reason(preview)
            if reason is None:
                continue
            _log.info("preview_releasing", pr=preview.pr, reason=reason)
            self._release(preview)
            released += 1
        return released

    def _release_reason(self, preview: _LivePreview) -> str | None:
        """Why the device should go back now, or None to keep it."""
        if time.time() - preview.started_at >= LEASE_MAX_AGE_S:
            return "timeout"
        if not mcp_servers.held_by_live_session(self.client, self.namespace,
                                                preview.server, preview.lease_id):
            return "lease_gone"
        try:
            with httpx.Client(timeout=10) as http:
                token = self.github_token(preview.repo)()
                state = _pr_state(http, token, preview.repo, preview.pr)
                if state in ("closed", "merged"):
                    return state
                head = _pr_head_sha(http, token, preview.repo, preview.pr)
                if head is not None and head != preview.head_sha:
                    return "new_head"
                if _approved_since(http, token, preview.repo, preview.pr, preview.started_at):
                    return "approved"
        except Exception:  # noqa: BLE001 — GitHub down: the time limit still applies
            return None
        return None

    def _sweep_loop(self) -> None:
        while not self._stop.wait(CHECK_INTERVAL_S):
            try:
                self.sweep()
            except Exception as exc:  # noqa: BLE001
                _log.warning("preview_sweep_error", error=type(exc).__name__)


def _approved_since(http: httpx.Client, token: str, repo: str, pr: int, since: float) -> bool:
    """True when a human approved the PR after `since` (epoch seconds): the reviewer is
    done with the device. An approval from before the preview started does not count."""
    resp = http.get(f"{github_api_url()}/repos/{repo}/pulls/{pr}/reviews",
                    headers=_github_headers(token), params={"per_page": 100}, timeout=10)
    if resp.status_code != 200:
        return False
    for r in resp.json():
        user = r.get("user") or {}
        if r.get("state") != "APPROVED" or user.get("type") == "Bot":
            continue
        try:
            at = calendar.timegm(time.strptime(r.get("submitted_at") or "", "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            continue
        if at >= since:
            return True
    return False
