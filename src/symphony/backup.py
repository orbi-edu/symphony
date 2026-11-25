"""Backup, restore and archive of one namespace's Redis state.

**Backup file** (format 1): gzip of newline-delimited JSON, written as a stream.

- Line 1, the header: `{format, namespace, symphony_version, created_at, keys}`. `keys` is
  the number of keys that SCAN found.
- One record per key: `{key, type, ttl_ms, dump}`. `dump` is the base64 of Redis `DUMP`;
  `ttl_ms` is the remaining time to live, or null for a key that never expires. A key that
  expired between SCAN and DUMP has no record.
- The last line, the trailer: `{sha256, records}`. `sha256` is the hex SHA-256 of every line
  before the trailer (the header included), newlines included.

The backup holds the keys under `sym:<ns>:` and `admin:<ns>:`, and the `state` families
without a namespace prefix that the daemon cannot rebuild (`CORE_KEPT`). It leaves out the
API rate limit counters (`sym:<ns>:ratelimit:*`, a minute long), the files the assistant keeps
for an "Attach" click (`sym:<ns>:assistant:evidence:*`, an hour long) and the `state` families
that reconcile rebuilds from the tracker or that live only while a process runs (`CORE_SKIPPED`).
The `state` keys carry no namespace: Symphony runs one Redis per namespace
(`deploy/redis.yaml` is in the namespaced kustomization), so the header's namespace names them.

**Restore** checks the whole file first (format, checksum, counts, and that every key is
under the prefixes of the backup's namespace or in a `CORE_KEPT` family). Only then does it
run `RESTORE … REPLACE` with each key's TTL. It refuses while the daemon holds its lease and
is not paused, refuses another namespace unless `namespace_from` names it, and never changes
the pause flag (`admin:<ns>:admission`).

**Targets** keep `backups/<stamp>.ndjson.gz` and `archive/<stamp>.jsonl.gz` under
`<prefix><ns>/` of an S3 bucket (`symphony.s3`) or under `<dir>/<ns>/` of a directory.

**Archive** appends one JSONL object per run: the ended sessions that the archive has not
seen (meta, events, transcript), then the audit entries written since the last run. It
runs before Redis drops them (`symphony.retention`).
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import zlib
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from . import __version__, retention, state
from .admin_commands import AdminStore
from .s3 import S3Client, S3Error

FORMAT = 1
BATCH = 200
BACKUPS = "backups"
ARCHIVE = "archive"
FOLDERS = (BACKUPS, ARCHIVE)
NAME = {
    BACKUPS: re.compile(r"\d{8}T\d{6}\.\d{3}Z\.ndjson\.gz\Z"),
    ARCHIVE: re.compile(r"\d{8}T\d{6}\.\d{3}Z\.jsonl\.gz\Z"),
}
ACCESS_KEY_ENV = "SYMPHONY_BACKUP_ACCESS_KEY_ID"
SECRET_KEY_ENV = "SYMPHONY_BACKUP_SECRET_ACCESS_KEY"
# An audit entry younger than this waits for the next run, so a writer with a slow clock
# cannot add an entry behind the archive's watermark.
ARCHIVE_LAG_MS = 5 * 60 * 1000
NOT_CONFIGURED = (
    "No backup target: set backup.s3 or backup.dir in WORKFLOW.md (docs/backup.md)."
)
NO_WORKFLOW = "WORKFLOW.md is missing, empty or invalid."


class BackupError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def prefixes(namespace: str) -> tuple[str, str]:
    return f"sym:{namespace}:", f"admin:{namespace}:"


# Every unprefixed `state` family (`state.py`, `kv.NO_TTL_PREFIXES`), with why it is backed
# up or not. A family ending in `:` is a prefix; the others are single keys.
CORE_KEPT = {
    "hold:": "a person's hold never expires; losing it would release a stopped ticket",
    "generation:": "dispatch fencing tombstones; losing one lets an old Job address a reopened ticket",
    "metrics:": "monthly counters that feed telemetry; the tracker cannot rebuild them",
    "retro:": "retrospective tokens; losing one repeats a retrospective and its learnings PR",
    "lessons:usage_age": "learning decay counters; the tracker cannot rebuild them",
    "totals:tokens": "the token ledger totals; the tracker cannot rebuild them",
    "answered:": "PR comments that answered a question; losing one lets the review watch act on it again",
    "abandoned:": "canceled-ticket markers; losing one leaves that ticket's questions open until their reminder check",
}
CORE_SKIPPED = {
    "issue:": "tracking entries; reconcile rebuilds them from the tracker",
    "dispatch:": "tokens of live runs; a restore leaves the live ones, and an old one is stale",
    "toolcall:": "tool call replay cache of live runs; a restore leaves it, an old one is stale",
    "subissue:": "sub-issue replay records of live runs; a restore leaves them",
    "telemetry:": "span rollups of the current attempts; the runners write them",
    "lock:": "the daemon lease; a restored lease would block the live daemon",
    "orchestrator:heartbeat": "liveness; the daemon writes it each tick",
    "daemon:setup": "setup status; the daemon writes it each tick",
    "refresh:requested": "a pending refresh; the next tick reads the tracker anyway",
    "ratelimits:engine": "engine rate limit state; the next calls observe it again",
    "poll:backoff": "tracker poll backoff; it starts again from zero",
}


def kept_core(key: str) -> bool:
    """A `CORE_KEPT` key: under a prefix family, or one of the single keys."""
    return any(key == f or (f.endswith(":") and key.startswith(f)) for f in CORE_KEPT)


def _volatile(namespace: str) -> tuple[str, ...]:
    # Rate limit counters live a minute; a file waiting for an "Attach" click lives an hour
    # and may be megabytes (`assistant/share.py`).
    return (f"sym:{namespace}:ratelimit:", f"sym:{namespace}:assistant:evidence:")


def _kept(namespace: str) -> frozenset[str]:
    return frozenset({f"admin:{namespace}:admission"})


def status_key(namespace: str) -> str:
    return f"sym:{namespace}:backup:status"


def _sessions_marker(namespace: str) -> str:
    return f"sym:{namespace}:archive:sessions"


def _audit_marker(namespace: str) -> str:
    return f"sym:{namespace}:archive:audit"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def stamp(ms: int) -> str:
    moment = datetime.fromtimestamp(ms / 1000, UTC)
    return moment.strftime("%Y%m%dT%H%M%S") + f".{ms % 1000:03d}Z"


def _line(doc: Mapping) -> bytes:
    return (json.dumps(doc, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n").encode()


def _glob(prefix: str) -> str:
    return re.sub(r"([\\*?\[\]])", r"\\\1", prefix) + "*"


def record_status(client, namespace: str, kind: str, doc: dict) -> None:
    """The last `backup`, `archive` or `restore` outcome, shown by `GET /v1/backup`."""
    client.hset(status_key(namespace), kind, json.dumps(doc, sort_keys=True))


# -- export -----------------------------------------------------------------------------

class Export:
    """A backup file as a stream of gzip bytes. Only the key names are held in memory; the
    values are read in batches of `BATCH` keys, one pipeline round trip per batch. (Not a
    MULTI: redis-py decodes an EXEC reply whole, and a DUMP payload is not text.)"""

    def __init__(self, client, namespace: str, *, now_ms: int | None = None) -> None:
        self.client, self.namespace = client, namespace
        self.created_ms = _now_ms() if now_ms is None else now_ms
        self.header: dict | None = None
        self.records = 0

    def _names(self) -> list[str]:
        owned, volatile = prefixes(self.namespace), _volatile(self.namespace)
        names: set[str] = set()  # SCAN may return a key twice
        for prefix in (*owned, *CORE_KEPT):
            for key in self.client.scan_iter(match=_glob(prefix), count=1000):
                if (key.startswith(owned) or kept_core(key)) and not key.startswith(volatile):
                    names.add(key)
        return sorted(names)

    def __iter__(self) -> Iterator[bytes]:
        names = self._names()
        self.header = {
            "format": FORMAT,
            "namespace": self.namespace,
            "symphony_version": __version__,
            "created_at": _iso(self.created_ms),
            "keys": len(names),
        }
        compressor = zlib.compressobj(6, zlib.DEFLATED, 31)  # 31: a gzip member
        digest = hashlib.sha256()

        def hashed(doc: Mapping) -> bytes:
            raw = _line(doc)
            digest.update(raw)
            return compressor.compress(raw)

        yield hashed(self.header)
        for start in range(0, len(names), BATCH):
            batch = names[start : start + BATCH]
            pipe = self.client.pipeline(transaction=False)
            for key in batch:
                pipe.type(key)
                pipe.dump(key)
                pipe.pttl(key)
            values = pipe.execute()
            out = []
            for index, key in enumerate(batch):
                kind, payload, ttl_ms = values[3 * index : 3 * index + 3]
                # Gone before DUMP, or expired between DUMP and PTTL (-2): no record.
                if payload is None or ttl_ms == -2:
                    continue
                self.records += 1
                out.append(hashed({
                    "key": key,
                    "type": kind,
                    "ttl_ms": ttl_ms if ttl_ms >= 0 else None,
                    "dump": base64.b64encode(payload).decode(),
                }))
            # A sync flush per batch: the reader gets bytes as the export runs.
            yield b"".join(out) + compressor.flush(zlib.Z_SYNC_FLUSH)
        trailer = _line({"sha256": digest.hexdigest(), "records": self.records})
        yield compressor.compress(trailer) + compressor.flush()

    def summary(self) -> dict:
        return {**(self.header or {}), "records": self.records}


def write_export(client, namespace: str, dest: BinaryIO, *, now_ms: int | None = None) -> dict:
    export = Export(client, namespace, now_ms=now_ms)
    for chunk in export:
        dest.write(chunk)
    dest.flush()
    return export.summary()


# -- verify and restore -----------------------------------------------------------------

def _invalid(message: str) -> BackupError:
    return BackupError("invalid_backup", message, 422)


def _lines(source: BinaryIO) -> Iterator[bytes]:
    source.seek(0)
    try:
        with gzip.GzipFile(fileobj=source, mode="rb") as stream:
            yield from stream
    except (OSError, EOFError, zlib.error):
        raise BackupError(
            "truncated", "The file is not a complete gzip backup.", 422
        ) from None


def _records(source: BinaryIO) -> Iterator[tuple[str, dict, bytes]]:
    """`("header", doc, raw)`, then `("record", doc, raw)` per key, then the trailer."""
    first = True
    for raw in _lines(source):
        try:
            doc = json.loads(raw)
        except ValueError:
            raise _invalid("A line of the backup is not JSON.") from None
        if not isinstance(doc, dict):
            raise _invalid("A line of the backup is not a JSON object.")
        if first:
            first = False
            yield "header", doc, raw
        elif "key" in doc:
            yield "record", doc, raw
        elif "sha256" in doc:
            yield "trailer", doc, raw
        else:
            raise _invalid("A line of the backup is neither a record nor the trailer.")


def verify(source: BinaryIO) -> dict:
    """Read the whole file once and check it. Returns the header and the record count."""
    digest = hashlib.sha256()
    header: dict | None = None
    trailer: dict | None = None
    records = 0
    owned: tuple[str, ...] = ()
    for kind, doc, raw in _records(source):
        if trailer is not None:
            raise _invalid("The backup has data after its trailer.")
        if kind == "header":
            if doc.get("format") != FORMAT:
                raise BackupError(
                    "unsupported_format",
                    f"This Symphony reads backup format {FORMAT}, not {doc.get('format')!r}.",
                    422,
                )
            namespace = doc.get("namespace")
            if not isinstance(namespace, str) or not namespace:
                raise _invalid("The backup header has no namespace.")
            if type(doc.get("keys")) is not int or not isinstance(doc.get("created_at"), str):
                raise _invalid("The backup header is incomplete.")
            header, owned = doc, prefixes(namespace)
            digest.update(raw)
        elif kind == "record":
            key, ttl_ms, dump = doc.get("key"), doc.get("ttl_ms"), doc.get("dump")
            if not isinstance(key, str) or not isinstance(dump, str) or not (
                ttl_ms is None or (type(ttl_ms) is int and ttl_ms >= 0)
            ):
                raise _invalid("A record of the backup is malformed.")
            if not (key.startswith(owned) or kept_core(key)):
                raise BackupError(
                    "outside_prefix",
                    "The backup holds a key outside the namespace prefixes and the kept "
                    "state families. Nothing was restored.",
                    422,
                )
            records += 1
            digest.update(raw)
        else:
            trailer = doc
    if header is None:
        raise _invalid("The backup is empty.")
    if trailer is None:
        raise BackupError("truncated", "The backup has no trailer: the file is incomplete.", 422)
    expected = trailer.get("sha256")
    if not isinstance(expected, str) or not hmac.compare_digest(expected, digest.hexdigest()):
        raise BackupError(
            "checksum_mismatch",
            "The backup's SHA-256 does not match its content: the file was changed or damaged.",
            422,
        )
    if trailer.get("records") != records or records > header["keys"]:
        raise BackupError(
            "checksum_mismatch", "The backup's record count does not match its content.", 422
        )
    return {**header, "records": records}


def daemon_running(client) -> bool:
    """The daemon (or the local stack) holds a live lease."""
    raw = client.get(state.LOCK_KEY)
    if not raw:
        return False
    try:
        lease = json.loads(raw)
    except ValueError:
        return True  # unreadable: assume it runs
    seconds, micros = client.time()
    return int(lease.get("lease_expires", 0)) > seconds * 1000 + micros // 1000


def check_restorable(client, namespace: str) -> None:
    if daemon_running(client) and not AdminStore(client, namespace).paused():
        raise BackupError(
            "daemon_running",
            "The daemon is running and not paused. Run `symphony pause` (or stop the daemon) "
            "first.",
            409,
        )


def restore(
    client,
    namespace: str,
    source: BinaryIO,
    *,
    confirm: bool,
    namespace_from: str | None = None,
    now_ms: int | None = None,
) -> dict:
    """Restore a backup file into `namespace`. Every check runs before the first write."""
    if not confirm:
        raise BackupError(
            "confirm_required", "A restore replaces keys. Repeat it with --confirm.", 422
        )
    check_restorable(client, namespace)
    info = verify(source)
    origin = info["namespace"]
    if origin != (namespace_from or namespace):
        hint = (
            f" Give --namespace-from {origin} to restore it into {namespace!r}."
            if namespace_from is None
            else ""
        )
        raise BackupError(
            "namespace_mismatch", f"The backup is of namespace {origin!r}.{hint}", 409
        )
    mapping = list(zip(prefixes(origin), prefixes(namespace), strict=True))
    kept = _kept(namespace)
    restored = skipped = failed = 0
    error = None
    pending: list[tuple[str, int, bytes]] = []

    def flush() -> None:
        nonlocal restored, failed, error
        if not pending:
            return
        pipe = client.pipeline(transaction=False)
        for key, ttl_ms, payload in pending:
            pipe.restore(key, ttl_ms, payload, replace=True)
        for result in pipe.execute(raise_on_error=False):
            if isinstance(result, Exception):
                failed += 1
                error = error or str(result).split("\n", 1)[0][:200]
            else:
                restored += 1
        pending.clear()

    for kind, doc, _raw in _records(source):
        if kind != "record":
            continue
        key = doc["key"]
        for old, new in mapping:
            if key.startswith(old):
                key = new + key[len(old) :]
                break
        if key in kept:
            skipped += 1
            continue
        try:
            payload = base64.b64decode(doc["dump"], validate=True)
        except (binascii.Error, ValueError):
            raise _invalid("A record of the backup has an invalid dump.") from None
        ttl_ms = doc["ttl_ms"]
        # RESTORE reads 0 as "no expiry": a key that was about to expire keeps 1 ms.
        pending.append((key, 0 if ttl_ms is None else max(ttl_ms, 1), payload))
        if len(pending) >= BATCH:
            flush()
    flush()
    report = {
        "namespace": namespace,
        "source_namespace": origin,
        "created_at": info["created_at"],
        "symphony_version": info.get("symphony_version"),
        "keys": info["keys"],
        "records": info["records"],
        "restored": restored,
        "kept": skipped,
        "failed": failed,
    }
    done = _now_ms() if now_ms is None else now_ms
    record_status(client, namespace, "restore", {
        "status": "failed" if failed else "ok", "at": _iso(done),
        **{k: report[k] for k in ("source_namespace", "created_at", "restored", "failed")},
    })
    if failed:
        raise BackupError(
            "restore_incomplete",
            f"{failed} keys could not be restored and {restored} were ({error}).",
            500,
        )
    return report


# -- targets ----------------------------------------------------------------------------

class Target(Protocol):
    def describe(self) -> dict: ...
    def put(self, folder: str, name: str, source: BinaryIO) -> None: ...
    def get(self, folder: str, name: str, dest: BinaryIO) -> None: ...
    def list(self, folder: str) -> list[dict]: ...
    def delete(self, folder: str, name: str) -> None: ...


def check_name(folder: str, name: str) -> str:
    if folder not in FOLDERS or not NAME[folder].fullmatch(name or ""):
        raise BackupError("invalid_name", f"{name!r} is not a {folder} object name.", 422)
    return name


class DirTarget:
    def __init__(self, root: str, namespace: str) -> None:
        self.root = root
        self.base = Path(root) / namespace

    def describe(self) -> dict:
        return {"kind": "dir", "dir": self.root}

    def _path(self, folder: str, name: str) -> Path:
        return self.base / folder / check_name(folder, name)

    def put(self, folder: str, name: str, source: BinaryIO) -> None:
        path = self._path(folder, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{path.name}.partial")
        source.seek(0)
        with open(partial, "wb") as out:
            while chunk := source.read(1024 * 1024):
                out.write(chunk)
        os.replace(partial, path)

    def get(self, folder: str, name: str, dest: BinaryIO) -> None:
        path = self._path(folder, name)
        try:
            with open(path, "rb") as source:
                while chunk := source.read(1024 * 1024):
                    dest.write(chunk)
        except FileNotFoundError:
            raise BackupError("not_found", f"{folder}/{name} does not exist.", 404) from None

    def list(self, folder: str) -> list[dict]:
        directory = self.base / folder
        if not directory.is_dir():
            return []
        out = []
        for path in sorted(directory.iterdir()):
            if NAME[folder].fullmatch(path.name):
                info = path.stat()
                out.append({"name": path.name, "size": info.st_size,
                            "modified": _iso(int(info.st_mtime * 1000))})
        return out

    def delete(self, folder: str, name: str) -> None:
        self._path(folder, name).unlink(missing_ok=True)


class S3Target:
    def __init__(self, s3: dict, namespace: str, env: Mapping[str, str]) -> None:
        access_key, secret_key = env.get(ACCESS_KEY_ENV), env.get(SECRET_KEY_ENV)
        if not access_key or not secret_key:
            raise BackupError(
                "not_configured",
                f"backup.s3 is set, but the symphony-backup Secret has no {ACCESS_KEY_ENV} "
                f"and {SECRET_KEY_ENV}.",
            )
        self.s3 = s3
        self.base = f"{s3['prefix']}{namespace}/"
        self.client = S3Client(
            endpoint=s3["endpoint"], bucket=s3["bucket"], region=s3["region"],
            access_key=access_key, secret_key=secret_key,
        )

    def describe(self) -> dict:
        return {"kind": "s3", **self.s3}

    def _key(self, folder: str, name: str) -> str:
        return f"{self.base}{folder}/{check_name(folder, name)}"

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise BackupError("not_found", "The object does not exist.", 404) from None
            raise BackupError(
                "target_error", f"The S3 target answered {exc.code} ({exc.status}).", 502
            ) from None

    def put(self, folder: str, name: str, source: BinaryIO) -> None:
        self._call(self.client.put, self._key(folder, name), source)

    def get(self, folder: str, name: str, dest: BinaryIO) -> None:
        self._call(self.client.get, self._key(folder, name), dest)

    def list(self, folder: str) -> list[dict]:
        prefix = f"{self.base}{folder}/"
        out = []
        for item in self._call(self.client.list, prefix):
            name = item["key"][len(prefix) :]
            if NAME[folder].fullmatch(name):
                out.append({"name": name, "size": item["size"], "modified": item["last_modified"]})
        return sorted(out, key=lambda item: item["name"])

    def delete(self, folder: str, name: str) -> None:
        self._call(self.client.delete, self._key(folder, name))


def target(cfg, namespace: str, env: Mapping[str, str] | None = None) -> Target:
    """The configured target. Raises `not_configured` when there is no WORKFLOW or it
    names no target."""
    env = os.environ if env is None else env
    if cfg is None:
        raise BackupError("not_configured", NO_WORKFLOW)
    if cfg.backup_s3:
        return S3Target(cfg.backup_s3, namespace, env)
    if cfg.backup_dir:
        return DirTarget(cfg.backup_dir, namespace)
    raise BackupError("not_configured", NOT_CONFIGURED)


# -- backup to a target -----------------------------------------------------------------

def backup_to(client, namespace: str, dest: Target, keep: int, *, now_ms: int | None = None) -> dict:
    """Write a backup to the target, then delete the oldest ones beyond `keep`."""
    now = _now_ms() if now_ms is None else now_ms
    name = f"{stamp(now)}.ndjson.gz"
    with tempfile.TemporaryFile() as spool:
        summary = write_export(client, namespace, spool, now_ms=now)
        size = spool.tell()
        dest.put(BACKUPS, name, spool)
    names = [item["name"] for item in dest.list(BACKUPS)]
    pruned = names[:-keep] if len(names) > keep else []
    for old in pruned:
        dest.delete(BACKUPS, old)
    result = {"name": name, "keys": summary["keys"], "records": summary["records"],
              "bytes": size, "pruned": pruned}
    record_status(client, namespace, "backup", {"status": "ok", "at": _iso(now), **result})
    return result


def restore_from(
    client, namespace: str, source: Target, name: str, *, confirm: bool,
    namespace_from: str | None = None,
) -> dict:
    if not confirm:
        raise BackupError(
            "confirm_required", "A restore replaces keys. Repeat it with --confirm.", 422
        )
    check_restorable(client, namespace)
    with tempfile.TemporaryFile() as spool:
        source.get(BACKUPS, check_name(BACKUPS, name), spool)
        return {"name": name, **restore(
            client, namespace, spool, confirm=True, namespace_from=namespace_from
        )}


# -- archive ----------------------------------------------------------------------------

def _event_ms(event: dict) -> int:
    instant = datetime.fromisoformat(str(event["time"]).replace("Z", "+00:00"))
    return int(instant.timestamp() * 1000)


def _ended_sessions(client, namespace: str) -> Iterator[tuple[str, dict]]:
    index = f"sym:{namespace}:sessions"
    batch: list[str] = []

    def metas(ids: list[str]) -> Iterator[tuple[str, dict]]:
        raws = client.mget([f"sym:{namespace}:session:{sid}:meta" for sid in ids])
        for sid, raw in zip(ids, raws, strict=True):
            if raw:
                meta = json.loads(raw)
                if meta.get("state") == "ended":
                    yield sid, meta

    for sid, _score in client.zscan_iter(index, count=500):
        batch.append(sid)
        if len(batch) >= BATCH:
            yield from metas(batch)
            batch = []
    if batch:
        yield from metas(batch)


def _session_lines(client, namespace: str, sid: str, meta: dict) -> Iterator[dict]:
    base = f"sym:{namespace}:session:{sid}"
    yield {"kind": "session", "sid": sid, "meta": meta}
    after = "-"
    while True:
        rows = client.xrange(f"{base}:events", min=after, count=500)
        for event_id, fields in rows:
            data = fields.get("data")
            try:
                data = json.loads(data) if data is not None else None
            except ValueError:
                pass
            yield {"kind": "event", "sid": sid, "id": event_id, "event": fields.get("kind"),
                   "data": data, "t": fields.get("t")}
        if len(rows) < 500:
            break
        after = f"({rows[-1][0]}"
    start = 0
    while True:
        raws = client.lrange(f"{base}:transcript", start, start + 499)
        for offset, raw in enumerate(raws):
            yield {"kind": "transcript", "sid": sid, "index": start + offset,
                   "entry": json.loads(raw)}
        if len(raws) < 500:
            break
        start += 500


def archive(client, namespace: str, dest: Target, *, now_ms: int | None = None) -> dict:
    """Append what Redis will drop to the archive: ended sessions not archived yet and the
    audit entries since the last run. Markers move only after the object is stored, so a
    failed run repeats its entries the next time (at least once)."""
    now = _now_ms() if now_ms is None else now_ms
    markers = {
        sid: value for sid, value in client.hgetall(_sessions_marker(namespace)).items()
    }
    watermark = client.get(_audit_marker(namespace)) or ""
    cutoff = now - ARCHIVE_LAG_MS
    sessions: dict[str, str] = {}
    seen: set[str] = set()
    newest = watermark
    lines = audit = 0
    name = f"{stamp(now)}.jsonl.gz"
    with tempfile.TemporaryFile() as spool:
        with gzip.GzipFile(fileobj=spool, mode="wb") as out:
            for sid, meta in _ended_sessions(client, namespace):
                seen.add(sid)
                ended = str(meta.get("ended_at") or meta.get("updated_at") or "")
                if markers.get(sid) == ended:
                    continue
                for doc in _session_lines(client, namespace, sid, meta):
                    out.write(_line(doc))
                    lines += 1
                sessions[sid] = ended
            events = []
            for _field, raw in client.hscan_iter(
                f"admin:{namespace}:commands", match="audit:*", count=500
            ):
                event = json.loads(raw)
                at = _event_ms(event)
                position = f"{at:013d}:{event['id']}"
                if position > watermark and at <= cutoff:
                    events.append((position, event))
            for position, event in sorted(events, key=lambda item: item[0]):
                out.write(_line({"kind": "audit", "event": event}))
                lines += 1
                audit += 1
                newest = max(newest, position)
        if lines:
            dest.put(ARCHIVE, name, spool)
    pipe = client.pipeline(transaction=False)
    if sessions:
        pipe.hset(_sessions_marker(namespace), mapping=sessions)
    gone = [sid for sid in markers if sid not in seen]
    if gone:
        pipe.hdel(_sessions_marker(namespace), *gone)
    if newest != watermark:
        pipe.set(_audit_marker(namespace), newest)
    pipe.execute()
    result = {"name": name if lines else None, "sessions": len(sessions), "audit": audit}
    record_status(client, namespace, "archive", {"status": "ok", "at": _iso(now), **result})
    return result


def read_archive(source: Target, name: str, dest: BinaryIO) -> None:
    source.get(ARCHIVE, check_name(ARCHIVE, name), dest)


# -- the nightly run --------------------------------------------------------------------

def run(client, namespace: str, cfg, env: Mapping[str, str] | None = None, *,
        now_ms: int | None = None) -> dict:
    """What the CronJob does: archive, then back up and prune. A missing target is
    recorded as `not_configured` and raised."""
    now = _now_ms() if now_ms is None else now_ms
    try:
        dest = target(cfg, namespace, env)
    except BackupError as exc:
        record_status(client, namespace, "backup", {
            "status": "not_configured", "at": _iso(now), "reason": exc.message,
        })
        raise
    try:
        archived = archive(client, namespace, dest, now_ms=now)
        backed = backup_to(client, namespace, dest, cfg.backup_keep, now_ms=now)
    except BackupError as exc:
        record_status(client, namespace, "backup", {
            "status": "failed", "at": _iso(now), "reason": exc.message,
        })
        raise
    return {"target": dest.describe(), "archive": archived, "backup": backed}


def status(client, namespace: str, cfg, env: Mapping[str, str] | None = None) -> dict:
    """The target (never a credential), the retention, and the last outcome of each run."""
    try:
        where, reason = target(cfg, namespace, env).describe(), None
    except BackupError as exc:
        where, reason = None, exc.message
    last = {
        kind: json.loads(raw) for kind, raw in client.hgetall(status_key(namespace)).items()
    }
    return {
        "namespace": namespace,
        "target": where,
        "reason": reason,
        "keep": cfg.backup_keep if cfg is not None else None,
        "retention": retention.read(client, namespace),
        "last": last,
    }
