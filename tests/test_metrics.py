"""`/metrics`: values from the store and process state, the Prometheus text format, label
bounds, the scrape cache, and where the endpoint is (the health port, never the public API)."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml
from tests.api_support import client_for, make_services, tokens_for
from tests.test_dispatch import FakeEffects, cfg

from symphony import mcp_servers, metrics, state, usage
from symphony.kv import RedisKV
from symphony.merger import agents
from symphony.merger.store import MergerStore
from symphony.orchestrator import mark_clean_continuation, mark_retry_after_failure, tick
from symphony.sessions import SessionStore

ROOT = Path(__file__).resolve().parent.parent
NS = "test"
OWNER = "daemon-test"

# --------------------------------------------------------------------------- the parser
# Faithful to scripts/mocks/specs/prometheus-exposition-formats.md (text format 0.0.4).
_IDENT = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_FLOAT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_INT64 = re.compile(r"[+-]?\d+\Z")
TYPES = ("counter", "gauge", "histogram", "summary", "untyped")


class FormatError(ValueError):
    pass


def _unescape(text: str, allowed: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            nxt = text[i + 1:i + 2]
            if nxt not in allowed or not nxt:
                raise FormatError(f"bad escape \\{nxt}")
            out.append("\n" if nxt == "n" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _value(token: str) -> float:
    if token in ("NaN", "+Inf", "-Inf"):
        return float(token.replace("Inf", "inf").replace("NaN", "nan"))
    if not _FLOAT.match(token):
        raise FormatError(f"bad value {token!r}")
    return float(token)


def _family_of(name: str, types: dict[str, str]) -> str:
    for suffix, kinds in (("_bucket", ("histogram",)), ("_sum", ("histogram", "summary")),
                          ("_count", ("histogram", "summary"))):
        base = name[: -len(suffix)]
        if name.endswith(suffix) and types.get(base) in kinds:
            return base
    return name


def _sample(line: str) -> tuple[str, dict[str, str], float]:
    match = _IDENT.match(line)
    if not match:
        raise FormatError(f"bad metric name in {line!r}")
    name, i = match.group(), match.end()
    labels: dict[str, str] = {}
    while i < len(line) and line[i] in " \t":
        i += 1
    if line[i:i + 1] == "{":
        i += 1
        while True:
            while line[i:i + 1] in (" ", "\t"):
                i += 1
            if line[i:i + 1] == "}":
                i += 1
                break
            label = _LABEL.match(line, i)
            if not label:
                raise FormatError(f"bad label name in {line!r}")
            i = label.end()
            while line[i:i + 1] in (" ", "\t"):
                i += 1
            if line[i:i + 2] != '="':
                raise FormatError(f"expected =\" in {line!r}")
            i += 2
            start = i
            while i < len(line) and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            if i >= len(line):
                raise FormatError(f"unterminated label value in {line!r}")
            if label.group() in labels:
                raise FormatError(f"duplicate label {label.group()}")
            labels[label.group()] = _unescape(line[start:i], '\\"n')
            i += 1
            while line[i:i + 1] in (" ", "\t"):
                i += 1
            if line[i:i + 1] == ",":
                i += 1
            elif line[i:i + 1] != "}":
                raise FormatError(f"expected , or }} in {line!r}")
    rest = line[i:].split()
    if not rest or len(rest) > 2:
        raise FormatError(f"expected value [timestamp] in {line!r}")
    if len(rest) == 2 and not _INT64.match(rest[1]):
        raise FormatError(f"bad timestamp in {line!r}")
    return name, labels, _value(rest[0])


def parse(text: str) -> dict[str, dict]:
    """Families by name: `{type, help, samples: [(name, labels, value)]}`. Raises on any rule
    of the text format that the exposition breaks."""
    if not text.endswith("\n"):
        raise FormatError("the last line must end with a line feed")
    families: dict[str, dict] = {}
    types: dict[str, str] = {}
    helps: set[str] = set()
    seen: set[tuple] = set()
    closed: set[str] = set()
    current = None
    for raw in text.split("\n"):
        line = raw.strip(" \t")
        if not line:
            continue
        if line.startswith("#"):
            tokens = line[1:].split(None, 2)
            if not tokens or tokens[0] not in ("HELP", "TYPE"):
                continue
            if len(tokens) < 2 or not _IDENT.fullmatch(tokens[1]):
                raise FormatError(f"bad {tokens[0]} line {line!r}")
            name = tokens[1]
            fam = families.setdefault(name, {"type": "untyped", "help": None, "samples": []})
            if name in closed or (fam["samples"] and tokens[0] == "TYPE"):
                raise FormatError(f"{tokens[0]} for {name} after its samples")
            if tokens[0] == "HELP":
                if name in helps:
                    raise FormatError(f"second HELP for {name}")
                helps.add(name)
                fam["help"] = _unescape(tokens[2] if len(tokens) > 2 else "", "\\n")
            else:
                if name in types or len(tokens) != 3 or tokens[2] not in TYPES:
                    raise FormatError(f"bad TYPE line {line!r}")
                types[name] = fam["type"] = tokens[2]
            if current not in (None, name):
                closed.add(current)
            current = name
            continue
        name, labels, number = _sample(line)
        family = _family_of(name, types)
        if family in closed:
            raise FormatError(f"samples of {family} are not one group")
        if current not in (None, family):
            closed.add(current)
        current = family
        key = (name, tuple(sorted(labels.items())))
        if key in seen:
            raise FormatError(f"duplicate series {key}")
        seen.add(key)
        families.setdefault(family, {"type": "untyped", "help": None, "samples": []})[
            "samples"].append((name, labels, number))
    return families


def value(families: dict, name: str, **labels) -> float:
    matches = [v for fam in families.values() for n, ls, v in fam["samples"]
               if n == name and ls == labels]
    assert len(matches) == 1, (name, labels, matches)
    return matches[0]


def label_values(families: dict, name: str, label: str) -> set[str]:
    return {ls[label] for _, ls, _ in families[name]["samples"] if label in ls}


# --------------------------------------------------------------------------- format


def test_render_escapes_and_the_parser_reads_it_back():
    weird = 'C:\\DIR\\FILE.TXT "quoted"\nnext line'
    fam = metrics.Family("symphony_x", "gauge", "Help with \\ and\na new line.")
    fam.add(1, path=weird).add(2.5, path="plain").add(math.inf, path="inf").add(-math.inf, path="m")
    nan = metrics.Family("symphony_y_total", "counter", "Not a number.").add(math.nan)
    families = parse(metrics.render([fam, nan]))
    assert families["symphony_x"]["help"] == "Help with \\ and\na new line."
    assert value(families, "symphony_x", path=weird) == 1
    assert value(families, "symphony_x", path="plain") == 2.5
    assert value(families, "symphony_x", path="inf") == math.inf
    assert value(families, "symphony_x", path="m") == -math.inf
    assert math.isnan(value(families, "symphony_y_total"))
    assert families["symphony_y_total"]["type"] == "counter"


@pytest.mark.parametrize("text", [
    "a 1",  # no final line feed
    'a{l="x"y"} 1\n',  # unescaped quote
    'a{l="\\t"} 1\n',  # an escape the format does not have
    "a 1\na 2\n",  # the same series twice
    "a 1\n# TYPE a gauge\n",  # TYPE after the samples
    "a 1\nb 1\na{x=\"1\"} 2\n",  # one family in two groups
    "# TYPE a gauge\n# TYPE a counter\n",  # two TYPE lines
    "a one\n",  # not a float
    "1a 1\n",  # not a metric name
    'a{l="1",l="2"} 1\n',  # a label twice
])
def test_the_parser_rejects_what_the_spec_forbids(text):
    with pytest.raises(FormatError):
        parse(text)


def test_the_parser_reads_the_spec_example():
    spec = (ROOT / "scripts/mocks/specs/prometheus-exposition-formats.md").read_text()
    example = spec.split("### Example", 1)[1].split("```", 2)[1]
    # The UTF-8 quoted-name line is a newer extension than 0.0.4; Symphony never emits it.
    lines = [line for line in example.splitlines() if not line.startswith("{")]
    families = parse("\n".join(lines).strip() + "\n")
    assert value(families, "http_requests_total", method="post", code="400") == 3
    assert value(families, "msdos_file_access_time_seconds", path="C:\\DIR\\FILE.TXT",
                 error='Cannot find file:\n"FILE.TXT"') == 1.458255915e9
    assert value(families, "http_request_duration_seconds_bucket", le="+Inf") == 144320


# --------------------------------------------------------------------------- seeded store


def _seed(client, now_ms: int) -> None:
    kv = RedisKV(client)
    assert state.renew_lease(kv, OWNER, now_ms)
    state.heartbeat(kv, now_ms)
    state.report_setup(kv, now_ms, None)
    for issue_id, status in (("run", "running"), ("claim", "claimed"), ("retry", "retry_queued"),
                             ("block", "blocked"), ("park", "parked")):
        kv.put(state.issue_key(issue_id), {"status": status, "identifier": f"ENG-{issue_id}"})
    client.set(state.hold_key("park"), json.dumps(state.hold_record("ann", "mine", now_ms)))
    client.set(state.hold_key("untracked"), json.dumps(state.hold_record("ann", "mine", now_ms)))
    yyyymm = metrics._yyyymm(now_ms)
    for reason in ("failure", "failure", "continuation"):
        state.bump_metric_category(kv, yyyymm, "retries", reason)

    sessions = SessionStore(client, NS)
    for sid, kind, outcome in (("i1:1", "implement", "done"), ("i2:1", "implement", "failed"),
                               ("i3:1", "answer", "failed"), ("merger:a:b:1:review:1", "review", "done")):
        sessions.open(sid, kind=kind)
        sessions.close(sid, outcome)
    sessions.open("i4:1", kind="implement")
    sessions.update("i4:1", state="running")
    sessions.open("assist:slack:c:t:1", kind="assist")

    usage.record_rate_limit(client, NS, "amazon-bedrock", "m1", {"stage": "throttled"})
    usage.record_rate_limit(client, NS, "amazon-bedrock", "m2", {"stage": "throttled"})
    agents.record_rate_limit(sessions, {"provider": "anthropic", "model": "m3"}, {"stage": "wrap_up"})
    for _ in range(2):
        metrics.count_tokens(client, NS, "amazon-bedrock/claude", {
            "input": 10, "output": 5, "cache_read": 3, "cache_write": 0, "total": 18,
            "cost_estimate": 0.25})
    metrics.record_setup(client, NS, {"complete": False, "steps": [
        {"id": "tracker", "status": "done"}, {"id": "github_app", "status": "todo"}]}, now_ms)

    app = MergerStore(client, NS, "acme/app")
    app.register()
    prefix = app.prefix
    client.set(prefix + "queue", json.dumps([{"pr": 1}, {"pr": 2}]))
    client.hset(prefix + "reviewing", "3", "{}")
    client.hset(prefix + "rec", mapping={"4": json.dumps({"state": "train"}),
                                         "5": json.dumps({"state": "reviewed"})})
    client.hset(prefix + "parked", "6", "{}")
    lib = MergerStore(client, NS, "acme/lib")
    lib.register()
    client.delete(lib.prefix + "heartbeat")


def test_daemon_metrics_match_the_seeded_store(redis_client):
    now = int(time.time() * 1000)
    _seed(redis_client, now)
    families = parse(metrics.render(metrics.daemon_families(redis_client, NS, OWNER, now)))

    assert value(families, "symphony_daemon_leader") == 1
    for status, expected in (("running", 1), ("claimed", 1), ("retry_queued", 1), ("blocked", 1),
                             ("parked", 1), ("interrupting", 0)):
        assert value(families, "symphony_tickets", state=status) == expected
    assert value(families, "symphony_attempts", state="running") == 1
    assert value(families, "symphony_attempts", state="queued") == 2
    assert value(families, "symphony_attempts", state="held") == 2
    assert value(families, "symphony_metrics_truncated") == 0
    assert value(families, "symphony_attempts_finished_total", outcome="done") == 1
    assert value(families, "symphony_attempts_finished_total", outcome="failed") == 2
    assert value(families, "symphony_attempts_finished_total", outcome="error") == 0
    assert value(families, "symphony_retries_total", reason="failure") == 2
    assert value(families, "symphony_retries_total", reason="continuation") == 1
    assert value(families, "symphony_sessions", kind="implement", state="running") == 1
    assert value(families, "symphony_sessions", kind="assist", state="starting") == 1
    assert len(families["symphony_sessions"]["samples"]) == 2  # ended sessions are not live
    assert value(families, "symphony_rate_limit_events_total", provider="amazon-bedrock",
                 stage="throttled") == 2
    assert value(families, "symphony_rate_limit_events_total", provider="anthropic",
                 stage="wrap_up") == 1
    model = "amazon-bedrock/claude"
    assert value(families, "symphony_tokens_total", model=model, type="input") == 20
    assert value(families, "symphony_tokens_total", model=model, type="output") == 10
    assert value(families, "symphony_tokens_total", model=model, type="cache_read") == 6
    assert value(families, "symphony_cost_usd_total", model=model) == pytest.approx(0.5)
    assert value(families, "symphony_daemon_heartbeat_timestamp_seconds") == now / 1000
    assert value(families, "symphony_daemon_setup_ready") == 1
    assert value(families, "symphony_setup_step", step="tracker", status="done") == 1
    assert value(families, "symphony_setup_step", step="github_app", status="done") == 0
    assert value(families, "symphony_setup_step", step="github_app", status="todo") == 1
    assert value(families, "symphony_setup_complete") == 0
    assert value(families, "symphony_merger_queue", repo="acme/app") == 2
    assert value(families, "symphony_merger_reviewing", repo="acme/app") == 1
    assert value(families, "symphony_merger_train", repo="acme/app") == 1
    assert value(families, "symphony_merger_parked", repo="acme/app") == 1
    assert value(families, "symphony_merger_heartbeat_up", repo="acme/app") == 1
    assert value(families, "symphony_merger_heartbeat_up", repo="acme/lib") == 0


def test_a_standby_daemon_exposes_no_store_metrics(redis_client):
    now = int(time.time() * 1000)
    _seed(redis_client, now)
    families = parse(metrics.render(metrics.daemon_families(redis_client, NS, "daemon-other", now)))
    assert set(families) == {"symphony_daemon_leader"}
    assert value(families, "symphony_daemon_leader") == 0


def test_the_daemon_counts_redispatches_as_retries(redis_client, clock, tracker):
    kv = RedisKV(redis_client)
    tracker.add_issue("i1", "ENG-1")
    tracker.add_issue("i2", "ENG-2")
    c = cfg()
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    mark_retry_after_failure(kv, "i1", kv.get(state.issue_key("i1")), c, clock.now_ms(), "boom")
    # The ticket changed since the clean exit, so the anti-runaway cooldown does not hold it.
    mark_clean_continuation(kv, "i2", kv.get(state.issue_key("i2")), clock.now_ms(), "earlier")
    clock.advance(11_000)
    tick(kv, tracker, c, clock.now_ms(), FakeEffects(), owner="d1")
    assert state.renew_lease(kv, "d1", clock.now_ms())
    families = parse(metrics.render(metrics.daemon_families(redis_client, "test", "d1",
                                                            clock.now_ms())))
    assert value(families, "symphony_retries_total", reason="failure") == 1
    assert value(families, "symphony_retries_total", reason="continuation") == 1


def test_gateway_metrics_match_the_store(redis_client):
    for server, role in (("seiji", "implementer"), ("seiji", "implementer"), ("files", "reviewer")):
        mcp_servers.count_call(redis_client, NS, server, role)
    mcp_servers.record_held(redis_client, NS, "tok-a", "i1:1", "seiji", "lease-1")
    mcp_servers.record_held(redis_client, NS, "tok-a", "i1:1", "seiji", "lease-2")
    mcp_servers.record_held(redis_client, NS, "tok-b", "i2:1", "files", "lease-3")

    class NoServers:
        def get(self, name):
            return None

        def headers(self, name):
            return {}

    counts = mcp_servers.release_held(redis_client, NS, NoServers(), mcp_servers.token_hash("tok-b"))
    assert counts["failed"] == 1
    metrics.count_mcp_cleanup(redis_client, NS, 3, 0)
    families = parse(metrics.render(metrics.gateway_families(redis_client, NS)))
    assert value(families, "symphony_mcp_calls_total", server="seiji", role="implementer") == 2
    assert value(families, "symphony_mcp_calls_total", server="files", role="reviewer") == 1
    assert value(families, "symphony_mcp_cleanup_total", result="released") == 3
    assert value(families, "symphony_mcp_cleanup_total", result="failed") == 1
    assert value(families, "symphony_mcp_held_leases", server="seiji") == 2
    assert label_values(families, "symphony_mcp_held_leases", "server") == {"seiji"}


def test_assistant_metrics_come_from_process_state():
    class Transport:
        def __init__(self, connected):
            self.connected = connected

    families = parse(metrics.render(metrics.assistant_families(
        {"threads": 3, "active": 2, "queued": 4}, {"slack": Transport(True), "discord": Transport(False)})))
    assert value(families, "symphony_assistant_threads", state="open") == 3
    assert value(families, "symphony_assistant_threads", state="active") == 2
    assert value(families, "symphony_assistant_threads", state="queued") == 4
    assert value(families, "symphony_assistant_transport_connected", transport="slack") == 1
    assert value(families, "symphony_assistant_transport_connected", transport="discord") == 0


# --------------------------------------------------------------------------- bounds and cost


def test_label_values_are_bounded_and_carry_no_ids(redis_client):
    now = int(time.time() * 1000)
    _seed(redis_client, now)
    kv = RedisKV(redis_client)
    sessions = SessionStore(redis_client, NS)
    for n in range(60):
        kv.put(state.issue_key(f"issue-{n}"), {"status": f"weird-{n}"})
        metrics.count_tokens(redis_client, NS, f"p/model-{n}", {"input": 1})
        usage.record_rate_limit(redis_client, NS, f"provider-{n}", "m", {"stage": f"stage-{n}"})
        mcp_servers.count_call(redis_client, NS, f"server-{n}", f"role-{n}")
        sessions.open(f"session-{n}:1", kind="implement")
        sessions.close(f"session-{n}:1", f"outcome-{n}")
    redis_client.hset(metrics.counters_key(NS), "attempts_finished|made-up", 1)
    daemon = metrics.render(metrics.daemon_families(redis_client, NS, OWNER, now))
    gateway = metrics.render(metrics.gateway_families(redis_client, NS))
    families = {**parse(daemon), **parse(gateway)}

    assert label_values(families, "symphony_tickets", "state") == {*metrics.TICKET_STATES, "other"}
    assert label_values(families, "symphony_attempts_finished_total", "outcome") <= {
        *metrics.OUTCOMES, "other"}
    assert len(label_values(families, "symphony_tokens_total", "model")) <= metrics.MODEL_LIMIT + 1
    for name, label in (("symphony_rate_limit_events_total", "provider"),
                        ("symphony_rate_limit_events_total", "stage"),
                        ("symphony_mcp_calls_total", "server"), ("symphony_mcp_calls_total", "role")):
        assert len(label_values(families, name, label)) <= metrics.LABEL_LIMIT + 1, name
    for fam in families.values():
        assert len(fam["samples"]) <= 100
    for text in (daemon, gateway):
        assert "issue-" not in text and "session-" not in text and "i1:1" not in text


class CountingClient:
    """Counts the round trips a scrape makes: direct commands and executed pipelines."""

    def __init__(self, client):
        self.client = client
        self.direct: list[str] = []
        self.pipelines = 0

    def pipeline(self, *args, **kwargs):
        pipe = self.client.pipeline(*args, **kwargs)
        execute = pipe.execute

        def counted(*a, **kw):
            self.pipelines += 1
            return execute(*a, **kw)

        pipe.execute = counted
        return pipe

    def __getattr__(self, name):
        self.direct.append(name)
        return getattr(self.client, name)


def test_a_scrape_costs_two_pipelines_and_a_bounded_scan(redis_client):
    now = int(time.time() * 1000)
    _seed(redis_client, now)
    counting = CountingClient(redis_client)
    metrics.daemon_families(counting, NS, OWNER, now)
    assert counting.pipelines == 2
    assert set(counting.direct) == {"scan"}
    assert len(counting.direct) <= metrics.SCAN_PAGES
    counting = CountingClient(redis_client)
    metrics.gateway_families(counting, NS)
    assert counting.pipelines <= 2 and counting.direct == []


def test_the_scrape_is_cached_for_ten_seconds():
    now = [100.0]
    calls = []

    def collect():
        calls.append(now[0])
        return [metrics.Family("symphony_tick", "gauge", "Calls.").add(len(calls))]

    scrape = metrics.Scrape("daemon", collect, clock=lambda: now[0])
    first = scrape.text()
    now[0] += 9.9
    assert scrape.text() == first and len(calls) == 1
    now[0] += 0.2
    second = parse(scrape.text().decode())
    assert len(calls) == 2 and value(second, "symphony_tick") == 2
    assert metrics.CACHE_S == 10.0


def test_a_failed_read_still_answers():
    def broken():
        raise ConnectionError("redis is down")

    families = parse(metrics.Scrape("gateway", broken).text().decode())
    assert value(families, "symphony_metrics_up") == 0
    assert label_values(families, "symphony_build_info", "component") == {"gateway"}


# --------------------------------------------------------------------------- where it is served


def test_the_health_port_serves_healthz_and_metrics(redis_client):
    now = int(time.time() * 1000)
    _seed(redis_client, now)
    scrape = metrics.Scrape("daemon", lambda: metrics.daemon_families(redis_client, NS, OWNER))
    server = metrics.serve(0, lambda: True, scrape, host="127.0.0.1")
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as resp:
            assert resp.headers["Content-Type"] == metrics.CONTENT_TYPE
            families = parse(resp.read().decode())
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
            assert resp.read() == b"ok"
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/other", timeout=5)
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
    assert value(families, "symphony_metrics_up") == 1
    assert value(families, "symphony_tickets", state="running") == 1
    [(_, info, _)] = families["symphony_build_info"]["samples"]
    assert info["component"] == "daemon" and info["version"] and info["commit"]


def test_the_public_api_port_serves_no_metrics(redis_client):
    services = make_services(redis_client)
    client = client_for(services)
    admin = tokens_for(services)["admin"]
    for headers in ({}, {"Authorization": f"Bearer {admin}"}):
        resp = client.get("/metrics", headers=headers)
        assert resp.status_code == 404
        assert "symphony_" not in resp.text


def _yaml(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def test_monitoring_manifests_scrape_the_health_port_from_a_labeled_namespace():
    base = yaml.safe_load((ROOT / "deploy/kustomization.yaml").read_text())
    assert not any("monitoring" in r for r in base["resources"])
    folder = ROOT / "deploy/monitoring"
    docs = [d for name in yaml.safe_load((folder / "kustomization.yaml").read_text())["resources"]
            for d in _yaml(folder / name)]
    [policy] = [d for d in docs if d["kind"] == "NetworkPolicy"]
    [rule] = policy["spec"]["ingress"]
    assert rule["from"] == [{"namespaceSelector": {"matchLabels": {"symphony.dev/monitoring": "true"}}}]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8081}]
    components = set(policy["spec"]["podSelector"]["matchExpressions"][0]["values"])
    assert components == {"daemon", "merger", "assistant", "gateway"}
    assert "api" not in components
    [monitor] = [d for d in docs if d["kind"] == "ServiceMonitor"]
    [endpoint] = monitor["spec"]["endpoints"]
    services = [d for d in docs if d["kind"] == "Service"]
    assert {s["spec"]["selector"]["component"] for s in services} == components
    workloads = {d["spec"]["selector"]["matchLabels"]["component"]: d
                 for name in ("daemon", "merger", "assistant", "gateway")
                 for d in _yaml(ROOT / f"deploy/{name}.yaml") if d["kind"] == "Deployment"}
    for service in services:
        assert service["metadata"]["labels"].items() >= monitor["spec"]["selector"]["matchLabels"].items()
        [port] = service["spec"]["ports"]
        assert port["name"] == endpoint["port"] and endpoint["path"] == "/metrics"
        container = workloads[service["spec"]["selector"]["component"]]["spec"]["template"]["spec"][
            "containers"][0]
        ports = {p["name"]: p["containerPort"] for p in container["ports"]}
        assert ports[port["targetPort"]] == port["port"] == 8081
    api = _yaml(ROOT / "deploy/api.yaml")
    [api_container] = [d for d in api if d["kind"] == "Deployment"][0]["spec"]["template"]["spec"][
        "containers"]
    assert [p["containerPort"] for p in api_container["ports"]] == [8080]


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")
def test_monitoring_kustomization_renders():
    rendered = subprocess.run(["kubectl", "kustomize", str(ROOT / "deploy/monitoring")],
                              capture_output=True, text=True, check=True).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    assert {d["kind"] for d in docs} == {"Service", "ServiceMonitor", "NetworkPolicy"}
    assert all(d["metadata"]["namespace"] == "symphony" for d in docs)


# --------------------------------------------------------------------------- docs


def test_the_docs_list_every_metric(redis_client):
    now = int(time.time() * 1000)
    _seed(redis_client, now)
    names = set(parse(metrics.render(metrics.daemon_families(redis_client, NS, OWNER, now))))
    names |= set(parse(metrics.render(metrics.gateway_families(redis_client, NS))))
    names |= set(parse(metrics.render(metrics.assistant_families({}, {}))))
    names |= set(parse(metrics.Scrape("x", lambda: []).text().decode()))
    doc = (ROOT / "docs/observability.md").read_text()
    documented = set(re.findall(r"^\| `(symphony_[a-z_]+)` \|", doc, re.M))
    assert names <= documented, sorted(names - documented)
    assert documented <= names, sorted(documented - names)
