"""Session telemetry — pure over OTLP payloads and state dicts.

Two halves, both pure:

- `fold_export` turns one decoded OTLP trace batch into a BOUNDED rollup. Bounded is the
  whole design constraint: a four-hour session emits spans without limit, and the rollup
  lives in shared state that has no business growing with them. Every field here is a
  counter, a duration, or a map with a hard cardinality cap.
- `render_session_report` turns a rollup plus the existing token ledger into the comment a
  reviewer reads on the ticket.

The engine is Oh My Pi's native OpenTelemetry GenAI exporter, which speaks OTLP/HTTP
protobuf only. `decode_export` maps either wire encoding onto the OTLP/JSON shape, so the
fold reads one representation.

The rollup has no field capable of holding a prompt, a response, or a tool argument. That is
the enforcement mechanism for "telemetry carries no content" — a shape that cannot express
it, rather than a filter that can be forgotten. Raising the agent's capture level for a
debugging session changes what the spans carry, never what is stored here.
"""

from __future__ import annotations

import json

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

# Cardinality caps. Tool and model names come from the agent's own registry, so overflow is
# not an expected path — but one misbehaving extension registering a name per call must not
# be able to grow orchestrator state without bound.
MAX_TOOLS = 24
MAX_MODELS = 8
OTHER = "other"

# OMP tags every span with its GenAI operation; span names embed model/tool ids and are not
# a stable key. `chat` is one provider call of the agent loop; `judgment` is an auxiliary
# model call (classification/summary) that the RPC event stream never reports.
ATTR_OPERATION = "gen_ai.operation.name"
OP_AGENT = "invoke_agent"
OP_MODEL_CALLS = ("chat", "judgment")
OP_TOOL = "execute_tool"
# Stamped on `invoke_agent` when the loop finishes: one step = one model call plus the
# tools it requested.
ATTR_STEP_COUNT = "omp.gen_ai.agent.step.count"

ATTR_ISSUE_ID = "symphony.issue_id"
ATTR_DISPATCH_SEQ = "symphony.dispatch_seq"
ATTR_IDENTIFIER = "symphony.identifier"

_COUNTERS = (
    "spans", "interactions", "turns", "llm_requests", "tool_calls", "errors",
    "llm_ms", "tool_ms", "batches",
)


def new_rollup() -> dict:
    rollup: dict = dict.fromkeys(_COUNTERS, 0)
    rollup.update(by_tool={}, by_model={}, first_ts=0, last_ts=0)
    return rollup


# -- OTLP decoding -----------------------------------------------------------
# The fold reads the OTLP/JSON shape. Two of its encoding rules a hand-written reader gets
# wrong by default: 64-bit integers travel as STRINGS, and attribute values are tagged unions.

PROTOBUF = "application/x-protobuf"
JSON = "application/json"


def decode_export(body: bytes, media_type: str) -> dict | None:
    """Decode an OTLP/HTTP trace export body of a lowercase media type, or None when it is
    not one.

    Protobuf is mapped through the canonical proto3 JSON mapping, which is exactly OTLP/JSON
    for the fields read here (ids differ — base64 vs hex — and only their presence is read).
    """
    if media_type == PROTOBUF:
        try:
            request = ExportTraceServiceRequest.FromString(body)
        except DecodeError:
            return None
        return MessageToDict(request, use_integers_for_enums=True)
    if media_type == JSON:
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _int(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    if isinstance(value, float):
        return int(value)
    return 0


def _attr_value(wrapped) -> object:
    """Unwrap {"stringValue": ...} / {"intValue": "42"} / {"boolValue": true} / ..."""
    if not isinstance(wrapped, dict):
        return None
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in wrapped:
            return wrapped[key]
    if "intValue" in wrapped:
        return _int(wrapped["intValue"])
    return None


def _attrs(items) -> dict:
    out: dict = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = _attr_value(item.get("value"))
    return out


def attempt_attrs(payload: dict) -> tuple[str | None, int | None]:
    """The (issue_id, dispatch_seq) the batch claims to belong to.

    Read from resource attributes, which the runner sets via OTEL_RESOURCE_ATTRIBUTES. A
    payload carrying neither is unattributable and must be refused rather than guessed at:
    guessing would file one attempt's spans under another's rollup.
    """
    for resource_span in (payload.get("resourceSpans") or []):
        if not isinstance(resource_span, dict):
            continue
        attrs = _attrs((resource_span.get("resource") or {}).get("attributes"))
        issue_id = attrs.get(ATTR_ISSUE_ID)
        if isinstance(issue_id, str) and issue_id:
            raw_seq = attrs.get(ATTR_DISPATCH_SEQ)
            seq = _int(raw_seq) if raw_seq is not None else None
            return issue_id, seq
    return None, None


def _iter_spans(payload: dict):
    for resource_span in (payload.get("resourceSpans") or []):
        if not isinstance(resource_span, dict):
            continue
        for scope_span in (resource_span.get("scopeSpans") or []):
            if not isinstance(scope_span, dict):
                continue
            for span in (scope_span.get("spans") or []):
                if isinstance(span, dict):
                    yield span


def _bump(bucket: dict, key: str, cap: int, fields: dict) -> dict:
    """Accumulate into a capped map, folding overflow into a single `other` entry.
    Returns the slot written to, which is not necessarily the one named."""
    if key not in bucket and len(bucket) >= cap:
        key = OTHER
    entry = bucket.setdefault(key, {})
    for field, delta in fields.items():
        entry[field] = entry.get(field, 0) + delta
    return entry


def fold_export(rollup: dict | None, payload: dict) -> dict:
    """Fold one decoded OTLP trace batch into the rollup. Never raises on malformed input:
    a batch we cannot read is a batch we drop, and dropping telemetry is always preferable
    to raising on a path the agent cannot see (FR-015)."""
    out = dict(new_rollup(), **(rollup or {}))
    out["by_tool"] = dict(out.get("by_tool") or {})
    out["by_model"] = dict(out.get("by_model") or {})
    out["batches"] = out.get("batches", 0) + 1

    for span in _iter_spans(payload):
        name = span.get("name")
        if not isinstance(name, str):
            continue
        attrs = _attrs(span.get("attributes"))
        operation = attrs.get(ATTR_OPERATION)
        start = _int(span.get("startTimeUnixNano"))
        end = _int(span.get("endTimeUnixNano"))
        # Clamp: a span whose end precedes its start (clock adjustment mid-span) must not
        # subtract from the totals.
        duration_ms = max(0, (end - start)) // 1_000_000

        out["spans"] += 1
        for ts in (start, end):
            if ts:
                out["first_ts"] = min(out["first_ts"] or ts, ts)
                out["last_ts"] = max(out["last_ts"], ts)

        status = span.get("status") or {}
        # OTLP status codes: 0 UNSET, 1 OK, 2 ERROR. Only ERROR is a failure; OMP sets it
        # for every tool status other than `ok` and for errored or aborted model calls.
        failed = _int(status.get("code")) == 2
        if failed:
            out["errors"] += 1

        if operation == OP_AGENT:
            # Subagents run their own loop under the parent's tool span. Only the root loop
            # is one prompt of this attempt; their model and tool spans still count below.
            if not span.get("parentSpanId"):
                out["interactions"] += 1
                out["turns"] += _int(attrs.get(ATTR_STEP_COUNT))
        elif operation in OP_MODEL_CALLS:
            out["llm_requests"] += 1
            out["llm_ms"] += duration_ms
            model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model")
            _bump(
                out["by_model"],
                str(model) if isinstance(model, str) and model else "unknown",
                MAX_MODELS,
                {
                    "requests": 1,
                    "input": _int(attrs.get("gen_ai.usage.input_tokens")),
                    "output": _int(attrs.get("gen_ai.usage.output_tokens")),
                },
            )
        elif operation == OP_TOOL:
            out["tool_calls"] += 1
            out["tool_ms"] += duration_ms
            tool = attrs.get("gen_ai.tool.name")
            slot = _bump(
                out["by_tool"],
                str(tool) if isinstance(tool, str) and tool else "unknown",
                MAX_TOOLS,
                {"calls": 1, "ms": duration_ms, "errors": 1 if failed else 0},
            )
            slot["max_ms"] = max(slot.get("max_ms", 0), duration_ms)

    return out


def wall_ms(rollup: dict | None) -> int:
    if not rollup:
        return 0
    return max(0, (rollup.get("last_ts", 0) - rollup.get("first_ts", 0))) // 1_000_000


# -- the review report -----------------------------------------------------


def has_recorded_run(entry: dict) -> bool:
    """Did an attempt actually run? A claim that never reached a workspace and never spent a
    token has nothing to report, and a report saying so would be noise on every ticket a
    human moves straight to review (FR-007)."""
    if entry.get("sandbox_name") or entry.get("session_id") or entry.get("session_path"):
        return True
    tokens = entry.get("tokens") or {}
    return any(tokens.get(field) for field in ("input", "output", "total"))


def _human_ms(ms: int) -> str:
    if ms <= 0:
        return "—"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def render_session_report(entry: dict, rollup: dict | None = None) -> str:
    """The comment a reviewer reads. Renders with or without a rollup: the token half comes
    from the ledger, which is collected over a channel that is not best-effort, so the report
    degrades to it whenever telemetry is off, unreachable, or pointed elsewhere."""
    tokens = entry.get("tokens") or {}
    runs = entry.get("runs") or entry.get("attempt") or 1
    lines = ["**Symphony — session report**", ""]

    elapsed = 0
    claimed_at, updated_at = entry.get("claimed_at"), entry.get("updated_at")
    if isinstance(claimed_at, int) and isinstance(updated_at, int):
        elapsed = max(0, updated_at - claimed_at)

    lines.append(f"- Runs: **{runs}**" + (f" · elapsed **{_human_ms(elapsed)}**" if elapsed else ""))

    total = _int(tokens.get("total"))
    if total or tokens.get("input") or tokens.get("output"):
        detail = (
            f"{_thousands(_int(tokens.get('input')))} in · "
            f"{_thousands(_int(tokens.get('output')))} out"
        )
        cached = _int(tokens.get("cache_read"))
        if cached:
            detail += f" · {_thousands(cached)} cached"
        lines.append(f"- Tokens: **{_thousands(total)}** ({detail})")
    else:
        # An attempt that failed before its first model request. Saying "0 tokens" without
        # this note reads as a free successful run.
        lines.append("- Tokens: **none recorded** — no model request completed")

    cost = tokens.get("cost_estimate") or 0.0
    lines.append(
        f"- Cost: **~US$ {cost:.4f}** — *estimate*, computed locally from the engine's "
        "bundled price table, not a billed charge"
    )
    replaced = entry.get("repos_changed_from")
    if isinstance(replaced, list):
        was = ", ".join(f"`{repo}`" for repo in replaced) or "another repository"
        lines.append(f"- Workspace: rebuilt when the ticket's repositories changed (was {was})")
    # One line per run: the attached trace (`sessiontrace`) or why it is not there.
    for trace in entry.get("traces") or []:
        if not isinstance(trace, dict):
            continue
        if trace.get("url"):
            redacted = _int(trace.get("redactions"))
            lines.append(
                f"- **Session trace**: {trace['url']} (run {trace.get('run', '?')}, "
                f"{redacted} {'redaction' if redacted == 1 else 'redactions'})"
            )
        elif trace.get("note"):
            lines.append(f"- Session trace (run {trace.get('run', '?')}): {trace['note']}")

    if rollup and rollup.get("spans"):
        lines += ["", "**Session trace**", ""]
        lines.append(
            f"- {rollup.get('llm_requests', 0)} model requests · "
            f"{rollup.get('tool_calls', 0)} tool calls · "
            f"{rollup.get('turns', 0)} turns"
        )
        lines.append(
            f"- Time: **{_human_ms(rollup.get('llm_ms', 0))}** in the model · "
            f"**{_human_ms(rollup.get('tool_ms', 0))}** in tools · "
            f"span of **{_human_ms(wall_ms(rollup))}**"
        )
        if rollup.get("errors"):
            lines.append(f"- Errored spans: **{rollup['errors']}**")

        busiest = sorted(
            (rollup.get("by_tool") or {}).items(),
            key=lambda kv: kv[1].get("ms", 0),
            reverse=True,
        )[:5]
        if busiest:
            lines += ["", "| Tool | Calls | Time | Slowest |", "|---|---:|---:|---:|"]
            for tool, agg in busiest:
                lines.append(
                    f"| `{tool}` | {agg.get('calls', 0)} | {_human_ms(agg.get('ms', 0))} "
                    f"| {_human_ms(agg.get('max_ms', 0))} |"
                )
    elif rollup is not None:
        lines += ["", "*No trace data was collected for this run.*"]

    return "\n".join(lines)
