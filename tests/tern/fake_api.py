"""A fake Symphony control API (`/v1`, docs/api.md) for the Tern plugin's headless scenarios.

It serves one team's believable state, enforces the Bearer token, answers `If-None-Match` with
304, applies the writes the plugin makes (messages, interrupts, publications, tokens, provider
keys, merger actions) and appends every write to a JSONL log the tests assert on.

    python tests/tern/fake_api.py --port 0 --log writes.jsonl --ready ready.txt [--state empty]
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import redis
from history_fixture import empty_history_doc, history_doc
from real_stack import seed_merger

from symphony.api.setup import GITHUB_EVENTS
from symphony.merger.store import MergerStore, normalize_repo, registered_repos

TOKEN = "sym_tern_7f3a9c2e5b1d4e8f"
ME = {"name": "alice", "role": "admin", "token_id": "tern"}


def now_ms() -> int:
    return int(time.time() * 1000)


def ago(seconds: float) -> int:
    return now_ms() - int(seconds * 1000)


def iso_ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))


KIMI = "global.moonshotai.kimi-k3.1"
MERGER_NAMESPACE = "tern-fake"


def team_state() -> dict:
    sessions = [
        {
            "sid": "iss-139:2", "kind": "implement", "identifier": "ENG-139",
            "title": "Paginate the invoices API for the mobile app", "issue_id": "iss-139", "seq": 2,
            "repo": "acme/billing", "branch": "symphony/eng-139-pagination", "model": f"amazon-bedrock/{KIMI}",
            "state": "waiting_input", "turn": 4, "updated_at": ago(95),
            "tokens": {"input": 61200, "output": 8400, "total": 69600, "cost": 0.94},
            "input_request": {
                "id": "ask-1",
                "question": "Should /v1/invoices paginate by cursor or by offset? "
                "The mobile app still uses offset.",
                "options": ["Cursor, with offset for compatibility", "Offset only"],
                "at": ago(95),
            },
            "last_message": "I mapped the three consumers of the endpoint. The pagination format is still open.",
        },
        {
            "sid": "iss-142:1", "kind": "implement", "identifier": "ENG-142",
            "title": "Export the sales report as CSV", "issue_id": "iss-142", "seq": 1,
            "repo": "acme/web", "branch": "symphony/eng-142-export-csv", "model": f"amazon-bedrock/{KIMI}",
            "state": "running", "turn": 7, "updated_at": ago(3),
            "tokens": {"input": 162000, "output": 22100, "total": 184100, "cost": 2.41},
            "pr_url": "https://github.com/acme/web/pull/821",
            "last_message": "The exporter tests pass. Now covering time zones in the CSV header.",
            "evidence": [
                {"title": "Exported report in the browser", "kind": "screenshot",
                 "url": "https://symphony.example/evidence/eng-142/export.png"},
                {"title": "Exporter test output", "kind": "log",
                 "url": "https://symphony.example/evidence/eng-142/pytest.txt"},
            ],
        },
        {
            "sid": "merger:acme:gateway:812:review:1", "kind": "review", "identifier": "PR #812",
            "title": "Review: session cache in the gateway", "repo": "acme/gateway",
            "model": "amazon-bedrock/deepseek.v3.2", "state": "running", "turn": 2, "updated_at": ago(12),
            "tokens": {"input": 48000, "output": 3100, "total": 51100, "cost": 0.61},
        },
        {
            "sid": "iss-151:1", "kind": "implement", "identifier": "ENG-151",
            "title": "Resend invite button on the teams page", "issue_id": "iss-151", "seq": 1,
            "repo": "acme/web", "model": f"amazon-bedrock/{KIMI}", "state": "starting", "turn": 0,
            "updated_at": ago(20),
        },
        {
            "sid": "iss-128:3", "kind": "answer", "identifier": "ENG-128",
            "title": "Why did the reconciliation job take twice as long?", "issue_id": "iss-128", "seq": 3,
            "repo": "acme/billing", "model": "anthropic/claude-sonnet-5", "state": "idle", "turn": 3,
            "updated_at": ago(1500), "tokens": {"total": 40200, "cost": 0.48},
            "last_message": "I answered on the ticket: migration 0142 dropped the index on ledger_entries.created_at.",
        },
        {
            "sid": "assist:slack:C0123ABCD:1731700000.000200:1", "kind": "assist", "identifier": "slack C0123ABCD",
            "title": "research how other teams paginate their invoice APIs", "transport": "slack",
            "channel": "C0123ABCD", "thread": "1731700000.000200", "role": "researcher", "asker": "bob",
            "model": f"amazon-bedrock/{KIMI}", "state": "running", "turn": 1, "updated_at": ago(8),
            "tokens": {"input": 22000, "output": 1900, "total": 23900, "cost": 0.21},
            "last_message": "Reading the Stripe and Adyen pagination docs.",
        },
        {
            "sid": "assist:discord:1290000000000000001:1290000000000000001:1", "kind": "assist",
            "identifier": "discord DM", "title": "plan the CSV export follow-ups", "transport": "discord",
            "channel": "1290000000000000001", "thread": "1290000000000000001", "role": "planner",
            "asker": "discord:331122334455667788", "model": "amazon-bedrock/global.anthropic.claude-haiku-5-5",
            "state": "idle", "turn": 2, "updated_at": ago(420),
            "last_message": "I proposed 3 tickets. Waiting for a person to create them.",
        },
        {
            "sid": "iss-120:1", "kind": "implement", "identifier": "ENG-120",
            "title": "Remove the old checkout feature flag", "issue_id": "iss-120", "seq": 1,
            "repo": "acme/web", "model": f"amazon-bedrock/{KIMI}", "state": "ended", "outcome": "pr_opened",
            "turn": 5, "updated_at": ago(7200), "tokens": {"total": 90200, "cost": 1.12},
            "pr_url": "https://github.com/acme/web/pull/809",
        },
    ]
    issues = [
        {"issue_id": "iss-139", "identifier": "ENG-139", "title": sessions[0]["title"], "repo": "acme/billing",
         "status": "active", "phase": "waiting_input", "tracker_state": "In Progress", "attempt": 2, "seq": 2,
         "session_id": "iss-139:2", "cost": 0.94, "pr_url": None, "updated_at": iso_ago(95)},
        {"issue_id": "iss-142", "identifier": "ENG-142", "title": sessions[1]["title"], "repo": "acme/web",
         "status": "active", "phase": "running", "tracker_state": "In Progress", "attempt": 1, "seq": 1,
         "session_id": "iss-142:1", "cost": 2.41, "pr_url": "https://github.com/acme/web/pull/821",
         "updated_at": iso_ago(3)},
        {"issue_id": "iss-151", "identifier": "ENG-151", "title": sessions[3]["title"], "repo": "acme/web",
         "status": "active", "phase": "running", "tracker_state": "In Progress", "attempt": 1, "seq": 1,
         "session_id": "iss-151:1", "cost": 0.0, "pr_url": None, "updated_at": iso_ago(20)},
        {"issue_id": "iss-155", "identifier": "ENG-155", "title": "Per-customer rate limit on the webhook",
         "repo": "acme/gateway", "status": "queued", "phase": None, "tracker_state": "Todo", "attempt": 0,
         "cost": None, "pr_url": None, "reason": "mixed_owner_repo_designation", "updated_at": iso_ago(300)},
        {"issue_id": "iss-147", "identifier": "ENG-147", "title": "Fix tax rounding in BRL",
         "repo": "acme/billing", "status": "retrying", "phase": "retrying", "tracker_state": "In Progress",
         "attempt": 3, "cost": 3.80, "pr_url": None, "reason": None, "updated_at": iso_ago(600),
         "held": {"by": "bob", "reason": "Taking over to fix the rounding by hand", "at": iso_ago(1200)}},
        {"issue_id": "iss-133", "identifier": "ENG-133", "title": "Move the email queue to SQS",
         "repo": "acme/web", "status": "review", "phase": "in_review", "tracker_state": "In Review",
         "attempt": 1, "cost": 4.12, "pr_url": "https://github.com/acme/web/pull/815", "updated_at": iso_ago(2400)},
        {"issue_id": "iss-129", "identifier": "ENG-129", "title": "Upgrade the Stripe SDK to v14",
         "repo": "acme/billing", "status": "merging", "phase": "merging", "tracker_state": "Merging",
         "attempt": 1, "cost": 1.95, "pr_url": "https://github.com/acme/billing/pull/402", "updated_at": iso_ago(900)},
    ]
    models = {
        "implementer": {"provider": "amazon-bedrock", "model": KIMI, "thinking": "high"},
        "reviewer": {"provider": "amazon-bedrock", "model": "deepseek.v3.2", "thinking": "high"},
        "oracle": {"provider": "openai", "model": "gpt-5.5", "thinking": "medium"},
        "arbiter": {"provider": "anthropic", "model": "claude-sonnet-5", "thinking": "high"},
        "developer": {"provider": "amazon-bedrock", "model": KIMI, "thinking": "high"},
        "reflector": {"provider": "amazon-bedrock", "model": KIMI, "thinking": "medium"},
        "probe": {"provider": "anthropic", "model": "claude-haiku-5", "thinking": "low"},
        "assistant": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-haiku-5-5", "thinking": "low"},
        "researcher": {"provider": "amazon-bedrock", "model": KIMI, "thinking": "high"},
        "planner": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-haiku-5-5", "thinking": "medium"},
    }
    config = {
        "generation": 3, "models": models,
        "omp": {"version": "18.20.2", "settings": {"compaction": {"auto": True, "threshold": 0.8},
                                                    "tools": {"web_search": True}}},
        "updated_at": ago(7300), "updated_by": "alice", "reason": "Oracle on a different provider for independence",
    }
    observed = {
        "daemon": {"generation": 3, "at": ago(4)},
        "merger:review:acme/web": {"generation": 3, "at": ago(70)},
        "merger:train:acme/web": {"generation": 2, "at": ago(5400)},
        "runner:ENG-142": {"generation": 3, "at": ago(400)},
        "runner:ENG-139": {"generation": 2, "at": ago(8000)},
        "member:alice": {"generation": 3, "at": ago(7000)},
        "member:bob": {"generation": 1, "at": ago(86400 * 3)},
    }
    catalog = {
        "source": "@oh-my-pi/pi-catalog/models.json", "version": "18.20.2",
        "providers": [
            {"id": "amazon-bedrock", "credential": "apiKey", "models": [
                {"id": "deepseek.v3.2", "name": "DeepSeek V3.2", "contextWindow": 160000, "reasoning": True},
                {"id": KIMI, "name": "Kimi K3.1", "contextWindow": 256000, "reasoning": True},
                {"id": "global.anthropic.claude-sonnet-5", "name": "Claude Sonnet 5", "contextWindow": 400000, "reasoning": True},
            ]},
            {"id": "anthropic", "credential": "apiKey", "models": [
                {"id": "claude-haiku-5", "name": "Claude Haiku 5", "contextWindow": 200000, "reasoning": True},
                {"id": "claude-sonnet-5", "name": "Claude Sonnet 5", "contextWindow": 400000, "reasoning": True},
            ]},
            {"id": "openai", "credential": "apiKey", "models": [
                {"id": "gpt-5.5", "name": "GPT-5.5", "contextWindow": 400000, "reasoning": True},
                {"id": "gpt-5.5-mini", "name": "GPT-5.5 mini", "contextWindow": 400000, "reasoning": True},
            ]},
            {"id": "google", "credential": "apiKey", "models": [
                {"id": "gemini-3-pro", "name": "Gemini 3 Pro", "contextWindow": 1000000, "reasoning": True},
            ]},
            {"id": "groq", "credential": "apiKey", "models": [
                {"id": "llama-4-maverick", "name": "Llama 4 Maverick", "contextWindow": 128000, "reasoning": False},
            ]},
            # Credentials from the host (ADC): in the model picker, never offered a key.
            {"id": "google-vertex", "credential": "host", "models": [
                {"id": "gemini-3-pro", "name": "Gemini 3 Pro (Vertex)", "contextWindow": 1000000, "reasoning": True},
            ]},
        ],
    }
    # internals §13: a built-in key is `{id, builtin, credentialConfigured}`; an own provider
    # keeps its definition. OpenAI, Bedrock and Google have no stored item.
    providers = {
        "revision": "4821", "storageReady": True,
        "items": [
            {"id": "anthropic", "builtin": True, "credentialConfigured": True},
            {"id": "vllm-internal", "name": "Internal vLLM", "api": "openai-completions",
             "baseUrl": "https://vllm.acme.internal/v1", "auth": "apiKey", "models": [{"id": "qwen3-coder"}],
             "credentialConfigured": False},
        ],
    }
    tokens = [
        {"id": "tern", "name": "alice", "role": "admin", "created_at": ago(86400 * 20), "created_by": "bootstrap",
         "expires_at": ago(-86400 * 70), "last_used_at": ago(2), "revoked_at": None},
        {"id": "k2p9", "name": "bob", "role": "operator", "created_at": ago(86400 * 12), "created_by": "alice",
         "expires_at": ago(-86400 * 78), "last_used_at": ago(3600 * 5), "revoked_at": None},
        {"id": "x7c1", "name": "ci-bot", "role": "viewer", "created_at": ago(86400 * 40), "created_by": "alice",
         "expires_at": None, "last_used_at": ago(600), "revoked_at": None},
        {"id": "m3q8", "name": "intern", "role": "operator", "created_at": ago(86400 * 60), "created_by": "alice",
         "expires_at": ago(-86400 * 30), "last_used_at": ago(86400 * 9), "revoked_at": ago(86400 * 8),
         "revoked_by": "alice"},
    ]
    return {
        "sessions": sessions, "issues": issues, "config": config, "observed": observed,
        "catalog": catalog, "providers": providers, "tokens": tokens,
        "mcp": mcp_servers(), "mcp_headers": {"linear-docs": {"Authorization": "Bearer lin_mcp_stored"}},
        "assistant": assistant_memory(),
        "daemon": {"alive": True, "paused": False},
        "setup": {
            "tracker": {"kind": "linear", "team_key": "ENG"}, "repositories": INSTALLED_REPOS,
            "missing": [], "default_repo": "acme/web", "notifications": {"slack": True},
            "tested": True, "webhooks": True, "deliveries": True, "models": models, "assistant": None,
        },
    }


# teamconfig.ROLES: the roles an MCP server can be given.
ROLES = ["implementer", "reviewer", "oracle", "arbiter", "developer", "reflector", "probe",
         "assistant", "researcher", "planner"]


def mcp_servers() -> dict:
    """Stored MCP definitions (mcp_servers.py), without the secret header values."""
    return {
        "linear-docs": {"transport": "http", "url": "https://mcp.linear.app/mcp",
                        "roles": ["implementer", "researcher", "planner"], "enabled": True},
        "sentry": {"transport": "sse", "url": "https://mcp.sentry.dev/sse", "roles": ["implementer", "developer"],
                   "enabled": False, "timeout_ms": 20000},
        # Not a browser server: omp drops those while its own browser is on (omp://mcp-config.md).
        "local-files": {"transport": "stdio", "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/work"], "roles": ["developer", "probe"],
                       "enabled": True},
    }


def mcp_doc(st: dict) -> dict:
    """`GET /v1/admin/mcp`: definitions with the names of the stored headers, never their values."""
    servers = [{"name": name, **d, **({"headers_set": sorted(st["mcp_headers"].get(name, {}))}
                                       if d["transport"] != "stdio" else {})}
               for name, d in sorted(st["mcp"].items())]
    return {"servers": servers, "roles": ROLES}


def skills_doc() -> dict:
    """`GET /v1/admin/skills`: the configured skill sources, resolved (api/admin.py `list_skills`)."""
    def source(repo, path, sha, skills, roles=None, repos=None, error=None):
        return {"repo": repo, "path": path, "ref": None, "roles": roles, "repos": repos,
                "scope": "repos" if repos else "global", "sha": sha, "skills": skills, "error": error}

    return {"sources": [
        source("acme/agent-skills", "global", "9f2c41e7a0b3d5c8e1f4a6b9c2d5e8f1a4b7c0d3",
               ["release-notes", "sql-migrations", "incident-report"]),
        source("acme/agent-skills", "artifacts", "9f2c41e7a0b3d5c8e1f4a6b9c2d5e8f1a4b7c0d3",
               ["preview-build"], repos=["acme/web"]),
        source("acme/agent-skills", "planning", "9f2c41e7a0b3d5c8e1f4a6b9c2d5e8f1a4b7c0d3",
               ["spec-template", "ticket-split"], roles=["planner", "researcher"]),
        source("acme/design-system", "skills", None, [], error="ref resolution failed"),
    ]}


def assistant_memory() -> dict:
    """The assistant's soul (from its heartbeat) and per-person memory (`assistant/memory.py`)."""
    def item(iid, text, kind, seconds):
        return {"id": iid, "text": text, "kind": kind, "at": ago(seconds), "thread": "1731700000.000100"}

    return {
        "soul": {"repo": "acme/web", "path": ".symphony/assistant.md",
                 "sha": "4b825dc642cb6eb9a060e54bf8d69288fbee4904"},
        "memory": {
            "alice": [item("a1b2c3d4", "deploys on Fridays after the release review", "explicit", 86400 * 3),
                      item("e5f6a7b8", "prefers answers in Portuguese", "inferred", 3600 * 5)],
            "bob": [item("0c1d2e3f", "owns the billing service", "explicit", 86400 * 12)],
        },
    }


def assistant_doc(st: dict, path: str) -> dict:
    """`GET /v1/assistant/memory` and `GET /v1/assistant/memory/{login}` (api/assistant.py)."""
    login = path.removeprefix("/v1/assistant/memory").strip("/")
    if login:
        return {"login": login, "items": st["memory"].get(login, [])}
    people = [{"login": who, "count": len(items), "updated_at": max(i["at"] for i in items)}
              for who, items in sorted(st["memory"].items())]
    return {"soul": st["soul"], "people": people}


MCP_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def mcp_invalid(name: str, d: dict) -> str | None:
    """The field the API's validation names (mcp_servers.py), or None."""
    if not MCP_NAME.match(name):
        return "name"
    if d.get("transport") not in ("http", "sse", "stdio"):
        return "transport"
    if d["transport"] == "stdio":
        if not d.get("command"):
            return "command"
    elif not (d.get("url") or "").startswith("https://"):
        return "url"
    roles = d.get("roles")
    if not roles or any(r not in ROLES for r in roles):
        return "roles"
    return None


def empty_state() -> dict:
    s = team_state()
    s["sessions"], s["issues"] = [], []
    s["mcp"], s["mcp_headers"] = {}, {}
    return s


def unconfigured_state() -> dict:
    """A fresh install: only Redis and the API with its bootstrap token (setup contract)."""
    s = empty_state()
    s["setup"] = {"tracker": None, "repositories": None, "missing": [], "default_repo": None,
                  "notifications": {}, "tested": False, "webhooks": False, "deliveries": False,
                  "models": s["config"]["models"], "assistant": None}
    return s


def unreachable_discord_state() -> dict:
    """Unconfigured, but Slack and Discord were set up and Discord has stopped answering since."""
    s = unconfigured_state()
    s["setup"]["notifications"] = {"slack": True, "discord": "unreachable"}
    s["setup"]["tested"] = True
    return s


# The Seiji preset (contract C): what `--preset seiji` fills, under the fields the request sets.
SEIJI_CLEANUP = {"acquire_tool": "acquire_device", "id_field": "lease_id",
                 "release_tool": "release_device", "release_arg": "lease_id"}
SEIJI_TOOLS = ["budget_status", "list_device_models", "acquire_device", "release_device", "install_app",
               "uninstall_app", "launch_app", "terminate_app", "open_url", "configure_device",
               "push_notification", "get_logs", "inspect_screen", "take_screenshot", "share_screen",
               "run_actions", "run_xctest", "run_flow", "list_runs", "get_run", "get_run_artifact",
               "export_evidence", "cheat_sheet"]
# Seiji's `GET /v1/me` for the user the stored token belongs to.
SEIJI_ME = {"user": "symphony-agents",
            "quota": {"monthlyMinutes": 6000, "maxConcurrent": 4, "maxLeaseMinutes": 120, "idleMinutes": 15}}


def apply_preset(body: dict) -> dict:
    """The definition `PUT /v1/admin/mcp/{name}` stores: the preset's fields, then the request's."""
    preset = body.get("preset")
    if preset != "seiji":
        return body
    url = body.get("url") or ""
    filled = {"transport": "http", "http_base": url[: -len("/mcp")] if url.endswith("/mcp") else url,
              "timeout_ms": 3_900_000, "agent_env": {"SEIJI_TOKEN": "{token}", "SEIJI_URL": "{http_url}"},
              "cleanup": dict(SEIJI_CLEANUP)}
    return {**filled, **{k: v for k, v in body.items() if k != "preset"}}


def is_seiji(d: dict) -> bool:
    """The admin test reads `/v1/me` for a server shaped like the Seiji preset."""
    return bool(d.get("http_base")) and "SEIJI_URL" in (d.get("agent_env") or {})


def seiji_state() -> dict:
    """The team, plus a Seiji server and two sessions that hold a device on it."""
    s = team_state()
    s["mcp"]["seiji"] = apply_preset({"preset": "seiji", "url": "https://seiji.acme.dev/mcp",
                                      "roles": ["implementer", "developer"], "enabled": True})
    s["mcp_headers"]["seiji"] = {"Authorization": "Bearer sj_fake_stored_token"}
    s["sessions"][1:1] = [
        {
            "sid": "iss-163:1", "kind": "implement", "identifier": "ENG-163",
            "title": "Sign-in screen crashes on iOS 18 when the password is empty", "issue_id": "iss-163",
            "seq": 1, "repo": "acme/mobile", "branch": "symphony/eng-163-signin-crash",
            "model": f"amazon-bedrock/{KIMI}", "state": "running", "turn": 6, "updated_at": ago(2),
            "tokens": {"input": 98000, "output": 11200, "total": 109200, "cost": 1.37},
            "last_message": "The fix is in. Running the sign-in flow on the iPhone 16 to record evidence.",
            "mcp_leases": [{"server": "seiji", "id": "l_3f9c2a7b1e04", "model": "iphone-16", "platform": "ios"}],
        },
        {
            "sid": "iss-164:1", "kind": "implement", "identifier": "ENG-164",
            "title": "Ask for the notification permission after the first order on Android",
            "issue_id": "iss-164", "seq": 1, "repo": "acme/mobile", "model": f"amazon-bedrock/{KIMI}",
            "state": "running", "turn": 3, "updated_at": ago(40),
            "mcp_leases": [{"server": "seiji", "id": "l_a1b2c3d4e5f6", "model": "pixel-8", "platform": "android"}],
        },
    ]
    return s


# What the fake GitHub App installation can reach, and the board states the fake tracker has.
INSTALLED_REPOS = ["acme/web", "acme/billing", "acme/gateway"]
BOARD_STATES = ["Backlog", "Todo", "In Progress", "In Review", "Merging", "Done", "Canceled"]


def assistant_detail(st: dict) -> dict:
    """The `assistant` step's detail (`symphony assistant status` without status and error)."""
    a = st["assistant"] or {}
    slack, discord = a.get("slack") or {}, a.get("discord") or {}
    return {
        "enabled": bool(a), "running": bool(a), "heartbeat_at": iso_ago(20) if a else None,
        "threads": 2 if a else 0, "repositories": a.get("repositories"),
        "slack": {"configured": bool(slack), "connected": bool(slack), "channels": slack.get("channels") or []},
        "discord": {"configured": bool(discord), "connected": bool(discord),
                    "channels": discord.get("channels") or [], "intent": "full" if discord else None},
    }


def repositories_detail(st: dict, issues: list[dict], mergers: list[str]) -> tuple[str, dict, dict | None]:
    """The `repositories` step as `_repositories_step` (api/setup.py) builds it on Kubernetes:
    the tracked tickets' repositories plus the default, each needing a merger that beats."""
    default = st["default_repo"]
    repositories = sorted({i["repo"] for i in issues if i.get("repo")} | ({default} if default else set()))
    without = [r for r in repositories if r not in mergers]
    detail = {"default_repo": default, "repositories": repositories, "mergers": mergers, "without_merger": without}
    if not repositories:
        return "todo", detail, None
    if without:
        return "error", detail, {"code": "merger_missing", "message": "no merger is registered for " + ", ".join(without)}
    return "done", detail, None


def setup_doc(st: dict, issues: list[dict], mergers: list[str]) -> dict:
    """`GET /v1/setup` in the API's shapes (api/setup.py), computed from the fake's state as the
    API computes it from real state. `mergers` are the repositories whose merger beats."""
    tracker = st["tracker"] or {}
    repos = st["repositories"]
    tested = {"ok": True, "error": None, "at": iso_ago(30)} if st["tested"] else None

    def step(sid, title, required, status, detail, error=None):
        return {"id": sid, "title": title, "required": required, "status": status, "detail": detail, "error": error}

    def channel(name):
        return {"configured": name in st["notifications"], "last_test": tested if name in st["notifications"] else None}

    def hook(source):
        return {"secret": st["webhooks"], "in_use": True, "last_delivery_at": iso_ago(120) if st["deliveries"] else None}

    steps = [
        step("tracker", "Tracker", True, "done" if st["tracker"] else "todo", {
            "kind": tracker.get("kind"), "team_key": tracker.get("team_key"), "project_slug": tracker.get("project_slug"),
            "owner": tracker.get("owner"), "project_number": tracker.get("project_number"),
            "status_field": tracker.get("status_field"), "credential": bool(st["tracker"]),
            "states": BOARD_STATES if st["tracker"] else [],
        }),
        step("github_app", "GitHub App", True,
             "todo" if repos is None else "error" if st["missing"] else "done",
             {"configured": repos is not None, "repositories": repos or [], "missing_permissions": st["missing"]},
             {"code": "github_permissions_missing", "message": "The App lacks permissions."} if st["missing"] else None),
        step("repositories", "Repositories", True, *repositories_detail(st, issues, mergers)),
        step("models", "Models", True, "done", {"roles": [
            {"role": role, "provider": ref["provider"], "model": ref["model"],
             "credential": "host" if ref["provider"] == "amazon-bedrock" else "stored"}
            for role, ref in st["models"].items()
        ]}),
        step("notifications", "Notifications", False, "done" if st["notifications"] and st["tested"] else "todo",
             {"slack": channel("slack"), "discord": channel("discord")}),
        step("assistant", "Assistant", False, "done" if st["assistant"] else "todo", assistant_detail(st)),
        step("webhooks", "Webhooks", False, "done" if st["deliveries"] else "todo",
             {"linear": hook("linear"), "github": hook("github")}),
        step("skills", "Skills", False, "skipped", {"sources": []}),
    ]
    complete = all(s["status"] == "done" for s in steps if s["required"])
    return {"complete": complete, "mode": "kubernetes", "steps": steps}


def fresh(*names: str) -> dict:
    return {n: {"status": "fresh", "observedAt": iso_ago(2), "note": "read fine"} for n in names}


def repos_doc(st: dict | None = None) -> dict:
    setup = (st or {}).get("setup", {})
    bb = setup.get("base_branches", {})
    rt = setup.get("repo_runtimes", {})
    comps = setup.get("components", {})
    return {
        "observedAt": iso_ago(1),
        "items": [
            {"name": "acme/web", "runtime": "node-22", "routes": ["issue", "default"], "issues": 3,
             "detected_runtime": "node-22", "default_branch": "main", "archived": False,
             "configured_runtime": rt.get("acme/web"),
             "base_branch": bb.get("acme/web"),
             "components": comps.get("acme/web") or {"frontend": {"path": "web", "runtime": "node-22"}},
             "components_source": "configured" if comps.get("acme/web") else "detected",
             "merger_policy": True, "policy_pr": "https://github.com/acme/web/pull/830",
             "provisioning_error": None},
            {"name": "acme/billing", "runtime": "python-3.13", "routes": ["issue", "retrospective"], "issues": 2,
             "detected_runtime": "python-3.13", "default_branch": "develop", "archived": False,
             "configured_runtime": rt.get("acme/billing"),
             "base_branch": bb.get("acme/billing"),
             "components": comps.get("acme/billing"),
             "components_source": "configured" if comps.get("acme/billing") else None,
             "merger_policy": True, "policy_pr": None, "provisioning_error": None},
            {"name": "acme/gateway", "runtime": "go-1.24", "routes": ["retrospective"], "issues": None,
             "detected_runtime": "go-1.24", "default_branch": "main", "archived": True,
             "configured_runtime": rt.get("acme/gateway"),
             "base_branch": bb.get("acme/gateway"),
             "components": comps.get("acme/gateway"),
             "components_source": "configured" if comps.get("acme/gateway") else None,
             "merger_policy": None, "policy_pr": None,
             "provisioning_error": "branch protection prevents push to main"},
        ],
        "sources": fresh("state", "workflow", "learning"),
        "limits": {"runs": 200, "resources": 200, "truncated": False},
    }


def infra_doc() -> dict:
    return {
        "observedAt": iso_ago(1), "paused": False, "heartbeatAt": iso_ago(3),
        "services": [
            {"name": "redis", "role": "redis", "status": "healthy", "detail": "ok"},
            {"name": "daemon", "role": "daemon", "status": "healthy", "detail": "ok"},
            {"name": "symphony-api-6d9f7b-x2k4p", "role": "api", "status": "healthy", "detail": "ready"},
            {"name": "symphony-merger-5c8d4-q7n2m", "role": "merger", "status": "degraded", "detail": "not_ready"},
            {"name": "symphony-gateway-7f6c9-h3j8w", "role": "gateway", "status": "healthy", "detail": "ready"},
        ],
        "resources": [
            {"name": "ws-eng-142-1-7f9c", "kind": "Pod", "status": "Running", "component": "workspace",
             "detail": "ready", "activePods": None, "createdAt": iso_ago(1800)},
            {"name": "run-eng-139-2", "kind": "Job", "status": "Running", "component": "runner",
             "detail": "active", "activePods": 1, "createdAt": iso_ago(2400)},
            {"name": "reflect-eng-118-1", "kind": "Job", "status": "Succeeded", "component": "reflect",
             "detail": "complete", "activePods": 0, "createdAt": iso_ago(5400)},
            {"name": "merger-mirror", "kind": "PersistentVolumeClaim", "status": "Bound", "component": "merger",
             "detail": "bound", "activePods": None, "createdAt": iso_ago(86400 * 9)},
        ],
        "sources": {**fresh("state"), "kubernetes": {"status": "stale", "observedAt": iso_ago(95),
                                                     "note": "list pods timed out"}},
        "limits": {"resources": 200, "truncated": False},
    }


def telemetry_doc() -> dict:
    return {
        "observedAt": iso_ago(1),
        "totals": {"input": 4_120_000, "output": 512_000, "cacheRead": 9_800_000, "cacheWrite": 640_000,
                   "total": 15_072_000, "costEstimate": 61.37},
        "models": [
            {"model": f"amazon-bedrock/{KIMI}", "requests": 1840, "input": 3_500_000, "output": 430_000},
            {"model": "openai/gpt-5.5", "requests": 410, "input": 520_000, "output": 70_000},
            {"model": "anthropic/claude-haiku-5", "requests": 260, "input": 100_000, "output": 12_000},
        ],
        "monthly": [
            {"month": "2026-11", "dispatched": 48, "rework": 6, "prsOpened": 41, "prsMerged": 35, "learningPrs": 4},
            {"month": "2026-10", "dispatched": 112, "rework": 15, "prsOpened": 97, "prsMerged": 88, "learningPrs": 9},
        ],
        "sessions": [
            {"sid": "iss-142:1", "identifier": "ENG-142", "kind": "implement", "state": "running",
             "model": f"amazon-bedrock/{KIMI}", "turn": 7, "tokens": {"input": 162000, "output": 22100,
                                                                      "total": 184100, "cost": 2.41},
             "updatedAt": iso_ago(3)},
            {"sid": "iss-139:2", "identifier": "ENG-139", "kind": "implement", "state": "waiting_input",
             "model": f"amazon-bedrock/{KIMI}", "turn": 4, "tokens": {"input": 61200, "output": 8400,
                                                                      "total": 69600, "cost": 0.94},
             "updatedAt": iso_ago(95)},
        ],
        "sources": fresh("telemetry", "metrics"),
        "limits": {"truncated": False, "sessions": 50},
    }


def learning_doc() -> dict:
    return {
        "observedAt": iso_ago(1),
        "items": [
            {"id": "l-31", "identifier": "ENG-118", "repo": "acme/web", "status": "completed",
             "provenance": "retrospective", "outcome": "pr_opened",
             "prUrl": "https://github.com/acme/web/pull/804", "updatedAt": iso_ago(86400)},
            {"id": "l-32", "identifier": "ENG-147", "repo": "acme/billing", "status": "pending",
             "provenance": "retained", "outcome": "pending", "prUrl": None, "updatedAt": iso_ago(900)},
            {"id": "l-30", "identifier": "ENG-109", "repo": "acme/gateway", "status": "completed",
             "provenance": "retrospective", "outcome": "nothing_to_learn", "prUrl": None,
             "updatedAt": iso_ago(86400 * 3)},
        ],
        "sources": fresh("learning"),
        "limits": {"truncated": False},
    }


AUDIT = [
    {"id": f"a{i}", "time": iso_ago(60 * i), "actor": actor, "action": action, "target": target,
     "result": result, "reason": reason, "commandId": None}
    for i, (actor, action, target, result, reason) in enumerate([
        ("alice", "teamconfig.publish", "generation:3", "succeeded", "Oracle on a different provider for independence"),
        ("bob", "merger.bypass", "pr:812", "accepted", "Approved production hotfix"),
        ("alice", "token.create", "token:k2p9", "succeeded", "operator bob"),
        ("bob", "pause", "namespace", "applied", "Deploying main"),
        ("bob", "resume", "namespace", "applied", "Deploy finished"),
        ("alice", "token.revoke", "token:m3q8", "succeeded", "intern"),
        ("bob", "hold", "iss-147", "succeeded", "Taking over to fix the rounding by hand"),
        ("bob", "release", "iss-151", "applying", "Manual test finished. Continue."),
    ] * 10, start=1)
]


def audit_page(query: dict) -> dict:
    limit = int((query.get("limit") or ["50"])[0])
    start = int((query.get("cursor") or ["0"])[0])
    items = AUDIT[start:start + limit]
    nxt = start + limit
    return {"items": items, "nextCursor": str(nxt) if nxt < len(AUDIT) else None}


def history(state: dict, query: dict) -> dict:
    """`GET /v1/telemetry/history`: the seeded rollups; none for a team without sessions."""
    today = dt.datetime.now(dt.UTC).date()
    if not state["sessions"]:
        return {**empty_history_doc(today), "observed_at": iso_ago(1)}
    frm = (query.get("from") or [None])[0]
    doc = history_doc(today, dt.date.fromisoformat(frm) if frm else None, (query.get("bucket") or [None])[0])
    return {**doc, "observed_at": iso_ago(1)}


PROJECTIONS = {"/v1/repos": repos_doc, "/v1/infra": infra_doc, "/v1/telemetry": telemetry_doc,
               "/v1/learning": learning_doc}


class Fake:
    def __init__(self, state: dict, log_path: str | None, client, namespace: str) -> None:
        self.state = state
        self.lock = threading.Lock()
        self.log_path = log_path
        self.seq = 0
        # Each repository's merger snapshot comes from the real `MergerStore` over a disposable
        # Redis, so its states and keys are exactly the ones the merger writes.
        self.client = client
        self.namespace = namespace
        # The gateway's held-lease registry (contract B): `(server, lease id)` → the lease.
        self.held = {(lease["server"], lease["id"]): lease
                     for sess in state["sessions"] for lease in sess.get("mcp_leases", [])}

    def live_mergers(self) -> list[str]:
        """Repositories whose merger beats (`MergerStore.alive`), as the setup step counts them."""
        return [m.repo for m in self.mergers() if m.alive()]

    def mergers(self) -> list[MergerStore]:
        return [MergerStore(self.client, self.namespace, repo)
                for repo in sorted(registered_repos(self.client, self.namespace))]

    def merger_counts(self) -> dict:
        total: dict[str, int] = {}
        for store in self.mergers():
            for key, n in store.counts().items():
                total[key] = total.get(key, 0) + n
        return total

    def log(self, method: str, path: str, body, headers: dict) -> None:
        if not self.log_path:
            return
        entry = {"method": method, "path": path, "body": body,
                 "if_match": headers.get("If-Match"), "at": now_ms()}
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def overview(self) -> dict:
        s = self.state
        counts = {"running": 0, "waiting_input": 0, "queued": 0, "retrying": 0, "in_review": 0, "merging": 0,
                  "held": sum(1 for i in s["issues"] if i.get("held"))}
        for sess in s["sessions"]:
            if sess["state"] in ("running", "starting"):
                counts["running"] += 1
            elif sess["state"] == "waiting_input":
                counts["waiting_input"] += 1
        for issue in s["issues"]:
            if issue.get("phase") in ("queued", "retrying", "in_review", "merging"):
                counts[issue["phase"]] += 1
        counts["blocked_on_person"] = sum(1 for i in s["issues"] if i.get("status") == "blocked_on_person")
        tokens = sum((x.get("tokens") or {}).get("total", 0) for x in s["sessions"])
        cost = sum((x.get("tokens") or {}).get("cost", 0) for x in s["sessions"])
        return {
            "daemon": {**s["daemon"], "heartbeat_at": ago(1)},
            "counts": counts, "tokens_today": tokens, "cost_today": round(cost, 2),
            "merger": self.merger_counts(),
            "config_generation": s["config"]["generation"],
            "rate_limits": [
                {"model": "anthropic/claude-haiku-5", "stage": "low_priority", "remaining": 18, "limit": 100,
                 "reset_at": iso_ago(-540), "retry_after_ms": None, "observed_at": iso_ago(60),
                 "source": "usage_limit"},
                {"model": "openai/gpt-5.5", "stage": "wrap_up", "remaining": 3, "limit": 100,
                 "reset_at": iso_ago(-1800), "retry_after_ms": None, "observed_at": iso_ago(90),
                 "source": "usage_limit"},
                {"model": f"amazon-bedrock/{KIMI}", "stage": "throttled", "remaining": None, "limit": None,
                 "reset_at": None, "retry_after_ms": 12000, "observed_at": iso_ago(40), "source": "retry"},
            ],
        }


def make_handler(fake: Fake):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def _send(self, status: int, doc, etag: str | None = None) -> None:
            body = b"" if doc is None else json.dumps(doc).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if etag:
                self.send_header("ETag", etag)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _error(self, status: int, code: str, message: str) -> None:
            self._send(status, {"error": {"code": code, "message": message}})

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._error(401, "unauthorized", "missing or invalid token")
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else None

        def _get_doc(self, path: str, query: dict):
            s = fake.state
            if path == "/v1/me":
                return ME
            if path == "/v1/overview":
                return fake.overview()
            if path == "/v1/sessions":
                items = [{"mcp_leases": [], **x} for x in s["sessions"]]
                limit = int((query.get("limit") or ["50"])[0])
                return {"items": items[:limit]}
            if path == "/v1/setup":
                return setup_doc(s["setup"], s["issues"], fake.live_mergers())
            if path == "/v1/issues":
                return {"items": s["issues"]}
            if path == "/v1/merger":
                return {"repos": [{**m.snapshot(), "repo": m.repo, "alive": m.alive()} for m in fake.mergers()]}
            if path == "/v1/admin/config":
                return s["config"]
            if path == "/v1/admin/config/observed":
                return s["observed"]
            if path == "/v1/admin/catalog":
                wanted = (query.get("provider") or [None])[0]
                if wanted is None:
                    return s["catalog"]
                return {**s["catalog"], "providers": [p for p in s["catalog"]["providers"] if p["id"] == wanted]}
            if path == "/v1/admin/providers":
                return s["providers"]
            if path == "/v1/repos":
                return repos_doc(s)
            if path == "/v1/telemetry/history":
                return history(s, query)
            if path in PROJECTIONS:
                return PROJECTIONS[path]()
            if path == "/v1/audit":
                return audit_page(query)
            if path == "/v1/admin/tokens":
                return {"items": s["tokens"]}
            if path == "/v1/admin/mcp":
                return mcp_doc(s)
            if path == "/v1/admin/skills":
                return skills_doc()
            if path.startswith("/v1/assistant/memory"):
                fake.log("GET", path, None, {})  # every read is audited: the tests count them
                return assistant_doc(s["assistant"], path)
            return None

        def do_GET(self) -> None:
            if urlsplit(self.path).path.startswith("/view/open/"):
                return self._viewer_page()
            if not self._authorized():
                return
            url = urlsplit(self.path)
            with fake.lock:
                doc = self._get_doc(url.path, parse_qs(url.query))
                if doc is None:
                    self._error(404, "not_found", url.path)
                    return
                body = json.dumps(doc, sort_keys=True)
            etag = '"' + hashlib.sha1(body.encode()).hexdigest()[:16] + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, doc, etag)

        def _write(self, method: str) -> None:
            if not self._authorized():
                return
            path = urlsplit(self.path).path
            body = self._body()
            fake.log(method, path, body, dict(self.headers))
            parts = path.strip("/").split("/")
            s = fake.state
            with fake.lock:
                fake.seq += 1
                intent = {"intent_id": f"1731700000000-{fake.seq}"}
                if method == "POST" and len(parts) == 4 and parts[1] == "sessions" and parts[3] == "messages":
                    sid = parts[2]
                    for sess in s["sessions"]:
                        if sess["sid"] == sid and sess.get("input_request"):
                            sess["input_request"] = None
                            sess["state"] = "running"
                    return self._send(202, intent)
                if method == "POST" and len(parts) == 4 and parts[1] == "sessions" and parts[3] == "interrupt":
                    return self._send(202, intent)
                if method == "POST" and path == "/v1/commands":
                    action = (body or {}).get("action")
                    if action in ("pause", "resume"):
                        s["daemon"]["paused"] = action == "pause"
                    if action in ("hold", "release"):
                        issue_id = ((body or {}).get("target") or {}).get("issueId")
                        for issue in s["issues"]:
                            if issue["issue_id"] == issue_id:
                                issue["held"] = ({"by": ME["name"], "reason": body.get("reason"), "at": iso_ago(0)}
                                                 if action == "hold" else None)
                    return self._send(202, {"id": intent["intent_id"], "action": action})
                if method == "PUT" and path == "/v1/admin/config":
                    gen = (self.headers.get("If-Match") or "").strip('"')
                    if gen == "":
                        return self._error(428, "precondition_required", "If-Match is required")
                    if gen != str(s["config"]["generation"]):
                        return self._error(412, "precondition_failed",
                                           f"generation {s['config']['generation']} is current")
                    if not (body or {}).get("reason"):
                        return self._error(422, "invalid", "a reason is required")
                    cfg = copy.deepcopy(s["config"])
                    for role, ref in ((body or {}).get("models") or {}).items():
                        cfg["models"][role] = {**cfg["models"].get(role, {}), **ref}
                    cfg.update(generation=cfg["generation"] + 1, updated_at=now_ms(), updated_by=ME["name"],
                               reason=body["reason"])
                    s["config"] = cfg
                    s["observed"]["member:alice"] = {"generation": cfg["generation"], "at": now_ms()}
                    return self._send(200, cfg, f'"{cfg["generation"]}"')
                if method == "POST" and path == "/v1/admin/tokens":
                    name, role = (body or {}).get("name"), (body or {}).get("role")
                    if not name or role not in ("viewer", "operator", "admin"):
                        return self._error(422, "invalid", "name and role are required")
                    tid = hashlib.sha1(name.encode()).hexdigest()[:4]
                    days = (body or {}).get("expires_days")
                    rec = {"id": tid, "name": name, "role": role, "created_at": now_ms(), "created_by": ME["name"],
                           "expires_at": now_ms() + int(days) * 86400000 if days else None,
                           "last_used_at": None, "revoked_at": None}
                    s["tokens"].insert(0, rec)
                    return self._send(201, {**rec, "token": f"sym_{tid}_Qm9vYmFyLXRlcm4tc2NlbmFyaW8"})
                if method == "DELETE" and parts[:3] == ["v1", "admin", "tokens"] and len(parts) == 4:
                    for rec in s["tokens"]:
                        if rec["id"] == parts[3]:
                            rec["revoked_at"], rec["revoked_by"] = now_ms(), ME["name"]
                            return self._send(200, rec)
                    return self._error(404, "not_found", "token")
                if method == "PUT" and parts[:3] == ["v1", "admin", "providers"] and len(parts) == 4:
                    if self.headers.get("If-Match") != f'"{s["providers"]["revision"]}"':
                        return self._error(412, "providers_conflict", "Providers changed; reload before saving again.")
                    pid, doc = parts[3], body or {}
                    items = s["providers"]["items"]
                    old = next((item for item in items if item["id"] == pid), None)
                    if doc.get("builtin") is True:
                        if pid not in {p["id"] for p in s["catalog"]["providers"]}:
                            return self._error(422, "unknown_provider", "Not a provider of the omp catalog.")
                        item = {"id": pid, "builtin": True}
                    else:
                        item = {"id": pid, **{k: v for k, v in doc.items() if k != "apiKey"}}
                    if not doc.get("apiKey") and not (old and old.get("credentialConfigured")):
                        return self._error(422, "provider_key_required", "An API key is required for this provider.")
                    item["credentialConfigured"] = True
                    if old:
                        items[items.index(old)] = item
                    else:
                        items.append(item)
                    s["providers"]["revision"] = str(int(s["providers"]["revision"]) + 1)
                    return self._send(200, s["providers"])
                if method == "POST" and parts[:3] == ["v1", "merger", "prs"] and len(parts) == 5:
                    pr, action = int(parts[3]), parts[4]
                    if not (body or {}).get("reason"):
                        return self._error(422, "invalid_request", "invalid request: reason")
                    stores = {m.repo: m for m in fake.mergers()}
                    repo = body.get("repo")
                    if repo is None and len(stores) > 1:
                        return self._error(422, "repo_required", "Name the repository: more than one merger runs.")
                    store = stores.get(normalize_repo(repo)) if repo is not None else next(iter(stores.values()), None)
                    if store is None:
                        return self._error(404, "unknown_repo", "No merger serves this repository.")
                    return self._send(202, store.request_action(pr, action, actor=ME["name"], reason=body["reason"]))
                if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "mcp"] and parts[3] == "viewer":
                    return self._viewer(parts[2], body or {}, s)
                if method == "POST" and len(parts) == 5 and parts[:3] == ["v1", "_test", "leases"] and parts[4] == "release":
                    # Test-only: the agent released the device after the panel's last poll.
                    for key in [k for k in fake.held if k[1] == parts[3]]:
                        del fake.held[key]
                    return self._send(204, None)
                if method == "DELETE" and parts[:3] == ["v1", "assistant", "memory"] and len(parts) == 4:
                    items = s["assistant"]["memory"].pop(parts[3], None)
                    if items is None:
                        return self._error(404, "not_found", "memory")
                    return self._send(200, {"login": parts[3], "removed": len(items)})
                if parts[:3] == ["v1", "admin", "mcp"] and len(parts) >= 4:
                    return self._mcp(method, parts, body or {}, s)
                if path.startswith("/v1/setup"):
                    return self._setup(method, path, body or {}, s["setup"])
            self._error(404, "not_found", path)

        def _viewer(self, name: str, body: dict, s: dict) -> None:
            """`POST /v1/mcp/{name}/viewer` (contract D): only a lease a live session holds; Seiji's own
            refusal (an Android device) comes back as `viewer_refused` with Seiji's text."""
            if name not in s["mcp"]:
                return self._error(404, "not_found", name)
            lease = fake.held.get((name, body.get("lease_id")))
            if lease is None:
                return self._error(404, "lease_not_held", "No live Symphony session holds this lease.")
            if lease.get("platform") == "android":
                return self._error(409, "viewer_refused", "the screen viewer supports iOS simulators only for now")
            ticket = hashlib.sha256(f"{lease['id']}:{fake.seq}".encode()).hexdigest()[:32]
            # Seiji's link, absolute against `http_base`; here the fake plays Seiji's host too.
            return self._send(200, {"url": f"http://{self.headers.get('Host')}/view/open/{ticket}",
                                    "expires_in": 60, "control": body.get("control") is True})

        def _viewer_page(self) -> None:
            """What Tern's browser loads from a viewer link: a stand-in for Seiji's viewer page."""
            body = b"<!doctype html><title>Seiji viewer</title><p>Seiji viewer (test stand-in)</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _mcp(self, method: str, parts: list[str], body: dict, s: dict) -> None:
            """Admin MCP routes (api/admin.py): definitions, write-only headers and the test."""
            name, rest = parts[3], parts[4:]
            if method == "PUT" and not rest:
                body = apply_preset(body)
                field = mcp_invalid(name, body)
                if field:
                    return self._error(422, "invalid_mcp_server", f"Invalid MCP server: check {field}.")
                keys = ("transport", "url", "command", "args", "env", "roles", "enabled", "timeout_ms", "instructions",
                        "http_base", "agent_env", "rewrite_origins", "cleanup")
                s["mcp"][name] = {"enabled": True, **{k: body[k] for k in keys if k in body}}
                if body["transport"] == "stdio":
                    s["mcp_headers"].pop(name, None)
                return self._send(200, next(x for x in mcp_doc(s)["servers"] if x["name"] == name))
            if name not in s["mcp"]:
                return self._error(404, "not_found", name)
            if method == "DELETE" and not rest:
                del s["mcp"][name]
                s["mcp_headers"].pop(name, None)
                return self._send(204, None)
            if method == "PUT" and rest == ["headers"]:
                if s["mcp"][name]["transport"] == "stdio":
                    return self._error(422, "invalid_mcp_server", "Invalid MCP server: stdio servers take no secret headers.")
                stored = s["mcp_headers"].setdefault(name, {})
                for header, value in (body.get("headers") or {}).items():
                    if value is None:
                        stored.pop(header, None)
                    else:
                        stored[header] = value
                return self._send(200, {"headers_set": sorted(stored)})
            if method == "POST" and rest == ["test"]:
                d = s["mcp"][name]
                if d["transport"] == "stdio":
                    return self._send(200, {"ok": None, "note": "stdio servers are tested when an agent starts"})
                if "unreachable" in (d.get("url") or ""):
                    return self._send(200, {"ok": False, "tools": [], "protocol_version": None,
                                            "error": {"code": "unreachable", "message": "connect timeout after 10 s"}})
                if not s["mcp_headers"].get(name):
                    return self._send(200, {"ok": False, "tools": [], "protocol_version": None,
                                            "error": {"code": "rejected", "message": "401 Unauthorized"}})
                tools = (["search_issues", "get_issue", "list_projects", "get_document", "search_documentation"]
                         if name == "linear-docs" else SEIJI_TOOLS if is_seiji(d) else ["echo", "lookup_ticket_context"])
                result = {"ok": True, "tools": tools, "protocol_version": "2025-11-25"}
                if is_seiji(d):
                    result["me"] = SEIJI_ME
                return self._send(200, result)
            return self._error(404, "not_found", "/".join(parts))

        def _setup(self, method: str, path: str, body: dict, st: dict) -> None:
            """Setup actions (api/setup.py): each verifies first and refuses to store what fails."""
            if method == "PUT" and path == "/v1/setup/tracker":
                if body.get("kind") == "linear":
                    key = body.get("api_key") or ""
                    if not key.startswith("lin_api_") or "bad" in key:
                        return self._error(422, "linear_unauthorized", "Linear rejected the API key.")
                    if body.get("team_key") and body["team_key"] != "ENG":
                        return self._error(422, "linear_team_not_found",
                                           f"Linear has no team with the key {body['team_key']}.")
                    st["tracker"] = {"kind": "linear", "team_key": body.get("team_key"),
                                     "project_slug": body.get("project_slug")}
                else:
                    if body.get("owner") != "acme" or body.get("project_number") != 7:
                        return self._error(422, "github_project_not_found", "GitHub has no such project for this App.")
                    st["tracker"] = {"kind": "github", "owner": "acme", "project_number": 7,
                                     "status_field": body.get("status_field") or "Status"}
                return self._send(200, {"kind": st["tracker"]["kind"], "states": BOARD_STATES,
                                        "warnings": [{"code": "state_missing", "state": "Rework"}],
                                        "restarted": ["symphony-daemon"]})
            if method == "PUT" and path == "/v1/setup/github-app":
                if "PRIVATE KEY" not in (body.get("private_key") or ""):
                    return self._error(422, "invalid_private_key", "The private key is not a PEM RSA key.")
                if not str(body.get("app_id", "")).isdigit():
                    return self._error(422, "invalid_github_app", "The App ID must be a number.")
                # Minting an installation token and listing repositories takes a moment at GitHub;
                # the scenarios photograph the card while it verifies.
                time.sleep(1.5)
                st["repositories"], st["missing"] = list(INSTALLED_REPOS), []
                return self._send(200, {"repositories": INSTALLED_REPOS, "missing_permissions": [],
                                        "restarted": ["symphony-daemon", "symphony-merger"]})
            if method == "PUT" and path == "/v1/setup/repositories":
                repo = body.get("default_repo")
                if repo is not None and repo not in INSTALLED_REPOS:
                    return self._error(422, "repo_not_installed", f"The GitHub App is not installed on {repo}.")
                if "default_repo" in body:
                    st["default_repo"] = repo
                # Merge maps: the client sends the FULL map each time.
                if "base_branches" in body:
                    st["base_branches"] = {k: v for k, v in (body["base_branches"] or {}).items() if v}
                if "runtimes" in body:
                    st["repo_runtimes"] = {k: v for k, v in (body["runtimes"] or {}).items() if v}
                if "components" in body:
                    st["components"] = {k: v for k, v in (body["components"] or {}).items() if v}
                _, detail, _ = repositories_detail(st, fake.state["issues"], fake.live_mergers())
                return self._send(200, {
                    "default_repo": st.get("default_repo"),
                    "base_branches": st.get("base_branches", {}),
                    "repo_runtimes": st.get("repo_runtimes", {}),
                    "components": st.get("components", {}),
                    "mergers": detail["mergers"],
                    "without_merger": detail["without_merger"]})
            tested = {"ok": True, "error": None, "at": iso_ago(0)}
            if method == "PUT" and path == "/v1/setup/notifications":
                for provider in ("slack", "discord"):
                    if body.get(provider):  # a new channel gets a test message before it is stored
                        st["notifications"][provider] = True
                st["tested"] = bool(st["notifications"])
                return self._send(200, {p: {"configured": p in st["notifications"],
                                            "last_test": tested if p in st["notifications"] else None}
                                        for p in ("slack", "discord")} | {"restarted": ["symphony-daemon"]})
            if method == "POST" and path == "/v1/setup/notifications/test":
                if not st["notifications"]:
                    return self._error(409, "notifications_not_configured", "No Slack or Discord channel is configured.")
                failed = {"ok": False, "error": "unreachable", "at": iso_ago(0)}
                return self._send(200, {"results": {p: failed if v == "unreachable" else tested
                                                    for p, v in sorted(st["notifications"].items())}})
            if method == "POST" and path == "/v1/setup/webhooks":
                if st["webhooks"] and not body.get("rotate"):
                    return self._error(409, "webhooks_exist", "Webhook secrets exist. Rotate them to get new ones.")
                st["webhooks"] = True
                # As the API: `/v1/webhooks/<source>`, 64-hex secrets and GitHub's event list.
                base = f"http://{self.headers.get('Host')}"
                return self._send(200, {
                    "linear": {"url": base + "/v1/webhooks/linear", "secret": hashlib.sha256(b"linear").hexdigest()},
                    "github": {"url": base + "/v1/webhooks/github", "secret": hashlib.sha256(b"github").hexdigest(),
                               "events": list(GITHUB_EVENTS)},
                    "restarted": ["symphony-daemon", "symphony-merger", "symphony-api"],
                })
            if method == "PUT" and path == "/v1/setup/assistant":
                return self._assistant(body, st)
            return self._error(404, "not_found", path)

        def _assistant(self, body: dict, st: dict) -> None:
            """`PUT /v1/setup/assistant`: checks each transport before it stores anything. A
            transport the body leaves out is turned off; repositories left out are kept."""
            slack, discord = body.get("slack"), body.get("discord")
            if not slack and not discord:
                return self._error(422, "invalid_assistant", "Name at least one Slack or Discord channel.")
            prev = st["assistant"] or {}
            out = {"slack": None, "discord": None}
            if slack:
                token = slack.get("app_token")
                if token is not None and (not token.startswith("xapp-") or "bad" in token):
                    return self._error(422, "slack_app_token_rejected",
                                       "Slack rejected the app token. Use an app-level token (xapp-…) with connections:write.")
                if token is None and not prev.get("slack"):
                    return self._error(422, "invalid_request", "invalid request: slack.app_token")
                if not st["notifications"].get("slack"):
                    return self._error(422, "slack_bot_token_missing",
                                       "Set up Slack in Notifications first. The assistant uses its bot token.")
                out["slack"] = {"configured": True, "connected": True, "channels": slack.get("channels") or []}
            if discord:
                if "discord" not in st["notifications"]:
                    return self._error(422, "discord_bot_token_missing",
                                       "Set up Discord in Notifications first. The assistant uses its bot token.")
                out["discord"] = {"configured": True, "connected": True, "channels": discord.get("channels") or [],
                                  "intent": "full"}
            repos = body.get("repositories") or prev.get("repositories")
            st["assistant"] = {"slack": out["slack"], "discord": out["discord"], "repositories": repos}
            return self._send(200, {**out, "repositories": repos or INSTALLED_REPOS, "restarted": ["symphony-assistant"]})

        def do_POST(self) -> None:
            self._write("POST")

        def do_PUT(self) -> None:
            self._write("PUT")

        def do_DELETE(self) -> None:
            self._write("DELETE")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log")
    parser.add_argument("--ready", help="write the bound URL here once listening")
    parser.add_argument("--state", choices=("team", "empty", "unconfigured", "unreachable-discord", "seiji"),
                        default="team")
    parser.add_argument("--redis-url", required=True, help="disposable Redis for the MergerStore")
    args = parser.parse_args()
    state = {"team": team_state, "empty": empty_state, "unconfigured": unconfigured_state,
             "unreachable-discord": unreachable_discord_state, "seiji": seiji_state}[args.state]()

    client = redis.Redis.from_url(args.redis_url, decode_responses=True)
    if args.state in ("team", "seiji"):
        seed_merger(client, MERGER_NAMESPACE)
        # The team tracks tickets in acme/billing too: its merger runs, idle.
        MergerStore(client, MERGER_NAMESPACE, "acme/billing").register(ttl_ms=3_600_000)
    else:
        # A fresh install's merger for the default repository (acme/web) is up and beating.
        MergerStore(client, MERGER_NAMESPACE, "acme/web").register(ttl_ms=3_600_000)
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(Fake(state, args.log, client, MERGER_NAMESPACE)))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    if args.ready:
        with open(args.ready + ".tmp", "w", encoding="utf-8") as fh:
            fh.write(url)
        os.replace(args.ready + ".tmp", args.ready)
    server.serve_forever()


if __name__ == "__main__":
    main()
