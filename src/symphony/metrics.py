"""Prometheus metrics: `/metrics` on the health port (`SYMPHONY_HEALTH_PORT`, 8081).

The daemon, the merger, the assistant and the gateway serve `/healthz` and `/metrics` on that
port (`serve`). The public API port never serves metrics. Each component exposes the part it
owns, so no series is exposed twice:

- every component: `symphony_build_info`;
- the daemon (the lease holder only): tickets, attempts, retries, sessions, rate limits, token
  usage, setup and the merger of each repository (`daemon_families`);
- the gateway: MCP calls, MCP cleanup and held MCP leases (`gateway_families`);
- the assistant: its threads and transports, from process state (`assistant_families`).

The text is the Prometheus text format 0.0.4 (`render`; spec in
`scripts/mocks/specs/prometheus-exposition-formats.md`). A scrape is cached for `CACHE_S`, and
its Redis work is bounded: one SCAN pass of at most `SCAN_PAGES` pages and two pipelines.

Counters live in the hash `sym:<ns>:metrics:counters` (field `<name>|<label>|…`, written with
HINCRBY by `count_*`), except the retries, which are a field of the monthly `metrics:<yyyymm>`
document. Label values are bounded: known sets map anything else to `other`, and free label
values keep at most `LABEL_LIMIT` values per label (sorted), the rest becoming `other`. No label
holds a ticket id or a session id. No metric holds a credential.
"""

from __future__ import annotations

import datetime as dt
import http.server
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata

from . import state

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
CACHE_S = 10.0
SCAN_COUNT = 1000
SCAN_PAGES = 50
SESSION_LIMIT = 500
REPO_LIMIT = 50
HELD_LIMIT = 500
LABEL_LIMIT = 20
MODEL_LIMIT = 8
LABEL_CHARS = 100
OTHER = "other"

TICKET_STATES = ("claimed", "running", "interrupting", "retry_queued", "blocked", "parked")
OUTCOMES = ("done", "continuation", "failed", "blocked", "fenced", "error")
RETRY_REASONS = ("failure", "continuation")
ATTEMPT_KINDS = ("implement", "answer")
TOKEN_TYPES = ("input", "output", "cache_read", "cache_write")
SETUP_STEPS = ("tracker", "github_app", "repositories", "models", "notifications", "assistant",
               "webhooks", "skills")
SETUP_STATUSES = ("done", "todo", "error", "skipped")
CLEANUP_RESULTS = ("released", "failed")
TRANSPORTS = ("slack", "discord")

# Counter names in the counters hash.
ATTEMPTS_FINISHED = "attempts_finished"
RATE_LIMIT_EVENTS = "rate_limit_events"
TOKENS = "tokens"
COST = "cost"
MCP_CLEANUP = "mcp_cleanup"


# -- exposition ---------------------------------------------------------------------------
@dataclass
class Family:
    """One metric family: a name, a type (`counter` or `gauge`), a help text and samples."""

    name: str
    type: str
    help: str
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)

    def add(self, value: float, **labels: str) -> Family:
        self.samples.append((labels, value))
        return self


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _value(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def render(families: Iterable[Family]) -> str:
    """The Prometheus text format: per family `# HELP`, `# TYPE`, then its samples, sorted."""
    lines: list[str] = []
    for fam in families:
        lines.append(f"# HELP {fam.name} {_escape_help(fam.help)}")
        lines.append(f"# TYPE {fam.name} {fam.type}")
        for labels, value in sorted(fam.samples, key=lambda s: sorted(s[0].items())):
            if labels:
                pairs = ",".join(f'{k}="{_escape_label(str(v))}"' for k, v in sorted(labels.items()))
                lines.append(f"{fam.name}{{{pairs}}} {_value(value)}")
            else:
                lines.append(f"{fam.name} {_value(value)}")
    return "\n".join(lines) + "\n"


# -- label bounds -----------------------------------------------------------------------------
def _known(value, allowed: tuple[str, ...]) -> str:
    return value if value in allowed else OTHER


def _clean(value) -> str:
    text = str(value) if value is not None else ""
    text = "".join(ch for ch in text if ch.isprintable())[:LABEL_CHARS]
    return text or OTHER


def _capped(values: Iterable[str], limit: int = LABEL_LIMIT) -> Callable[[str], str]:
    """Keeps the first `limit` distinct values (sorted); the others map to `other`."""
    keep = set(sorted(set(values))[:limit])
    return lambda value: value if value in keep else OTHER


def _add_to(totals: dict, key: tuple, value: float) -> None:
    totals[key] = totals.get(key, 0) + value


# -- counter writers ----------------------------------------------------------------------------
def counters_key(namespace: str) -> str:
    return f"sym:{namespace}:metrics:counters"


def _field(name: str, *labels) -> str:
    return "|".join((name, *(_clean(label).replace("|", "/") for label in labels)))


def count_attempt_finished(client, namespace: str, outcome: str) -> None:
    """An implementation or answer attempt ended with `outcome` (`OUTCOMES`)."""
    client.hincrby(counters_key(namespace), _field(ATTEMPTS_FINISHED, _known(outcome, OUTCOMES)), 1)


def count_rate_limit(client, namespace: str, provider: str, stage: str) -> None:
    """One provider rate-limit observation (internals §10)."""
    client.hincrby(counters_key(namespace), _field(RATE_LIMIT_EVENTS, provider, stage), 1)


def count_tokens(client, namespace: str, model: str, usage: Mapping) -> None:
    """Adds a usage delta (`state.TOKEN_FIELDS`) to the counters of `model` (`provider/model`)."""
    key = counters_key(namespace)
    pipe = client.pipeline(transaction=False)
    for kind in TOKEN_TYPES:
        if usage.get(kind):
            pipe.hincrby(key, _field(TOKENS, model, kind), int(usage[kind]))
    if usage.get("cost_estimate"):
        pipe.hincrbyfloat(key, _field(COST, model), float(usage["cost_estimate"]))
    pipe.execute()


def count_mcp_cleanup(client, namespace: str, released: int, failed: int) -> None:
    """The ids that one MCP cleanup released, and those whose release failed."""
    pipe = client.pipeline(transaction=False)
    for result, by in (("released", released), ("failed", failed)):
        if by:
            pipe.hincrby(counters_key(namespace), _field(MCP_CLEANUP, result), by)
    pipe.execute()


def setup_status_key(namespace: str) -> str:
    return f"sym:{namespace}:setup:status"


def record_setup(client, namespace: str, status: Mapping, now_ms: int) -> None:
    """The last setup status the API computed: `{complete, steps: {id: status}, at}`."""
    client.set(setup_status_key(namespace), json.dumps({
        "complete": bool(status.get("complete")),
        "steps": {step["id"]: step["status"] for step in status.get("steps", [])},
        "at": now_ms,
    }))


def _counters(raw: Mapping[str, str]) -> dict[str, list[tuple[list[str], float]]]:
    out: dict[str, list[tuple[list[str], float]]] = {}
    for key, value in raw.items():
        name, *labels = key.split("|")
        try:
            number = float(value)
        except ValueError:
            continue
        out.setdefault(name, []).append((labels, number))
    return out


# -- collectors ---------------------------------------------------------------------------------
def _version() -> str:
    try:
        return metadata.version("symphony")
    except metadata.PackageNotFoundError:
        return "unknown"


def build_info(component: str) -> Family:
    return Family("symphony_build_info", "gauge",
                  "Always 1. The labels give the component, the version and the commit.").add(
        1, component=component, version=_version(),
        commit=_clean(os.environ.get("SYMPHONY_COMMIT") or "unknown"))


def _json(raw):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _scan_state_keys(client) -> tuple[list[str], list[str], bool]:
    """`issue:*` and `hold:*` keys in one SCAN pass of at most `SCAN_PAGES` pages."""
    issues: list[str] = []
    holds: list[str] = []
    cursor = 0
    for _ in range(SCAN_PAGES):
        cursor, keys = client.scan(cursor=cursor, count=SCAN_COUNT)
        for key in keys:
            if key.startswith(state.ISSUE_PREFIX):
                issues.append(key)
            elif key.startswith(state.HOLD_PREFIX):
                holds.append(key)
        if int(cursor) == 0:
            return sorted(set(issues)), sorted(set(holds)), False
    return sorted(set(issues)), sorted(set(holds)), True


def _yyyymm(now_ms: int) -> str:
    day = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC)
    return f"{day.year:04d}{day.month:02d}"


def daemon_families(client, namespace: str, owner: str, now_ms: int | None = None) -> list[Family]:
    """The store-wide metrics. Only the daemon that holds the lease exposes them, so a rolling
    update never exposes them twice."""
    from .sessions import KINDS, STATES

    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    ns = namespace
    leader = Family("symphony_daemon_leader", "gauge",
                    "1 when this daemon holds the lease and exposes the store metrics.")
    pipe = client.pipeline(transaction=False)
    pipe.get(state.LOCK_KEY)
    pipe.get(state.HEARTBEAT_KEY)
    pipe.get(state.SETUP_KEY)
    pipe.get(setup_status_key(ns))
    pipe.hgetall(counters_key(ns))
    pipe.get(state.metrics_key(_yyyymm(now_ms)))
    pipe.zrevrange(f"sym:{ns}:sessions", 0, SESSION_LIMIT - 1)
    pipe.smembers(f"sym:{ns}:merger:repos")
    lock, heartbeat, daemon_setup, setup, counters, monthly, sids, repos = pipe.execute()
    lock = _json(lock)
    if not isinstance(lock, dict) or lock.get("owner") != owner:
        return [leader.add(0)]
    leader.add(1)

    issue_keys, hold_keys, truncated = _scan_state_keys(client)
    repos = sorted(repos or ())[:REPO_LIMIT]
    pipe = client.pipeline(transaction=False)
    for repo in repos:
        prefix = f"sym:{ns}:merger:{repo}:"
        pipe.get(prefix + "queue")
        pipe.hlen(prefix + "reviewing")
        pipe.hvals(prefix + "rec")
        pipe.hlen(prefix + "parked")
        pipe.exists(prefix + "heartbeat")
    if issue_keys:
        pipe.mget(issue_keys)
    if sids:
        pipe.mget([f"sym:{ns}:session:{sid}:meta" for sid in sids])
    results = pipe.execute() if repos or issue_keys or sids else []
    merger_rows = [results[i * 5:(i + 1) * 5] for i in range(len(repos))]
    rest = iter(results[len(repos) * 5:])
    issues = [_json(raw) for raw in next(rest)] if issue_keys else []
    metas = [_json(raw) for raw in next(rest)] if sids else []

    families = [leader]
    held = {key[len(state.HOLD_PREFIX):] for key in hold_keys}
    tickets = Family("symphony_tickets", "gauge", "Tickets that the daemon tracks, by state.")
    attempts = Family("symphony_attempts", "gauge",
                      "Attempts by state: running, queued (claimed or waiting for a retry), held.")
    by_state: dict[tuple, float] = {(s,): 0 for s in TICKET_STATES}
    phases = {"running": 0, "queued": 0, "held": len(held)}
    for key, entry in zip(issue_keys, issues, strict=True):
        if not isinstance(entry, dict):
            continue
        status = _known(entry.get("status"), TICKET_STATES)
        _add_to(by_state, (status,), 1)
        if key[len(state.ISSUE_PREFIX):] in held:
            continue
        if status in ("running", "interrupting"):
            phases["running"] += 1
        elif status in ("claimed", "retry_queued"):
            phases["queued"] += 1
    for (status,), value in by_state.items():
        tickets.add(value, state=status)
    for phase, value in phases.items():
        attempts.add(value, state=phase)
    families += [tickets, attempts, Family(
        "symphony_metrics_truncated", "gauge",
        "1 when the ticket scan stopped at its page limit, so ticket counts are partial.",
    ).add(int(truncated))]

    parsed = _counters(counters or {})
    finished = Family("symphony_attempts_finished_total", "counter",
                      "Implementation and answer attempts that ended, by outcome.")
    totals: dict[tuple, float] = {(o,): 0 for o in OUTCOMES}
    for labels, value in parsed.get(ATTEMPTS_FINISHED, []):
        _add_to(totals, (_known(labels[0] if labels else None, OUTCOMES),), value)
    for (outcome,), value in totals.items():
        finished.add(value, outcome=outcome)
    families.append(finished)

    monthly = _json(monthly)
    retries_doc = monthly.get("retries") if isinstance(monthly, dict) else None
    retries = Family("symphony_retries_total", "counter",
                     "Redispatches of a ticket in the current UTC month, by reason: failure "
                     "(after a failed attempt) or continuation (after a clean exit).")
    for reason in RETRY_REASONS:
        value = retries_doc.get(reason, 0) if isinstance(retries_doc, dict) else 0
        retries.add(value if isinstance(value, (int, float)) else 0, reason=reason)
    families.append(retries)

    sessions = Family("symphony_sessions", "gauge",
                      f"Sessions that have not ended, by kind and state (the {SESSION_LIMIT} "
                      "most recently updated sessions).")
    live: dict[tuple, float] = {}
    for meta in metas:
        if not isinstance(meta, dict) or meta.get("state") == "ended":
            continue
        _add_to(live, (_known(meta.get("kind"), KINDS), _known(meta.get("state"), STATES)), 1)
    for (kind, st), value in sorted(live.items()):
        sessions.add(value, kind=kind, state=st)
    families.append(sessions)

    events = parsed.get(RATE_LIMIT_EVENTS, [])
    providers = _capped(labels[0] for labels, _ in events if len(labels) == 2)
    stages = _capped(labels[1] for labels, _ in events if len(labels) == 2)
    limited: dict[tuple, float] = {}
    for labels, value in events:
        if len(labels) == 2:
            _add_to(limited, (providers(labels[0]), stages(labels[1])), value)
    limits = Family("symphony_rate_limit_events_total", "counter",
                    "Provider rate-limit observations of the runner and the merger agents, by "
                    "provider and stage.")
    for (provider, stage), value in sorted(limited.items()):
        limits.add(value, provider=provider, stage=stage)
    families.append(limits)

    tokens_rows = [(labels, v) for labels, v in parsed.get(TOKENS, []) if len(labels) == 2]
    cost_rows = [(labels, v) for labels, v in parsed.get(COST, []) if len(labels) == 1]
    models = _capped([labels[0] for labels, _ in tokens_rows + cost_rows], MODEL_LIMIT)
    token_totals: dict[tuple, float] = {}
    for labels, value in tokens_rows:
        _add_to(token_totals, (models(labels[0]), _known(labels[1], TOKEN_TYPES)), value)
    cost_totals: dict[tuple, float] = {}
    for labels, value in cost_rows:
        _add_to(cost_totals, (models(labels[0]),), value)
    tokens = Family("symphony_tokens_total", "counter",
                    "Tokens that implementation attempts used, by model and type.")
    for (model, kind), value in sorted(token_totals.items()):
        tokens.add(value, model=model, type=kind)
    cost = Family("symphony_cost_usd_total", "counter",
                  "Estimated cost of implementation attempts in US dollars, by model. The engine "
                  "computes it from its bundled price table; it is not provider billing.")
    for (model,), value in sorted(cost_totals.items()):
        cost.add(value, model=model)
    families += [tokens, cost]

    heartbeat = _json(heartbeat)
    if isinstance(heartbeat, int) and not isinstance(heartbeat, bool):
        families.append(Family("symphony_daemon_heartbeat_timestamp_seconds", "gauge",
                               "The last daemon heartbeat, as a Unix time.").add(heartbeat / 1000))
    daemon_setup = _json(daemon_setup)
    if isinstance(daemon_setup, dict) and daemon_setup.get("status") in ("ready", "incomplete"):
        families.append(Family("symphony_daemon_setup_ready", "gauge",
                               "1 when the daemon has a valid WORKFLOW and a usable tracker.").add(
            int(daemon_setup["status"] == "ready")))
    setup = _json(setup)
    if isinstance(setup, dict) and isinstance(setup.get("steps"), dict):
        steps = Family("symphony_setup_step", "gauge",
                       "1 for the status of each setup step in the last status the API computed.")
        for step in SETUP_STEPS:
            current = setup["steps"].get(step)
            for status in SETUP_STATUSES:
                steps.add(int(current == status), step=step, status=status)
        families.append(steps)
        families.append(Family("symphony_setup_complete", "gauge",
                               "1 when every required setup step is done.").add(
            int(bool(setup.get("complete")))))
        if isinstance(setup.get("at"), int):
            families.append(Family("symphony_setup_checked_timestamp_seconds", "gauge",
                                   "When the API last computed the setup status, as a Unix time.")
                            .add(setup["at"] / 1000))

    merger = {
        "queue": Family("symphony_merger_queue", "gauge",
                        "Pull requests in the review queue of the merger, by repository."),
        "reviewing": Family("symphony_merger_reviewing", "gauge",
                            "Pull requests under review, by repository."),
        "train": Family("symphony_merger_train", "gauge",
                        "Pull requests in the merge train, by repository."),
        "parked": Family("symphony_merger_parked", "gauge",
                         "Reviews that wait for the head CI, by repository."),
        "up": Family("symphony_merger_heartbeat_up", "gauge",
                     "1 while the merger of the repository beats (TTL 90 s)."),
    }
    for repo, (queue, reviewing, recs, parked, alive) in zip(repos, merger_rows, strict=True):
        queue = _json(queue)
        in_train = 0
        for raw in recs or []:
            rec = _json(raw)
            if isinstance(rec, dict) and rec.get("state") in ("train", "finalizing", "resolve"):
                in_train += 1
        merger["queue"].add(len(queue) if isinstance(queue, list) else 0, repo=repo)
        merger["reviewing"].add(int(reviewing or 0), repo=repo)
        merger["train"].add(in_train, repo=repo)
        merger["parked"].add(int(parked or 0), repo=repo)
        merger["up"].add(int(bool(alive)), repo=repo)
    families += list(merger.values())
    return families


def gateway_families(client, namespace: str) -> list[Family]:
    """MCP: calls through the gateway, cleanup results, and the leases agents still hold."""
    ns = namespace
    pipe = client.pipeline(transaction=False)
    pipe.hgetall(f"sym:{ns}:mcp:calls")
    pipe.hgetall(counters_key(ns))
    pipe.smembers(f"sym:{ns}:mcp:held")
    calls, counters, digests = pipe.execute()
    digests = sorted(digests or ())[:HELD_LIMIT]
    pipe = client.pipeline(transaction=False)
    for digest in digests:
        pipe.hkeys(f"sym:{ns}:mcp:held:{digest}")
    held_fields = pipe.execute() if digests else []

    rows = []
    for key, value in (calls or {}).items():
        server, _, role = key.partition(":")
        try:
            rows.append((_clean(server), _clean(role), float(value)))
        except ValueError:
            continue
    servers = _capped(server for server, _, _ in rows)
    roles = _capped(role for _, role, _ in rows)
    call_totals: dict[tuple, float] = {}
    for server, role, value in rows:
        _add_to(call_totals, (servers(server), roles(role)), value)
    calls_family = Family("symphony_mcp_calls_total", "counter",
                          "MCP requests through the gateway, by server and agent role.")
    for (server, role), value in sorted(call_totals.items()):
        calls_family.add(value, server=server, role=role)

    cleanup = Family("symphony_mcp_cleanup_total", "counter",
                     "Held MCP ids that a cleanup released, or whose release failed.")
    results = dict.fromkeys(CLEANUP_RESULTS, 0.0)
    for labels, value in _counters(counters or {}).get(MCP_CLEANUP, []):
        if labels and labels[0] in results:
            results[labels[0]] += value
    for result, value in results.items():
        cleanup.add(value, result=result)

    names = [_clean(f.partition("/")[0]) for fields in held_fields for f in fields or ()]
    held_servers = _capped(names)
    held_totals: dict[tuple, float] = {}
    for name in names:
        _add_to(held_totals, (held_servers(name),), 1)
    held = Family("symphony_mcp_held_leases", "gauge",
                  "MCP ids (such as Seiji leases) that agent sessions hold, by server.")
    for (server,), value in sorted(held_totals.items()):
        held.add(value, server=server)
    return [calls_family, cleanup, held]


def assistant_families(stats: Mapping, transports: Mapping) -> list[Family]:
    """From the assistant's process state: `Engine.stats()` and its transports."""
    threads = Family("symphony_assistant_threads", "gauge",
                     "Assistant threads: open (with an omp session), active (a turn runs), "
                     "queued (turns that wait for a slot).")
    for name in ("open", "active", "queued"):
        threads.add(int(stats.get("threads" if name == "open" else name, 0)), state=name)
    connected = Family("symphony_assistant_transport_connected", "gauge",
                       "1 while a configured chat transport is connected.")
    for name in TRANSPORTS:
        if name in transports:
            connected.add(int(bool(getattr(transports[name], "connected", False))),
                          transport=name)
    return [threads, connected]


# -- scrape and serve --------------------------------------------------------------------------
class Scrape:
    """The rendered `/metrics` of one component, cached for `ttl_s`. `collect` reads the store
    or process state; when it raises, the scrape still answers, with `symphony_metrics_up` 0."""

    def __init__(self, component: str, collect: Callable[[], list[Family]] | None = None, *,
                 ttl_s: float = CACHE_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.component = component
        self.collect = collect
        self.ttl_s = ttl_s
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, bytes] | None = None

    def text(self) -> bytes:
        with self._lock:
            now = self.clock()
            if self._cached is not None and now - self._cached[0] < self.ttl_s:
                return self._cached[1]
            families = [build_info(self.component)]
            if self.collect is not None:
                up = Family("symphony_metrics_up", "gauge",
                            "1 when this scrape read its source (Redis or process state).")
                try:
                    families += self.collect()
                    up.add(1)
                except Exception as exc:  # noqa: BLE001 — a failed read is a metric, not a 500
                    from .edge import log

                    log.warning("metrics_collect_failed", component=self.component,
                                error=type(exc).__name__)
                    up.add(0)
                families.append(up)
            body = render(families).encode()
            self._cached = (now, body)
            return body


def serve(port: int, healthy: Callable[[], bool], scrape: Scrape | None = None,
          host: str = "0.0.0.0") -> http.server.ThreadingHTTPServer:
    """The health port: `GET /healthz` (200 `ok` or 503 `stale`) and `GET /metrics`."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server's contract
            if self.path == "/healthz":
                ok = healthy()
                self._send(200 if ok else 503, "text/plain", b"ok" if ok else b"stale")
            elif self.path == "/metrics" and scrape is not None:
                self._send(200, CONTENT_TYPE, scrape.text())
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # probe and scrape chatter has no business in the log
            pass

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
