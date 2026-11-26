"""The merger's state in Redis: records, leases, intents, and the reads the control API serves.

One merger Deployment serves one repository, and several can share a namespace (Symphony
tickets span repositories, one PR each). Everything a merger keeps is therefore scoped by its
repository: `<repo>` below is `owner/name` lower-cased (`normalize_repo`).

Keys, every value JSON:

- `sym:<ns>:merger:repos`             set of every repository a merger has served (SADD on start)
- `sym:<ns>:merger:<repo>:heartbeat`  the merger of `<repo>` is alive (TTL, renewed every ≤ 30 s)

and, with the prefix `sym:<ns>:merger:<repo>:`:

- `rec`        hash pr → train record (`publishing|reviewed|train|resolve|finalizing`), with the
               crash-safe intents `publication.done`, `pushing` and `outcome.done`
- `parked`     hash pr → a review whose head CI hadn't finished on
- `skips`      hash pr → why stage 1 stopped on a PR, and when it is worth another try
- `baseline`   hash pr → the last review of a PR (head, fork point, findings, rounds, outcome)
- `run`        hash pr → a finished review whose outcome was not applied yet (crash resume)
- `caps`       hash pr → the largest batch the PR may join after a red batch
- `batch`      the batch the train is merging on one CI run
- `probe`      hash role → the last tool check of that role
- `reviewing`  hash pr → the review in progress (worker, phase); `reviewing:<pr>:sessions` list
- `queue`      the review queue as the last pick saw it
- `mainci`     the main CI watch's state
- `actions`    stream of operator actions (`bypass`, `requeue`, `release`) the merger applies;
               `actions:cursor` the last one applied
- `seq:<pr>:<role>`  session numbering
- `lease:<n>`  leases with owner and expiry (`train`, `pr:<n>`, `fixslot:<k>`, `actions`)

Merger agent sessions are `merger:<owner>:<name>:<pr>:<role>:<n>` (lower-cased), so the same PR
number in two repositories never shares a session. A merger waits on the wake target
`merger:<repo>` (`wake_target`). Team-config consumers are `merger:review:<repo>` and
`merger:train:<repo>`.

Reads for the API: `registered_repos(client, ns)`, then per repository `MergerStore(client, ns,
repo)` with `.alive()`, `.snapshot()`, `.counts()` and `.request_action(…)`.

Leases replace the reference's lock directories: `SET NX PX` to take, compare-and-extend to
renew, compare-and-delete to release, so a crashed holder frees its lease when it expires.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

# Who holds a lease: this process, unique across pods and restarts (a worker id alone is not:
# two processes would each take the other's claims for their own).
INSTANCE = f"{socket.gethostname()}:{os.getpid()}"
CLAIM_TTL_MS = 120_000
HEARTBEAT_TTL_MS = 90_000

ACTIONS = ("bypass", "requeue", "release")
REC_STATES = ("publishing", "reviewed", "train", "resolve", "finalizing")
_REPO_PART = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")

_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class LeaseLost(Exception):
    """The lease expired or was taken over: the holder must stop before its next effect."""


class StateError(Exception):
    """A malformed stored value: fail closed, naming the key, for a person to fix."""


def normalize_repo(repo: str) -> str:
    """`owner/name` lower-cased; ValueError for anything else."""
    value = (repo or "").strip().lower()
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name or not _REPO_PART.fullmatch(owner) or not _REPO_PART.fullmatch(name):
        raise ValueError(f"not a repository (owner/name): {repo!r}")
    return value


def wake_target(repo: str) -> str:
    """The wake list one repository's merger waits on (`wake.wait(client, ns, target, …)`)."""
    return f"merger:{normalize_repo(repo)}"


def registered_repos(client, namespace: str) -> list[str]:
    """Every repository a merger has served in the namespace, sorted."""
    return sorted(client.smembers(f"sym:{namespace}:merger:repos"))


class MergerStore:
    def __init__(self, client, namespace: str, repo: str, *, now_ms: Callable[[], int] = _now_ms) -> None:
        self.client = client
        self.ns = namespace
        self.repo = normalize_repo(repo)
        self.prefix = f"sym:{namespace}:merger:{self.repo}:"
        self._now = now_ms
        self._held: dict[str, tuple[str, int]] = {}
        self._held_lock = threading.Lock()
        self._beat: threading.Thread | None = None

    # The effective label filter (`{"active", "labels"}`), set by the merger's fleet. Kept in
    # Redis: the API builds its own store to serve `GET /v1/merger`.
    @property
    def label_filter(self) -> dict | None:
        key = self._k("label_filter")
        return self._load(key, None, self.client.get(key))

    @label_filter.setter
    def label_filter(self, value: dict | None) -> None:
        if value is None:
            self.client.delete(self._k("label_filter"))
        else:
            self.client.set(self._k("label_filter"), json.dumps(value, separators=(",", ":")))

    def _k(self, name: str) -> str:
        return self.prefix + name

    # -- registry and liveness -----------------------------------------------
    def register(self, ttl_ms: int = HEARTBEAT_TTL_MS) -> None:
        """The merger of this repository starts: listed for the API, and alive."""
        self.client.sadd(f"sym:{self.ns}:merger:repos", self.repo)
        self.heartbeat(ttl_ms)

    def heartbeat(self, ttl_ms: int = HEARTBEAT_TTL_MS) -> None:
        self.client.set(self._k("heartbeat"), str(self._now()), px=ttl_ms)

    def alive(self) -> bool:
        return bool(self.client.exists(self._k("heartbeat")))

    # -- JSON helpers --------------------------------------------------------
    def _load(self, key: str, field: str | None, raw) -> Any:
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            where = f"{key}[{field}]" if field else key
            raise StateError(f"{where} is malformed ({exc}): fix or remove it") from exc

    def _hget(self, name: str, pr: int) -> dict | None:
        key = self._k(name)
        return self._load(key, str(pr), self.client.hget(key, str(pr)))

    def _hall(self, name: str) -> dict[int, dict]:
        key = self._k(name)
        return {int(k): self._load(key, k, v) for k, v in self.client.hgetall(key).items()}

    def _hput(self, name: str, pr: int, value: dict) -> None:
        self.client.hset(self._k(name), str(pr), json.dumps(value, separators=(",", ":")))

    def _hdel(self, name: str, pr: int) -> None:
        self.client.hdel(self._k(name), str(pr))

    # -- train records ---------------------------------------------------------
    def rec(self, pr: int) -> dict | None:
        return self._hget("rec", pr)

    def recs(self) -> dict[int, dict]:
        return self._hall("rec")

    def put_rec(self, rec: dict) -> None:
        if rec.get("state") not in REC_STATES or not isinstance(rec.get("pr"), int):
            raise StateError(f"refusing to write a malformed train record: {str(rec)[:300]}")
        self._hput("rec", rec["pr"], rec)

    def drop_rec(self, pr: int) -> None:
        """A PR leaving the train: its cap goes with its record."""
        pipe = self.client.pipeline()
        pipe.hdel(self._k("rec"), str(pr))
        pipe.hdel(self._k("caps"), str(pr))
        pipe.execute()

    # -- parked, skips, baselines, saved runs ---------------------------------
    def parked(self, pr: int) -> dict | None:
        return self._hget("parked", pr)

    def all_parked(self) -> dict[int, dict]:
        return self._hall("parked")

    def park(self, rec: dict) -> None:
        self._hput("parked", rec["pr"], rec)

    def unpark(self, pr: int) -> None:
        self._hdel("parked", pr)

    def skips(self) -> dict[int, dict]:
        return self._hall("skips")

    def put_skip(self, pr: int, skip: dict) -> None:
        self._hput("skips", pr, skip)

    def drop_skip(self, pr: int) -> None:
        self._hdel("skips", pr)

    def baseline(self, pr: int) -> dict | None:
        return self._hget("baseline", pr)

    def put_baseline(self, pr: int, baseline: dict) -> None:
        self._hput("baseline", pr, baseline)

    def drop_baseline(self, pr: int) -> None:
        self._hdel("baseline", pr)

    def baseline_prs(self) -> set[int]:
        return {int(k) for k in self.client.hkeys(self._k("baseline"))}

    def saved_run(self, pr: int) -> dict | None:
        return self._hget("run", pr)

    def save_run(self, pr: int, saved: dict) -> None:
        self._hput("run", pr, saved)

    def drop_run(self, pr: int) -> None:
        self._hdel("run", pr)

    def polish_rounds(self, pr: int) -> int:
        raw = self.client.hget(self._k("polish"), str(pr))
        return int(raw) if raw is not None else 0

    def incr_polish(self, pr: int) -> int:
        return self.client.hincrby(self._k("polish"), str(pr), 1)

    # -- caps and batch ----------------------------------------------------------
    def caps(self) -> dict[int, int]:
        return {int(k): int(v) for k, v in self.client.hgetall(self._k("caps")).items()}

    def set_caps(self, caps: dict[int, int]) -> None:
        if caps:
            self.client.hset(self._k("caps"), mapping={str(k): int(v) for k, v in caps.items()})

    def drop_cap(self, pr: int) -> None:
        self.client.hdel(self._k("caps"), str(pr))

    def batch(self) -> dict | None:
        key = self._k("batch")
        return self._load(key, None, self.client.get(key))

    def save_batch(self, batch: dict | None) -> None:
        if batch is None:
            self.client.delete(self._k("batch"))
        else:
            self.client.set(self._k("batch"), json.dumps(batch, separators=(",", ":")))

    # -- probe cache ---------------------------------------------------------------
    def probe(self, role: str) -> dict | None:
        key = self._k("probe")
        return self._load(key, role, self.client.hget(key, role))

    def put_probe(self, role: str, result: dict) -> None:
        self.client.hset(self._k("probe"), role, json.dumps(result))

    # -- reviews in progress and the queue -----------------------------------------
    def set_reviewing(self, pr: int, **fields: Any) -> None:
        """Updated only by the worker holding the PR's claim."""
        doc = self._hget("reviewing", pr) or {"pr": pr, "started_at": self._now()}
        doc.update(fields)
        self._hput("reviewing", pr, doc)

    def add_session(self, pr: int, sid: str) -> None:
        """Records an agent session on the review of `pr`; parallel agents append at once."""
        self.client.rpush(self._k(f"reviewing:{pr}:sessions"), sid)

    def clear_reviewing(self, pr: int) -> None:
        pipe = self.client.pipeline()
        pipe.hdel(self._k("reviewing"), str(pr))
        pipe.delete(self._k(f"reviewing:{pr}:sessions"))
        pipe.execute()

    def reviewing(self) -> dict[int, dict]:
        docs = self._hall("reviewing")
        for pr, doc in docs.items():
            doc["sessions"] = self.client.lrange(self._k(f"reviewing:{pr}:sessions"), 0, -1)
        return docs

    def set_queue(self, items: list[dict]) -> None:
        self.client.set(self._k("queue"), json.dumps(items, separators=(",", ":")))

    def queue(self) -> list[dict]:
        key = self._k("queue")
        return self._load(key, None, self.client.get(key)) or []

    def mainci(self) -> dict | None:
        key = self._k("mainci")
        return self._load(key, None, self.client.get(key))

    def save_mainci(self, doc: dict) -> None:
        self.client.set(self._k("mainci"), json.dumps(doc))

    # -- session numbering -----------------------------------------------------------
    def next_session_id(self, pr: int, role: str) -> str:
        n = self.client.incr(self._k(f"seq:{pr}:{role}"))
        owner, name = self.repo.split("/", 1)
        return f"merger:{owner}:{name}:{pr}:{role}:{n}"

    # -- leases --------------------------------------------------------------------
    def acquire(self, name: str, owner: str, ttl_ms: int) -> bool:
        return bool(self.client.set(self._k(f"lease:{name}"), owner, nx=True, px=ttl_ms))

    def renew(self, name: str, owner: str, ttl_ms: int) -> bool:
        return bool(self.client.eval(_RENEW, 1, self._k(f"lease:{name}"), owner, ttl_ms))

    def release(self, name: str, owner: str) -> bool:
        return bool(self.client.eval(_RELEASE, 1, self._k(f"lease:{name}"), owner))

    def holder(self, name: str) -> str | None:
        return self.client.get(self._k(f"lease:{name}"))

    def force_release(self, name: str) -> bool:
        return bool(self.client.delete(self._k(f"lease:{name}")))

    def claim(self, pr: int, owner: str, ttl_ms: int = CLAIM_TTL_MS) -> bool:
        """A PR's claim: one review, resolution or train step holds it at a time. True when
        taken (or already ours). Held claims are renewed by a heartbeat, so a process that dies
        frees its PRs within `ttl_ms` instead of blocking them for hours."""
        name = f"pr:{pr}"
        taken = self.acquire(name, owner, ttl_ms) or self.holder(name) == owner
        if taken:
            with self._held_lock:
                self._held[name] = (owner, ttl_ms)
                if self._beat is None:
                    self._beat = threading.Thread(target=self._renew_claims, daemon=True)
                    self._beat.start()
        return taken

    def unclaim(self, pr: int, owner: str) -> None:
        name = f"pr:{pr}"
        with self._held_lock:
            if self._held.get(name, (None,))[0] == owner:
                del self._held[name]
        self.release(name, owner)

    def _renew_claims(self) -> None:
        while True:
            with self._held_lock:
                held = dict(self._held)
            pause = min((ttl for _, ttl in held.values()), default=CLAIM_TTL_MS) / 4000
            time.sleep(pause)
            for name, (owner, ttl) in held.items():
                try:
                    if not self.renew(name, owner, ttl):
                        with self._held_lock:  # lost (expired, or released by an operator)
                            if self._held.get(name, (None,))[0] == owner:
                                del self._held[name]
                except Exception:  # noqa: BLE001 — Redis blip: the next beat retries
                    pass

    def lease(self, name: str, owner: str, ttl_ms: int = 60_000) -> Lease:
        return Lease(self, name, owner, ttl_ms)

    # -- operator actions ------------------------------------------------------------
    def request_action(self, pr: int, action: str, *, actor: str, reason: str = "") -> dict:
        """A durable request the merger applies at its next pass (the API then wakes it)."""
        if action not in ACTIONS:
            raise ValueError(f"unknown merger action {action!r}: one of {', '.join(ACTIONS)}")
        if not isinstance(pr, int) or pr < 1:
            raise ValueError("pr must be a positive integer")
        at = self._now()
        doc = {"pr": pr, "action": action, "actor": actor, "reason": reason, "at": at}
        aid = self.client.xadd(
            self._k("actions"), {"doc": json.dumps(doc)}, maxlen=1000, approximate=True
        )
        return {"id": aid, **doc}

    def pending_actions(self) -> list[dict]:
        cursor = self.client.get(self._k("actions:cursor")) or "0-0"
        rows = self.client.xrange(self._k("actions"), min=f"({cursor}")
        return [{"id": aid, **json.loads(f["doc"])} for aid, f in rows]

    def ack_action(self, aid: str) -> None:
        self.client.set(self._k("actions:cursor"), aid)

    # -- reads for the control API ----------------------------------------------------
    def snapshot(self) -> dict:
        """One repository's entry of `GET /v1/merger`: what its merger is doing, from Redis only."""
        ttl = self.client.pttl(self._k("lease:train"))
        owner = self.holder("train")
        records = sorted(self.recs().values(), key=lambda r: r.get("reviewed_at", 0))
        return {
            "repo": self.repo,
            "alive": self.alive(),
            "reviewing": sorted(
                (
                    {
                        "pr": r["pr"],
                        "head_sha": r.get("head_sha", ""),
                        "worker": r.get("worker", ""),
                        "phase": r.get("phase", ""),
                        "started_at": r.get("started_at", 0),
                        "sessions": r.get("sessions", []),
                    }
                    for r in self.reviewing().values()
                ),
                key=lambda r: r["started_at"],
            ),
            # `title` is the PR's (an older record has none: the clients show `PR #n`); a
            # clean review says what it waits for, `ci` or `approval`.
            "parked": sorted(
                (
                    {
                        "pr": p["pr"],
                        "head_sha": p["head_sha"],
                        "kind": p["kind"],
                        "parked_at": p["parked_at"],
                        "comment": p.get("comment", ""),
                        **({"title": p["pr_title"]} if p.get("pr_title") else {}),
                        **({"waiting": p.get("waiting", "ci")} if p["kind"] == "clean" else {}),
                    }
                    for p in self.all_parked().values()
                ),
                key=lambda p: p["parked_at"],
            ),
            "train": {
                "lease": {"owner": owner, "expires_in_ms": max(ttl, 0)} if owner else None,
                "records": [
                    {
                        k: r[k]
                        for k in (
                            "pr", "state", "head_sha", "branch", "author", "reviewed_at",
                            "pushing", "conflict", "outcome", "title",
                        )
                        if k in r
                    }
                    for r in records
                ],
            },
            "batch": self.batch() or {},
            "queue": self.queue(),
            **({"label_filter": lf} if (lf := self.label_filter) is not None else {}),
        }

    def counts(self) -> dict[str, int]:
        """The overview's `merger` block."""
        in_train = sum(
            1 for r in self.recs().values() if r.get("state") in ("train", "finalizing", "resolve")
        )
        return {
            "reviewing": self.client.hlen(self._k("reviewing")),
            "train": in_train,
            "parked": self.client.hlen(self._k("parked")),
        }


class Lease:
    """A held lease with a heartbeat thread. `check()` raises `LeaseLost` once a renewal
    failed: the holder calls it before each irreversible effect (fencing)."""

    def __init__(self, store: MergerStore, name: str, owner: str, ttl_ms: int) -> None:
        self.store = store
        self.name = name
        self.owner = owner
        self.ttl_ms = ttl_ms
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self.held = False

    def __enter__(self) -> Lease:
        self.held = self.store.acquire(self.name, self.owner, self.ttl_ms)
        if self.held:
            self._thread = threading.Thread(target=self._beat, daemon=True)
            self._thread.start()
        return self

    def _beat(self) -> None:
        while not self._stop.wait(self.ttl_ms / 3000):
            try:
                if not self.store.renew(self.name, self.owner, self.ttl_ms):
                    self._lost.set()
                    return
            except Exception:  # noqa: BLE001 — a renewal that cannot be confirmed is lost
                self._lost.set()
                return

    def check(self) -> None:
        if not self.held or self._lost.is_set():
            raise LeaseLost(f"lease {self.name} is no longer held by {self.owner}")

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self.held:
            self.store.release(self.name, self.owner)
