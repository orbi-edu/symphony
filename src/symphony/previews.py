"""PR preview devices: a simulator running a PR build, from a Slack button or a PR link.

Two entry points share one start path (`PreviewManager._launch`):

- The merger posts a ``preview:<owner>/<name>:<pr>:<sha12>`` button on the review
  notification. A click is handled by the assistant process: the clicker must be a mapped
  reviewer, and the sha must still be the PR's head.
- The merger lists a signed link per build on the PR (`preview_links`). The API serves it
  (`api/previews.py`): any mapped person may open it, for that build's sha, as long as its
  artifact exists (`open_link`, then a status page while it starts).

The start downloads the ``ios-simulator-preview`` artifact of the sha, uploads the app to
Seiji, acquires a device, installs and launches the app, and records the lease.

One device per (repo, pr, sha), at most `MAX_LIVE_PER_PR` per PR: a third build releases the
oldest. A live device is reused with a fresh viewer link. The records live in Redis, so the
assistant and the API share them, and the sweep of either process (one at a time) releases
stale devices.
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
MAX_LIVE_PER_PR = 2
# Where a lease came from: a button follows the PR head and its review; a link pins its sha.
BUTTON, LINK = "button", "link"
# Redis key prefix for preview leases.
_KEY_PREFIX = "sym:{ns}:preview:"
_KEY_TTL_S = 60 * 60
# A start in progress (download, upload, boot, install, launch) and a failed start.
_STARTING_TTL_S = 15 * 60
_FAILED_TTL_S = 10 * 60
_ACTION_RE = re.compile(
    r"^preview:(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)"
    r":(?P<pr>\d+):(?P<sha12>[0-9a-f]{12})$"
)
_DOWNLOAD_TIMEOUT = httpx.Timeout(10, read=120)
RPC_TIMEOUT_S = 300


class PreviewError(Exception):
    """A failure the clicker can read: no secret, url or token in the message."""


class PreviewGone(PreviewError):
    """The build cannot be had: no such commit, no preview artifact, or an expired one."""


class PreviewLeaseGone(PreviewError):
    """The device lab ended the lease (idle or its time limit) before Symphony released it."""


@dataclass
class _LivePreview:
    """An active preview device for one (repo, pr, sha)."""

    repo: str
    pr: int
    head_sha: str
    lease_id: str
    server: str
    mcp_token: str  # synthetic token used with record_held
    started_at: float
    bundle_id: str = ""
    origin: str = BUTTON


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


def _commit_sha(http: httpx.Client, token: str, repo: str, ref: str) -> str:
    """The full SHA of commit `ref` (a 12-hex prefix). PreviewGone when the repository has
    no such commit."""
    resp = http.get(f"{github_api_url()}/repos/{repo}/commits/{ref}",
                    headers=_github_headers(token), timeout=10)
    if resp.status_code in (404, 422):
        raise PreviewGone(f"{repo} has no commit {ref}")
    if resp.status_code != 200:
        raise PreviewError(f"GitHub did not answer (HTTP {resp.status_code})")
    return resp.json()["sha"]


def _find_artifact(
    http: httpx.Client, token: str, repo: str, head_sha: str,
) -> dict | None:
    """The ``ios-simulator-preview`` artifact of a workflow run on *head_sha* (a full SHA).

    Returns ``{archive_download_url, expired, ...}``: a live one when there is one, else an
    expired one (GitHub deleted it after its retention), so a caller can tell the two apart.
    None when no run on the sha has one.
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
        raise PreviewError(f"GitHub did not list the workflow runs (HTTP {resp.status_code})")
    expired = None
    for run in resp.json().get("workflow_runs", []):
        aresp = http.get(
            f"{api}/repos/{repo}/actions/runs/{run['id']}/artifacts",
            headers=headers,
            params={"per_page": 50},
            timeout=10,
        )
        if aresp.status_code != 200:
            continue
        for artifact in aresp.json().get("artifacts", []):
            if artifact.get("name") != ARTIFACT_NAME:
                continue
            if not artifact.get("expired"):
                return artifact
            expired = expired or artifact
    return expired


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
# The preview manager: button clicks and links, one start path, leases in Redis
# ---------------------------------------------------------------------------

def _http_client() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True)


def _spawn(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="preview-start", daemon=True).start()


# What the app installation token may do: read the PR, its commits, runs and artifacts.
TOKEN_PERMISSIONS = {"actions": "read", "contents": "read", "pull_requests": "read",
                     "metadata": "read"}


def from_env(client, namespace: str, repository, env: Mapping[str, str], *,
             notify_users: Callable[[], dict] = dict) -> PreviewManager | None:
    """A manager with GitHub App installation tokens (`TOKEN_PERMISSIONS`), or None when the
    GitHub App or the MCP repository is missing."""
    from .edge import GITHUB_APP_ENV, now_ms
    from .ghapp import InstallationCredential

    if repository is None or not all(env.get(name) for name in GITHUB_APP_ENV):
        return None

    def token(repo: str) -> Callable[[], str]:
        return InstallationCredential.from_env(
            env, now_ms, target_repos=(repo,), permissions=TOKEN_PERMISSIONS).token

    return PreviewManager(client=client, namespace=namespace, repository=repository,
                          github_token=token, notify_users=notify_users)


@dataclass
class PreviewManager:
    """Preview devices of one namespace: one per (repo, pr, sha), recorded in Redis so the
    assistant (buttons) and the API (links) share them and either one's sweep releases them."""

    client: Any  # Redis client
    namespace: str
    repository: Any  # McpRepository
    github_token: Callable[[str], Callable[[], str]]  # repo → token factory
    notify_users: Callable[[], dict] = dict  # () → cfg.notify_users, for button clicks
    server_name: str = "seiji"
    http: Callable[[], httpx.Client] = field(default=_http_client, repr=False)
    spawn: Callable[[Callable[[], None]], None] = field(default=_spawn, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = field(default=None, repr=False)

    # -- records ---------------------------------------------------------------

    def _key(self, repo: str, pr: int, sha12: str) -> str:
        return f"{repo}:{pr}:{sha12}"

    def _prefix(self) -> str:
        return _KEY_PREFIX.format(ns=self.namespace)

    def _index(self) -> str:
        return self._prefix() + "live"

    def _save(self, preview: _LivePreview) -> None:
        self.client.hset(self._index(), self._key(preview.repo, preview.pr, preview.head_sha[:12]),
                         json.dumps(preview.__dict__, separators=(",", ":")))
        self.client.expire(self._index(), _KEY_TTL_S)

    def _load(self) -> dict[str, _LivePreview]:
        """Every recorded preview by its Redis field."""
        out: dict[str, _LivePreview] = {}
        for field_name, raw in (self.client.hgetall(self._index()) or {}).items():
            name = field_name.decode() if isinstance(field_name, bytes) else field_name
            try:
                out[name] = _LivePreview(**json.loads(raw))
            except (TypeError, ValueError):
                self.client.hdel(self._index(), name)
        return out

    def live(self) -> list[_LivePreview]:
        return list(self._load().values())

    def get(self, repo: str, pr: int, sha12: str) -> _LivePreview | None:
        raw = self.client.hget(self._index(), self._key(repo, pr, sha12))
        try:
            return _LivePreview(**json.loads(raw)) if raw else None
        except (TypeError, ValueError):
            return None

    def _held(self, preview: _LivePreview) -> bool:
        return mcp_servers.held_by_live_session(self.client, self.namespace, preview.server,
                                                preview.lease_id)

    # -- starts: one at a time per key, across processes ----------------------

    def _starting_key(self, key: str) -> str:
        return self._prefix() + "starting:" + key

    def _failed_key(self, key: str) -> str:
        return self._prefix() + "failed:" + key

    def _claim(self, key: str) -> bool:
        if not self.client.set(self._starting_key(key), json.dumps({"step": "queued"}),
                               nx=True, ex=_STARTING_TTL_S):
            return False
        self.client.delete(self._failed_key(key))
        return True

    def _step(self, key: str, step: str) -> None:
        self.client.set(self._starting_key(key), json.dumps({"step": step}), xx=True,
                        keepttl=True)

    def _fail(self, key: str, error: str) -> None:
        self.client.set(self._failed_key(key), error[:500], ex=_FAILED_TTL_S)

    def _unclaim(self, key: str) -> None:
        self.client.delete(self._starting_key(key))

    def progress(self, repo: str, pr: int, sha12: str) -> dict:
        """For a status page: `{state: ready | starting | error | stopped}`, with the `step`
        (queued, downloading, installing, launching) while starting and the `error`."""
        key = self._key(repo, pr, sha12)
        live = self.get(repo, pr, sha12)
        if live is not None and self._held(live):
            return {"state": "ready"}
        raw = self.client.get(self._starting_key(key))
        if raw:
            try:
                step = json.loads(raw).get("step") or "queued"
            except (ValueError, AttributeError):
                step = "queued"
            return {"state": "starting", "step": step}
        failed = self.client.get(self._failed_key(key))
        if failed:
            return {"state": "error",
                    "error": failed.decode() if isinstance(failed, bytes) else failed}
        return {"state": "stopped"}

    def _make_room(self, repo: str, pr: int) -> None:
        """At most `MAX_LIVE_PER_PR` devices per PR: the oldest goes back for a new one."""
        same = sorted(((name, p) for name, p in self._load().items()
                       if p.repo == repo and p.pr == pr), key=lambda item: item[1].started_at)
        while len(same) >= MAX_LIVE_PER_PR:
            name, oldest = same.pop(0)
            _log.info("preview_releasing", pr=pr, reason="cap")
            self._release(oldest, name)

    def _launch(self, http: httpx.Client, token: str, repo: str, pr: int, head_sha: str,
                artifact: Mapping[str, Any], origin: str) -> _LivePreview:
        """Download, upload, acquire, install, launch, record: the one start path of a button
        click and of a link. The caller holds the start claim of the key. A device acquired
        but not recorded goes back at once."""
        key = self._key(repo, pr, head_sha[:12])
        self._step(key, "downloading")
        app_zip, meta = _extract_preview(
            _download_artifact_zip(http, token, artifact["archive_download_url"]))
        if meta.get("head_sha") != head_sha:
            raise PreviewError("the preview build is not of this commit")
        bundle_id = meta.get("bundle_id", "")
        definition = self.repository.get(self.server_name)
        if definition is None:
            raise PreviewError("no device lab is configured")
        stored = self.repository.headers(self.server_name)
        app_id = _upload_app(http, definition, stored, app_zip)
        self._step(key, "installing")
        self._make_room(repo, pr)
        lease = json.loads(_tool_text(_rpc_tool_call(
            http, definition, stored, "acquire_device", {"platform": "ios"})) or "{}")
        lease_id = lease.get("lease_id")
        if not lease_id:
            raise PreviewError("the device lab gave no device")
        try:
            _rpc_tool_call(http, definition, stored, "install_app",
                           {"lease_id": lease_id, "app_id": app_id})
            self._step(key, "launching")
            launch = {"lease_id": lease_id, **({"bundle_id": bundle_id} if bundle_id else {})}
            _rpc_tool_call(http, definition, stored, "launch_app", launch)
            return self._record(repo, pr, head_sha, lease_id, bundle_id, origin)
        except BaseException:
            self._release_lease(definition, stored, lease_id, pr)
            raise

    def _record(self, repo: str, pr: int, head_sha: str, lease_id: str, bundle_id: str,
                origin: str) -> _LivePreview:
        # A preview has no agent session. A token record of its own keeps the lease
        # visible to `held_by_live_session` and to the gateway's sweep; its role is not
        # a team role, so the gateway serves it no MCP server.
        session = f"preview:{repo}:{pr}:{head_sha[:12]}"
        synth_token = mcp_servers.TOKEN_PREFIX + "preview_" + secrets.token_urlsafe(24)
        self.client.set(
            mcp_servers.token_key(self.namespace, synth_token),
            json.dumps({"role": "preview", "session": session,
                        "exp": int(time.time()) + LEASE_MAX_AGE_S}),
            ex=LEASE_MAX_AGE_S,
        )
        mcp_servers.record_held(self.client, self.namespace, synth_token, session,
                                self.server_name, lease_id, extra={"platform": "ios"})
        preview = _LivePreview(repo=repo, pr=pr, head_sha=head_sha, lease_id=lease_id,
                               server=self.server_name, mcp_token=synth_token,
                               started_at=time.time(), bundle_id=bundle_id, origin=origin)
        self._save(preview)
        return preview

    def viewer_url(self, preview: _LivePreview) -> str:
        """A fresh control viewer link (one use, about 60 s) of a live preview."""
        import asyncio

        definition = self.repository.get(preview.server)
        if definition is None:
            raise PreviewError("no device lab is configured")
        try:
            link = asyncio.run(mcp_servers.request_viewer(
                definition, self.repository.headers(preview.server), preview.lease_id, True))
        except mcp_servers.McpError as exc:
            if exc.code == "lease_gone":
                raise PreviewLeaseGone("the device was released") from None
            raise PreviewError(f"the viewer link failed: {exc.message}") from None
        return link["url"]

    # -- links (the API) --------------------------------------------------------

    def open_link(self, repo: str, pr: int, sha12: str) -> str:
        """A preview link was opened: "ready" when a device runs this build (from now on
        pinned: neither the head moving nor an approval releases it), else "starting" (this
        call starts it in the background). PreviewGone when the build cannot be had."""
        key = self._key(repo, pr, sha12)
        live = self.get(repo, pr, sha12)
        if live is not None:
            if self._held(live):
                if live.origin != LINK:
                    live.origin = LINK
                    self._save(live)
                return "ready"
            self._forget(live)
        if self.client.exists(self._starting_key(key)):
            return "starting"
        with self.http() as http:
            token = self.github_token(repo)()
            head_sha = _commit_sha(http, token, repo, sha12)
            artifact = _find_artifact(http, token, repo, head_sha)
        if artifact is None:
            raise PreviewGone(f"there is no preview build of {sha12}")
        if artifact.get("expired"):
            raise PreviewGone(f"the preview build of {sha12} has expired: GitHub deleted it "
                              "after its retention period")
        if self._claim(key):
            self.spawn(lambda: self._start_link(repo, pr, head_sha, artifact))
        return "starting"

    def _start_link(self, repo: str, pr: int, head_sha: str, artifact: Mapping[str, Any]) -> None:
        key = self._key(repo, pr, head_sha[:12])
        try:
            with self.http() as http:
                self._launch(http, self.github_token(repo)(), repo, pr, head_sha, artifact, LINK)
        except PreviewError as exc:
            _log.warning("preview_failed", error="PreviewError", pr=pr)
            self._fail(key, str(exc))
        except Exception as exc:  # noqa: BLE001 — the status page shows it
            _log.warning("preview_failed", error=type(exc).__name__, pr=pr)
            self._fail(key, f"an unexpected error ({type(exc).__name__})")
        finally:
            self._unclaim(key)

    def link_viewer(self, repo: str, pr: int, sha12: str) -> str | None:
        """A fresh viewer link of the live preview of this build, or None. A lease the device
        lab already ended is forgotten, so the next `open_link` starts the build again."""
        live = self.get(repo, pr, sha12)
        if live is None or not self._held(live):
            return None
        try:
            return self.viewer_url(live)
        except PreviewLeaseGone:
            _log.info("preview_releasing", pr=pr, reason="lease_gone")
            self._forget(live)
            return None

    # -- button clicks (the assistant) -------------------------------------------

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

        key = self._key(repo, pr, sha12)
        http = self.http()
        claimed = False
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
            live = self.get(repo, pr, sha12)
            if live is not None:
                if self._held(live):
                    return self._send_viewer(action, out, live)
                self._forget(live)
            claimed = self._claim(key)
            if not claimed:
                out.ack_action(action, f"A simulator for #{pr} is starting. Click again in two minutes.")
                return "starting"

            out.ack_action(action, f"Starting a simulator for #{pr}\u2026 (about two minutes)")

            # -- validate head SHA ----------
            current_head = _pr_head_sha(http, token, repo, pr)
            if current_head is None:
                raise PreviewError(f"could not read PR #{pr}")
            if not current_head.startswith(sha12):
                out.ack_action(action, f"The PR head has moved past {sha12[:8]}\u2026; "
                                       "click the button on the new notification.")
                return "stale_head"

            artifact = _find_artifact(http, token, repo, current_head)
            if artifact is None:
                raise PreviewError(f"no preview build for #{pr} at {current_head[:12]} yet")
            if artifact.get("expired"):
                raise PreviewError(f"the preview build of #{pr} at {current_head[:12]} has expired")
            preview = self._launch(http, token, repo, pr, current_head, artifact, BUTTON)
            return self._send_viewer(action, out, preview)
        except PreviewError as exc:
            _log.warning("preview_failed", error="PreviewError", pr=pr)
            if claimed:
                self._fail(key, str(exc))
            out.ack_action(action, f"Could not start the preview: {exc}")
            return "error"
        except Exception as exc:  # noqa: BLE001
            _log.warning("preview_failed", error=type(exc).__name__, pr=pr)
            if claimed:
                self._fail(key, f"an unexpected error ({type(exc).__name__})")
            out.ack_action(action, f"Could not start the preview ({type(exc).__name__}).")
            return "error"
        finally:
            if claimed:
                self._unclaim(key)
            http.close()

    def _send_viewer(self, action: Action, out: Outbound, preview: _LivePreview) -> str:
        """Request a control viewer and send it privately to the clicker."""
        try:
            url = self.viewer_url(preview)
        except PreviewLeaseGone:
            _log.info("preview_releasing", pr=preview.pr, reason="lease_gone")
            self._forget(preview)
            out.ack_action(action, "The simulator was released after sitting idle. Click the "
                                   "button again to start a new one.")
            return "lease_gone"
        except PreviewError as exc:
            _log.warning("preview_viewer_failed", error="PreviewError", pr=preview.pr)
            out.ack_action(action, f"The simulator is running, but {exc}")
            return "viewer_failed"
        except Exception as exc:  # noqa: BLE001
            _log.warning("preview_viewer_failed", error=type(exc).__name__, pr=preview.pr)
            out.ack_action(action, "The simulator is running, but the viewer link failed.")
            return "viewer_failed"
        out.ack_action(action, f"Preview of #{preview.pr}: {url}\n"
                               "The link opens once, within 60 s. Click the button again for a new one.")
        return "viewer_sent"

    # -- release -----------------------------------------------------------------

    def _forget(self, preview: _LivePreview, name: str | None = None) -> None:
        """Remove a preview record and clean up Redis."""
        self.client.hdel(self._index(),
                         name or self._key(preview.repo, preview.pr, preview.head_sha[:12]))
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

    def _release(self, preview: _LivePreview, name: str | None = None) -> None:
        """Release a preview device and forget its record."""
        definition = self.repository.get(preview.server)
        if definition is not None:
            self._release_lease(definition, self.repository.headers(preview.server),
                                preview.lease_id, preview.pr)
        self._forget(preview, name)

    def release_pr(self, repo: str, pr: int) -> int:
        """Release every live device of a PR now (the merger closed it: its ticket was
        canceled). Returns how many."""
        released = 0
        for name, preview in self._load().items():
            if preview.repo.lower() == repo.lower() and preview.pr == pr:
                _log.info("preview_releasing", pr=pr, reason="abandoned")
                self._release(preview, name)
                released += 1
        return released

    # -- background sweep -------------------------------------------------------

    def start(self) -> None:
        """Start the sweep loop."""
        if self._thread is not None:
            return
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

    def sweep(self) -> int:
        """Check all live previews and release stale ones.  Returns count released."""
        released = 0
        for name, preview in self._load().items():
            reason = self._release_reason(preview)
            if reason is None:
                continue
            _log.info("preview_releasing", pr=preview.pr, reason=reason)
            self._release(preview, name)
            released += 1
        return released

    def _release_reason(self, preview: _LivePreview) -> str | None:
        """Why the device should go back now, or None to keep it. A link's device keeps its
        sha: the head moving or an approval does not release it."""
        if time.time() - preview.started_at >= LEASE_MAX_AGE_S:
            return "timeout"
        if not self._held(preview):
            return "lease_gone"
        try:
            with self.http() as http:
                token = self.github_token(preview.repo)()
                state = _pr_state(http, token, preview.repo, preview.pr)
                if state in ("closed", "merged"):
                    return state
                if preview.origin == LINK:
                    return None
                head = _pr_head_sha(http, token, preview.repo, preview.pr)
                if head is not None and head != preview.head_sha:
                    return "new_head"
                if _approved_since(http, token, preview.repo, preview.pr, preview.started_at):
                    return "approved"
        except Exception:  # noqa: BLE001 — GitHub down: the time limit still applies
            return None
        return None

    def _sweep_loop(self) -> None:
        # The assistant and the API both run one: a short Redis lock lets one sweep a round.
        lock = self._prefix() + "sweep"
        while not self._stop.wait(CHECK_INTERVAL_S):
            try:
                if self.client.set(lock, "1", nx=True, ex=CHECK_INTERVAL_S - 5):
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
