"""Read-only, metadata-only administrative projection of retained operational data.

Bounds: 200 issue, telemetry and retrospective records each; Redis SCAN stops
at ten pages (COUNT 200); the last twelve calendar months of retained counters;
200 Kubernetes resources total (67 Pods, 67 Jobs, 66 PVCs), one bounded page per
kind and a five-second request timeout. Truncation is explicit, never a global
count. GET deliberately bypasses RedisKV's idle-TTL renewal: observing the admin
panel must not silently change the retention of operational records.

No historical run is reconstructed from today's issue entry. Token totals are
retained usage-bearing RPC deltas, not provider billing; telemetry is a separately
retained, capped OTLP rollup, not an hourly token series or exhaustive usage ledger.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from collections import defaultdict
from itertools import islice
from typing import Any

from . import endpoints, state
from .repos import parse_designation
from .telemetry import MAX_MODELS, OTHER

RUN_LIMIT = 200
RESOURCE_LIMIT = 200
SCAN_PAGES = 10
MONTH_LIMIT = 12
HEARTBEAT_STALE_MS = 90_000
RATE_LIMIT_WINDOW_MS = 3_600_000
CLOCK_SKEW_MS = 60_000
MAX_RECORD_BYTES = 1_048_576
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,159}\Z")
# `<GITHUB_SERVER_URL>/<owner>/<repo>/pull/<n>`; the host follows the configured server.
_PR_PATH = r"/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/[1-9][0-9]*(?:#[A-Za-z0-9_-]+)?\Z"
_RUN_REASONS = {
    "running": "Runner is executing this dispatch.",
    "claimed": "Dispatch is claimed; runner execution is not yet reported.",
    "interrupting": "Interruption is pending; this generation occupies capacity until its stop is confirmed.",
    "retry_queued": "Retry is queued under the daemon's retry policy.",
    "blocked": "Execution requires a human decision.",
    "parked": "Execution is parked for external review or tracker progress.",
    "unknown": "Lifecycle status is not available.",
}
_ROUTE_REASONS = {
    "no-repo-designation": "No repository route is designated.",
    "malformed-repo-designation": "Repository designation is malformed.",
    "mixed-owner-repo-designation": "Repository designations name more than one owner.",
    "repo-not-authorized": "The runtime reported repository authorization failure.",
}
# Pod `component` label → the stable service role code the API exposes.
_ROLES = {
    "api": "api",
    "gateway": "gateway",
    "daemon": "daemon",
    "merger": "merger",
    "redis": "redis",
    "runner": "runner",
    "reflect": "runner",
    "workspace": "workspace",
    "workspace-sessions": "workspace",
    "sweep": "sweep",
}
# Why an issue cannot be dispatched, as codes (`_ROUTE_REASONS` keeps the operator prose).
ROUTE_REASON_CODES = {key: key.replace("-", "_") for key in _ROUTE_REASONS}


class ProjectionError(Exception):
    code = "state_unavailable"
    message = "Run state is unavailable."
    status_code = 503

    def __init__(self) -> None:
        super().__init__(self.message)


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and int(number) == number else None


def _text(value: Any, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:limit] or None


def _name(value: Any) -> str | None:
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def _repo(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 201:
        return None
    parsed = parse_designation(f"repo:{value}")
    if parsed and all(part not in {".", ".."} for part in parsed.split("/")):
        return parsed
    return None


def _pr_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 512:
        return None
    match = re.fullmatch(re.escape(endpoints.github_server_url()) + _PR_PATH, value)
    return value if match and _repo(f"{match[1]}/{match[2]}") else None


def _iso(value: Any) -> str | None:
    try:
        if isinstance(value, dt.datetime):
            instant = value
        elif _number(value) is not None and value > 0:
            instant = dt.datetime.fromtimestamp(value / 1000, dt.UTC)
        elif isinstance(value, str):
            instant = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            return None
        if instant.tzinfo is None:
            return None
        return instant.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _source(status: str, observed_at: str | None, message: str) -> dict:
    # `status` is the code clients render; `note` is English operator detail for logs only.
    return {"status": status, "observedAt": observed_at, "note": message}


def _attr(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _run(issue_id: str, entry: dict) -> dict | None:
    seq = _count(entry.get("dispatch_seq"))
    if not seq:
        return None
    identifier = _text(entry.get("identifier"), 160) or issue_id
    status = entry.get("status")
    if status not in _RUN_REASONS:
        status = "unknown"
    tokens = entry.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    events = []
    claimed_at = _iso(entry.get("claimed_at"))
    for key, title, detail in (
        ("claimed_at", "Dispatch claimed", "The daemon recorded this dispatch claim."),
        ("last_clean_exit_at", "Clean exit recorded", "The runner recorded a clean engine exit."),
        (
            "last_event_at",
            "Runner activity",
            "Most recent persisted runner activity; trace content is withheld.",
        ),
        ("retry_at", "Retry due", "Scheduled retry time; not evidence that a retry has executed."),
    ):
        timestamp = _iso(entry.get(key))
        if timestamp and claimed_at and timestamp >= claimed_at:
            events.append({"time": timestamp, "title": title, "detail": detail})
    events.sort(key=lambda event: event["time"])
    return {
        "issueId": issue_id,
        "id": identifier,
        "title": _text(entry.get("title")) or identifier,
        "repo": _repo(entry.get("target_repo")),
        "status": status,
        "attempt": _count(entry.get("attempt")),
        "seq": seq,
        "runtime": _name(entry.get("runtime")),
        "model": _name(entry.get("model")),
        "workspace": _name(entry.get("sandbox_name")),
        "tokens": _count(tokens.get("total")),
        "cost": _number(tokens.get("cost_estimate")),
        "updatedAt": _iso(entry.get("updated_at")) or _iso(entry.get("last_event_at")),
        "reason": _ROUTE_REASONS.get(entry.get("undispatchable_reason"), _RUN_REASONS[status]),
        "events": events,
        "prUrl": _pr_url(entry.get("pr_url")),
    }


def _learning(issue_id: str, token: dict) -> dict:
    identifier = _text(token.get("identifier"), 160) or issue_id
    raw_status = token.get("status")
    status = (
        "completed" if raw_status == "done" else "failed" if raw_status == "failed" else "pending"
    )
    pr = _pr_url(token.get("learnings_pr"))
    if raw_status == "done":
        outcome = (
            "nothing_to_learn" if token.get("outcome") == "nothing-to-learn"
            else "pr_opened" if pr
            else "completed"
        )
    else:
        outcome = status
    return {
        "id": issue_id,
        "identifier": identifier,
        "repo": _repo(token.get("target_repo")),
        "status": status,
        # Stable codes, never prose: clients render them in the user's language.
        "provenance": "retrospective" if token.get("provenance") == "retrospective" else "retained",
        "outcome": outcome,
        "prUrl": pr or _pr_url(token.get("source_pr")),
        "updatedAt": _iso(token.get("at")),
    }


class AdminProjection:
    def __init__(self, kv, store, core, batch, namespace, workflow) -> None:
        self.kv = kv
        self.store = store
        self.core = core
        self.batch = batch
        self.namespace = namespace
        self.workflow = workflow

    def _get(self, key: str) -> Any:
        raw = self.kv.client.get(key)
        if raw is None:
            return None
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("Operational record exceeds projection limit")
        return json.loads(raw)

    def _records(self, prefix: str) -> tuple[list[tuple[str, dict]], bool]:
        cursor = 0
        records = []
        seen = set()
        for _ in range(SCAN_PAGES):
            cursor, keys = self.kv.client.scan(cursor=cursor, match=f"{prefix}*", count=RUN_LIMIT)
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                if not key.startswith(prefix) or key in seen:
                    continue
                seen.add(key)
                if len(seen) > RUN_LIMIT:
                    return records, True
                value = self._get(key)
                if value is not None:
                    if not isinstance(value, dict):
                        raise ValueError("Invalid operational record")
                    records.append((key[len(prefix) :], value))
            if int(cursor) == 0:
                return records, False
        return records, True

    def run(self, issue_id, seq) -> dict | None:
        if not isinstance(issue_id, str) or not issue_id or len(issue_id) > 200 or not _count(seq):
            return None
        try:
            entry = self._get(state.issue_key(issue_id))
            if isinstance(entry, dict) and entry.get("dispatch_seq") == seq:
                return _run(issue_id, entry)
            # A retained entry must carry its own sequence; never substitute the
            # requested sequence, or attach today's mutable telemetry to history.
            history = self.store.run_history(issue_id, seq)
            if isinstance(history, dict) and history.get("dispatch_seq") == seq:
                return _run(issue_id, history)
            return None
        except Exception:
            raise ProjectionError() from None

    def _skeleton(self) -> tuple[dict, dt.datetime, int, str]:
        now = dt.datetime.now(dt.UTC)
        observed_at = _iso(now)
        result = {
            "schemaVersion": 1,
            "namespace": self.namespace,
            "observedAt": observed_at,
            "sources": {},
            "paused": None,
            "heartbeatAt": None,
            "runs": [],
            "services": [],
            "repositories": [],
            "learning": [],
            "resources": [],
            "telemetry": {"totals": None, "models": [], "monthly": []},
            "limits": {"runs": RUN_LIMIT, "resources": RESOURCE_LIMIT, "truncated": False},
        }
        return result, now, int(now.timestamp() * 1000), observed_at

    def snapshot(self) -> dict:
        result, now, now_ms, observed_at = self._skeleton()
        self._state(result, now_ms, observed_at)
        values = self._workflow_values(result, observed_at)
        self._telemetry(result, observed_at)
        self._metrics(result, observed_at, now)
        self._learning_records(result, observed_at)
        self._resources(result, observed_at)
        result["repositories"] = self._repositories(
            result["runs"],
            result["learning"],
            values,
            state_available=result["sources"]["state"]["status"] != "unavailable",
        )
        return result

    # -- sections the control API serves on their own (`/v1/repos`, `/v1/infra`, ...) ----
    def repos(self) -> dict:
        result, _, now_ms, observed_at = self._skeleton()
        self._state(result, now_ms, observed_at)
        values = self._workflow_values(result, observed_at)
        self._learning_records(result, observed_at)
        repositories = self._repositories(
            result["runs"],
            result["learning"],
            values,
            state_available=result["sources"]["state"]["status"] != "unavailable",
        )
        return {
            "observedAt": observed_at,
            "items": repositories,
            "sources": {k: result["sources"][k] for k in ("state", "workflow", "learning")},
            "limits": result["limits"],
        }

    def infra(self) -> dict:
        result, _, now_ms, observed_at = self._skeleton()
        self._state(result, now_ms, observed_at)
        self._resources(result, observed_at)
        return {
            "observedAt": observed_at,
            "paused": result["paused"],
            "heartbeatAt": result["heartbeatAt"],
            "services": result["services"],
            "resources": result["resources"],
            "sources": {k: result["sources"][k] for k in ("state", "kubernetes")},
            "limits": {"resources": RESOURCE_LIMIT, "truncated": result["limits"]["truncated"]},
        }

    def telemetry(self, sessions=None, *, limit: int = 200) -> dict:
        """Usage totals, per-model rollups, monthly counters and, when a SessionStore is
        given, the token rollup of the most recently updated sessions."""
        result, now, _, observed_at = self._skeleton()
        self._telemetry(result, observed_at)
        self._metrics(result, observed_at, now)
        rows = []
        if sessions is not None:
            for meta in sessions.list(limit=limit):
                tokens = meta.get("tokens") if isinstance(meta.get("tokens"), dict) else {}
                rows.append({
                    "sid": meta.get("sid"),
                    "identifier": _text(meta.get("identifier"), 160),
                    "kind": meta.get("kind"),
                    "state": meta.get("state"),
                    "model": _name(meta.get("model")),
                    "turn": _count(meta.get("turn")),
                    "tokens": {
                        key: _count(tokens.get(key)) for key in ("input", "output", "total")
                    } | {"cost": _number(tokens.get("cost"))},
                    "updatedAt": _iso(meta.get("updated_at")),
                })
        return {
            "observedAt": observed_at,
            **result["telemetry"],
            "sessions": rows,
            "sources": {k: result["sources"][k] for k in ("telemetry", "metrics")},
            "limits": {"truncated": result["limits"]["truncated"], "sessions": limit},
        }

    def learning(self) -> dict:
        result, _, _, observed_at = self._skeleton()
        self._learning_records(result, observed_at)
        return {
            "observedAt": observed_at,
            "items": result["learning"],
            "sources": {"learning": result["sources"]["learning"]},
            "limits": {"truncated": result["limits"]["truncated"]},
        }

    def _state(self, result: dict, now_ms: int, observed_at: str) -> None:
        sources = result["sources"]
        try:
            entries, truncated = self._records(state.ISSUE_PREFIX)
            heartbeat = self._get(state.HEARTBEAT_KEY)
            result["heartbeatAt"] = _iso(heartbeat)
            result["paused"] = self.store.paused()
            result["runs"] = [run for issue_id, entry in entries if (run := _run(issue_id, entry))]
            invalid_identity = len(result["runs"]) != len(entries)
            fresh = (
                result["heartbeatAt"] is not None and 0 <= now_ms - heartbeat <= HEARTBEAT_STALE_MS
            )
            result["limits"]["truncated"] |= truncated
            message = (
                "At most 200 current issue records; repository counts cover only displayed runs. "
            )
            message += (
                "Daemon heartbeat is current."
                if fresh
                else "Daemon heartbeat is missing, future-dated or older than 90 seconds."
            )
            if truncated:
                message += " Issue scan was truncated (ten SCAN pages maximum)."
            if invalid_identity:
                message += " Records without a valid dispatch sequence were omitted."
            sources["state"] = _source(
                "fresh" if fresh and not invalid_identity else "stale", observed_at, message
            )
            result["services"].append(
                {"name": "redis", "role": "redis", "status": "healthy", "detail": "ok"}
            )
            result["services"].append(
                {
                    "name": "daemon",
                    "role": "daemon",
                    "status": "healthy" if fresh else "degraded",
                    # The daemon is seen only through its persisted heartbeat.
                    "detail": "ok" if fresh else "stale",
                }
            )
        except Exception:
            sources["state"] = _source(
                "unavailable",
                None,
                "Operational state could not be read; runs, admission and heartbeat are unavailable.",
            )
            result.update(runs=[], paused=None, heartbeatAt=None)
            result["services"].append(
                {"name": "redis", "role": "redis", "status": "unknown", "detail": "unreadable"}
            )

    def _workflow_values(self, result: dict, observed_at: str) -> dict:
        try:
            workflow = self.workflow.read()
            values = workflow["values"]
            if not isinstance(values, dict):
                raise ValueError("Invalid workflow values")
            result["sources"]["workflow"] = _source(
                "fresh",
                observed_at,
                "Published ConfigMap read successfully; publication is distinct from consumer observation.",
            )
            return values
        except Exception:
            result["sources"]["workflow"] = _source(
                "unavailable",
                None,
                "Published workflow could not be read; configured repository defaults are unavailable.",
            )
            return {}

    def _learning_records(self, result: dict, observed_at: str) -> None:
        try:
            tokens, truncated = self._records(state.RETRO_PREFIX)
            result["learning"] = [_learning(issue_id, token) for issue_id, token in tokens]
            result["limits"]["truncated"] |= truncated
            result["sources"]["learning"] = _source(
                "fresh",
                observed_at,
                "At most 200 retained retrospective outcomes; configured idle expiry is seven days by default. Old records may lack provenance. GitHub approval/merge state is not collected."
                + (" Retrospective scan was truncated." if truncated else ""),
            )
        except Exception:
            result["sources"]["learning"] = _source(
                "unavailable",
                None,
                "Retrospective records could not be read; an empty list does not indicate no outcomes.",
            )

    def _telemetry(self, result: dict, observed_at: str) -> None:
        failures = []
        successes = 0
        try:
            totals = self._get(state.TOTALS_KEY)
            if totals is not None:
                if not isinstance(totals, dict):
                    raise ValueError("Invalid totals")
                result["telemetry"]["totals"] = {
                    "input": _count(totals.get("input")),
                    "output": _count(totals.get("output")),
                    "cacheRead": _count(totals.get("cache_read")),
                    "cacheWrite": _count(totals.get("cache_write")),
                    "total": _count(totals.get("total")),
                    "costEstimate": _number(totals.get("cost_estimate")),
                }
            successes += 1
        except Exception:
            failures.append("Usage totals unavailable.")
        try:
            rollups, truncated = self._records(state.TELEMETRY_PREFIX)
            models: dict[str, dict] = {}
            for _, rollup in rollups:
                buckets = rollup.get("by_model") or {}
                if not isinstance(buckets, dict):
                    raise ValueError("Invalid model rollup")
                for model, bucket in islice(buckets.items(), MAX_MODELS + 1):
                    if not _name(model) or not isinstance(bucket, dict):
                        continue
                    # Eight named buckets and one overflow bucket, matching ingest.
                    name = model if model in models or len(models) < MAX_MODELS else OTHER
                    target = models.setdefault(
                        name, {"model": name, "requests": 0, "input": 0, "output": 0}
                    )
                    for key in ("requests", "input", "output"):
                        value = _count(bucket.get(key))
                        target[key] = (
                            target[key] + value
                            if value is not None and target[key] is not None
                            else None
                        )
            result["telemetry"]["models"] = sorted(models.values(), key=lambda row: row["model"])
            result["limits"]["truncated"] |= truncated
            successes += 1
            if truncated:
                failures.append("Model rollup scan was truncated.")
        except Exception:
            failures.append("Model rollups unavailable.")
        message = "USD cost estimates use the engine's bundled price table, not provider billing. Totals are retained RPC usage, exclude unreported auxiliary calls, and are not a time-window total. Run tokens are cumulative retained issue usage, not isolated dispatch usage. Models cover at most 200 retained rollups, capped at eight models plus other; cleanup deletes rollups. Missing totals remain unknown. "
        status = "unavailable" if not successes else "stale" if failures else "fresh"
        result["sources"]["telemetry"] = _source(
            status, None if status == "unavailable" else observed_at, message + " ".join(failures)
        )

    def _metrics(self, result: dict, observed_at: str, now: dt.datetime) -> None:
        monthly = []
        try:
            year, month = now.year, now.month
            for _ in range(MONTH_LIMIT):
                key = f"{year:04d}{month:02d}"
                metric = self._get(state.metrics_key(key))
                if metric is not None:
                    if not isinstance(metric, dict):
                        raise ValueError("Invalid monthly metric")
                    monthly.append(
                        {
                            "month": f"{year:04d}-{month:02d}",
                            "dispatched": _count(metric.get("dispatched")),
                            "rework": _count(metric.get("rework")),
                            "prsOpened": _count(metric.get("prs_opened")),
                            "prsMerged": _count(metric.get("prs_merged")),
                            "learningPrs": _count(metric.get("learnings_prs")),
                        }
                    )
                month -= 1
                if month == 0:
                    year, month = year - 1, 12
            result["telemetry"]["monthly"] = list(reversed(monthly))
            result["sources"]["metrics"] = _source(
                "fresh",
                observed_at,
                "Only retained monthly counters from the last twelve calendar months; absent months/counters are unknown, not zero. PR counters are recorded during retrospectives, not a live GitHub inventory. No hourly or seven-day history is retained.",
            )
        except Exception:
            result["sources"]["metrics"] = _source(
                "unavailable",
                None,
                "Monthly counters could not be read; chart data is unavailable.",
            )

    def _resources(self, result: dict, observed_at: str) -> None:
        failures = []
        successes = 0
        for client, method, kind, cap in (
            (self.core, "list_namespaced_pod", "Pod", 67),
            (self.batch, "list_namespaced_job", "Job", 67),
            (self.core, "list_namespaced_persistent_volume_claim", "PersistentVolumeClaim", 66),
        ):
            try:
                if client is None:
                    raise ValueError("Kubernetes client unavailable")
                response = getattr(client, method)(self.namespace, limit=cap, _request_timeout=5)
                items = _attr(response, "items", [])
                metadata = _attr(response, "metadata")
                truncated = (
                    bool(_attr(metadata, "_continue") or _attr(metadata, "continue"))
                    or len(items) > cap
                )
                result["limits"]["truncated"] |= truncated
                for item in items[:cap]:
                    resource, service = self._resource(item, kind)
                    result["resources"].append(resource)
                    if service:
                        result["services"].append(service)
                successes += 1
                if truncated:
                    failures.append(f"{kind} list truncated.")
            except Exception:
                failures.append(f"{kind} list unavailable.")
        status = "unavailable" if not successes else "stale" if failures else "fresh"
        result["sources"]["kubernetes"] = _source(
            status,
            observed_at if successes else None,
            "Namespace-scoped inventory: at most 67 Pods, 67 Jobs, 66 PVCs; one page per kind, five-second request timeout. Readiness is from resource status, not a network health probe. "
            + " ".join(failures),
        )

    @staticmethod
    def _resource(item: Any, kind: str) -> tuple[dict, dict | None]:
        metadata = _attr(item, "metadata")
        status = _attr(item, "status")
        labels = _attr(metadata, "labels") or {}
        component = _name(labels.get("component"))
        name = _name(_attr(metadata, "name")) or "unknown"
        service_status = "unknown"
        active = None
        if kind == "Pod":
            phase = _attr(status, "phase")
            phase = (
                phase
                if phase in {"Pending", "Running", "Succeeded", "Failed", "Unknown"}
                else "Unknown"
            )
            ready = next(
                (
                    _attr(condition, "status")
                    for condition in (_attr(status, "conditions") or [])
                    if _attr(condition, "type") == "Ready"
                ),
                None,
            )
            ready = ready if ready in {"True", "False", "Unknown"} else "Unknown"
            detail = {
                "Running": "ready" if ready == "True" else "not_ready",
                "Pending": "pending",
                "Failed": "failed",
                "Succeeded": "succeeded",
            }.get(phase, "unknown")
            if phase == "Running" and ready == "True":
                service_status = "healthy"
            elif phase in {"Pending", "Failed"} or (phase == "Running" and ready == "False"):
                service_status = "degraded"
        elif kind == "Job":
            conditions = _attr(status, "conditions") or []
            complete = any(
                _attr(c, "type") == "Complete" and _attr(c, "status") == "True" for c in conditions
            )
            failed = any(
                _attr(c, "type") == "Failed" and _attr(c, "status") == "True" for c in conditions
            )
            active = _count(_attr(status, "active"))
            phase = (
                "Failed"
                if failed
                else "Complete"
                if complete
                else "Active"
                if active
                else "Unknown"
            )
            detail = phase.lower()
        else:
            phase = _attr(status, "phase")
            phase = phase if phase in {"Pending", "Bound", "Lost"} else "Unknown"
            detail = phase.lower()
        resource = {
            "name": name,
            "kind": kind,
            "status": phase,
            "component": component,
            "detail": detail,
            "activePods": active if kind == "Job" else None,
            "createdAt": _iso(
                _attr(metadata, "creation_timestamp") or _attr(metadata, "creationTimestamp")
            ),
        }
        service = (
            {"name": name, "role": _ROLES.get(component, "other"), "status": service_status,
             "detail": detail}
            if kind == "Pod"
            else None
        )
        return resource, service

    @staticmethod
    def _repositories(
        runs: list[dict], learning: list[dict], values: dict, *, state_available: bool
    ) -> list[dict]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        routes: dict[str, set[str]] = defaultdict(set)
        for run in runs:
            if run["repo"]:
                grouped[run["repo"]].append(run)
                routes[run["repo"]].add("issue")
        for proposal in learning:
            if proposal["repo"]:
                routes[proposal["repo"]].add("retrospective")
        default = _repo(values.get("kubernetes.default_repo"))
        if default:
            routes[default].add("default")
        repositories = []
        for repo in sorted(routes):
            observed = grouped[repo]
            runtimes = {run["runtime"] for run in observed}
            runtime = next(iter(runtimes)) if len(runtimes) == 1 else None
            repositories.append(
                {
                    "name": repo,
                    "runtime": runtime,
                    # How Symphony knows this repo: `issue` (a tracked issue routes to it),
                    # `retrospective` (a retained retrospective), `default` (the workflow's
                    # default_repo). `issues` counts tracked runs only.
                    "routes": sorted(routes[repo]),
                    "issues": len(observed) if state_available else None,
                }
            )
        return repositories


def _utc_midnight_ms(now_ms: int) -> int:
    day = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(day.timestamp() * 1000)


class LiveReader:
    """Hot reads for the control API (overview, issues): Redis only, a handful of round trips.

    Unlike `AdminProjection` it never touches Kubernetes or the workflow ConfigMap, so the Tern
    panel can poll it every two seconds. Like it, reads bypass RedisKV's idle-TTL renewal.
    """

    def __init__(
        self,
        client,
        *,
        sessions,
        teamconfig,
        store,
        merger=None,
        review_state: str = "In Review",
        merging_state: str = "Merging",
    ) -> None:
        self.client = client
        self.sessions = sessions
        self.teamconfig = teamconfig
        self.store = store
        self.merger = merger
        self.review_state = review_state.casefold()
        self.merging_state = merging_state.casefold()

    def issue_entries(self) -> list[tuple[str, dict]]:
        keys = sorted(set(self.client.scan_iter(match=f"{state.ISSUE_PREFIX}*", count=1000)))
        if not keys:
            return []
        out = []
        for key, raw in zip(keys, self.client.mget(keys), strict=True):
            if raw is None or len(raw) > MAX_RECORD_BYTES:
                continue
            entry = json.loads(raw)
            if isinstance(entry, dict):
                out.append((key[len(state.ISSUE_PREFIX) :], entry))
        return out

    def _attempt_sessions(self, entries: list[tuple[str, dict]]) -> list[dict | None]:
        sids = [
            f"{issue_id}:{entry['dispatch_seq']}" if _count(entry.get("dispatch_seq")) else None
            for issue_id, entry in entries
        ]
        metas = self.sessions.metas(sid for sid in sids if sid)
        return [metas.get(sid) if sid else None for sid in sids]

    def _updated_since(self, since_ms: int) -> list[dict]:
        return list(self.sessions.metas(self.sessions.updated_since(since_ms)).values())

    def _phase(self, entry: dict, meta: dict | None) -> str | None:
        tracker_state = entry.get("tracker_state")
        folded = tracker_state.casefold() if isinstance(tracker_state, str) else ""
        if meta is not None and meta.get("state") == "waiting_input":
            return "waiting_input"
        if folded == self.merging_state:
            return "merging"
        if folded == self.review_state:
            return "in_review"
        return {
            "running": "running",
            "interrupting": "running",
            "claimed": "queued",
            "retry_queued": "retrying",
            "parked": "in_review",
        }.get(entry.get("status"))

    def holds(self) -> dict[str, dict]:
        """Tickets a person took over (internals §12): issue id → `{by, reason, at}` (ISO `at`).
        A plain read: going through RedisKV.get would put a TTL on a hold, which has none."""
        keys = sorted(set(self.client.scan_iter(match=f"{state.HOLD_PREFIX}*", count=1000)))
        if not keys:
            return {}
        out = {}
        for key, raw in zip(keys, self.client.mget(keys), strict=True):
            try:
                record = json.loads(raw) if raw else None
            except ValueError:
                continue
            if isinstance(record, dict):
                out[key[len(state.HOLD_PREFIX) :]] = {
                    "by": _text(record.get("by"), 128),
                    "reason": _text(record.get("reason"), 1000),
                    "at": _iso(record.get("at")),
                }
        return out

    def overview(self, now_ms: int) -> dict:
        entries = self.issue_entries()
        metas = self._attempt_sessions(entries)
        holds = self.holds()
        counts = dict.fromkeys(
            ("running", "waiting_input", "queued", "retrying", "in_review", "merging", "held"), 0
        )
        identifiers = {}
        for (issue_id, entry), meta in zip(entries, metas, strict=True):
            identifiers[issue_id] = _text(entry.get("identifier"), 160)
            if issue_id in holds:
                continue  # a person owns it now; counted once below
            phase = self._phase(entry, meta)
            if phase:
                counts[phase] += 1
        # Includes tickets held before Symphony ever tracked them.
        counts["held"] = len(holds)
        heartbeat = self.client.get(state.HEARTBEAT_KEY)
        heartbeat = json.loads(heartbeat) if heartbeat is not None else None
        if not isinstance(heartbeat, int) or isinstance(heartbeat, bool):
            heartbeat = None
        tokens_today, cost_today = 0, 0.0
        # A session that spans midnight counts entirely toward the day it was last updated.
        for meta in self._updated_since(_utc_midnight_ms(now_ms)):
            tokens = meta.get("tokens")
            if isinstance(tokens, dict):
                tokens_today += _count(tokens.get("total")) or 0
                cost_today += _number(tokens.get("cost")) or 0.0
        merger = {"reviewing": 0, "train": 0, "parked": 0}
        if self.merger is not None:
            # One merger per repository: the panel shows the namespace total.
            for repo in self.merger.repos():
                repo_counts = self.merger.store(repo).counts()
                for key in merger:
                    merger[key] += int(repo_counts.get(key, 0))
        return {
            "daemon": {
                "alive": heartbeat is not None and 0 <= now_ms - heartbeat <= HEARTBEAT_STALE_MS,
                "paused": self.store.paused(),
                "heartbeat_at": _iso(heartbeat),
                # "incomplete" | "ready" as the daemon last reported, None if it never did.
                "setup": self._daemon_setup(),
            },
            "counts": counts,
            "tokens_today": tokens_today,
            "cost_today": round(cost_today, 6),
            "merger": merger,
            "config_generation": self.teamconfig.get()["generation"],
            "rate_limits": self.rate_limits(now_ms),
            "held": [
                {"issue_id": issue_id, "identifier": identifiers.get(issue_id), **hold}
                for issue_id, hold in sorted(holds.items(), key=lambda item: item[1]["at"] or "")
            ],
        }

    def _daemon_setup(self) -> str | None:
        raw = self.client.get(state.SETUP_KEY)
        try:
            record = json.loads(raw) if raw else None
        except ValueError:
            return None
        status = record.get("status") if isinstance(record, dict) else None
        return status if status in ("incomplete", "ready") else None

    def rate_limits(self, now_ms: int) -> list[dict]:
        """Provider limits the runner and merger observed in the last hour (internals §10)."""
        raw = self.client.hgetall(f"sym:{self.sessions.ns}:ratelimits")
        out = []
        for model, value in raw.items():
            try:
                entry = json.loads(value)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            observed = _iso(entry.get("observed_at"))
            if observed is None:
                continue
            age = now_ms - int(dt.datetime.fromisoformat(observed).timestamp() * 1000)
            if not -CLOCK_SKEW_MS <= age < RATE_LIMIT_WINDOW_MS:
                continue
            out.append({
                "model": _name(model) or model[:160],
                "stage": _text(entry.get("stage"), 40),
                "remaining": _number(entry.get("remaining")),
                "limit": _number(entry.get("limit")),
                "reset_at": _iso(entry.get("reset_at")),
                "retry_after_ms": _count(entry.get("retry_after_ms")),
                "observed_at": observed,
                "source": _text(entry.get("source"), 40),
            })
        out.sort(key=lambda item: item["observed_at"], reverse=True)
        return out

    def issues(self) -> list[dict]:
        entries = self.issue_entries()
        metas = self._attempt_sessions(entries)
        holds = self.holds()
        items = []
        for (issue_id, entry), meta in zip(entries, metas, strict=True):
            run = _run(issue_id, entry)
            if run is None:
                continue
            items.append(
                {
                    "issue_id": issue_id,
                    "identifier": run["id"],
                    "title": run["title"],
                    "repo": run["repo"],
                    "status": run["status"],
                    "phase": "held" if issue_id in holds else self._phase(entry, meta),
                    "held": holds.get(issue_id),
                    "tracker_state": _text(entry.get("tracker_state"), 80),
                    "attempt": run["attempt"],
                    "seq": run["seq"],
                    "session_id": f"{issue_id}:{run['seq']}",
                    "session_state": meta.get("state") if meta else None,
                    "input_request": meta.get("input_request") if meta else None,
                    "model": run["model"],
                    "tokens": run["tokens"],
                    "cost": run["cost"],
                    "pr_url": run["prUrl"],
                    "reason": ROUTE_REASON_CODES.get(entry.get("undispatchable_reason")),
                    "updated_at": run["updatedAt"],
                    "evidence_count": len(meta.get("evidence") or []) if meta else 0,
                }
            )
        items.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        return items
