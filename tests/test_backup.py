"""Backup, restore, archive and retention against a real Redis (`symphony.backup`)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import time

import pytest

from symphony import backup, retention
from symphony.admin_commands import AdminStore, process_commands
from symphony.config import ConfigError, parse_workflow
from symphony.kv import RedisKV
from symphony.sessions import SessionStore
from symphony.trackers.memory import MemoryTracker

NS = "prod"
DAY_MS = 24 * 3600 * 1000


def workflow(extra: str = "") -> str:
    return f"---\ntracker: {{kind: memory}}\n{extra}---\nWork on the ticket.\n"


def seed(client) -> None:
    """One key of each type under both prefixes, some with a TTL."""
    client.set(f"sym:{NS}:teamconfig", json.dumps({"generation": 3}))
    client.set(f"sym:{NS}:setup:webhook:linear", "123", px=9_000_000)
    client.hset(f"sym:{NS}:tokens", mapping={"t1": "{}", "t2": "{}"})
    client.rpush(f"sym:{NS}:teamconfig:history", "a", "b", "c")
    client.sadd(f"sym:{NS}:merger:repos", "acme/app", "acme/lib")
    client.zadd(f"sym:{NS}:sessions", {"s1": 1, "s2": 2})
    client.xadd(f"sym:{NS}:session:s1:events", {"kind": "k", "data": "{}", "t": "1"})
    client.expire(f"sym:{NS}:session:s1:events", 3600)
    client.hset(f"admin:{NS}:commands", "audit:x:accepted", json.dumps(
        {"id": "x:accepted", "time": "2026-11-16T00:00:00Z", "commandId": "x"}))
    for i in range(backup.BATCH + 5):  # more than one export batch
        client.set(f"sym:{NS}:wake:target-{i}", str(i))
    # the unprefixed state families that a restore must bring back (kv.RedisKV writes JSON)
    client.set("hold:iss-1", json.dumps({"by": "alice", "reason": "takeover", "at": 1}))
    client.set("generation:iss-1", "4")
    client.set("metrics:202611", json.dumps({"dispatched": 9}), ex=7 * 86400)
    client.set("retro:iss-2", json.dumps({"at": 1}), ex=7 * 86400)
    client.set("lessons:usage_age", json.dumps({"slug": 2}), ex=7 * 86400)
    client.set("totals:tokens", json.dumps({"total": 1200}), ex=7 * 86400)


TRANSIENT = {
    "issue:iss-1": "{}",
    "dispatch:iss-1:4": "{}",
    "toolcall:iss-1:4:c1": "{}",
    "subissue:iss-1:k": "{}",
    "telemetry:iss-1": "{}",
    "lock:orchestrator": json.dumps({"owner": "old", "lease_expires": 0}),
    "orchestrator:heartbeat": "1",
    "poll:backoff": "{}",
}


def _names(client, namespace: str) -> list[str]:
    keys = set()
    for prefix in (*backup.prefixes(namespace), *backup.CORE_KEPT):
        keys.update(k for k in client.scan_iter(match=prefix + "*")
                    if k.startswith(backup.prefixes(namespace)) or backup.kept_core(k))
    return sorted(keys)


def _value(client, key: str):
    """DUMP, except a set compares by its members: Redis 7.0 (ubuntu-24.04's package) keeps
    small string sets in a hash table, whose DUMP order can differ after a restore."""
    if client.type(key) in (b"set", "set"):
        return sorted(client.smembers(key))
    return client.dump(key)


def snapshot(client, namespace: str = NS) -> dict:
    return {key: (_value(client, key), client.pttl(key)) for key in _names(client, namespace)}


def flush(client, namespace: str = NS) -> None:
    for key in _names(client, namespace):
        client.delete(key)


def exported(client, namespace: str = NS) -> io.BytesIO:
    out = io.BytesIO()
    backup.write_export(client, namespace, out)
    return out


def lines_of(blob: io.BytesIO) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(blob.getvalue()).splitlines()]


def rewrite(docs: list[dict], *, rehash: bool) -> io.BytesIO:
    """A backup file from edited lines; `rehash` recomputes the trailer like a forger would."""
    body = [(json.dumps(d, separators=(",", ":"), sort_keys=True) + "\n").encode() for d in docs[:-1]]
    trailer = dict(docs[-1])
    if rehash:
        trailer["sha256"] = hashlib.sha256(b"".join(body)).hexdigest()
        trailer["records"] = len(body) - 1
    raw = b"".join(body) + (json.dumps(trailer) + "\n").encode()
    return io.BytesIO(gzip.compress(raw))


def test_round_trip_restores_every_key_and_ttl(redis_client):
    seed(redis_client)
    redis_client.set(f"sym:{NS}:ratelimit:token:abc:1", "4", ex=60)
    redis_client.set(f"sym:{NS}:assistant:proposalfile:abc", "ZHJhZnQ=", ex=600)
    redis_client.set("sym:other:teamconfig", "{}")
    for key, value in TRANSIENT.items():
        redis_client.set(key, value)
    before = snapshot(redis_client)
    del before[f"sym:{NS}:ratelimit:token:abc:1"]  # volatile, never exported
    del before[f"sym:{NS}:assistant:proposalfile:abc"]

    blob = exported(redis_client)
    docs = lines_of(blob)
    header, trailer = docs[0], docs[-1]
    assert header["format"] == 1 and header["namespace"] == NS and header["keys"] == len(before)
    assert header["symphony_version"] and header["created_at"].endswith("Z")
    assert trailer["records"] == len(before) == len(docs) - 2
    assert {d["key"] for d in docs[1:-1]} == set(before)
    assert {"hold:iss-1", "generation:iss-1", "metrics:202611", "retro:iss-2",
            "lessons:usage_age", "totals:tokens"} <= set(before)
    assert not any(d["key"].startswith(tuple(backup.CORE_SKIPPED)) for d in docs[1:-1])

    flush(redis_client)
    report = backup.restore(redis_client, NS, blob, confirm=True)
    assert report["restored"] == len(before) and report["failed"] == 0
    after = snapshot(redis_client)
    assert after.pop(backup.status_key(NS))  # the restore records its outcome
    assert set(after) == set(before)
    for key, (dump, ttl) in before.items():
        assert after[key][0] == dump, key
        if ttl < 0:
            assert after[key][1] == -1, key
        else:
            assert 0 < after[key][1] <= ttl and ttl - after[key][1] < 5000, key
    # nothing outside the backup's families was touched or exported
    assert redis_client.get("sym:other:teamconfig") == "{}"
    assert {key: redis_client.get(key) for key in TRANSIENT} == TRANSIENT


def test_every_state_family_is_classified():
    from symphony import kv, state

    families = {value for name, value in vars(state).items()
                if name.endswith(("_PREFIX", "_KEY")) and isinstance(value, str)}
    families |= set(kv.NO_TTL_PREFIXES) | {"subissue:"}
    lock = {state.LOCK_KEY: "lock:"}
    classified = set(backup.CORE_KEPT) | set(backup.CORE_SKIPPED)
    assert {lock.get(f, f) for f in families} <= classified
    assert not set(backup.CORE_KEPT) & set(backup.CORE_SKIPPED)


def test_export_streams_gzip_chunks(redis_client):
    seed(redis_client)
    export = backup.Export(redis_client, NS)
    chunks = list(export)
    # the header, then compressed bytes after each batch of keys, then the trailer
    assert len(chunks) == 1 + -(-export.records // backup.BATCH) + 1
    assert all(chunks)
    assert gzip.decompress(b"".join(chunks)).count(b"\n") == export.records + 2


@pytest.mark.parametrize(
    "damage", ["value", "header", "trailer_count", "no_trailer", "cut", "not_gzip"]
)
def test_tampered_backup_is_refused_before_any_write(redis_client, damage):
    seed(redis_client)
    blob = exported(redis_client)
    docs = lines_of(blob)
    if damage == "value":
        docs[1]["dump"] = docs[2]["dump"]
        blob, code = rewrite(docs, rehash=False), "checksum_mismatch"
    elif damage == "header":
        docs[0]["created_at"] = "2020-01-01T00:00:00.000Z"
        blob, code = rewrite(docs, rehash=False), "checksum_mismatch"
    elif damage == "trailer_count":
        docs[-1]["records"] += 1
        blob, code = rewrite(docs, rehash=False), "checksum_mismatch"
    elif damage == "no_trailer":
        raw = gzip.decompress(blob.getvalue())
        blob, code = io.BytesIO(gzip.compress(raw.rsplit(b"\n", 2)[0] + b"\n")), "truncated"
    elif damage == "cut":
        blob, code = io.BytesIO(blob.getvalue()[:-40]), "truncated"
    else:
        blob, code = io.BytesIO(b"not a backup"), "truncated"
    flush(redis_client)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(redis_client, NS, blob, confirm=True)
    assert caught.value.code == code
    assert snapshot(redis_client) == {}


def test_a_key_outside_the_prefix_is_never_restored(redis_client):
    seed(redis_client)
    docs = lines_of(exported(redis_client))
    docs[1]["key"] = "lock:orchestrator"
    forged = rewrite(docs, rehash=True)  # a valid checksum does not make it acceptable
    flush(redis_client)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(redis_client, NS, forged, confirm=True)
    assert caught.value.code == "outside_prefix"
    assert redis_client.exists("lock:orchestrator") == 0
    assert snapshot(redis_client) == {}


def test_restore_is_refused_while_the_daemon_runs_unpaused(redis_client):
    seed(redis_client)
    blob = exported(redis_client)
    kv = RedisKV(redis_client)
    from symphony import state

    assert state.renew_lease(kv, "daemon-1", time.time_ns() // 1_000_000)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(redis_client, NS, blob, confirm=True)
    assert caught.value.code == "daemon_running" and caught.value.status_code == 409
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(redis_client, NS, blob, confirm=False)
    assert caught.value.code == "confirm_required"

    # paused (what a processed `pause` command leaves): the restore runs and keeps the pause
    redis_client.set(f"admin:{NS}:admission", "true")
    report = backup.restore(redis_client, NS, blob, confirm=True)
    assert report["failed"] == 0
    assert AdminStore(redis_client, NS).paused() is True


def test_the_pause_flag_is_never_restored(redis_client):
    seed(redis_client)
    redis_client.set(f"admin:{NS}:admission", "false")
    blob = exported(redis_client)
    redis_client.set(f"admin:{NS}:admission", "true")
    report = backup.restore(redis_client, NS, blob, confirm=True)
    assert report["kept"] == 1
    assert redis_client.get(f"admin:{NS}:admission") == "true"


def test_another_namespace_needs_namespace_from(redis_client):
    seed(redis_client)
    blob = exported(redis_client)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(redis_client, "staging", blob, confirm=True)
    assert caught.value.code == "namespace_mismatch" and "--namespace-from prod" in caught.value.message
    with pytest.raises(backup.BackupError):
        backup.restore(redis_client, "staging", blob, confirm=True, namespace_from="dev")
    report = backup.restore(redis_client, "staging", blob, confirm=True, namespace_from=NS)
    assert report["source_namespace"] == NS and report["namespace"] == "staging"
    restored = set(snapshot(redis_client, "staging")) - {backup.status_key("staging")}
    assert restored == {key.replace(f":{NS}:", ":staging:", 1) for key in snapshot(redis_client)}


def test_backups_on_a_directory_keep_the_newest(redis_client, tmp_path):
    seed(redis_client)
    target = backup.DirTarget(str(tmp_path), NS)
    names = [backup.backup_to(redis_client, NS, target, 2, now_ms=1_800_000_000_000 + i)["name"]
             for i in range(3)]
    assert [item["name"] for item in target.list(backup.BACKUPS)] == names[1:]
    flush(redis_client)
    report = backup.restore_from(redis_client, NS, target, names[-1], confirm=True)
    assert report["restored"] == report["records"] > backup.BATCH
    with pytest.raises(backup.BackupError) as caught:
        backup.restore_from(redis_client, NS, target, "../../etc/passwd", confirm=True)
    assert caught.value.code == "invalid_name"


# -- archive ------------------------------------------------------------------------------

def _ended_session(sessions: SessionStore, sid: str) -> None:
    sessions.open(sid, kind="implement", identifier="ENG-1", title="Fix it")
    sessions.append_event(sid, "message_end", {"text": "done"})
    sessions.append_transcript(sid, [{"role": "user", "text": "hi"}, {"role": "agent", "text": "ok"}])
    sessions.close(sid, "succeeded")


def _archive_lines(target, name) -> list[dict]:
    out = io.BytesIO()
    backup.read_archive(target, name, out)
    return [json.loads(line) for line in gzip.decompress(out.getvalue()).splitlines()]


def test_archive_keeps_ended_sessions_and_audit_before_redis_drops_them(redis_client, tmp_path):
    sessions = SessionStore(redis_client, NS)
    store = AdminStore(redis_client, NS)
    _ended_session(sessions, "iss-1:1")
    sessions.open("iss-2:1", kind="implement", identifier="ENG-2")  # still running: not archived
    event = store.audit_event(actor="alice", action="token.create", target="token:t1", reason="new")
    # the session's keys still exist, with the retention TTL that will drop them
    assert 0 < redis_client.ttl(f"sym:{NS}:session:iss-1:1:events") <= retention.SESSIONS_DAYS * 86400

    target = backup.DirTarget(str(tmp_path), NS)
    later = time.time_ns() // 1_000_000 + backup.ARCHIVE_LAG_MS + 1000
    first = backup.archive(redis_client, NS, target, now_ms=later)
    assert first["sessions"] == 1 and first["audit"] == 1
    lines = _archive_lines(target, first["name"])
    kinds = [line["kind"] for line in lines]
    assert kinds.count("session") == 1 and kinds.count("event") >= 2
    assert [line["entry"]["text"] for line in lines if line["kind"] == "transcript"] == ["hi", "ok"]
    assert next(line for line in lines if line["kind"] == "session")["meta"]["outcome"] == "succeeded"
    assert [line["event"]["id"] for line in lines if line["kind"] == "audit"] == [event["id"]]

    # nothing new: no object; a later session and audit entry go in the next one
    assert backup.archive(redis_client, NS, target, now_ms=later + 1)["name"] is None
    _ended_session(sessions, "iss-3:1")
    store.audit_event(actor="alice", action="token.revoke", target="token:t1", reason="old")
    second = backup.archive(redis_client, NS, target, now_ms=later + 2 * backup.ARCHIVE_LAG_MS)
    assert second["sessions"] == 1 and second["audit"] == 1
    assert [item["name"] for item in target.list(backup.ARCHIVE)] == [first["name"], second["name"]]

    # Redis drops the first session; the archive still has it and forgets its marker
    for part in ("meta", "events", "transcript", "intents"):
        redis_client.delete(f"sym:{NS}:session:iss-1:1:{part}")
    backup.archive(redis_client, NS, target, now_ms=later + 3 * backup.ARCHIVE_LAG_MS)
    assert "iss-1:1" not in redis_client.hgetall(f"sym:{NS}:archive:sessions")
    assert any(line.get("sid") == "iss-1:1" for line in _archive_lines(target, first["name"]))


def test_run_idles_and_records_when_not_configured(redis_client, monkeypatch):
    from symphony.entry import backup as entry

    path = redis_client.connection_pool.connection_kwargs["path"]
    monkeypatch.setenv("REDIS_URL", f"unix://{path}")
    monkeypatch.setenv("POD_NAMESPACE", NS)
    assert entry.main(["--workflow", "/nonexistent/WORKFLOW.md"]) == "not-configured"
    doc = backup.status(redis_client, NS, None)
    assert doc["target"] is None and doc["last"]["backup"]["status"] == "not_configured"

    cfg = parse_workflow(workflow())
    with pytest.raises(backup.BackupError) as caught:
        backup.run(redis_client, NS, cfg)
    assert caught.value.code == "not_configured"
    assert "backup.s3 or backup.dir" in backup.status(redis_client, NS, cfg)["reason"]


def test_the_cron_entry_archives_and_backs_up(redis_client, monkeypatch, tmp_path):
    from symphony.entry import backup as entry

    seed(redis_client)
    path = redis_client.connection_pool.connection_kwargs["path"]
    monkeypatch.setenv("REDIS_URL", f"unix://{path}")
    monkeypatch.setenv("POD_NAMESPACE", NS)
    file = tmp_path / "WORKFLOW.md"
    file.write_text(workflow(f"backup: {{dir: {tmp_path / 'store'}, keep: 3}}\n"))
    assert entry.main(["--workflow", str(file)]) == "done"
    target = backup.DirTarget(str(tmp_path / "store"), NS)
    [item] = target.list(backup.BACKUPS)
    assert backup.status(redis_client, NS, parse_workflow(file.read_text()))["last"]["backup"][
        "name"] == item["name"]


# -- retention ----------------------------------------------------------------------------

def test_retention_config_has_defaults_and_bounds():
    cfg = parse_workflow(workflow())
    assert (cfg.retention_sessions_days, cfg.retention_audit_days) == (7, 30)
    cfg = parse_workflow(workflow("retention: {sessions_days: 3, audit_days: 90}\n"))
    assert (cfg.retention_sessions_days, cfg.retention_audit_days) == (3, 90)
    for bad in ("{sessions_days: 1}", "{audit_days: 4000}", "{sessions_days: 0}",
                "{audit_days: true}", "{days: 3}", "[7]"):
        with pytest.raises(ConfigError):
            parse_workflow(workflow(f"retention: {bad}\n"))


def test_backup_config_is_checked():
    cfg = parse_workflow(workflow(
        "backup: {keep: 5, s3: {endpoint: 'https://s3.eu-west-1.amazonaws.com', "
        "bucket: my-backups, prefix: /symphony/prod, region: eu-west-1}}\n"
    ))
    assert cfg.backup_keep == 5 and cfg.backup_s3 == {
        "endpoint": "https://s3.eu-west-1.amazonaws.com", "bucket": "my-backups",
        "prefix": "symphony/prod/", "region": "eu-west-1",
    }
    for bad in ("{keep: 0}", "{dir: relative/path}", "{s3: {endpoint: 'ftp://x', bucket: abc}}",
                "{s3: {endpoint: 'https://x/path', bucket: abc}}",
                "{s3: {endpoint: 'https://x', bucket: 'A_B'}}",
                "{s3: {endpoint: 'https://x', bucket: abc, prefix: '../up'}}",
                "{s3: {endpoint: 'https://x', bucket: abc, access_key: k}}",
                "{s3: {endpoint: 'https://x', bucket: abc}, dir: /tmp/x}"):
        with pytest.raises(ConfigError):
            parse_workflow(workflow(f"backup: {bad}\n"))


class _NoEffects:
    pass


def test_retention_from_the_workflow_reaches_sessions_and_audit(redis_client, monkeypatch):
    from symphony import admin_commands

    cfg = parse_workflow(workflow("retention: {sessions_days: 2, audit_days: 10}\n"))
    store = AdminStore(redis_client, NS)
    process_commands(
        store, RedisKV(redis_client), MemoryTracker(), cfg, _NoEffects(), "daemon",
        time.time_ns() // 1_000_000, publish_workflow=lambda *a, **k: None,
        authorize=lambda record: True,
    )
    assert retention.read(redis_client, NS) == {"sessions_days": 2, "audit_days": 10}

    sessions = SessionStore(redis_client, NS)
    _ended_session(sessions, "iss-9:1")
    ttl = redis_client.ttl(f"sym:{NS}:session:iss-9:1:meta")
    assert 2 * 86400 - 5 <= ttl <= 2 * 86400

    now = 1_800_000_000_000
    monkeypatch.setattr(admin_commands, "_now", lambda: now)
    old = store.audit_event(actor="alice", action="token.create", target="token:t1", reason="a")
    now += 9 * DAY_MS
    assert [e["id"] for e in store.audit()["items"]] == [old["id"]]
    now += 2 * DAY_MS  # 11 days: past the 10-day window, before the 30-day default
    assert store.audit()["items"] == []
    store.prune()
    assert redis_client.hlen(store.key) == 0
