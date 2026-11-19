"""Tests for the assistant's Seiji integration and PR previews.

Covers:
- share_device host tool and device: click handling;
- viewer refused when the lease is not held;
- the preview happy-path against the Seiji mock;
- stale head;
- a non-reviewer is refused;
- lease reuse;
- release on merge / approval / timeout.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from scripts.mocks.seiji import server as mock_seiji

from symphony import mcp_servers
from symphony.assistant import Action, Ref
from symphony.assistant.engine import (
    _device_shares,
    _device_shares_lock,
    _make_device_handler,
    _parse_device_action,
    share_device_tool,
)
from symphony.previews import (
    LEASE_MAX_AGE_S,
    PreviewManager,
    _extract_preview,
    parse_action,
)

NS = "test"
STORED = f"Bearer {mock_seiji.DEFAULT_TOKEN}"

USERS = {"slack": {"alice": "U_ALICE", "bob": "U_BOB"}, "discord": {}}

WORKFLOW = """\
---
tracker: {kind: memory}
notify:
  users:
    slack:
      alice: U_ALICE
      bob: U_BOB
assistant:
  enabled: true
  slack: {channels: [C1]}
---
x"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seiji_server():
    srv = mock_seiji.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    yield base
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def mcp_repo(tmp_path, seiji_server, monkeypatch):
    monkeypatch.setenv("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "127.0.0.1")
    repo = mcp_servers.McpRepository(None, NS, local_path=tmp_path / "mcp.json")
    repo.put("seiji", {
        "preset": "seiji",
        "url": f"{seiji_server}/mcp",
        "roles": ["implementer", "assistant", "researcher"],
    })
    repo.set_headers("seiji", {"Authorization": STORED})
    return repo


class FakeOut:
    """Minimal Outbound for testing."""

    name = "slack"
    message_limit = 40_000

    def __init__(self):
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.acked: list[dict] = []
        self.ephemerals: list[dict] = []

    def post(self, channel, thread, text, buttons=()):
        ref = Ref(channel, f"msg_{len(self.posted)}")
        self.posted.append({"channel": channel, "thread": thread, "text": text,
                            "buttons": list(buttons), "ref": ref})
        return ref

    def update(self, ref, text, buttons=()):
        self.updated.append({"ref": ref, "text": text, "buttons": list(buttons)})

    def ephemeral(self, channel, thread, user_id, text):
        self.ephemerals.append({"channel": channel, "thread": thread,
                                "user_id": user_id, "text": text})

    def ack_action(self, action, text=None):
        self.acked.append({"action_id": action.action_id, "user_id": action.user_id,
                           "text": text})

    def history(self, channel, thread, limit):
        return []


def action(action_id, user_id="U_ALICE", transport="slack"):
    return Action(transport=transport, channel="C1", thread="t1",
                  message_id="m1", user_id=user_id, action_id=action_id)


def _make_artifact_zip(head_sha, bundle_id="com.example.app"):
    """Build a fake artifact zip containing app.zip and preview.json."""
    meta = {"platform": "ios", "bundle_id": bundle_id,
            "head_sha": head_sha, "fingerprint": "abc123"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # app.zip: a nested zip with a fake .app directory marker
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as inner_zf:
            inner_zf.writestr("MyApp.app/Info.plist", "<plist></plist>")
        zf.writestr("app.zip", inner.getvalue())
        zf.writestr("preview.json", json.dumps(meta))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# parse_action
# ---------------------------------------------------------------------------

def test_parse_action_valid():
    result = parse_action("preview:acme/app:42:abcdef012345")
    assert result == {"repo": "acme/app", "pr": 42, "sha12": "abcdef012345"}


def test_parse_action_invalid():
    assert parse_action("proposal:abc:create") is None
    assert parse_action("preview:bad") is None
    assert parse_action("preview:a/b:x:abc") is None  # pr not digit


# ---------------------------------------------------------------------------
# share_device host tool
# ---------------------------------------------------------------------------

def test_share_device_posts_button():
    out = FakeOut()
    tool = share_device_tool(out, "C1", "t1", lambda: "U_ALICE")
    result = tool.answer({"server": "seiji", "lease_id": "l_123"}, 0)
    assert "button" in result.lower()
    assert len(out.posted) == 1
    posted = out.posted[0]
    assert any(b.id == "device:seiji:l_123" for b in posted["buttons"])


def test_parse_device_action():
    assert _parse_device_action("device:seiji:l_123") == ("seiji", "l_123")
    assert _parse_device_action("preview:a:b") is None
    assert _parse_device_action("device::l_x") is None


# ---------------------------------------------------------------------------
# device: click handler — mapping and asker checks
# ---------------------------------------------------------------------------

def _share_and_click(redis_client, clicker: str, held_by: str | None, sid: str = "assist:s1"):
    """Share lease l_abc from session `sid` for asker U_ALICE, hold it under `held_by`,
    then click as `clicker`. The viewer callback checks the session's own hold."""
    out = FakeOut()
    tool = share_device_tool(out, "C1", "t1", lambda: "U_ALICE", lambda: sid)
    tool.answer({"server": "seiji", "lease_id": "l_abc"}, 0)
    if held_by is not None:
        mcp_servers.record_held(redis_client, NS, "mcp_tok_" + held_by, held_by, "seiji", "l_abc")

    def viewer_fn(server, lease_id, share_sid):
        if not mcp_servers.held_by_session(redis_client, NS, share_sid, server, lease_id):
            return None
        return {"url": "https://seiji.example.com/view/open/xyz", "expires_in": 60}

    engine = MagicMock()
    engine.policy.return_value = MagicMock(notify_users=USERS)
    engine.share_device_tool_fn = viewer_fn
    try:
        return _make_device_handler(engine)(action("device:seiji:l_abc", clicker), out), out
    finally:
        with _device_shares_lock:
            _device_shares.pop("device:seiji:l_abc", None)


def test_device_click_by_the_asker_gets_a_viewer_of_the_sharing_sessions_lease(redis_client):
    result, out = _share_and_click(redis_client, "U_ALICE", held_by="assist:s1")
    assert result == "viewer_sent"
    assert "xyz" in out.acked[-1]["text"]


def test_device_click_by_someone_else_is_refused(redis_client):
    result, out = _share_and_click(redis_client, "U_BOB", held_by="assist:s1")
    assert result == "not_allowed"
    assert "Only the person who asked" in out.acked[-1]["text"]


def test_a_lease_of_another_session_cannot_be_shared(redis_client):
    # The agent names a lease that an issue agent holds: no viewer for it.
    result, out = _share_and_click(redis_client, "U_ALICE", held_by="run:other")
    assert result == "lease_gone"
    assert "no longer available" in out.acked[-1]["text"]


def test_a_released_lease_gives_no_viewer(redis_client):
    result, _ = _share_and_click(redis_client, "U_ALICE", held_by=None)
    assert result == "lease_gone"


# ---------------------------------------------------------------------------
# _extract_preview
# ---------------------------------------------------------------------------

def test_extract_preview():
    head_sha = "a" * 40
    zip_bytes = _make_artifact_zip(head_sha)
    app_zip, meta = _extract_preview(zip_bytes)
    assert meta["head_sha"] == head_sha
    assert meta["bundle_id"] == "com.example.app"
    assert len(app_zip) > 0


# ---------------------------------------------------------------------------
# Preview click handler — integration with Seiji mock
# ---------------------------------------------------------------------------


@pytest.fixture
def preview_mgr(redis_client, mcp_repo, seiji_server, monkeypatch):
    """A PreviewManager wired to the Seiji mock and a fake GitHub."""
    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:0")  # not used directly

    def fake_github_token(repo):
        return lambda: "ghp_fake_token"

    mgr = PreviewManager(
        client=redis_client,
        namespace=NS,
        repository=mcp_repo,
        github_token=fake_github_token,
        notify_users=lambda: USERS,
    )
    return mgr


class FakeGitHubResponses:
    """Provides canned GitHub REST responses for previews."""

    def __init__(self, pr_data=None, reviews=None, artifacts=None, artifact_zip=None):
        self.pr_data = pr_data or {}
        self.reviews = reviews or []
        self.artifacts = artifacts
        self.artifact_zip = artifact_zip
        self.calls = []

    def handler(self, request):
        """An httpx transport handler."""
        import httpx

        url = str(request.url)
        self.calls.append(url)

        # PR endpoint
        if "/pulls/" in url and "/reviews" not in url:
            return httpx.Response(200, json=self.pr_data)

        # Reviews endpoint
        if "/reviews" in url:
            return httpx.Response(200, json=self.reviews)

        # Actions runs endpoint
        if "/actions/runs" in url and "/artifacts" not in url:
            runs = self.pr_data.get("_runs", [])
            return httpx.Response(200, json={"workflow_runs": runs, "total_count": len(runs)})

        # Artifacts endpoint
        if "/artifacts" in url:
            arts = self.artifacts or []
            return httpx.Response(200, json={"artifacts": arts, "total_count": len(arts)})

        # Artifact download
        if "/_download/" in url:
            return httpx.Response(200, content=self.artifact_zip or b"")

        return httpx.Response(404, json={"message": "not found"})


def test_preview_unmapped_user_refused(preview_mgr):
    """A user not mapped in notify.users is refused."""
    out = FakeOut()
    act = action("preview:acme/app:42:abcdef012345", user_id="U_RANDOM")
    result = preview_mgr.handle(act, out)
    assert result == "unmapped"
    assert "mapped" in (out.acked[-1]["text"] or "").lower()


def test_preview_not_reviewer_refused(preview_mgr, monkeypatch):
    """A mapped user who is not a reviewer is refused."""
    import httpx

    head_sha = "abcdef012345" + "0" * 28
    pr_data = {
        "number": 42,
        "state": "open",
        "head": {"sha": head_sha},
        "user": {"login": "charlie"},
        "requested_reviewers": [{"login": "charlie"}],
    }
    reviews = []

    def fake_get(self, url, **kwargs):
        url_str = str(url)
        if "/reviews" in url_str:
            return httpx.Response(200, json=reviews)
        if "/pulls/" in url_str:
            return httpx.Response(200, json=pr_data)
        return httpx.Response(404)

    with patch.object(httpx.Client, "get", fake_get):
        out = FakeOut()
        act = action("preview:acme/app:42:abcdef012345", user_id="U_ALICE")
        result = preview_mgr.handle(act, out)
    assert result == "not_reviewer"
    assert "reviewers" in (out.acked[-1]["text"] or "").lower()


def test_preview_stale_head(preview_mgr, monkeypatch):
    """When the PR head has moved past the button's sha12, the click is refused."""
    import httpx

    # The button says abcdef012345, but the PR is now at a different sha
    pr_data = {
        "number": 42,
        "state": "open",
        "head": {"sha": "b" * 40},
        "user": {"login": "alice"},
        "requested_reviewers": [{"login": "alice"}],
    }
    reviews = [{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}}]

    def fake_get(self, url, **kwargs):
        url_str = str(url)
        if "/reviews" in url_str:
            return httpx.Response(200, json=reviews)
        if "/pulls/" in url_str:
            return httpx.Response(200, json=pr_data)
        return httpx.Response(404)

    with patch.object(httpx.Client, "get", fake_get):
        out = FakeOut()
        act = action("preview:acme/app:42:abcdef012345", user_id="U_ALICE")
        result = preview_mgr.handle(act, out)
    assert result == "stale_head"
    assert "moved" in (out.acked[-1]["text"] or "").lower()


def test_preview_happy_path(redis_client, mcp_repo, seiji_server, monkeypatch):
    """Full happy path: click → acquire → install → launch → viewer."""
    import httpx

    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:0")

    head_sha = "abcdef012345" + "0" * 28
    artifact_zip = _make_artifact_zip(head_sha)

    pr_data = {
        "number": 42,
        "state": "open",
        "head": {"sha": head_sha},
        "user": {"login": "alice"},
        "requested_reviewers": [{"login": "alice"}],
        "_runs": [{"id": 100, "head_sha": head_sha}],
    }
    reviews = [{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}}]
    artifacts = [{"name": "ios-simulator-preview", "expired": False,
                  "archive_download_url": "http://127.0.0.1:0/_download/art.zip"}]

    call_log = []

    orig_get = httpx.Client.get
    orig_post = httpx.Client.post

    def fake_get(self, url, **kwargs):
        url_str = str(url)
        call_log.append(("GET", url_str))
        if "/reviews" in url_str:
            return httpx.Response(200, json=reviews)
        if "/pulls/" in url_str:
            return httpx.Response(200, json=pr_data)
        if "/actions/runs/" in url_str and "/artifacts" in url_str:
            return httpx.Response(200, json={"artifacts": artifacts, "total_count": 1})
        if "/actions/runs" in url_str:
            return httpx.Response(200, json={
                "workflow_runs": pr_data["_runs"], "total_count": 1})
        if "/_download/" in url_str:
            return httpx.Response(200, content=artifact_zip)
        return orig_get(self, url, **kwargs)

    def fake_post(self, url, **kwargs):
        url_str = str(url)
        call_log.append(("POST", url_str))
        return orig_post(self, url, **kwargs)

    def fake_github_token(repo):
        return lambda: "ghp_fake_token"

    mgr = PreviewManager(
        client=redis_client,
        namespace=NS,
        repository=mcp_repo,
        github_token=fake_github_token,
        notify_users=lambda: USERS,
    )

    with patch.object(httpx.Client, "get", fake_get), \
         patch.object(httpx.Client, "post", fake_post):
        out = FakeOut()
        act = action("preview:acme/app:42:abcdef012345", user_id="U_ALICE")
        result = mgr.handle(act, out)

    assert result == "viewer_sent"
    # Verify a viewer link was acked
    assert any("opens once" in (a.get("text") or "").lower() for a in out.acked)
    # The seiji mock should have recorded requests
    seiji_state = httpx.get(f"{seiji_server}/_control/state").json()
    assert len(seiji_state["leases"]) >= 1
    assert len(seiji_state["viewers"]) >= 1

    # A restarted assistant (a new manager on the same Redis) gives the device back.
    [live] = mgr._previews.values()
    restarted = PreviewManager(client=redis_client, namespace=NS, repository=mcp_repo,
                               github_token=fake_github_token, notify_users=lambda: USERS)
    assert restarted.release_leftovers() == 1
    assert not mcp_servers.held_by_live_session(redis_client, NS, "seiji", live.lease_id)
    assert restarted.release_leftovers() == 0


def test_preview_lease_reuse(redis_client, mcp_repo, seiji_server, monkeypatch):
    """A second click reuses the live lease and gets a fresh viewer."""
    import httpx

    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:0")

    head_sha = "aabbcc001122" + "0" * 28
    artifact_zip = _make_artifact_zip(head_sha)

    pr_data = {
        "number": 10,
        "state": "open",
        "head": {"sha": head_sha},
        "user": {"login": "alice"},
        "requested_reviewers": [{"login": "alice"}],
        "_runs": [{"id": 200, "head_sha": head_sha}],
    }
    reviews = [{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}}]
    artifacts = [{"name": "ios-simulator-preview", "expired": False,
                  "archive_download_url": "http://127.0.0.1:0/_download/art2.zip"}]

    orig_get = httpx.Client.get
    orig_post = httpx.Client.post

    def fake_get(self, url, **kwargs):
        url_str = str(url)
        if "/reviews" in url_str:
            return httpx.Response(200, json=reviews)
        if "/pulls/" in url_str:
            return httpx.Response(200, json=pr_data)
        if "/actions/runs/" in url_str and "/artifacts" in url_str:
            return httpx.Response(200, json={"artifacts": artifacts, "total_count": 1})
        if "/actions/runs" in url_str:
            return httpx.Response(200, json={
                "workflow_runs": pr_data["_runs"], "total_count": 1})
        if "/_download/" in url_str:
            return httpx.Response(200, content=artifact_zip)
        return orig_get(self, url, **kwargs)

    def fake_github_token(repo):
        return lambda: "ghp_fake_token"

    mgr = PreviewManager(
        client=redis_client,
        namespace=NS,
        repository=mcp_repo,
        github_token=fake_github_token,
        notify_users=lambda: USERS,
    )

    with patch.object(httpx.Client, "get", fake_get), \
         patch.object(httpx.Client, "post", orig_post):
        out1 = FakeOut()
        act1 = action("preview:acme/app:10:aabbcc001122", user_id="U_ALICE")
        r1 = mgr.handle(act1, out1)
        assert r1 == "viewer_sent"

        seiji_state1 = httpx.get(f"{seiji_server}/_control/state").json()
        lease_count_1 = len(seiji_state1["leases"])
        viewer_count_1 = len(seiji_state1["viewers"])

        # Second click by bob (who is also a reviewer)
        reviews.append({"state": "APPROVED", "user": {"login": "bob"}})
        out2 = FakeOut()
        act2 = action("preview:acme/app:10:aabbcc001122", user_id="U_BOB")
        r2 = mgr.handle(act2, out2)

    assert r2 == "viewer_sent"
    seiji_state2 = httpx.get(f"{seiji_server}/_control/state").json()
    # Same number of leases: the lease was reused
    assert len(seiji_state2["leases"]) == lease_count_1
    # One more viewer
    assert len(seiji_state2["viewers"]) == viewer_count_1 + 1


def test_preview_release_on_merge(redis_client, mcp_repo, seiji_server, monkeypatch):
    """The sweep releases the device when the PR is merged."""
    import httpx

    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:0")

    head_sha = "112233445566" + "0" * 28
    artifact_zip = _make_artifact_zip(head_sha)

    pr_data = {
        "number": 20,
        "state": "open",
        "head": {"sha": head_sha},
        "user": {"login": "alice"},
        "requested_reviewers": [{"login": "alice"}],
        "_runs": [{"id": 300, "head_sha": head_sha}],
    }
    reviews = [{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}}]
    artifacts = [{"name": "ios-simulator-preview", "expired": False,
                  "archive_download_url": "http://127.0.0.1:0/_download/art3.zip"}]

    merged = [False]

    orig_get = httpx.Client.get
    orig_post = httpx.Client.post

    def fake_get(self, url, **kwargs):
        url_str = str(url)
        if "/reviews" in url_str:
            return httpx.Response(200, json=reviews)
        if "/pulls/" in url_str:
            data = dict(pr_data)
            if merged[0]:
                data["state"] = "closed"
                data["merged_at"] = "2026-01-01T00:00:00Z"
            return httpx.Response(200, json=data)
        if "/actions/runs/" in url_str and "/artifacts" in url_str:
            return httpx.Response(200, json={"artifacts": artifacts, "total_count": 1})
        if "/actions/runs" in url_str:
            return httpx.Response(200, json={
                "workflow_runs": pr_data["_runs"], "total_count": 1})
        if "/_download/" in url_str:
            return httpx.Response(200, content=artifact_zip)
        return orig_get(self, url, **kwargs)

    def fake_github_token(repo):
        return lambda: "ghp_fake_token"

    mgr = PreviewManager(
        client=redis_client,
        namespace=NS,
        repository=mcp_repo,
        github_token=fake_github_token,
        notify_users=lambda: USERS,
    )

    with patch.object(httpx.Client, "get", fake_get), \
         patch.object(httpx.Client, "post", orig_post):
        out = FakeOut()
        act = action("preview:acme/app:20:112233445566", user_id="U_ALICE")
        result = mgr.handle(act, out)
        assert result == "viewer_sent"
        assert len(mgr._previews) == 1

        # Now merge the PR
        merged[0] = True
        released = mgr.sweep()
        assert released >= 1
        assert len(mgr._previews) == 0


def test_preview_release_on_approval(redis_client, mcp_repo, seiji_server, monkeypatch):
    """The sweep releases the device when the PR is approved."""
    import httpx

    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:0")

    head_sha = "aabb11223344" + "0" * 28
    artifact_zip = _make_artifact_zip(head_sha)

    pr_data = {
        "number": 30,
        "state": "open",
        "head": {"sha": head_sha},
        "user": {"login": "alice"},
        "requested_reviewers": [{"login": "alice"}],
        "_runs": [{"id": 400, "head_sha": head_sha}],
    }
    reviews_list: list[dict] = [{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}}]
    artifacts = [{"name": "ios-simulator-preview", "expired": False,
                  "archive_download_url": "http://127.0.0.1:0/_download/art4.zip"}]

    orig_get = httpx.Client.get
    orig_post = httpx.Client.post

    def fake_get(self, url, **kwargs):
        url_str = str(url)
        if "/reviews" in url_str:
            return httpx.Response(200, json=reviews_list)
        if "/pulls/" in url_str:
            return httpx.Response(200, json=pr_data)
        if "/actions/runs/" in url_str and "/artifacts" in url_str:
            return httpx.Response(200, json={"artifacts": artifacts, "total_count": 1})
        if "/actions/runs" in url_str:
            return httpx.Response(200, json={
                "workflow_runs": pr_data["_runs"], "total_count": 1})
        if "/_download/" in url_str:
            return httpx.Response(200, content=artifact_zip)
        return orig_get(self, url, **kwargs)

    def fake_github_token(repo):
        return lambda: "ghp_fake_token"

    mgr = PreviewManager(
        client=redis_client,
        namespace=NS,
        repository=mcp_repo,
        github_token=fake_github_token,
        notify_users=lambda: USERS,
    )

    with patch.object(httpx.Client, "get", fake_get), \
         patch.object(httpx.Client, "post", orig_post):
        out = FakeOut()
        act = action("preview:acme/app:30:aabb11223344", user_id="U_ALICE")
        result = mgr.handle(act, out)
        assert result == "viewer_sent"

        # An approval submitted before the preview started keeps the device…
        reviews_list.append({"state": "APPROVED", "user": {"login": "bob", "type": "User"},
                             "submitted_at": "2020-01-01T00:00:00Z"})
        assert mgr.sweep() == 0
        # …one submitted after it releases the device: the reviewer is done.
        reviews_list.append({"state": "APPROVED", "user": {"login": "bob", "type": "User"},
                             "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                           time.gmtime(time.time() + 5))})
        assert mgr.sweep() == 1
        assert len(mgr._previews) == 0


def test_preview_release_on_timeout(redis_client, mcp_repo, seiji_server, monkeypatch):
    """The sweep releases the device after 55 minutes."""
    import httpx

    monkeypatch.setenv("GITHUB_API_URL", "http://127.0.0.1:0")

    head_sha = "ccdd11223344" + "0" * 28
    artifact_zip = _make_artifact_zip(head_sha)

    pr_data = {
        "number": 40,
        "state": "open",
        "head": {"sha": head_sha},
        "user": {"login": "alice"},
        "requested_reviewers": [{"login": "alice"}],
        "_runs": [{"id": 500, "head_sha": head_sha}],
    }
    reviews = [{"state": "CHANGES_REQUESTED", "user": {"login": "alice"}}]
    artifacts = [{"name": "ios-simulator-preview", "expired": False,
                  "archive_download_url": "http://127.0.0.1:0/_download/art5.zip"}]

    orig_get = httpx.Client.get
    orig_post = httpx.Client.post

    def fake_get(self, url, **kwargs):
        url_str = str(url)
        if "/reviews" in url_str:
            return httpx.Response(200, json=reviews)
        if "/pulls/" in url_str:
            return httpx.Response(200, json=pr_data)
        if "/actions/runs/" in url_str and "/artifacts" in url_str:
            return httpx.Response(200, json={"artifacts": artifacts, "total_count": 1})
        if "/actions/runs" in url_str:
            return httpx.Response(200, json={
                "workflow_runs": pr_data["_runs"], "total_count": 1})
        if "/_download/" in url_str:
            return httpx.Response(200, content=artifact_zip)
        return orig_get(self, url, **kwargs)

    def fake_github_token(repo):
        return lambda: "ghp_fake_token"

    mgr = PreviewManager(
        client=redis_client,
        namespace=NS,
        repository=mcp_repo,
        github_token=fake_github_token,
        notify_users=lambda: USERS,
    )

    with patch.object(httpx.Client, "get", fake_get), \
         patch.object(httpx.Client, "post", orig_post):
        out = FakeOut()
        act = action("preview:acme/app:40:ccdd11223344", user_id="U_ALICE")
        result = mgr.handle(act, out)
        assert result == "viewer_sent"

        # Fast-forward the started_at to 56 minutes ago
        key = mgr._key("acme/app", 40)
        with mgr._lock:
            preview = mgr._previews.get(key)
        assert preview is not None
        preview.started_at = time.time() - (LEASE_MAX_AGE_S + 60)

        released = mgr.sweep()
        assert released >= 1
        assert len(mgr._previews) == 0
