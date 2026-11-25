"""Text output of the CLI: tables, the overview and session summaries."""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Sequence

from symphony.cli.acp_map import STATE_LABEL

KIND_LABEL = {
    "implement": "implementation",
    "answer": "answer",
    "review": "review",
    "oracle": "oracle",
    "arbiter": "arbiter",
    "resolve": "conflict",
    "reflect": "retrospective",
}


def state_label(state: str | None) -> str:
    return STATE_LABEL.get(state or "", state or "—")


def to_ms(value) -> float | None:
    """Epoch milliseconds from what the API sends: store fields are epoch ms, admin views send
    ISO 8601 (`2026-11-16T00:46:41.341000Z`)."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        instant = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=dt.UTC)
    return instant.timestamp() * 1000


def ago(value, now_ms: float | None = None) -> str:
    ms = to_ms(value)
    if not ms:
        return "—"
    now = time.time() * 1000 if now_ms is None else now_ms
    secs = max(0, int((now - ms) / 1000))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def num(value) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def money(value) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], empty: str = "Nothing to show.") -> str:
    if not rows:
        return empty
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) for i, h in enumerate(headers)]
    def line(values: Sequence[str]) -> str:
        return "  ".join(v.ljust(w) for v, w in zip(values, widths, strict=True)).rstrip()
    return "\n".join([line(headers), *(line(r) for r in cells)])


def session_label(meta: dict) -> str:
    """`ENG-1` for attempts, `PR #12 · review` for merger agents."""
    if meta.get("identifier"):
        return str(meta["identifier"])
    sid = str(meta.get("sid", ""))
    if sid.startswith("merger:"):
        parts = sid.split(":")
        if len(parts) >= 3:
            return f"PR #{parts[1]} · {KIND_LABEL.get(meta.get('kind', ''), parts[2])}"
    return sid


def session_title(meta: dict) -> str:
    """The chat title in Tern: `ENG-1 · <title> · <state>`."""
    parts = [session_label(meta)]
    if meta.get("title"):
        parts.append(str(meta["title"]))
    parts.append(state_label(meta.get("state")))
    return " · ".join(parts)


def sessions_table(items: list[dict]) -> str:
    rows = []
    for m in items:
        tokens = m.get("tokens") or {}
        rows.append([
            session_label(m),
            KIND_LABEL.get(m.get("kind", ""), m.get("kind", "—")),
            state_label(m.get("state")),
            str(m.get("turn", "—")),
            num(tokens.get("total")),
            money(tokens.get("cost")) if tokens.get("cost") is not None else "—",
            ago(m.get("updated_at")),
            str(m.get("sid", "")),
        ])
    return table(["TICKET", "KIND", "STATE", "TURN", "TOKENS", "COST", "UPDATED", "SESSION"],
                 rows, empty="No sessions.")


def overview(doc: dict) -> str:
    daemon = doc.get("daemon") or {}
    counts = doc.get("counts") or {}
    merger = doc.get("merger") or {}
    if not daemon.get("alive"):
        d = "down"
    elif daemon.get("paused"):
        d = "paused"
    else:
        d = "up"
    lines = [
        f"Symphony: daemon {d} (heartbeat {ago(daemon.get('heartbeat_at'))})",
        "",
        table(["RUNNING", "WAITING", "QUEUED", "RETRYING", "IN REVIEW", "MERGING", "HELD"], [[
            str(counts.get("running", 0)), str(counts.get("waiting_input", 0)),
            str(counts.get("queued", 0)), str(counts.get("retrying", 0)),
            str(counts.get("in_review", 0)), str(counts.get("merging", 0)),
            str(counts.get("held", 0)),
        ]]),
        "",
        f"Today: {num(doc.get('tokens_today'))} tokens · {money(doc.get('cost_today'))}",
        f"Merger: {_count(merger.get('reviewing'))} in review · {_count(merger.get('train'))} in the"
        f" train · {_count(merger.get('parked'))} parked",
        f"Team config: generation {doc.get('config_generation', 0)}",
    ]
    limits = doc.get("rate_limits") or []
    if limits:
        lines += ["", "Provider limits (last hour):"]
        lines += [f"  {rate_limit(item)}" for item in limits]
    held = doc.get("held")
    if held:
        lines += ["", "Held by a person (Symphony does not work on these until `symphony release`):"]
        lines += [f"  {_held_line(item)}" for item in held]
    if counts.get("waiting_input"):
        lines += ["", "Agents are waiting for an answer: run `symphony sessions --state waiting_input`."]
    return "\n".join(lines)


def held_text(held: dict | None) -> str:
    """`held: {by, reason, at}` (internals §12) as a short cell."""
    if not held:
        return "—"
    return f"by {held.get('by') or '?'} {ago(held.get('at'))}"


def _held_line(item: dict) -> str:
    ticket = item.get("identifier") or item.get("issue_id") or "?"
    reason = f": {item['reason']}" if item.get("reason") else ""
    return f"{ticket} {held_text(item)}{reason}"


def until(value, now_ms: float | None = None) -> str | None:
    ms = to_ms(value)
    if ms is None:
        return None
    now = time.time() * 1000 if now_ms is None else now_ms
    secs = max(0, int((ms - now) / 1000))
    if secs < 60:
        return f"in {secs}s"
    if secs < 3600:
        return f"in {secs // 60}m"
    return f"in {secs // 3600}h"


_LIMIT_SOURCE = {"usage_limit": "quota", "retry": "throttling"}


# §10 stages as `usage.py` writes them: omp's `usageLimit.stage` and 429 retries.
_STAGE_LABEL = {"low_priority": "low priority (quota almost used)",
                "wrap_up": "wrapping up (quota nearly used)",
                "throttled": "throttled by the provider"}


def rate_limit(item: dict, now_ms: float | None = None) -> str:
    """One `overview.rate_limits` entry (internals §10)."""
    parts = [f"{item.get('model', '?')}: {_label(_STAGE_LABEL, item.get('stage'))}"]
    if item.get("remaining") is not None:
        total = f"/{num(item['limit'])}" if item.get("limit") is not None else ""
        parts.append(f"{num(item['remaining'])}{total} left")
    if item.get("reset_at") is not None:
        when = until(item["reset_at"], now_ms)
        if when:
            parts.append(f"resets {when}")
    elif item.get("retry_after_ms") is not None:
        parts.append(f"next retry in {max(1, int(item['retry_after_ms']) // 1000)}s")
    source = _LIMIT_SOURCE.get(item.get("source", ""), item.get("source"))
    parts.append(f"{source}, seen {ago(item.get('observed_at'), now_ms)}" if source
                 else f"seen {ago(item.get('observed_at'), now_ms)}")
    return " · ".join(parts)


# API error codes (`{"error": {"code", "message"}}`) as people read them. The JSON under
# `--json` stays the API's own; this is only the text a person sees.
ERROR_TEXT = {
    "unauthorized": "The token is missing or invalid. Run `symphony login`.",
    "invalid_token": "The token is invalid, expired or revoked. Run `symphony login`.",
    "forbidden": "Your token does not allow this. Ask for a token with a higher role.",
    "not_found": "Not found.",
    "method_not_allowed": "This route does not support this operation.",
    "invalid_request": "The request is not valid.",
    "invalid_revision": "The If-Match revision is not valid.",
    "precondition_required": "The If-Match header with the current revision is missing.",
    "precondition_failed": "The configuration changed since you read it. Reload and try again.",
    "conflict": "Another change conflicts with this one. Reload and try again.",
    "review_mismatch": "The document does not match the reviewed changes. Review it again.",
    "stale_target": "The target changed since you read it. Reload and try again.",
    "session_ended": "This session has ended.",
    "already_held": "A person already holds this ticket.",
    "not_held": "This ticket is not held.",
    "rate_limited": "Too many attempts. Wait a moment and try again.",
    "unavailable": "Symphony is not available right now.",
    "store_busy": "Symphony is busy. Try again in a moment.",
    "store_unavailable": "Symphony storage is not available.",
    "audit_unavailable": "The audit could not be recorded. Nothing was changed.",
    "queue_full": "The command queue is full. Try again in a moment.",
    "lease_lost": "The daemon lost its lease. Try again.",
    "invalid_command": "The command is not valid.",
    "invalid_target": "The target is not valid.",
    "invalid_cursor": "The page cursor is not valid.",
    "invalid_limit": "The page limit is not valid.",
    "invalid_admission": "Admission refused the request.",
    "invalid_audit": "The audit metadata is not valid.",
    "workflow_unavailable": "WORKFLOW.md is not reachable right now.",
    "catalog_unavailable": "The model catalog is not reachable right now.",
    "providers_unavailable": "Providers are not reachable right now.",
    "providers_outcome_unknown": "Symphony could not confirm that the provider was saved. Reload before you save again.",
    "unknown_provider": "This provider is not in the omp catalog.",
    "invalid_provider": "The provider definition is not valid.",
    "provider_key_required": "This provider needs an API key.",
    "provider_model_missing": "This model does not exist for this provider.",
    "providers_conflict": "Providers changed since you read them. Try again.",
    "providers_full": "Provider storage is full.",
    "providers_invalid": "Provider storage is corrupt. Ask the host administrator.",
    "providers_not_bootstrapped": "Provider storage is not installed on the host yet.",
    "providers_rejected": "Kubernetes refused to save the providers.",
    "merger_unavailable": "The merger is not reachable right now.",
    "repo_required": "More than one repository has a merger. Name one with --repo.",
    "unknown_repo": "No merger serves this repository.",
    "webhook_unconfigured": "The webhook is not configured.",
    "projection_unavailable": "This view is not available on the server yet.",
    "linear_unauthorized": "Linear refused the API key. Create a personal API key in the Linear "
                           "settings and try again.",
    "linear_team_not_found": "Linear has no team with this key.",
    "linear_project_not_found": "Linear has no project with this slug.",
    "github_project_not_found": "GitHub has no such project, or the GitHub App cannot read it. "
                                "Check the owner, the project number and the App's project "
                                "permission.",
    "github_status_field_missing": "The GitHub project has no single-select field with this "
                                   "name. Check --status-field.",
    "github_app_required": "Set up the GitHub App first. The GitHub tracker uses it.",
    "invalid_github_app": "The App ID and the installation ID must be numbers.",
    "invalid_private_key": "The private key is not a valid PEM RSA key.",
    "github_app_unauthorized": "GitHub refused the App ID or the private key.",
    "github_installation_not_found": "This GitHub App has no installation with this ID.",
    "github_app_rejected": "GitHub refused the App.",
    "vendor_unreachable": "The service could not be reached. Try again in a moment.",
    "repo_not_installed": "The GitHub App is not installed on this repository.",
    "notification_unverified": "The test message failed, so nothing was saved. Check the token, "
                               "the channel or the URL.",
    "notifications_not_configured": "No Slack or Discord channel is configured.",
    "webhooks_exist": "Webhook secrets exist already. Run `symphony setup webhooks --rotate` to "
                      "replace them.",
    "secrets_unavailable": "Symphony cannot save secrets on this host.",
    "secret_unavailable": "Symphony could not read or write a Kubernetes Secret.",
    "secret_missing": "A Kubernetes Secret is missing. Apply the install manifests, then try "
                      "again.",
    "secret_forbidden": "Kubernetes does not let the API change this Secret. Check the API's "
                        "RBAC.",
    "restart_forbidden": "The secret was saved, but Kubernetes does not let the API restart the "
                         "components. Check the API's RBAC.",
    "restart_failed": "The secret was saved, but the components could not be restarted.",
    "host_env_overrides": "The host environment sets this value, and it wins over setup. Remove "
                          "it from the host environment first.",
    "slack_app_token_missing": "Give the Slack app-level token (xapp-…) on stdin or at the "
                               "prompt.",
    "slack_app_token_rejected": "Slack refused the app-level token. Turn on Socket Mode and use "
                                "an app-level token with the connections:write scope.",
    "slack_bot_token_rejected": "Slack refused the stored bot token. Run `symphony setup "
                                "notifications slack` again.",
    "slack_bot_token_missing": "The assistant uses the Slack bot token. Run `symphony setup "
                               "notifications slack --channel C…` first.",
    "discord_bot_token_rejected": "Discord refused the stored bot token. Run `symphony setup "
                                  "notifications discord` again.",
    "discord_bot_token_missing": "The assistant uses the Discord bot token. Run `symphony setup "
                                 "notifications discord --channel-id ID` first.",
    "message_content_intent_missing": "The Discord bot cannot read messages. In the Developer "
                                      "Portal, open the application, then Bot, and turn on "
                                      "Message Content Intent.",
    "invalid_mcp_server": "The MCP server definition is not valid.",
    "lease_not_held": "No live Symphony session holds this lease. Watch only a device that an "
                      "agent holds now.",
    "viewer_refused": "The MCP server refused the viewer link.",
    "viewer_unsupported": "This MCP server has no viewer.",
    "rejected": "The MCP server refused the stored credential. Store it again with "
                "`symphony admin mcp headers`.",
    "mcp_unreachable": "The MCP server could not be reached.",
}
# Codes whose server message names what was wrong (the team key, the project, the service,
# the Secret): the message follows the sentence, because the admin needs it to fix the value.
DETAIL_CODES = {"linear_team_not_found", "linear_project_not_found", "github_project_not_found",
                "github_status_field_missing", "github_app_rejected", "vendor_unreachable",
                "repo_not_installed", "notification_unverified", "secret_unavailable",
                "secret_missing", "secret_forbidden", "restart_forbidden", "restart_failed",
                "host_env_overrides", "invalid_mcp_server", "viewer_refused"}
_STATUS_TEXT = {400: ERROR_TEXT["invalid_request"], 401: ERROR_TEXT["unauthorized"],
                403: ERROR_TEXT["forbidden"], 404: ERROR_TEXT["not_found"],
                409: ERROR_TEXT["conflict"], 412: ERROR_TEXT["precondition_failed"],
                422: ERROR_TEXT["invalid_request"], 428: ERROR_TEXT["precondition_required"],
                429: ERROR_TEXT["rate_limited"], 503: ERROR_TEXT["unavailable"]}


# Codes the CLI raises itself (`client.py`), whose message is written for people already.
CLIENT_CODES = {"unreachable", "no_session"}


def error_text(code: str, status: int, message: str) -> str:
    """The line a person reads for an API error. The server's `message` is operator detail:
    it stays in the `--json` error document, except for `DETAIL_CODES`."""
    if code in CLIENT_CODES and message:
        return message
    text = ERROR_TEXT.get(code) or _STATUS_TEXT.get(status)
    if text and code in DETAIL_CODES and message:
        return f"{text} Details: {message}"
    if text:
        return text
    if 500 <= status < 600:
        return f"The Symphony server failed (HTTP {status})."
    return f"The server refused the request (HTTP {status})."


def _count(value) -> str:
    if isinstance(value, list | dict):
        return str(len(value))
    return str(value if value is not None else 0)


def session_detail(meta: dict) -> str:
    tokens = meta.get("tokens") or {}
    rows = [
        ("Session", meta.get("sid")),
        ("Ticket", session_label(meta)),
        ("Title", meta.get("title")),
        ("Kind", KIND_LABEL.get(meta.get("kind", ""), meta.get("kind"))),
        ("State", state_label(meta.get("state"))),
        ("Turn", meta.get("turn")),
        ("Tokens", f"{num(tokens.get('total'))} ({num(tokens.get('input'))} input, "
                   f"{num(tokens.get('output'))} output)" if tokens else None),
        ("Cost", money(tokens.get("cost")) if tokens.get("cost") is not None else None),
        ("Model", meta.get("model")),
        ("Repository", meta.get("repo")),
        ("Branch", meta.get("branch")),
        ("PR", meta.get("pr_url")),
        ("Updated", ago(meta.get("updated_at"))),
    ]
    out = [f"{k}: {v}" for k, v in rows if v not in (None, "")]
    req = meta.get("input_request")
    if req:
        out += ["", f"Open question: {req.get('question')}"]
        out += [f"  {i}. {o}" for i, o in enumerate(req.get("options") or [], 1)]
    if meta.get("last_message"):
        out += ["", f"Last message: {meta['last_message']}"]
    return "\n".join(out)


# `phase` codes, and the raw `status` shown when an issue has no phase.
_PHASE_LABEL = {"running": "running", "waiting_input": "waiting for an answer",
                "queued": "queued", "retrying": "retrying", "in_review": "in review",
                "merging": "merging", "held": "held", "blocked": "blocked", "claimed": "queued",
                "retry_queued": "retrying", "parked": "parked", "interrupting": "interrupting",
                "blocked_on_person": "waiting for a person"}
# Why an issue cannot be dispatched (`/v1/issues` `reason`).
_BLOCK_LABEL = {
    "no_repo_designation": "the ticket names no repository",
    "malformed_repo_designation": "the repository name is malformed",
    "mixed_owner_repo_designation": "the ticket's repositories have more than one owner",
    "repo_not_authorized": "Symphony is not authorized for the repository",
}


def issues_table(items: list[dict]) -> str:
    """`GET /v1/issues` items (`admin_read.issues`): `phase` is Symphony's view of the ticket,
    `tracker_state` the state on the tracker (Linear or GitHub Projects), `reason` why it
    cannot be dispatched (a code)."""
    rows = [[
        str(i.get("identifier") or "—"),
        _clip(str(i.get("title") or ""), 48),
        _label(_PHASE_LABEL, i.get("phase") or i.get("status")),
        str(i.get("tracker_state") or "—"),
        held_text(i.get("held")),
        _label(_BLOCK_LABEL, i.get("reason")),
        ago(i.get("updated_at")),
        str(i.get("pr_url") or "—"),
    ] for i in items]
    return table(["TICKET", "TITLE", "SYMPHONY", "TRACKER STATE", "HELD", "BLOCKED", "UPDATED",
                  "PR"],
                 rows, empty="No tracked tickets.")


def providers_table(doc: dict) -> str:
    """`GET /v1/admin/providers` (internals §13). The key is never in the document; the
    table only says whether one is stored."""
    def key(item: dict) -> str:
        if not item.get("builtin") and item.get("auth") == "none":
            return "not needed"
        return "yes" if item.get("credentialConfigured") else "no"

    rows = [[str(i.get("id") or "—"), "built-in" if i.get("builtin") else "custom",
             str(i.get("name") or "—"), str(i.get("api") or "—"), key(i)]
            for i in doc.get("items") or []]
    body = table(["ID", "TYPE", "NAME", "PROTOCOL", "KEY SET"], rows,
                 empty="No providers are configured. Models use only the host environment.")
    if doc.get("storageReady") is False:
        body += "\n\nWarning: provider storage is not installed on the host yet."
    return body


def tokens_table(items: list[dict]) -> str:
    rows = [[
        str(t.get("id", "")), str(t.get("name", "")), str(t.get("role", "")),
        ago(t.get("created_at")), _when(t.get("expires_at")), ago(t.get("last_used_at")),
    ] for t in items]
    return table(["ID", "NAME", "ROLE", "CREATED", "EXPIRES", "LAST USED"], rows,
                 empty="No tokens.")


def _when(value) -> str:
    ms = to_ms(value)
    if not ms:
        return "never"
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def config_doc(doc: dict) -> str:
    models = doc.get("models") or {}
    rows = [[role, f"{ref.get('provider')}/{ref.get('model')}", ref.get("thinking", "medium")]
            for role, ref in models.items()]
    lines = [f"Generation {doc.get('generation', 0)}"
             + (f" · by {doc['updated_by']} ({ago(doc.get('updated_at'))}): {doc.get('reason')}"
                if doc.get("updated_by") else ""), "",
             table(["ROLE", "MODEL", "THINKING"], rows)]
    omp = doc.get("omp") or {}
    lines += ["", f"omp {omp.get('version', '—')}"]
    if omp.get("settings"):
        lines.append("settings: " + json.dumps(omp["settings"], ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def observed(doc: dict, generation: int | None = None) -> str:
    rows = []
    for consumer, info in sorted(doc.items()):
        gen = info.get("generation")
        mark = "" if generation is None or gen == generation else "  (out of date)"
        rows.append([consumer, f"{gen}{mark}", ago(info.get("at"))])
    return table(["CONSUMER", "GENERATION", "WHEN"], rows, empty="No consumer has reported yet.")


# Merger codes, with the same words as the Tern panel (`tern-plugin/lib/views.luau`).
_MERGER_PHASE_LABEL = {"pin": "preparing the review", "review": "reviewing",
                       "verify": "verifying findings", "arbitrate": "arbitrating",
                       "ci": "waiting for CI", "resolve": "resolving a conflict"}
_TRAIN_STATE_LABEL = {"publishing": "publishing the review",
                      "reviewed": "reviewed, waiting for the train", "train": "in the train",
                      "resolve": "resolving a conflict", "finalizing": "finishing the merge"}
_PARKED_LABEL = {"clean": "clean review, waiting for CI",
                 "report": "will not merge, report published"}
_QUEUE_KIND_LABEL = {"bypass": "bypass", "rereview": "new review", "first": "first review"}


def _pr_head(item: dict) -> str:
    title = item.get("title")
    return f"#{item.get('pr', '?')}" + (f" {_clip(str(title), 60)}" if title else "")


def merger(doc: dict) -> str:
    """`GET /v1/merger`: one section per repository a merger has served."""
    repos = doc.get("repos") or []
    if not repos:
        return "No merger has registered a repository yet."
    blocks = []
    for entry in repos:
        alive = "running" if entry.get("alive") else "not responding"
        body = "\n".join(f"  {line}" if line else "" for line in merger_repo(entry).splitlines())
        blocks.append(f"{entry.get('repo', '?')} (merger {alive})\n{body}")
    return "\n\n".join(blocks)


def merger_repo(doc: dict) -> str:
    """One repository's merger snapshot (`{reviewing, parked, train, batch, queue}`)."""
    def section(title: str, items, line) -> list[str]:
        items = items or []
        if not items:
            return [f"{title}: none"]
        return [f"{title}:"] + [f"  {line(i)}" for i in items]

    def reviewing(r: dict) -> str:
        parts = [_pr_head(r), _label(_MERGER_PHASE_LABEL, r.get("phase"))]
        if r.get("worker"):
            parts.append(f"worker {r['worker']}")
        parts.append(f"since {ago(r.get('started_at'))}")
        return " · ".join(parts)

    def queued(q: dict) -> str:
        parts = [_pr_head(q)]
        if q.get("author"):
            parts.append(str(q["author"]))
        if q.get("kind"):
            parts.append(_label(_QUEUE_KIND_LABEL, q["kind"]))
        if q.get("ready_at"):
            parts.append(f"ready {ago(q['ready_at'])}")
        return " · ".join(parts)

    def parked(p: dict) -> str:
        return " · ".join([_pr_head(p), _label(_PARKED_LABEL, p.get("kind")),
                           f"since {ago(p.get('parked_at'))}"])

    def record(r: dict) -> str:
        state = "conflict" if r.get("conflict") else _label(_TRAIN_STATE_LABEL, r.get("state"))
        parts = [_pr_head(r), state]
        for key in ("branch", "author"):
            if r.get(key):
                parts.append(str(r[key]))
        if r.get("reviewed_at"):
            parts.append(f"reviewed {ago(r['reviewed_at'])}")
        return " · ".join(parts)

    lines = section("In review", doc.get("reviewing"), reviewing)
    lines += section("Queue", doc.get("queue"), queued)
    lines += section("Parked", doc.get("parked"), parked)
    train = doc.get("train") if isinstance(doc.get("train"), dict) else {}
    lease = train.get("lease") if isinstance(train.get("lease"), dict) else None
    lines.append(f"Train: lease held by {lease.get('owner')} · renews in "
                 f"{max(0, int(lease.get('expires_in_ms') or 0) // 1000)}s" if lease
                 else "Train: no lease, the train is stopped")
    lines += [f"  {record(r)}" for r in train.get("records") or []] or ["  empty"]
    batch = doc.get("batch") if isinstance(doc.get("batch"), dict) else {}
    members = batch.get("members") or []
    lines.append("Current batch: " + "  ".join(f"#{m.get('pr')}" + (" ✓" if m.get("merged") else "")
                                               for m in members) if members else "Batch: empty")
    return "\n".join(lines)


# -- read projections (`/v1/repos`, `/v1/infra`, `/v1/telemetry`, `/v1/learning`, `/v1/audit`) --
# The projections carry stable codes, never prose (`api/board.py`): these tables are their
# labels. An unknown code is shown as is, so a newer server never renders blank.
_SOURCE_LABEL = {"fresh": "fresh", "stale": "stale", "unavailable": "unavailable"}
_SOURCE_NAME_LABEL = {"state": "operational state", "workflow": "WORKFLOW.md",
                      "learning": "learning", "kubernetes": "Kubernetes",
                      "telemetry": "telemetry", "metrics": "metrics"}


def _label(labels: dict, code) -> str:
    if code is None or code == "":
        return "—"
    return labels.get(code, str(code))


def sources_note(doc: dict) -> list[str]:
    """Lines for the projection sources that are not fresh: a missing list then means
    "unknown", not "none". A source's `note` is operator detail for logs, never shown."""
    out = []
    for name, info in sorted((doc.get("sources") or {}).items()):
        status = (info or {}).get("status")
        if status and status != "fresh":
            out.append(f"Warning: the {_label(_SOURCE_NAME_LABEL, name)} source is "
                       f"{_label(_SOURCE_LABEL, status)}.")
    if (doc.get("limits") or {}).get("truncated"):
        out.append("Warning: the server truncated this list at its limit.")
    return out


def _with_sources(doc: dict, body: str) -> str:
    notes = sources_note(doc)
    return "\n".join([body, "", *notes]) if notes else body


_ROUTE_LABEL = {"issue": "ticket", "retrospective": "retrospective", "default": "workflow default",
                "installation": "GitHub App", "workflow": "workflow"}


def _repo_runtime(r: dict) -> str:
    """What a new ticket gets: the workflow's runtime, else the detected one (marked)."""
    if r.get("configured_runtime"):
        return str(r["configured_runtime"])
    if r.get("detected_runtime"):
        return f"{r['detected_runtime']} (detected)"
    return str(r.get("runtime") or "—")


def _repo_components(r: dict) -> str:
    names = sorted((r.get("components") or {}).keys())
    if not names:
        return "—"
    return ", ".join(names) + (" (detected)" if r.get("components_source") == "detected" else "")


def repos(doc: dict) -> str:
    rows = [[str(r.get("name") or "—") + (" (archived)" if r.get("archived") else ""),
             _repo_runtime(r), _repo_components(r),
             ", ".join(_label(_ROUTE_LABEL, c) for c in r.get("routes") or []) or "—",
             "?" if r.get("issues") is None else str(r["issues"])] for r in doc.get("items") or []]
    return _with_sources(doc, table(["REPOSITORY", "RUNTIME", "COMPONENTS", "SOURCE", "TICKETS"],
                                    rows, empty="No known repositories."))


_HEALTH_LABEL = {"healthy": "healthy", "degraded": "degraded", "unknown": "unknown"}
_ROLE_LABEL = {"redis": "Redis", "daemon": "daemon", "api": "API", "gateway": "gateway",
               "merger": "merger", "runner": "runner", "workspace": "workspace",
               "sweep": "cleanup", "other": "other"}
_DETAIL_LABEL = {
    # services: redis and the daemon heartbeat
    "ok": "ok", "stale": "stale", "unreadable": "unreadable",
    # pods
    "ready": "ready", "not_ready": "not ready", "pending": "pending", "failed": "failed",
    "succeeded": "succeeded", "unknown": "unknown",
    # jobs
    "active": "active", "complete": "complete",
    # volume claims
    "bound": "bound", "lost": "lost",
}


def infra(doc: dict) -> str:
    paused = doc.get("paused")
    head = ("Symphony " + ("is paused" if paused else "is taking tickets") if paused is not None
            else "Symphony: pause state unknown")
    lines = [f"{head} · heartbeat {ago(doc.get('heartbeatAt'))}", ""]
    lines.append(table(["SERVICE", "ROLE", "HEALTH", "DETAIL"], [
        [str(s.get("name") or "—"), _label(_ROLE_LABEL, s.get("role")),
         _label(_HEALTH_LABEL, s.get("status")), _label(_DETAIL_LABEL, s.get("detail"))]
        for s in doc.get("services") or []], empty="No services observed."))
    lines.append("")
    lines.append(table(["RESOURCE", "KIND", "PHASE", "DETAIL", "COMPONENT", "CREATED"], [
        [str(r.get("name") or "—"), str(r.get("kind") or "—"), str(r.get("status") or "—"),
         _label(_DETAIL_LABEL, r.get("detail")), _label(_ROLE_LABEL, r.get("component")),
         ago(r.get("createdAt"))]
        for r in doc.get("resources") or []], empty="No Kubernetes resources observed."))
    return _with_sources(doc, "\n".join(lines))


def telemetry(doc: dict, *, sessions: int = 10) -> str:
    totals = doc.get("totals")
    if totals:
        head = (f"Total: {num(totals.get('total'))} tokens ({num(totals.get('input'))} input, "
                f"{num(totals.get('output'))} output, {num(totals.get('cacheRead'))} cache) · "
                f"estimated cost {money(totals.get('costEstimate'))}")
    else:
        head = "Total: no telemetry"
    lines = [head, "",
             table(["MODEL", "REQUESTS", "INPUT", "OUTPUT"], [
                 [str(m.get("model") or "—"), num(m.get("requests")), num(m.get("input")),
                  num(m.get("output"))] for m in doc.get("models") or []],
                 empty="No models recorded."),
             "",
             table(["MONTH", "DISPATCHED", "REWORK", "PRS OPENED", "PRS MERGED", "LEARNING"], [
                 [str(m.get("month") or "—"), num(m.get("dispatched")), num(m.get("rework")),
                  num(m.get("prsOpened")), num(m.get("prsMerged")), num(m.get("learningPrs"))]
                 for m in doc.get("monthly") or []], empty="No monthly counters.")]
    recent = (doc.get("sessions") or [])[:sessions]
    if recent:
        lines += ["", f"Recent sessions ({len(recent)}):", table(
            ["TICKET", "KIND", "STATE", "TOKENS", "COST", "UPDATED"], [
                [session_label({"identifier": s.get("identifier"), "sid": s.get("sid"),
                                "kind": s.get("kind")}),
                 KIND_LABEL.get(s.get("kind") or "", s.get("kind") or "—"),
                 state_label(s.get("state")),
                 num((s.get("tokens") or {}).get("total")),
                 money((s.get("tokens") or {}).get("cost"))
                 if (s.get("tokens") or {}).get("cost") is not None else "—",
                 ago(s.get("updatedAt"))] for s in recent])]
    return _with_sources(doc, "\n".join(lines))


_LEARNING_LABEL = {"pending": "pending", "completed": "completed", "failed": "failed"}
_PROVENANCE_LABEL = {"retrospective": "retrospective", "retained": "retained"}
_OUTCOME_LABEL = {"nothing_to_learn": "nothing to learn", "pr_opened": "PR opened",
                  "completed": "completed", "failed": "failed", "pending": "pending"}


def learning(doc: dict) -> str:
    rows = [[str(i.get("identifier") or "—"), str(i.get("repo") or "—"),
             _label(_LEARNING_LABEL, i.get("status")), _label(_PROVENANCE_LABEL, i.get("provenance")),
             _label(_OUTCOME_LABEL, i.get("outcome")), str(i.get("prUrl") or "—"),
             ago(i.get("updatedAt"))] for i in doc.get("items") or []]
    return _with_sources(doc, table(
        ["TICKET", "REPOSITORY", "STATUS", "SOURCE", "OUTCOME", "PR", "UPDATED"], rows,
        empty="No learning proposals."))


_RESULT_LABEL = {"accepted": "accepted", "finished": "finished", "attempted": "attempted",
                 "succeeded": "succeeded", "failed": "failed", "unknown": "unknown"}


# Audit `action` codes: durable commands (`admin_commands.ACTIONS`) and the standalone
# changes the API records itself.
_ACTION_LABEL = {
    "pause": "pause", "resume": "resume", "refresh": "reread the tracker", "reevaluate": "reevaluate",
    "interrupt": "interrupt", "workflow": "publish WORKFLOW.md", "hold": "hold ticket",
    "release": "release ticket", "token.create": "create token", "token.revoke": "revoke token",
    "teamconfig.publish": "publish team config", "merger.bypass": "merger: skip review",
    "merger.requeue": "merger: requeue", "merger.release": "merger: release PR",
    "provider.save": "save provider", "setup.tracker": "setup: tracker",
    "setup.github_app": "setup: GitHub App", "setup.repositories": "setup: repositories",
    "setup.notifications": "setup: notifications", "setup.webhooks": "setup: webhook secrets",
    "setup.assistant": "setup: assistant", "setup.skills": "setup: skill sources",
    "admin.mcp.put": "MCP: save server",
    "admin.mcp.remove": "MCP: remove server", "admin.mcp.headers": "MCP: set headers",
    "admin.mcp.test": "MCP: test server", "assistant.tickets_created": "assistant: tickets created",
    "mcp.viewer": "MCP: viewer link",
    "assistant.file_shared": "assistant: file shared",
    "assistant.evidence_attached": "assistant: file attached to ticket",
    "assistant.team_fact_pr": "assistant: team fact PR", "assistant.memory_read": "assistant: read memory",
    "assistant.memory_forget": "assistant: forget memory item", "assistant.memory_wipe": "assistant: wipe memory",
    "backup.export": "backup: download", "backup.restore": "backup: restore",
    "backup.run": "backup: run now", "backup.archive.get": "backup: archive download",
}


def audit(doc: dict) -> str:
    rows = [[ago(e.get("time")), str(e.get("actor") or "—"), _label(_ACTION_LABEL, e.get("action")),
             _clip(str(e.get("target") or "—"), 32),
             _RESULT_LABEL.get(e.get("result"), str(e.get("result") or "—")),
             _clip(str(e.get("reason") or ""), 48)] for e in doc.get("items") or []]
    body = table(["WHEN", "WHO", "ACTION", "TARGET", "RESULT", "REASON"], rows,
                 empty="No audit events.")
    if doc.get("nextCursor"):
        body += f"\n\nOlder events: symphony audit --cursor {doc['nextCursor']}"
    return body


def mcp_servers(doc: dict) -> str:
    """`GET /v1/admin/mcp`: one row per server; header values are never shown."""
    rows = []
    for item in doc.get("servers") or []:
        where = item.get("url") or " ".join([item.get("command") or "", *(item.get("args") or [])])
        rows.append([item.get("name", ""), item.get("transport", ""),
                     "on" if item.get("enabled") else "off",
                     ",".join(item.get("roles") or []) or "-",
                     ",".join(item.get("headers_set") or []) or "-", where.strip()])
    return table(["NAME", "TRANSPORT", "ENABLED", "ROLES", "HEADERS", "WHERE"], rows,
                 empty="No MCP servers. Add one with `symphony admin mcp set`.")


def mcp_test(name: str, doc: dict) -> str:
    if doc.get("ok") is None:
        return f"{name}: {doc.get('note') or 'not tested'}."
    if doc.get("ok"):
        tools = doc.get("tools") or []
        version = doc.get("protocol_version") or "?"
        text = (f"{name} works (protocol {version}). Tools: "
                f"{', '.join(tools) if tools else 'none'}.")
        me = doc.get("me")
        if isinstance(me, dict) and me.get("user"):
            text += f" Account: {me['user']}."
        return text
    error = doc.get("error") or {}
    return f"{name} failed ({error.get('code', 'error')}): {error.get('message', '')}".rstrip(": ")


def mcp_viewer(name: str, lease: str, doc: dict) -> str:
    """The link opens once, within `expires_in` seconds."""
    mode = "control" if doc.get("control") else "view only"
    expires = doc.get("expires_in")
    within = f" within {expires} s" if isinstance(expires, int) else ""
    return f"Viewer of {lease} on {name} ({mode}). Open it once{within}:\n{doc.get('url', '')}"


# -- assistant memory (`symphony assistant memory …`) ---------------------------------------

def assistant_soul(soul: dict | None) -> str:
    if not soul:
        return "Soul: none reported (the assistant is not running, or no soul repository is set)."
    where = f"{soul.get('repo')}:{soul.get('path')}"
    if not soul.get("sha"):
        return f"Soul: the built-in text ({where} is missing)."
    return f"Soul: {where} at {str(soul['sha'])[:12]}."


def assistant_memory_people(doc: dict) -> str:
    rows = [[p.get("login", ""), str(p.get("count", 0)), ago(p.get("updated_at"))]
            for p in doc.get("people") or []]
    people = table(["LOGIN", "ITEMS", "UPDATED"], rows,
                   empty="The assistant remembers nothing about anyone.")
    return assistant_soul(doc.get("soul")) + "\n\n" + people


def assistant_memory_items(doc: dict) -> str:
    rows = [[i.get("id", ""), i.get("kind", ""), ago(i.get("at")), i.get("text", "")]
            for i in doc.get("items") or []]
    return table(["ID", "KIND", "AT", "TEXT"], rows,
                 empty=f"The assistant remembers nothing about {doc.get('login', 'them')}.")


# -- backup (`symphony backup …`) -----------------------------------------------------------

def _target_text(target: dict | None) -> str:
    if not target:
        return "none"
    if target.get("kind") == "s3":
        return f"s3 {target.get('endpoint')}/{target.get('bucket')}/{target.get('prefix') or ''}"
    return f"directory {target.get('dir')}"


def backup_status(doc: dict) -> str:
    keep = doc.get("keep")
    lines = [f"Target: {_target_text(doc.get('target'))}"
             + (f" (keeps {keep} backups)" if doc.get("target") and keep else "")]
    if doc.get("reason"):
        lines.append(f"  {doc['reason']}")
    kept = doc.get("retention") or {}
    lines.append(f"Retention in Redis: ended sessions {kept.get('sessions_days', '—')} days, "
                 f"audit {kept.get('audit_days', '—')} days.")
    rows = []
    for kind in ("backup", "archive", "restore"):
        last = (doc.get("last") or {}).get(kind)
        if not last:
            rows.append([kind, "never", "—", ""])
            continue
        detail = last.get("reason") or last.get("name") or ""
        if kind == "restore":
            detail = f"{last.get('restored', 0)} keys from {last.get('source_namespace', '?')}"
        elif kind == "archive":
            detail = f"{last.get('sessions', 0)} sessions, {last.get('audit', 0)} audit entries"
        rows.append([kind, str(last.get("status", "—")), ago(last.get("at")), _clip(detail, 60)])
    return "\n".join(lines) + "\n\n" + table(["RUN", "STATUS", "WHEN", "DETAIL"], rows)


def backup_objects(doc: dict, what: str) -> str:
    rows = [[item.get("name", ""), num(item.get("size")), ago(item.get("modified"))]
            for item in doc.get("items") or []]
    return (f"Target: {_target_text(doc.get('target'))}\n\n"
            + table(["NAME", "BYTES", "WHEN"], rows, empty=f"No {what}."))


def backup_restore(doc: dict) -> str:
    return (f"Restored {doc.get('restored', 0)} keys into {doc.get('namespace')} from the backup of "
            f"namespace {doc.get('source_namespace')} taken {doc.get('created_at')}. The pause "
            "flag did not change: run `symphony resume` when the state looks right.")


def backup_run(doc: dict) -> str:
    made, archived = doc.get("backup") or {}, doc.get("archive") or {}
    text = f"Backup {made.get('name')} ({made.get('records', 0)} keys) on {_target_text(doc.get('target'))}."
    if made.get("pruned"):
        text += f" Deleted {len(made['pruned'])} older backups."
    if archived.get("name"):
        text += (f"\nArchived {archived.get('sessions', 0)} sessions and "
                 f"{archived.get('audit', 0)} audit entries in {archived['name']}.")
    return text
