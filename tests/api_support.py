"""Shared wiring for the control API tests: real Redis, real stores, fakes only at the edges."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import redis
import redis.asyncio
import uvicorn
from fastapi.testclient import TestClient

from symphony import state
from symphony.admin_commands import AdminStore
from symphony.admin_read import AdminProjection, LiveReader
from symphony.api import Services, build_app
from symphony.api.catalog import Catalog
from symphony.apitokens import TokenStore
from symphony.kv import RedisKV
from symphony.sessions import SessionStore
from symphony.setup_secrets import check_names
from symphony.teamconfig import TeamConfig

NS = "test"
LINEAR_SECRET = "linear-test-secret"
GITHUB_SECRET = "github-test-secret"


class FakeRepoMerger:
    """One repository's merger, with the read/action surface of `merger.store.MergerStore`."""

    def __init__(self, fleet: FakeMerger, repo: str) -> None:
        self.fleet = fleet
        self.repo = repo

    def alive(self) -> bool:
        return self.repo not in self.fleet.dead

    def snapshot(self) -> dict:
        return {
            "repo": self.repo,
            "alive": self.alive(),
            "reviewing": [{"pr": 7, "head_sha": "abc", "worker": "w1", "phase": "oracle",
                           "started_at": 1, "sessions": [f"merger:{self.repo.replace('/', ':')}:7:review:1"]}],
            "parked": [],
            "train": {"lease": None, "records": []},
            "batch": {},
            "queue": [{"pr": 9, "title": "Fix", "author": "bob", "ready_at": 2, "kind": "first"}],
        }

    def counts(self) -> dict:
        return {"reviewing": 1, "train": 0, "parked": 2}

    def request_action(self, pr, action, *, actor, reason):
        if action not in ("bypass", "requeue", "release") or pr < 1:
            raise ValueError("unknown merger action")
        if pr == 404:
            raise ValueError("PR 404 is not known to the merger")
        record = {"id": f"a{len(self.fleet.actions)}", "pr": pr, "action": action,
                  "actor": actor, "reason": reason, "at": 1}
        self.fleet.actions.append((self.repo, pr, action, actor, reason))
        return record


class FakeMerger:
    """The merger fleet of a namespace (`api.control.MergerFleet`): one merger per repository.

    `FakeMerger()` serves `acme/app`; `FakeMerger(["acme/app", "acme/lib"])` two repositories;
    `dead={"acme/lib"}` makes those report `alive: false`. `actions` records
    `(repo, pr, action, actor, reason)`."""

    def __init__(self, repos=("acme/app",), *, dead=()) -> None:
        self._repos = sorted(repos)
        self.dead = set(dead)
        self.actions: list[tuple] = []

    def repos(self) -> list[str]:
        return list(self._repos)

    def store(self, repo: str) -> FakeRepoMerger:
        return FakeRepoMerger(self, repo)


class FakeSecretStore:
    """A `setup_secrets.SecretStore` in memory: `puts` records what was stored, `has()` reads
    it back, nothing restarts. Values never leave the test."""

    def __init__(self, mode: str = "kubernetes", host: dict[str, set[str]] | None = None) -> None:
        self.mode = mode
        self.values: dict[str, dict[str, str]] = {}
        self.host = host or {}
        self.restarts: list[list[str]] = []

    def put(self, group, values, *, restart=True):
        check_names(group, values)
        self.values.setdefault(group, {}).update(values)
        return self.restart([group]) if restart else []

    def has(self, group, name):
        return name in self.values.get(group, {}) or name in self.host.get(group, set())

    def remove(self, group, names, *, restart=True):
        names = list(names)
        check_names(group, names)
        for name in names:
            self.values.get(group, {}).pop(name, None)
        return self.restart([group]) if restart else []

    def restart(self, groups):
        self.restarts.append(list(groups))
        return []


def make_services(client, *, merger=None, catalog_path=None, workflow=None, providers=None,
                  clock=None, ping_interval_s=15.0, secrets=None) -> Services:
    kv = RedisKV(client)
    tokens = TokenStore(client, NS, **({"now_ms": clock} if clock else {}))
    sessions = SessionStore(client, NS)
    teamconfig = TeamConfig(client, NS)
    store = AdminStore(client, NS)
    path = client.connection_pool.connection_kwargs["path"]
    aclient = redis.asyncio.Redis(unix_socket_path=path, decode_responses=True, socket_timeout=60)
    return Services(
        client=client,
        aclient=aclient,
        namespace=NS,
        sessions=sessions,
        tokens=tokens,
        teamconfig=teamconfig,
        store=store,
        kv=kv,
        reader=LiveReader(client, sessions=sessions, teamconfig=teamconfig, store=store,
                          merger=merger),
        projection=AdminProjection(kv, store, None, None, NS, workflow),
        catalog=Catalog(catalog_path or "/nonexistent/models.json"),
        workflow=workflow,
        providers=providers,
        merger=merger,
        secrets=secrets,
        linear_secret=LINEAR_SECRET,
        github_secret=GITHUB_SECRET,
        ping_interval_s=ping_interval_s,
    )


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def tokens_for(services: Services) -> dict[str, str]:
    """One token per role, named after the role (`viewer`, `operator`, `admin`)."""
    return {
        role: services.tokens.create(f"{role}-person", role, actor="test")[0]
        for role in ("viewer", "operator", "admin")
    }


def client_for(services: Services, **kwargs) -> TestClient:
    return TestClient(build_app(services, **kwargs), raise_server_exceptions=False)


def fresh_daemon(client) -> None:
    kv = RedisKV(client)
    now = time.time_ns() // 1_000_000
    assert state.renew_lease(kv, "daemon", now)
    state.heartbeat(kv, now)


class Server:
    """The app on a real uvicorn server (random port), for streaming tests."""

    def __init__(self, app) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.url = f"http://127.0.0.1:{self.port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)


__all__ = ["redis"]
