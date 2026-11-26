"""An in-process control API implementing the `/v1` contract (docs/api.md) for the CLI
and ACP bridge tests: FastAPI on a random port over the real `SessionStore`/`TeamConfig` on a
disposable Redis, with a real SSE stream that tails the session's event Stream. Other test
modules import `fake_api` from here."""

from __future__ import annotations

import json
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from symphony import __version__
from symphony.sessions import SessionStore
from symphony.teamconfig import ConflictError, TeamConfig

ROLES = {"viewer": 0, "operator": 1, "admin": 2}
TOKENS = {
    "sym_v1_viewer": {"name": "carol", "role": "viewer", "token_id": "v1"},
    "sym_o1_operator": {"name": "dave", "role": "operator", "token_id": "o1"},
    "sym_a1_admin": {"name": "alice", "role": "admin", "token_id": "a1"},
}


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class FakeApi:
    def __init__(self, client) -> None:
        self.store = SessionStore(client, "test")
        self.config = TeamConfig(client, "test")
        self.commands: list[dict] = []
        self.merger_actions: list[dict] = []
        self.merger_repos: list[dict] = [
            {"repo": "acme/web", "alive": True,
             "reviewing": [{"pr": 12, "title": "Fix login", "phase": "review", "started_at": 1}],
             "parked": [], "train": {"lease": None, "records": []}, "batch": {},
             "queue": [{"pr": 14}]},
            {"repo": "acme/api", "alive": False, "reviewing": [], "parked": [],
             "train": {"lease": None, "records": []}, "batch": {}, "queue": []},
        ]
        self.requests: list[tuple[str, str, dict]] = []
        self.sse_connects: list[str | None] = []
        self.drop_after: int | None = None  # close each SSE response after N events
        self.closing = threading.Event()
        self.tokens = {"t1": {"id": "t1", "name": "carol", "role": "viewer", "created_at": 1}}
        self.providers: dict[str, dict] = {
            "local-llm": {"name": "Local LLM", "api": "openai-completions",
                          "baseUrl": "http://llm:8000/v1", "auth": "none", "models": [{"id": "m"}]}}
        self.providers_revision = 7
        self.rate_limits: list[dict] = []
        # The shape of `admin_read.issues()` items (the fields the CLI reads).
        self.issues = [{"issue_id": "iss-1", "identifier": "ENG-1", "title": "Fix login",
                        "status": "running", "phase": "running", "reason": None,
                        "tracker_state": "In Progress", "pr_url": None, "held": None,
                        "updated_at": "2026-11-16T00:46:41.341000Z"}]
        # First-run setup (`/v1/setup`): what each step has stored. Statuses are computed from
        # it on every read, as the real API computes them from real state.
        self.setup: dict = {"tracker": None, "github_app": None, "default_repo": None,
                            "slack": None, "discord": None, "tests": {}, "webhooks": None,
                            "assistant": None, "app_token": None}
        # The assistant's heartbeat, as `GET /v1/setup` reads it (None: no heartbeat).
        self.assistant_beat: dict | None = None
        self.setup_app_token = "xapp-good"
        # MCP servers (`/v1/admin/mcp*`): definitions and the stored header values.
        self.mcp: dict[str, dict] = {}
        self.mcp_headers: dict[str, dict[str, str]] = {}
        self.setup_failing_channels: set[str] = set()
        # The release the server runs (`version` in the overview): the target of `symphony update`.
        self.server_version = __version__
        # Values the fake "verifies" against, standing in for Linear, GitHub, Slack and Discord.
        self.setup_linear_key = "lin_api_good"
        self.setup_app_id = "101"
        self.app = self._build()
        self.url = ""
        self._server: uvicorn.Server | None = None

    # -- auth --------------------------------------------------------------
    def _who(self, request: Request, role: str = "viewer"):
        auth = request.headers.get("authorization", "")
        ident = TOKENS.get(auth.removeprefix("Bearer ").strip()) if auth.startswith("Bearer ") else None
        if ident is None:
            return None, _err(401, "unauthorized", "invalid token")
        if ROLES[ident["role"]] < ROLES[role]:
            return None, _err(403, "forbidden", f"requires {role}")
        return ident, None

    def _build(self) -> FastAPI:
        app = FastAPI()
        api = self

        @app.middleware("http")
        async def record(request: Request, call_next):
            body = await request.body()
            api.requests.append((request.method, request.url.path,
                                 {"headers": dict(request.headers),
                                  "body": json.loads(body) if body else None}))
            return await call_next(request)

        @app.get("/v1/me")
        def me(request: Request):
            ident, err = api._who(request)
            return err or ident

        @app.get("/v1/overview")
        def overview(request: Request):
            _, err = api._who(request)
            if err:
                return err
            return JSONResponse({
                "version": api.server_version,
                # The real API (admin_read) sends ISO 8601 here, not epoch ms.
                "daemon": {"alive": True, "paused": False,
                           "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())},
                "counts": {"running": 1, "waiting_input": 1, "queued": 2, "retrying": 0,
                           "in_review": 1, "merging": 0,
                           "held": sum(1 for i in api.issues if i.get("held"))},
                "tokens_today": 123456, "cost_today": 4.5,
                "merger": {"reviewing": 1, "train": 0, "parked": 0},
                "config_generation": api.config.get()["generation"],
                "rate_limits": api.rate_limits,
                "held": [{"issue_id": i["issue_id"], "identifier": i["identifier"], **i["held"]}
                         for i in api.issues if i.get("held")],
            }, headers={"ETag": '"1"'})

        @app.get("/v1/sessions")
        def sessions(request: Request, state: str | None = None, limit: int = 50):
            _, err = api._who(request)
            return err or {"items": api.store.list(limit=limit, states=[state] if state else None)}

        @app.get("/v1/sessions/{sid}")
        def session(sid: str, request: Request):
            _, err = api._who(request)
            if err:
                return err
            meta = api.store.meta(sid)
            if meta is None:
                return _err(404, "not_found", f"session {sid}")
            last = api.store.last_event_id(sid)
            return {"meta": meta, "transcript": api.store.transcript(sid), "last_event_id": last}

        @app.get("/v1/sessions/{sid}/events")
        def events(sid: str, request: Request):
            _, err = api._who(request)
            if err:
                return err
            if api.store.meta(sid) is None:
                return _err(404, "not_found", f"session {sid}")
            after = request.headers.get("last-event-id")
            api.sse_connects.append(after)
            limit = api.drop_after

            def stream():
                cursor, sent = after, 0
                yield ": ping\n\n"
                while not api.closing.is_set():
                    for eid, kind, data in api.store.read_events(sid, cursor, block_ms=200):
                        cursor = eid
                        yield f"id: {eid}\nevent: {kind}\ndata: {json.dumps(data)}\n\n"
                        sent += 1
                        if limit is not None and sent >= limit:
                            return

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.post("/v1/sessions/{sid}/messages")
        async def messages(sid: str, request: Request):
            ident, err = api._who(request, "operator")
            if err:
                return err
            body = await request.json()
            meta = api.store.meta(sid)
            if meta is None:
                return _err(404, "not_found", f"session {sid}")
            mode = body.get("mode", "auto")
            if mode == "auto":
                req = meta.get("input_request")
                if req:
                    iid = api.store.send_intent(sid, "answer", actor=ident["name"],
                                                request_id=req["id"], text=body["text"])
                    return JSONResponse({"intent_id": iid}, status_code=202)
                mode = "steer" if meta.get("state") == "running" else "follow_up"
            iid = api.store.send_intent(sid, mode, actor=ident["name"], text=body["text"])
            return JSONResponse({"intent_id": iid}, status_code=202)

        @app.post("/v1/sessions/{sid}/interrupt")
        async def interrupt(sid: str, request: Request):
            ident, err = api._who(request, "operator")
            if err:
                return err
            body = await request.json()
            iid = api.store.send_intent(sid, "interrupt", actor=ident["name"], reason=body.get("reason"))
            return JSONResponse({"intent_id": iid}, status_code=202)

        @app.get("/v1/sessions/{sid}/export.jsonl")
        def export(sid: str, request: Request):
            _, err = api._who(request)
            if err:
                return err
            lines = "".join(json.dumps(e) + "\n" for e in api.store.transcript(sid))
            return Response(lines, media_type="application/x-ndjson")

        @app.get("/v1/issues")
        def issues(request: Request):
            _, err = api._who(request)
            return err or {"items": api.issues}

        @app.post("/v1/commands")
        async def commands(request: Request):
            ident, err = api._who(request, "operator")
            if err:
                return err
            body = await request.json()
            action, target = body.get("action"), body.get("target")
            # Like Control.command: reevaluate names a ticket; hold/release take an identifier,
            # a tracked issue id or `{issueId}` (internals §12); the rest act on the namespace.
            if action in ("hold", "release"):
                if isinstance(target, dict) and set(target) == {"issueId"}:
                    wanted = target["issueId"]
                elif isinstance(target, str) and target:
                    wanted = target
                else:
                    return _err(422, "invalid_request", "target must be an issue id/identifier or {issueId}")
                issue = next((i for i in api.issues
                              if wanted in (i["issue_id"], i["identifier"])), None)
                if issue is None:
                    return _err(404, "not_found", "tracked issue not found")
            elif target is not None and action != "reevaluate":
                return _err(422, "invalid_request", "this action targets the whole namespace")
            if not 8 <= len(str(body.get("reason", "")).strip()) <= 1000:
                return _err(422, "invalid_request", "reason must have 8-1000 characters")
            if action == "hold":
                if issue.get("held"):
                    return _err(409, "already_held", "this ticket is already held by a person")
                issue["held"] = {"by": ident["name"], "reason": body["reason"],
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            elif action == "release":
                if not issue.get("held"):
                    return _err(409, "not_held", "this ticket is not held")
                issue["held"] = None
            api.commands.append(body)
            return JSONResponse({"id": f"c{len(api.commands)}", **body}, status_code=202)

        @app.get("/v1/admin/config")
        def get_config(request: Request):
            _, err = api._who(request)
            return err or api.config.get()

        @app.put("/v1/admin/config")
        async def put_config(request: Request):
            ident, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            try:
                expected = int(request.headers.get("if-match", ""))
            except ValueError:
                return _err(428, "precondition_required", "If-Match: <generation>")
            current = api.config.get()["generation"]
            if current != expected:  # the real API answers 412 before trying to publish
                return _err(412, "precondition_failed", f"generation {current} is current")
            try:
                return api.config.publish({k: v for k, v in body.items() if k in ("models", "omp")},
                                          expected_generation=expected, actor=ident["name"],
                                          reason=body.get("reason", ""))
            except ConflictError as exc:
                return _err(409, "conflict", str(exc))

        @app.get("/v1/admin/config/observed")
        def observed(request: Request):
            _, err = api._who(request)
            return err or api.config.observed()

        @app.post("/v1/admin/config/observed")
        async def observe(request: Request):
            ident, err = api._who(request)
            if err:
                return err
            body = await request.json()
            api.config.observe(f"member:{ident['name']}", int(body["generation"]))
            return {"ok": True}

        # Providers (internals §13): stored items never leave with their key.
        catalog = {"anthropic", "openai", "amazon-bedrock"}

        def providers_doc():
            return {"revision": str(api.providers_revision), "storageReady": True, "items": [
                {**{k: v for k, v in item.items() if k != "apiKey"}, "id": pid,
                 "credentialConfigured": bool(item.get("apiKey"))}
                for pid, item in sorted(api.providers.items())]}

        @app.get("/v1/admin/providers")
        def list_providers(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            return JSONResponse(providers_doc(), headers={"ETag": f'"{api.providers_revision}"'})

        @app.put("/v1/admin/providers/{pid}")
        async def save_provider(pid: str, request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            match = request.headers.get("if-match")
            if match is None:
                return _err(428, "precondition_required", "an If-Match header is required")
            if match.strip('"') != str(api.providers_revision):
                return _err(412, "providers_conflict", "Providers changed; reload before saving again.")
            body = await request.json()
            if body.get("builtin") is True:
                if pid not in catalog:
                    return _err(422, "unknown_provider", f"{pid} is not an omp provider")
                if set(body) - {"builtin", "apiKey"}:
                    return _err(422, "invalid_provider", "builtin items carry only apiKey")
                if not body.get("apiKey") and not api.providers.get(pid, {}).get("apiKey"):
                    return _err(422, "provider_key_required", "An API key is required.")
                api.providers[pid] = {"builtin": True,
                                      "apiKey": body.get("apiKey") or api.providers[pid]["apiKey"]}
            else:
                api.providers[pid] = body
            api.providers_revision += 1
            return JSONResponse(providers_doc(), headers={"ETag": f'"{api.providers_revision}"'})

        @app.get("/v1/admin/tokens")
        def tokens(request: Request):
            _, err = api._who(request, "admin")
            return err or {"items": list(api.tokens.values())}

        @app.post("/v1/admin/tokens")
        async def create_token(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            tid = f"t{len(api.tokens) + 1}"
            api.tokens[tid] = {"id": tid, "name": body["name"], "role": body["role"], "created_at": 1}
            return JSONResponse({**api.tokens[tid], "token": f"sym_{tid}_secret"}, status_code=201)

        @app.delete("/v1/admin/tokens/{tid}")
        def revoke(tid: str, request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            if api.tokens.pop(tid, None) is None:
                return _err(404, "not_found", f"token {tid}")
            return Response(status_code=204)

        # Merger (contracts: merger store docstring): one snapshot per registered repository.
        @app.get("/v1/merger")
        def merger(request: Request):
            _, err = api._who(request)
            return err or {"repos": sorted(api.merger_repos, key=lambda r: r["repo"])}

        @app.post("/v1/merger/prs/{pr}/{action}")
        async def merger_action(pr: int, action: str, request: Request):
            ident, err = api._who(request, "operator")
            if err:
                return err
            if action not in ("bypass", "requeue", "release"):
                return _err(404, "not_found", "merger action not found")
            body = await request.json()
            if not 1 <= len(str(body.get("reason", "")).strip()) <= 1000:
                return _err(422, "invalid_request", "reason must have 1-1000 characters")
            registered = {r["repo"] for r in api.merger_repos}
            repo = body.get("repo")
            if repo is None:
                if len(registered) > 1:
                    return _err(422, "repo_required", "several repositories have a merger")
                if not registered:
                    return _err(404, "unknown_repo", "no merger has registered a repository")
                repo = next(iter(registered))
            elif repo not in registered:
                return _err(404, "unknown_repo", f"no merger serves {repo}")
            record = {"id": f"a{len(api.merger_actions)}", "pr": pr, "action": action,
                      "repo": repo, "actor": ident["name"], "reason": body["reason"], "at": 1}
            api.merger_actions.append(record)
            return JSONResponse(record, status_code=202)

        # First-run setup, in the shapes of `symphony/api/setup.py`. Verification is faked: the
        # Linear key, the App id and the channels are "real" when they match what the test set.
        def step(status, detail, error=None):
            return {"status": status, "detail": detail, "error": error}

        def setup_doc():
            s = api.setup
            tracker = s["tracker"] or {}
            tracker_detail = {"kind": tracker.get("kind"), "team_key": tracker.get("team_key"),
                              "project_slug": tracker.get("project_slug"),
                              "owner": tracker.get("owner"),
                              "project_number": tracker.get("project_number"),
                              "status_field": tracker.get("status_field"),
                              "credential": bool(tracker),
                              "states": ["Todo", "Done"] if tracker else []}
            installed = (s["github_app"] or {}).get("repositories", [])
            repos = [s["default_repo"]] if s["default_repo"] else []
            channels = {c: {"configured": bool(s[c]), "last_test": s["tests"].get(c)}
                        for c in ("slack", "discord")}
            configured = [c for c in channels if channels[c]["configured"]]
            failed = [c for c in configured if (channels[c]["last_test"] or {}).get("ok") is False]
            hooks = {source: {"secret": bool(s["webhooks"]), "in_use": True,
                              "last_delivery_at": None} for source in ("linear", "github")}
            steps = {
                "tracker": step("done" if tracker else "todo", tracker_detail),
                "github_app": step("done" if installed else "todo", {
                    "configured": bool(installed), "repositories": installed,
                    "missing_permissions": []}),
                "repositories": step("done" if repos else "todo", {
                    "default_repo": s["default_repo"], "repositories": repos,
                    "mergers": repos, "without_merger": []}),
                "models": step("done", {"roles": [
                    {"role": "implement", "provider": "amazon-bedrock", "model": "opus",
                     "credential": "host"}]}),
                "notifications": step(
                    "skipped" if not configured else "error" if failed else "done", channels,
                    {"code": "notification_failed",
                     "message": "the last test message failed on " + ", ".join(failed)}
                    if failed else None),
                "webhooks": step("todo" if s["webhooks"] else "skipped", hooks),
                "assistant": assistant_step(),
            }
            titles = {"tracker": "Tracker", "github_app": "GitHub App",
                      "repositories": "Repositories", "models": "Models",
                      "notifications": "Notifications", "assistant": "Assistant",
                      "webhooks": "Webhooks"}
            items = [{"id": i, "title": t,
                      "required": i not in ("notifications", "assistant", "webhooks"),
                      **steps[i]} for i, t in titles.items()]
            return {"complete": all(x["status"] == "done" for x in items if x["required"]),
                    "mode": "kubernetes", "steps": items}

        def assistant_step():
            config = api.setup["assistant"] or {}
            beat = api.assistant_beat or {}
            running = beat.get("state") == "running"
            detail = {
                "enabled": bool(config), "running": running, "heartbeat_at": beat.get("at"),
                "threads": beat.get("threads", 0) if running else 0,
                "repositories": config.get("repositories"),
                "slack": {"configured": bool(api.setup["app_token"]),
                          "connected": running and bool((beat.get("slack") or {}).get("connected")),
                          "channels": (config.get("slack") or {}).get("channels", [])},
                "discord": {"configured": True,
                            "connected": running
                            and bool((beat.get("discord") or {}).get("connected")),
                            "channels": (config.get("discord") or {}).get("channels", []),
                            "intent": "full" if config.get("discord") else None},
            }
            if not config:
                return step("skipped", detail)
            return step("done" if running else "todo", detail)

        @app.put("/v1/setup/assistant")
        async def setup_assistant(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            if not (body.get("slack") or body.get("discord")):
                return _err(422, "invalid_request", "send slack and/or discord")
            if body.get("slack"):
                token = body["slack"].get("app_token") or api.setup["app_token"]
                if not token:
                    return _err(422, "slack_app_token_missing",
                                "give the Slack app-level token (xapp-…)")
                if token != api.setup_app_token:
                    return _err(422, "slack_app_token_rejected",
                                "Slack rejected the app token (invalid_auth).")
                api.setup["app_token"] = token
            repos = body.get("repositories") or None
            if "repositories" not in body and api.setup["assistant"]:
                repos = api.setup["assistant"].get("repositories")
            api.setup["assistant"] = {"slack": body.get("slack") and {
                "channels": body["slack"]["channels"]}, "discord": body.get("discord"),
                "repositories": repos}
            return {"slack": {"connected": True, "channels": body["slack"]["channels"]}
                    if body.get("slack") else None,
                    "discord": {"connected": True, "channels": body["discord"]["channels"],
                                "intent": "full"} if body.get("discord") else None,
                    "repositories": repos, "restarted": ["symphony-assistant"]}

        # MCP servers, in the shapes of `symphony/api/admin.py` (contract A4).
        def mcp_view(name: str) -> dict:
            return {"name": name, **api.mcp[name],
                    "headers_set": sorted(api.mcp_headers.get(name, {}))}

        @app.get("/v1/admin/mcp")
        def mcp_list(request: Request):
            _, err = api._who(request, "admin")
            return err or {"servers": [mcp_view(n) for n in sorted(api.mcp)],
                           "roles": ["implementer", "reviewer", "assistant", "researcher",
                                     "planner"]}

        @app.put("/v1/admin/mcp/{name}")
        async def mcp_put(name: str, request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            if body.get("transport") in ("http", "sse") and not str(
                    body.get("url", "")).startswith("https://"):
                return _err(422, "invalid_mcp_server", "url: use https://")
            api.mcp[name] = body
            return mcp_view(name)

        @app.delete("/v1/admin/mcp/{name}")
        def mcp_delete(name: str, request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            if name not in api.mcp:
                return _err(404, "not_found", "mcp server not found")
            api.mcp.pop(name)
            api.mcp_headers.pop(name, None)
            return Response(status_code=204)

        @app.put("/v1/admin/mcp/{name}/headers")
        async def mcp_put_headers(name: str, request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            if name not in api.mcp:
                return _err(404, "not_found", "mcp server not found")
            stored = api.mcp_headers.setdefault(name, {})
            for header, value in (await request.json())["headers"].items():
                if value is None:
                    stored.pop(header, None)
                else:
                    stored[header] = value
            return {"headers_set": sorted(stored)}

        @app.post("/v1/admin/mcp/{name}/test")
        def mcp_test(name: str, request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            if name not in api.mcp:
                return _err(404, "not_found", "mcp server not found")
            if api.mcp[name]["transport"] == "stdio":
                return {"ok": None, "note": "stdio servers are tested when an agent starts"}
            if not api.mcp_headers.get(name):
                return {"ok": False, "tools": [], "protocol_version": None,
                        "error": {"code": "rejected", "message": "the server answered 401"}}
            return {"ok": True, "tools": ["echo", "lookup_ticket_context"],
                    "protocol_version": "2025-11-25"}

        @app.get("/v1/setup")
        def setup_status(request: Request):
            _, err = api._who(request)
            return err or setup_doc()

        @app.put("/v1/setup/tracker")
        async def setup_tracker(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            warnings = [{"code": "state_missing", "state": "Rework"}]
            if body.get("kind") == "linear":
                if bool(body.get("team_key")) == bool(body.get("project_slug")):
                    return _err(422, "invalid_request", "give exactly one of team_key or project_slug")
                if body.get("api_key") != api.setup_linear_key:
                    return _err(422, "linear_unauthorized", "Linear rejected the API key")
                if body.get("team_key") not in (None, "ENG"):
                    return _err(422, "linear_team_not_found",
                                f"Linear has no team with key {body['team_key']!r}")
                api.setup["tracker"] = {k: v for k, v in body.items() if k != "api_key"}
                return {"kind": "linear", "states": ["Todo", "In Progress", "Done"],
                        "warnings": warnings, "restarted": ["symphony-daemon", "symphony-api"]}
            if not api.setup["github_app"]:
                return _err(409, "github_app_required", "set up the GitHub App before a GitHub tracker")
            if body.get("owner") != "acme" or body.get("project_number") != 3:
                return _err(422, "github_project_not_found",
                            f"GitHub project {body.get('owner')}#{body.get('project_number')} "
                            "was not found or the App cannot read it")
            api.setup["tracker"] = body
            return {"kind": "github", "states": ["Todo", "In Progress", "Done"], "warnings": [],
                    "restarted": []}

        @app.put("/v1/setup/github-app")
        async def setup_github_app(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            if "PRIVATE KEY" not in str(body.get("private_key", "")):
                return _err(422, "invalid_private_key", "the private key is not a valid PEM RSA key")
            if body.get("app_id") != api.setup_app_id:
                return _err(422, "github_app_unauthorized", "GitHub rejected the App id or private key")
            api.setup["github_app"] = {"repositories": ["acme/web"]}
            return {"repositories": ["acme/web"], "missing_permissions": ["checks"],
                    "restarted": ["symphony-daemon", "symphony-merger", "symphony-api"]}

        @app.put("/v1/setup/repositories")
        async def setup_repositories(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            repo = (await request.json()).get("default_repo")
            installed = (api.setup["github_app"] or {}).get("repositories")
            if repo and installed is not None and repo not in installed:
                return _err(422, "repo_not_installed", f"the GitHub App is not installed on {repo}")
            api.setup["default_repo"] = repo
            return {"default_repo": repo, "mergers": [], "without_merger": [repo] if repo else []}

        @app.put("/v1/setup/notifications")
        async def setup_notifications(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            changes = {c: body[c] for c in ("slack", "discord") if c in body}
            if not changes:
                return _err(422, "invalid_request", "send slack and/or discord")
            for name, values in changes.items():
                if values is not None and "broken" in json.dumps(values):
                    api.setup["tests"][name] = {"ok": False, "error": "rejected", "at": 1}
                    return _err(422, "notification_unverified",
                                f"the {name} test message was rejected; nothing was stored")
            for name, values in changes.items():  # the other channel stays as it is
                api.setup[name] = values
                if values is None:
                    api.setup["tests"].pop(name, None)
                else:
                    api.setup["tests"][name] = {"ok": True, "error": None, "at": 1}
            return {**{c: {"configured": bool(api.setup[c]), "last_test": api.setup["tests"].get(c)}
                       for c in ("slack", "discord")}, "restarted": ["symphony-daemon"]}

        @app.post("/v1/setup/notifications/test")
        def setup_notifications_test(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            results = {}
            for name in ("slack", "discord"):
                if api.setup[name]:
                    ok = name not in api.setup_failing_channels
                    results[name] = {"ok": ok, "error": None if ok else "unreachable", "at": 2}
                    api.setup["tests"][name] = results[name]
            if not results:
                return _err(409, "notifications_not_configured", "no Slack or Discord is configured")
            return {"results": results}

        @app.post("/v1/setup/webhooks")
        async def setup_webhooks(request: Request):
            _, err = api._who(request, "admin")
            if err:
                return err
            body = await request.json()
            if api.setup["webhooks"] and not body.get("rotate"):
                return _err(409, "webhooks_exist", "webhook secrets are already set and cannot be "
                                                   "shown again; send rotate: true")
            n = (api.setup["webhooks"] or 0) + 1
            api.setup["webhooks"] = n
            return JSONResponse({
                "linear": {"url": f"{api.url}/v1/webhooks/linear", "secret": f"lin_whsec_{n}"},
                "github": {"url": f"{api.url}/v1/webhooks/github", "secret": f"gh_whsec_{n}",
                           "events": ["pull_request", "check_suite", "push"]},
                "restarted": ["symphony-api"],
            }, headers={"Cache-Control": "no-store"})

        # Read projections, in the shapes documented at the top of `symphony/api/board.py`.
        fresh = {"status": "fresh", "observedAt": "2026-11-16T00:00:00Z", "note": ""}
        projections = {
            "/v1/repos": {"observedAt": "2026-11-16T00:00:00Z", "items": [
                {"name": "acme/web", "runtime": "kubernetes", "routes": ["issue", "default"],
                 "issues": 2}],
                "sources": {"state": fresh, "workflow": fresh,
                            "learning": {"status": "stale", "observedAt": None,
                                         "note": "reflector heartbeat older than 1h"}},
                "limits": {"runs": 500, "resources": 200, "truncated": False}},
            "/v1/infra": {"observedAt": "2026-11-16T00:00:00Z", "paused": False,
                          "heartbeatAt": "2026-11-16T00:00:00Z",
                          "services": [{"name": "redis", "role": "redis", "status": "healthy",
                                        "detail": "ok"},
                                       {"name": "symphony-api-7d9", "role": "api",
                                        "status": "degraded", "detail": "not_ready"}],
                          "resources": [{"name": "sym-eng-1", "kind": "Pod", "status": "Running",
                                         "component": "workspace", "detail": "ready",
                                         "activePods": None, "createdAt": "2026-11-16T00:00:00Z"},
                                        {"name": "sym-ws-eng-1", "kind": "PersistentVolumeClaim",
                                         "status": "Bound", "component": "workspace",
                                         "detail": "bound", "activePods": None,
                                         "createdAt": "2026-11-16T00:00:00Z"}],
                          "sources": {"state": fresh, "kubernetes": fresh},
                          "limits": {"resources": 200, "truncated": False}},
            "/v1/telemetry": {"observedAt": "2026-11-16T00:00:00Z",
                              "totals": {"input": 2000, "output": 1000, "cacheRead": 0,
                                         "cacheWrite": 0, "total": 3000, "costEstimate": 0.3},
                              "models": [{"model": "opus", "requests": 4, "input": 2000, "output": 1000}],
                              "monthly": [{"month": "2026-11", "dispatched": 3, "rework": 1,
                                           "prsOpened": 2, "prsMerged": 1, "learningPrs": 0}],
                              "sessions": [{"sid": "iss-1:1", "identifier": "ENG-1", "kind": "implement",
                                            "state": "running", "model": "opus", "turn": 2,
                                            "tokens": {"input": 1, "output": 1, "total": 2, "cost": 0.1},
                                            "updatedAt": "2026-11-16T00:00:00Z"}],
                              "sources": {"telemetry": fresh, "metrics": fresh},
                              "limits": {"truncated": False, "sessions": 200}},
            "/v1/learning": {"observedAt": "2026-11-16T00:00:00Z", "items": [
                {"id": "l1", "identifier": "ENG-7", "repo": "acme/web", "status": "completed",
                 "provenance": "retrospective", "outcome": "pr_opened",
                 "prUrl": "https://github.com/acme/web/pull/9", "updatedAt": "2026-11-16T00:00:00Z"},
                {"id": "l2", "identifier": "ENG-8", "repo": "acme/web", "status": "completed",
                 "provenance": "retained", "outcome": "nothing_to_learn", "prUrl": None,
                 "updatedAt": "2026-11-16T00:00:00Z"}],
                "sources": {"learning": fresh}, "limits": {"truncated": False}},
        }

        def projection_route(path: str):
            def route(request: Request):
                _, err = api._who(request)
                return err or projections[path]
            app.get(path)(route)

        for path in projections:
            projection_route(path)

        audit = [
            {"id": "e2", "time": "2026-11-16T00:00:02Z", "actor": "alice", "action": "token.create",
             "target": "token:t2", "result": "succeeded", "reason": "operator bob", "commandId": None},
            {"id": "e1", "time": "2026-11-16T00:00:01Z", "actor": "dave", "action": "pause",
             "target": "namespace", "result": "accepted", "reason": "holiday freeze", "commandId": "c"},
        ]

        @app.get("/v1/audit")
        def audit_route(request: Request, cursor: str | None = None, limit: int = 50):
            _, err = api._who(request)
            if err:
                return err
            start = 1 if cursor == "c1" else 0
            page = audit[start:start + limit]
            more = start + limit < len(audit)
            return {"items": page, "nextCursor": f"c{start + limit}" if more else None}

        return app

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning",
                                lifespan="off", timeout_graceful_shutdown=1)
        self._server = uvicorn.Server(config)
        thread = threading.Thread(target=self._server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake API did not start")
            time.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self._thread = thread

    def stop(self) -> None:
        self.closing.set()
        if self._server is not None:
            self._server.should_exit = True
            self._thread.join(timeout=5)

    # -- data helpers ---------------------------------------------------------
    def seed_session(self, sid: str = "iss-1:1", **meta) -> str:
        fields = {"kind": "implement", "identifier": "ENG-1", "title": "Fix login redirect",
                  "issue_id": "iss-1", "seq": 1, "repo": "acme/web", "branch": "sym/eng-1",
                  "model": "amazon-bedrock/global.anthropic.claude-opus-5-5", **meta}
        self.store.open(sid, **fields)
        self.store.update(sid, state="running", turn=2,
                          tokens={"input": 1000, "output": 200, "total": 1200, "cost": 0.12})
        return sid


@pytest.fixture
def fake_api(redis_client):
    api = FakeApi(redis_client)
    api.start()
    try:
        yield api
    finally:
        api.stop()


def test_fake_api_enforces_bearer_and_roles(fake_api):
    import httpx

    assert httpx.get(f"{fake_api.url}/v1/me").status_code == 401
    ok = httpx.get(f"{fake_api.url}/v1/me", headers={"Authorization": "Bearer sym_v1_viewer"})
    assert ok.json()["name"] == "carol"
    sid = fake_api.seed_session()
    denied = httpx.post(f"{fake_api.url}/v1/sessions/{sid}/messages", json={"text": "x"},
                        headers={"Authorization": "Bearer sym_v1_viewer"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
