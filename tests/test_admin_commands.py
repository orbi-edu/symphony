"""Real Redis regressions for durable acceptance and lifecycle fencing.

The server is disposable, has no TCP listener, and never uses a configured REDIS_URL.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis
from tests.test_dispatch import FakeEffects, cfg

from symphony import state
from symphony.admin_commands import AdminStore, CommandError, process_commands
from symphony.effects import kill_agent_generation
from symphony.kv import LeaseKV, RedisKV
from symphony.trackers.memory import MemoryTracker


@pytest.fixture(scope="module")
def isolated_redis():
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server is required for atomic command integration regressions")
    directory = tempfile.TemporaryDirectory(prefix="sa-", dir="/tmp")
    socket = f"{directory.name}/redis.sock"
    process = subprocess.Popen(
        [binary, "--port", "0", "--unixsocket", socket, "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(unix_socket_path=socket, decode_responses=True, socket_timeout=2)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                client.ping()
                break
            except redis.ConnectionError:
                if time.monotonic() >= deadline or process.poll() is not None:
                    pytest.fail("Disposable Redis did not start")
                time.sleep(0.02)
        yield client
    finally:
        client.close()
        process.terminate()
        process.wait(timeout=5)
        directory.cleanup()


@pytest.fixture
def commands(isolated_redis):
    isolated_redis.flushdb()
    kv = RedisKV(isolated_redis)
    now = time.time_ns() // 1_000_000
    assert state.renew_lease(kv, "daemon", now)
    state.heartbeat(kv, now)
    return AdminStore(isolated_redis, "test"), kv


def accept(store, action="pause", target=None, key="request", payload=None):
    return store.accept(
        actor="operator",
        role="admin",
        credential_fingerprint="f" * 64,
        action=action,
        target=target,
        reason="Maintenance requested",
        idempotency_key=key,
        payload=payload,
    )


def publish_revision(payload, command_id, *, before_write):
    before_write()
    return "revision"


def reconcile(store, kv, fx=None, authorize=lambda record: True, publish=publish_revision):
    tracker = MemoryTracker()
    tracker.add_issue("i1", "ENG-1")
    return process_commands(
        store,
        kv,
        tracker,
        cfg(),
        fx or FakeEffects(),
        "daemon",
        time.time_ns() // 1_000_000,
        publish_workflow=publish,
        authorize=authorize,
    )


def test_concurrent_idempotency_accepts_once_and_replays_after_heartbeat_loss(commands):
    store, kv = commands
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda _: accept(store), range(2)))
    assert records[0]["id"] == records[1]["id"]
    assert [item["commandId"] for item in store.audit()["items"]] == [records[0]["id"]]
    kv.delete(state.HEARTBEAT_KEY)
    assert accept(store)["id"] == records[0]["id"]
    with pytest.raises(CommandError, match="different request"):
        store.accept(
            actor="operator",
            role="admin",
            credential_fingerprint="f" * 64,
            action="pause",
            target=None,
            reason="Different maintenance reason",
            idempotency_key="request",
        )


def test_unavailable_audit_storage_never_accepts_intent(commands):
    store, _ = commands
    store.client.set(store.key, "invalid administrative source")
    with pytest.raises(redis.ResponseError):
        accept(store)
    assert store.client.get(store.key) == "invalid administrative source"
    assert store.client.get(store.admission_key) is None


def test_revoked_principal_cannot_execute_queued_pause(commands):
    store, kv = commands
    record = accept(store)
    reconcile(store, kv, authorize=lambda record: False)
    assert store.get(record["id"])["state"] == "failed"
    assert not store.paused()
    assert {item["result"] for item in store.audit()["items"]} == {"accepted", "failed"}


def test_pause_is_persistent_and_old_lease_cannot_resume(commands):
    store, kv = commands
    accept(store)
    reconcile(store, kv)
    assert store.paused()
    assert store.client.ttl(store.admission_key) == -1
    resume = accept(store, action="resume")
    kv.put(
        state.LOCK_KEY,
        {"owner": "successor", "lease_expires": time.time_ns() // 1_000_000 + 60_000},
    )
    with pytest.raises(CommandError, match="lease was lost"):
        reconcile(store, kv)
    assert store.paused()
    assert store.get(resume["id"])["state"] == "accepted"


def test_stale_daemon_snapshot_and_stale_runner_write_cannot_replace_successor(commands):
    _, kv = commands
    state.claim_issue(kv, "i1", "ENG-1", 1)
    state.acquire_dispatch_token(kv, "i1", 1, "daemon", 1)
    tick_kv = LeaseKV(kv, "daemon")
    stale = tick_kv.get(state.issue_key("i1"))
    successor = {**stale, "dispatch_seq": 2, "status": "running", "bridge_token": "new"}
    kv.put(state.issue_key("i1"), successor)
    with pytest.raises(RuntimeError, match="Lifecycle changed"):
        tick_kv.put(state.issue_key("i1"), {**stale, "status": "retry_queued"})
    assert not kv.fenced_put(
        state.issue_key("i1"), stale, {"attempt": 1, "dispatch_seq": 1}, state.dispatch_key("i1", 1)
    )
    assert kv.get(state.issue_key("i1")) == successor


def test_interruption_failure_stays_fenced_until_confirmed_and_revocation_holds_retry(commands):
    store, kv = commands
    state.claim_issue(kv, "i1", "ENG-1", 1)
    entry = kv.get(state.issue_key("i1"))
    entry.update(status="running", runner_call_id="owned-job", bridge_token="old")
    kv.put(state.issue_key("i1"), entry)
    record = accept(store, action="interrupt", target={"issueId": "i1", "dispatchSeq": 1})
    fx = FakeEffects()
    calls = []

    def unavailable(call_id, *, issue_id, seq):
        calls.append(call_id)
        raise RuntimeError("Kubernetes unavailable")

    fx.cancel_runner = unavailable
    reconcile(store, kv, fx)
    assert store.get(record["id"])["state"] == "applying"
    assert kv.get(state.issue_key("i1"))["status"] == "interrupting"
    assert kv.get(state.issue_key("i1"))["bridge_token"] is None
    fx.cancel_runner = lambda call_id, *, issue_id, seq: calls.append(call_id)
    reconcile(store, kv, fx, authorize=lambda record: False)
    assert store.get(record["id"])["state"] == "failed"
    held = kv.get(state.issue_key("i1"))
    assert held["status"] == "blocked"
    assert held["retry_at"] is None
    assert calls == ["owned-job", "owned-job"]


def test_publication_unknown_outcome_replays_same_command_before_later_intents(commands):
    store, kv = commands
    payload = {"baseRevision": "old", "patch": {}, "documentHash": "a" * 64}
    record = accept(store, action="workflow", payload=payload)
    later = accept(store, action="pause")
    committed = {}
    writes = []

    def publish(payload, command_id, *, before_write):
        if command_id in committed:
            return committed[command_id]
        before_write()
        writes.append(command_id)
        committed[command_id] = "published"
        raise RuntimeError("Connection lost after external write")

    reconcile(store, kv, publish=publish)
    assert store.get(record["id"])["state"] == "applying"
    assert store.get(later["id"])["state"] == "accepted"
    reconcile(store, kv, publish=publish)
    assert store.get(record["id"])["revision"] == "published"
    assert store.get(later["id"])["state"] == "succeeded"
    assert writes == [record["id"]]


def test_revocation_after_publication_does_not_falsify_committed_outcome(commands):
    store, kv = commands
    record = accept(store, action="workflow", payload={"documentHash": "a" * 64})
    committed = {}

    def publish(payload, command_id, *, before_write):
        if command_id in committed:
            return committed[command_id]
        before_write()
        committed[command_id] = "committed-revision"
        raise RuntimeError("Lost write acknowledgement")

    reconcile(store, kv, publish=publish)
    reconcile(store, kv, publish=publish, authorize=lambda record: False)
    final = store.get(record["id"])
    assert final["state"] == "succeeded"
    assert final["revision"] == "committed-revision"
    assert {event["result"] for event in store.audit()["items"]} == {"accepted", "succeeded"}


def test_publication_guard_reauthorizes_after_preparation(commands):
    store, kv = commands
    record = accept(store, action="workflow", payload={"documentHash": "a" * 64})
    authorized = True
    writes = []

    def publish(payload, command_id, *, before_write):
        nonlocal authorized
        authorized = False  # credentials revoked while remote configuration is prepared
        before_write()
        writes.append(command_id)
        return "revision"

    reconcile(store, kv, publish=publish, authorize=lambda record: authorized)
    assert writes == []
    assert store.get(record["id"])["state"] == "failed"
    assert "authorization_revoked" in store.get(record["id"])["result"]


def test_publication_guard_rechecks_lease_after_preparation(commands):
    store, kv = commands
    record = accept(store, action="workflow", payload={"documentHash": "a" * 64})
    writes = []

    def publish(payload, command_id, *, before_write):
        kv.put(
            state.LOCK_KEY,
            {"owner": "successor", "lease_expires": time.time_ns() // 1_000_000 + 60_000},
        )
        before_write()
        writes.append(command_id)
        return "revision"

    reconcile(store, kv, publish=publish)
    assert writes == []
    assert store.get(record["id"])["state"] == "applying"


def test_audit_cursor_pages_without_duplicate_events(commands):
    store, kv = commands
    accept(store)
    reconcile(store, kv)
    first = store.audit(limit=1)
    second = store.audit(cursor=first["nextCursor"], limit=1)
    assert first["items"][0]["result"] == "succeeded"
    assert second["items"][0]["result"] == "accepted"
    assert second["nextCursor"] is None


def test_terminal_publication_drops_private_payload_but_preserves_idempotency(commands):
    store, kv = commands
    payload = {"patch": {"prompt": "private prompt " * 10_000}}
    record = accept(store, action="workflow", payload=payload)
    reconcile(store, kv)
    assert store.get(record["id"])["payload"] is None
    assert accept(store, action="workflow", payload=payload)["id"] == record["id"]
    with pytest.raises(CommandError, match="different request"):
        accept(store, action="workflow", payload={"patch": {"prompt": "different"}})


def test_retention_keeps_pending_and_newest_terminal_commands(commands, monkeypatch):
    from symphony import admin_commands

    store, kv = commands
    monkeypatch.setattr(admin_commands, "MAX_TERMINAL", 2)
    first = accept(store, key="first")
    reconcile(store, kv)
    second = accept(store, key="second")
    reconcile(store, kv)
    # Finish times can share a millisecond; the retention order includes the ID.
    newest = max(
        (store.get(first["id"]), store.get(second["id"])),
        key=lambda record: (record["finishedMs"], record["id"]),
    )
    oldest_id = second["id"] if newest["id"] == first["id"] else first["id"]
    monkeypatch.setattr(admin_commands, "MAX_TERMINAL", 1)
    pending = accept(
        store, action="workflow", key="pending", payload={"patch": {"prompt": "large" * 30_000}}
    )
    store.prune()
    assert store.get(oldest_id) is None
    assert store.get(newest["id"])["state"] == "succeeded"
    assert store.get(pending["id"])["state"] == "accepted"
    assert {event["commandId"] for event in store.audit()["items"]} == {newest["id"], pending["id"]}


def test_atomic_lease_renewal_never_reacquires_after_expiry_or_disappearance(commands):
    _, kv = commands
    now = time.time_ns() // 1_000_000
    assert state.renew_lease(kv, "daemon", now, require_live=True)
    kv.put(state.LOCK_KEY, {"owner": "daemon", "lease_expires": now - 1})
    assert not state.renew_lease(kv, "daemon", now, require_live=True)
    kv.delete(state.LOCK_KEY)
    assert not state.renew_lease(kv, "daemon", now, require_live=True)
    assert kv.get(state.LOCK_KEY) is None
    assert state.renew_lease(kv, "successor", now)
    assert not state.renew_lease(kv, "daemon", now, require_live=True)
    assert kv.get(state.LOCK_KEY)["owner"] == "successor"


def test_inflight_guard_cannot_reacquire_between_check_and_renewal(commands, monkeypatch):
    from symphony.entry import daemon

    _, kv = commands
    renew = state.renew_lease
    renewed = threading.Event()

    def expire_before_atomic_renewal(kv, owner, now, **kwargs):
        kv.delete(state.LOCK_KEY)  # the previous lease expires while the guard is suspended
        result = renew(kv, owner, now, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(state, "renew_lease", expire_before_atomic_renewal)
    monkeypatch.setattr(state, "LEASE_TTL_MS", 3)
    with daemon._lease_guard(kv, "daemon") as authoritative:
        assert renewed.wait(timeout=2)
        assert not authoritative()
    assert kv.get(state.LOCK_KEY) is None


@pytest.mark.skipif(
    not os.path.isdir("/proc/self") or os.geteuid() == 0,
    reason="Requires unprivileged Linux procfs",
)
def test_unreadable_live_generation_cannot_confirm_termination():
    issue_id = uuid.uuid4().hex
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import ctypes,time; assert ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0; "
            "print('ready', flush=True); time.sleep(30)",
        ],
        env={**os.environ, "SYMPHONY_ISSUE_ID": issue_id, "SYMPHONY_DISPATCH_SEQ": "1"},
        stdout=subprocess.PIPE,
        text=True,
    )

    class LocalSandbox:
        def exec_script(self, script, timeout_s=None):
            result = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True, timeout=timeout_s
            )
            return result.returncode, result.stdout

    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(RuntimeError, match="termination could not be confirmed"):
            kill_agent_generation(LocalSandbox(), issue_id, 1)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def _every_process_environment_readable() -> bool:
    """The stop script refuses to confirm while a live process is unreadable, as in a
    workspace container. A host where another user runs processes (a CI runner) cannot pass."""
    if not os.path.isdir("/proc/self"):
        return False
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open(f"/proc/{entry}/environ", "rb") as environ:
                    environ.read(1)
            except FileNotFoundError:
                continue
            except PermissionError:
                return False
    return True


@pytest.mark.skipif(
    not _every_process_environment_readable(),
    reason="Workspace generation stopping needs Linux procfs with every process readable",
)
def test_delayed_process_stop_cannot_kill_a_successor_generation():
    issue_id = uuid.uuid4().hex
    processes = [
        subprocess.Popen(
            ["sleep", "30"],
            env={**os.environ, "SYMPHONY_ISSUE_ID": issue_id, "SYMPHONY_DISPATCH_SEQ": str(seq)},
        )
        for seq in (1, 2)
    ]

    class LocalSandbox:
        def exec_script(self, script, timeout_s=None):
            result = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True, timeout=timeout_s
            )
            return result.returncode, result.stdout

    try:
        kill_agent_generation(LocalSandbox(), issue_id, 1)
        assert processes[0].wait(timeout=2) != 0
        assert processes[1].poll() is None
        # Replay after the original is gone must still leave the successor untouched.
        kill_agent_generation(LocalSandbox(), issue_id, 1)
        assert processes[1].poll() is None
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_standalone_provider_audit_shares_cursor_without_command_receipts(commands):
    store, _ = commands
    command = accept(store)
    operation_id = uuid.uuid4().hex
    store.provider_audit(
        actor="operator", provider_id="custom", outcome="attempted", operation_id=operation_id
    )
    store.provider_audit(
        actor="operator", provider_id="custom", outcome="succeeded", operation_id=operation_id
    )
    events = []
    cursor = None
    while True:
        page = store.audit(cursor=cursor, limit=1)
        events.extend(page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert len({event["id"] for event in events}) == 3
    provider_events = [event for event in events if event["action"] == "provider.save"]
    assert {event["result"] for event in provider_events} == {"attempted", "succeeded"}
    assert all(event["commandId"] is None for event in provider_events)
    assert store.pending() == [command["id"]]
    assert store.get(operation_id) is None


def test_standalone_provider_audit_retention_removes_hash_fields(commands, monkeypatch):
    from symphony import admin_commands

    store, _ = commands
    monkeypatch.setattr(admin_commands, "MAX_TERMINAL", 1)
    now = 1_800_000_000_000
    monkeypatch.setattr(admin_commands, "_now", lambda: now)
    old_id = uuid.uuid4().hex
    store.provider_audit(
        actor="operator", provider_id="custom", outcome="attempted", operation_id=old_id
    )
    now += admin_commands.RETENTION_MS + 1
    current_id = uuid.uuid4().hex
    store.provider_audit(
        actor="operator", provider_id="custom", outcome="attempted", operation_id=current_id
    )
    assert store.client.hget(store.key, f"audit:{old_id}:attempted") is None
    now += 1
    store.provider_audit(
        actor="operator", provider_id="custom", outcome="succeeded", operation_id=current_id
    )
    now += 1
    newest_id = uuid.uuid4().hex
    store.provider_audit(
        actor="operator", provider_id="custom", outcome="attempted", operation_id=newest_id
    )
    assert store.client.hget(store.key, f"audit:{current_id}:attempted") is None
    assert {event["id"] for event in store.audit()["items"]} == {
        f"{current_id}:succeeded",
        f"{newest_id}:attempted",
    }
    now += admin_commands.RETENTION_MS + 1
    assert store.audit()["items"] == []
    store.prune()
    assert store.client.hlen(store.key) == 0


@pytest.mark.parametrize(
    "action", ["provider.oauth.login", "provider.subscription.seed", "provider.unknown"]
)
def test_provider_audit_only_records_saves(commands, action):
    """Every provider is configured by API key (internals §13): saving is the only change."""
    store, _ = commands
    with pytest.raises(CommandError) as rejected:
        store.provider_audit(actor="operator", provider_id="anthropic", outcome="attempted",
                             operation_id=uuid.uuid4().hex, action=action)
    assert rejected.value.code == "invalid_audit"
    assert store.audit()["items"] == []
