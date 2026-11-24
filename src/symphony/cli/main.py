"""`symphony`: the person's command line for Symphony (`--json` everywhere)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from symphony import __version__
from symphony.cli import creds, local, render, setup
from symphony.cli.client import Api, ApiError, reason_problem, sid_path


class CliError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _emit(args, data, text: str | Callable[[], str] | None = None) -> None:
    """JSON under `--json`, else the text. A callable is rendered only for text output,
    so a render problem never blocks the JSON the Tern plugin and scripts read."""
    if args.json or text is None:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=None if args.json else 2) + "\n")
    else:
        sys.stdout.write((text() if callable(text) else text).rstrip("\n") + "\n")


def _api() -> Api:
    return Api(creds.current())


def _read_stdin_value() -> str:
    return sys.stdin.read().strip()


# -- credentials -----------------------------------------------------------

def cmd_login(args) -> int:
    url = args.url or os.environ.get("SYMPHONY_URL") or input("Symphony URL: ")
    try:
        url = creds.normalize_url(url)
    except ValueError as exc:
        raise CliError("invalid_url", str(exc)) from exc
    token = args.token
    if token == "-":
        token = _read_stdin_value()
    elif not token:
        token = getpass.getpass("Token (sym_…): ").strip()
    if not token:
        raise CliError("missing_token", "Give the token with --token, or with --token - to read stdin.")
    api = Api(creds.Credential(url=url, token=token))
    try:
        me = api.me()
    except ApiError as exc:
        if exc.status == 401:
            raise CliError("invalid_token", "The server refused the token (401).") from exc
        raise
    storage = creds.save_login(url, token, me)
    where = "in the system keychain" if storage == "keychain" else f"in {creds.token_file()} (0600)"
    _emit(args, {"url": url, "name": me.get("name"), "role": me.get("role"),
                 "token_id": me.get("token_id"), "storage": storage},
          f"Logged in to {url} as {me.get('name')} ({me.get('role')}). The token is stored {where}.")
    return 0


def cmd_logout(args) -> int:
    url = creds.load_config().get("url")
    creds.forget(url)
    _emit(args, {"logged_out": True, "url": url},
          "The credential was removed." if url else "There was no saved login.")
    return 0


def cmd_whoami(args) -> int:
    cred = creds.current()
    me = Api(cred).me()
    _emit(args, {**me, "url": cred.url, "source": cred.source},
          f"{me.get('name')} ({me.get('role')}) on {cred.url} · token {me.get('token_id')}"
          + (" · from SYMPHONY_TOKEN" if cred.source == "env" else ""))
    return 0


def cmd_token_print(args) -> int:
    """For the Tern plugin host only: it keeps url+token in memory and never persists them."""
    cred = creds.current()
    name, role = cred.name, cred.role
    if not name or not role:
        me = Api(cred).me()
        name, role = me.get("name"), me.get("role")
    _emit(args, {"url": cred.url, "token": cred.token, "name": name, "role": role}, cred.token)
    return 0


_EXPIRES = re.compile(r"^(\d+)d$")


def cmd_token_create(args) -> int:
    body: dict = {"name": args.name, "role": args.role}
    if args.expires and args.expires != "never":
        match = _EXPIRES.match(args.expires)
        if not match:
            raise CliError("invalid_expires", "Use --expires <N>d (for example 90d) or never.")
        body["expires_days"] = int(match.group(1))
    doc = _api().post("/v1/admin/tokens", body)
    _emit(args, doc, "\n".join([
        f"Token {doc.get('id')} created for {doc.get('name', args.name)} ({doc.get('role', args.role)}):",
        "",
        f"  {doc.get('token')}",
        "",
        "This is the only time it is shown. Give it to the person over a secure channel.",
    ]))
    return 0


def cmd_token_list(args) -> int:
    doc = _api().get("/v1/admin/tokens")
    _emit(args, doc, lambda: render.tokens_table(doc.get("items", [])))
    return 0


def cmd_token_revoke(args) -> int:
    doc = _api().raw("DELETE", f"/v1/admin/tokens/{sid_path(args.id)}").body
    _emit(args, doc or {"revoked": args.id}, f"Token {args.id} revoked. This takes effect now.")
    return 0


# -- reading ------------------------------------------------------------------

def cmd_status(args) -> int:
    doc = _api().get("/v1/overview")
    _emit(args, doc, lambda: render.overview(doc))
    return 0


def cmd_sessions(args) -> int:
    items = _api().sessions(state=args.state, limit=args.limit)
    _emit(args, {"items": items}, lambda: render.sessions_table(items))
    return 0


def cmd_issues(args) -> int:
    doc = _api().get("/v1/issues")
    _emit(args, doc, lambda: render.issues_table(doc.get("items", [])))
    return 0


def _repo_name(value: str) -> str:
    """Merger repositories are `owner/name`, compared lower-cased (merger store contract)."""
    repo = value.strip().lower()
    if repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
        raise CliError("invalid_repo", f"Use --repo owner/name, not {value!r}.")
    return repo


def cmd_merger_status(args) -> int:
    doc = _api().get("/v1/merger")
    if args.repo:
        wanted = _repo_name(args.repo)
        doc = {**doc, "repos": [r for r in doc.get("repos") or [] if r.get("repo") == wanted]}
        if not doc["repos"]:
            raise CliError("unknown_repo", render.ERROR_TEXT["unknown_repo"])
    _emit(args, doc, lambda: render.merger(doc))
    return 0


_MERGER_DONE = {
    "bypass": "Bypass requested for PR #{pr}{where}. The merger skips its review.",
    "requeue": "PR #{pr}{where} is back in the merger queue.",
    "release": "PR #{pr}{where} is released from the merger.",
}


def _merger_action(action: str) -> Callable:
    """`POST /v1/merger/prs/{n}/{action}`. The API requires `repo` only when more than one
    repository has a merger; the CLI sends it whenever it is given."""
    def run(args) -> int:
        _check_reason("merger", args.reason)
        body: dict = {"reason": args.reason}
        if args.repo:
            body["repo"] = _repo_name(args.repo)
        doc = _api().post(f"/v1/merger/prs/{args.pr}/{action}", body)
        where = f" in {body['repo']}" if "repo" in body else ""
        _emit(args, doc, _MERGER_DONE[action].format(pr=args.pr, where=where))
        return 0
    return run


def _projection(path: str, renderer: Callable[[dict], str], **params: Callable) -> Callable:
    """Viewer read of one projection route; `params` maps query names to arg getters."""
    def run(args) -> int:
        doc = _api().get(path, **{k: get(args) for k, get in params.items()})
        _emit(args, doc, lambda: renderer(doc))
        return 0
    return run


def cmd_watch(args) -> int:
    from symphony.cli.watch import watch

    return watch(_api(), args.ref, as_json=args.json)


def cmd_chat(args) -> int:
    meta = _api().resolve(args.ref)
    sid = meta["sid"]
    if os.environ.get("TERM_PROGRAM") == "tern":
        argv = ["tern", "acp", "symphony", "--resume", sid]
        if args.json:
            _emit(args, {"sid": sid, "exec": argv})
        sys.stdout.flush()
        os.execvp("tern", argv)
    if args.json:
        _emit(args, {"sid": sid, "exec": None, "hint": f"tern acp symphony --resume {sid}"})
        return 0
    sys.stderr.write("Not in Tern: showing the session as text. In Tern, `symphony chat` opens "
                     "the native chat.\n")
    from symphony.cli.watch import watch

    return watch(_api(), sid, as_json=False)


# -- operating ----------------------------------------------------------------

def cmd_say(args) -> int:
    api = _api()
    sid = api.resolve(args.ref)["sid"]
    mode = "steer" if args.steer else "follow_up" if args.follow_up else "auto"
    doc = api.send_message(sid, " ".join(args.text), mode)
    label = {"steer": "steer", "follow_up": "follow-up", "auto": "auto"}[mode]
    _emit(args, doc, f"Message sent ({label}) · intent {doc.get('intent_id')}")
    return 0


def cmd_interrupt(args) -> int:
    _check_reason("interrupt", args.reason)
    api = _api()
    sid = api.resolve(args.ref)["sid"]
    doc = api.interrupt(sid, args.reason)
    _emit(args, doc, f"Interrupt sent · intent {doc.get('intent_id')}")
    return 0


def cmd_answer(args) -> int:
    api = _api()
    meta = api.resolve(args.ref)
    meta = api.session(meta["sid"]).get("meta") or meta
    request = meta.get("input_request")
    if not request:
        raise CliError("no_pending_question", f"{render.session_label(meta)} has no open question.")
    # `auto` answers the pending input request (docs/api.md).
    doc = api.send_message(meta["sid"], " ".join(args.text), "auto")
    _emit(args, {**doc, "request_id": request.get("id")},
          f"Answer sent to: {request.get('question')}")
    return 0


_COMMAND_DONE = {
    "pause": "Pause requested. Symphony takes no new tickets. Running tickets continue.",
    "resume": "Resume requested. Symphony takes tickets again.",
    "refresh": "Refresh requested. Symphony rereads the tracker now.",
    "reevaluate": "Reevaluation requested for {target}.",
}


def _command(action: str) -> Callable:
    """`POST /v1/commands`. pause/resume/refresh act on the whole namespace (the API refuses
    a target for them); only reevaluate names a ticket."""
    def run(args) -> int:
        target = getattr(args, "ticket", None)
        _check_reason("command", args.reason)
        doc = _api().command(action, target, args.reason)
        _emit(args, doc, _COMMAND_DONE[action].format(target=target))
        return 0
    return run


def _issue_target(ticket: str, issue_id: str | None = None) -> dict | str:
    """A hold/release target (internals §12): `{issueId}` when the session names its issue,
    else the identifier, which the API resolves (404 `not_found` if Symphony doesn't track it)."""
    return {"issueId": issue_id} if issue_id else ticket


def _actor(api: Api) -> str:
    return api.cred.name or api.me().get("name") or "cli"


def cmd_release(args) -> int:
    if args.reason is not None:
        _check_reason("command", args.reason)
    api = _api()
    reason = args.reason or f"returned by {_actor(api)}"
    doc = api.command("release", _issue_target(args.ticket), reason)
    _emit(args, doc, f"{args.ticket} returned to Symphony. Symphony can work on the ticket again.")
    return 0


def cmd_takeover(args) -> int:
    api = _api()
    meta = api.resolve(args.ticket)
    meta = api.session(meta["sid"]).get("meta") or meta
    sid, label = meta["sid"], render.session_label(meta)
    steps: dict = {"sid": sid}
    if not args.no_hold:
        # Hold first (internals §12): the daemon stops dispatching, retrying and continuing
        # this ticket; the interrupt then stops the turn now instead of at the next tick.
        target = _issue_target(meta.get("identifier") or args.ticket, meta.get("issue_id"))
        steps["hold"] = api.command("hold", target, f"taken over by {_actor(api)}")
        if meta.get("state") in ("running", "waiting_input", "starting"):
            steps["interrupt"] = api.interrupt(sid, "takeover: a person took over the session")
    transcript = local.cache_dir() / "takeover" / (sid.replace(":", "-").replace("/", "-") + ".jsonl")
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_bytes(api.export(sid))
    steps["transcript"] = str(transcript)
    steps["branch"] = local.fetch_branch(meta, Path(args.dir).resolve())
    argv = local.omp_resume_argv(transcript)
    steps["command"] = argv
    launch = not args.no_launch and not args.json and sys.stdin.isatty() and sys.stdout.isatty()
    ticket = meta.get("identifier") or args.ticket
    lines = [
        f"Takeover of {label}:",
        f"  ticket held: Symphony does not work on {ticket} until `symphony release {ticket}`"
        if steps.get("hold") else "  ticket not held (--no-hold)",
        "  agent turn interrupted" if steps.get("interrupt") else "  nothing was interrupted",
        f"  transcript: {transcript}",
        f"  branch: {steps['branch']['message']}",
        "",
        "To continue the conversation:" if not launch else "Opening omp with the conversation:",
        "  " + " ".join(_quote(a) for a in argv),
    ]
    _emit(args, steps, "\n".join(lines))
    if launch:
        sys.stdout.flush()
        os.chdir(args.dir)
        os.execvp(argv[0], argv)
    return 0


def _quote(arg: str) -> str:
    return f"'{arg}'" if re.search(r"[^\w@%+=:,./-]", arg) else arg


# -- admin ----------------------------------------------------------------------

def cmd_config_show(args) -> int:
    doc = _api().get("/v1/admin/config")
    _emit(args, doc, lambda: render.config_doc(doc))
    return 0


def parse_assignment(text: str) -> tuple[str, dict]:
    """`role=provider/model[:thinking]` → (role, {provider, model, thinking?}). The suffix is
    a thinking level only when it names one: model ids may contain `:` themselves."""
    from symphony.teamconfig import THINKING

    role, sep, value = text.partition("=")
    provider, slash, model = value.partition("/")
    if not sep or not role or not slash or not provider or not model:
        raise CliError("invalid_assignment", f"Use role=provider/model[:thinking], not {text!r}.")
    ref: dict = {"provider": provider}
    base, colon, level = model.rpartition(":")
    if colon and level in THINKING and base:
        ref["model"], ref["thinking"] = base, level
    else:
        ref["model"] = model
    return role.strip(), ref


def cmd_config_set(args) -> int:
    _check_reason("config", args.reason)
    api = _api()
    models = dict(parse_assignment(a) for a in args.assignments)
    current = api.get("/v1/admin/config")
    generation = int(current.get("generation", 0))
    doc = api.raw("PUT", "/v1/admin/config", json_body={"models": models, "reason": args.reason},
                  headers={"If-Match": str(generation)}).body
    changed = ", ".join(f"{r}={v['provider']}/{v['model']}" + (f":{v['thinking']}" if v.get("thinking") else "")
                        for r, v in models.items())
    _emit(args, doc, f"Generation {doc.get('generation', generation + 1)} published: {changed}.")
    return 0


def cmd_config_observed(args) -> int:
    api = _api()
    doc = api.get("/v1/admin/config/observed")
    generation = api.get("/v1/admin/config").get("generation") if not args.json else None
    _emit(args, doc, lambda: render.observed(doc, generation))
    return 0


def cmd_provider_list(args) -> int:
    doc = _api().get("/v1/admin/providers")
    _emit(args, doc, lambda: render.providers_table(doc))
    return 0


def _read_secret(prompt: str) -> str:
    """A secret never travels in argv: piped stdin, else a hidden prompt."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass(prompt).strip()


def cmd_provider_key(args) -> int:
    """Store the API key of an omp built-in provider (internals §13: `{builtin: true, apiKey}`)."""
    key = _read_secret(f"API key for {args.id}: ")
    if not key:
        raise CliError("missing_key", "Give the API key on stdin or at the prompt.")
    doc = setup.store_provider_key(_api(), args.id, key)
    _emit(args, doc, f"API key for {args.id} saved. Each agent uses it from its next attempt. "
                     "No restart is needed.")
    return 0


# -- MCP servers (admin) ------------------------------------------------------------

def cmd_mcp_list(args) -> int:
    doc = _api().get("/v1/admin/mcp")
    _emit(args, doc, lambda: render.mcp_servers(doc))
    return 0


def _pairs(items: list[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise CliError("invalid_env", f"Use {flag} NAME=value, not {item!r}.")
        out[key.strip()] = value
    return out


def mcp_definition(args) -> dict:
    """The `PUT /v1/admin/mcp/{name}` body from the flags. A PUT replaces the definition, so
    every field comes from this command line; stored secret headers stay. With `--preset`,
    the server fills what the flags leave out."""
    env = _pairs(args.env, "--env")
    agent_env = _pairs(args.agent_env, "--agent-env")
    transport = args.transport or ("http" if args.preset else None)
    if transport is None:
        raise CliError("invalid_mcp_server", "Give --transport, or --preset.")
    remote = transport in ("http", "sse")
    if remote and (args.command or args.arg or env):
        raise CliError("invalid_mcp_server", "--command, --arg and --env are for stdio servers.")
    if remote and not args.url:
        raise CliError("invalid_mcp_server", f"A {transport} server needs --url.")
    if not remote and (args.url or not args.command):
        raise CliError("invalid_mcp_server", "A stdio server needs --command and no --url.")
    if not remote and (args.http_base or agent_env or args.rewrite_origin or args.cleanup):
        raise CliError("invalid_mcp_server", "--http-base, --agent-env, --rewrite-origin and "
                                             "--cleanup are for http and sse servers.")
    body: dict = {"roles": list(dict.fromkeys(args.role)), "enabled": not args.disabled}
    if args.preset:
        body["preset"] = args.preset
    if args.transport:
        body["transport"] = args.transport
    elif not args.preset:
        body["transport"] = transport
    if remote:
        body["url"] = args.url
    else:
        body["command"] = args.command
        body["args"] = list(args.arg or [])
        body["env"] = env
    if args.timeout_ms is not None:
        body["timeout_ms"] = args.timeout_ms
    if args.instructions:
        body["instructions"] = True
    if args.http_base:
        body["http_base"] = args.http_base
    if agent_env:
        body["agent_env"] = agent_env
    if args.rewrite_origin:
        body["rewrite_origins"] = list(dict.fromkeys(args.rewrite_origin))
    if args.cleanup:
        parts = args.cleanup.split(":")
        if len(parts) != 4 or not all(parts):
            raise CliError("invalid_mcp_server",
                           "Use --cleanup ACQUIRE_TOOL:ID_FIELD:RELEASE_TOOL:RELEASE_ARG.")
        body["cleanup"] = dict(zip(("acquire_tool", "id_field", "release_tool", "release_arg"),
                                   parts, strict=True))
    return body


def cmd_mcp_set(args) -> int:
    body = mcp_definition(args)
    doc = _api().raw("PUT", f"/v1/admin/mcp/{sid_path(args.name)}", json_body=body).body
    _emit(args, doc, f"MCP server {args.name} saved for {', '.join(body['roles'])}. Agents "
                     "use it from their next start.")
    return 0


def cmd_mcp_viewer(args) -> int:
    """A viewer link to a device that a live session holds; open it in a browser."""
    doc = _api().post(f"/v1/mcp/{sid_path(args.name)}/viewer",
                      {"lease_id": args.lease, "control": bool(args.control)})
    _emit(args, doc, lambda: render.mcp_viewer(args.name, args.lease, doc))
    return 0


def cmd_mcp_remove(args) -> int:
    _api().raw("DELETE", f"/v1/admin/mcp/{sid_path(args.name)}")
    _emit(args, {"removed": args.name}, f"MCP server {args.name} removed.")
    return 0


def read_headers(text: str) -> dict:
    """Stdin JSON `{"headers": {name: value|null}}` or the bare map."""
    try:
        doc = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        raise CliError("invalid_json", "The headers on stdin are not JSON.") from None
    if isinstance(doc, dict) and set(doc) == {"headers"}:
        doc = doc["headers"]
    if (not isinstance(doc, dict) or not doc
            or not all(isinstance(k, str) and (v is None or isinstance(v, str))
                       for k, v in doc.items())):
        raise CliError("invalid_headers",
                       'Give a JSON object on stdin: {"Header-Name": "value"}; null removes one.')
    return doc


def cmd_mcp_headers(args) -> int:
    if sys.stdin.isatty():
        raise CliError("missing_secret", 'Pipe the headers as JSON on stdin: '
                                         '{"Header-Name": "value"}; null removes one.')
    headers = read_headers(sys.stdin.read())
    doc = _api().raw("PUT", f"/v1/admin/mcp/{sid_path(args.name)}/headers",
                     json_body={"headers": headers}).body
    names = doc.get("headers_set") or []
    _emit(args, doc, f"Headers of {args.name} saved. Stored: {', '.join(names) or 'none'}.")
    return 0


def cmd_mcp_test(args) -> int:
    doc = _api().post(f"/v1/admin/mcp/{sid_path(args.name)}/test", {})
    _emit(args, doc, lambda: render.mcp_test(args.name, doc))
    return 1 if doc.get("ok") is False else 0


# -- assistant -------------------------------------------------------------------------

def cmd_assistant_status(args) -> int:
    doc = setup.assistant_status(_api())
    _emit(args, doc, lambda: setup.assistant_status_text(doc, _style()))
    return 0


def cmd_assistant_memory_list(args) -> int:
    doc = _api().get("/v1/assistant/memory")
    _emit(args, doc, lambda: render.assistant_memory_people(doc))
    return 0


def cmd_assistant_memory_show(args) -> int:
    doc = _api().get(f"/v1/assistant/memory/{sid_path(args.login)}")
    _emit(args, doc, lambda: render.assistant_memory_items(doc))
    return 0


def cmd_assistant_memory_forget(args) -> int:
    doc = _api().raw("DELETE", f"/v1/assistant/memory/{sid_path(args.login)}/{sid_path(args.id)}").body
    _emit(args, doc, f"Forgot {args.id} of {doc.get('login', args.login)}: "
                     f"{(doc.get('removed') or {}).get('text', '')}".rstrip(": "))
    return 0


def cmd_assistant_memory_wipe(args) -> int:
    doc = _api().raw("DELETE", f"/v1/assistant/memory/{sid_path(args.login)}").body
    n = int(doc.get("removed", 0))
    _emit(args, doc, f"Wiped the memory of {doc.get('login', args.login)}: "
                     f"{n} item{'' if n == 1 else 's'} removed.")
    return 0


# -- first-run setup ---------------------------------------------------------------

def _style() -> setup.Style:
    return setup.Style(sys.stdout)


def _required_secret(prompt: str, what: str) -> str:
    value = _read_secret(prompt)
    if not value:
        raise CliError("missing_secret", f"Give the {what} on stdin or at the prompt.")
    return value


def cmd_setup(args) -> int:
    if args.status:
        doc = setup.get_status(_api())
        _emit(args, doc, lambda: setup.status_text(doc, _style()))
        return 0
    if args.json or not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CliError("not_a_terminal",
                       "The setup wizard needs a terminal. Run `symphony setup --status` to see "
                       "what is left, and `symphony setup <step> --help` to do one step.")
    return setup.terminal_wizard(_api())


def cmd_setup_tracker(args) -> int:
    body = setup.tracker_body(kind=args.kind, team=args.team, project=args.project,
                              owner=args.owner, project_number=args.project_number,
                              status_field=args.status_field)
    key = _required_secret("Linear API key: ", "Linear API key") if args.kind == "linear" else None
    doc = setup.put_tracker(_api(), body, key)
    _emit(args, doc, lambda: setup.tracker_text(doc, _style()))
    return 0


def cmd_setup_github_app(args) -> int:
    if args.private_key_file:
        try:
            pem = Path(args.private_key_file).expanduser().read_text()
        except OSError as exc:
            raise CliError("unreadable_file", f"Cannot read {args.private_key_file}.") from exc
    elif sys.stdin.isatty():
        raise CliError("missing_secret",
                       "Give the private key on stdin or with --private-key-file PATH.")
    else:
        pem = sys.stdin.read()
    doc = setup.put_github_app(_api(), app_id=args.app_id, installation_id=args.installation_id,
                               private_key=pem)
    _emit(args, doc, lambda: setup.github_app_text(doc, _style()))
    return 0


def cmd_setup_repositories(args) -> int:
    """`--branch`/`--runtime`/`--component` edit entries; the others stay (the API replaces the map)."""
    set_default = bool(args.clear_default_repo or args.default_repo is not None)
    has_flags = bool(args.branch or args.runtime or args.component)
    if not set_default and not has_flags:
        raise CliError("missing_option",
                       "Give --default-repo, --clear-default-repo, --branch, --runtime, or --component.")
    api = _api()
    step = None
    def _step_detail():
        nonlocal step
        if step is None:
            step = next((s for s in setup.get_status(api).get("steps") or []
                         if s.get("id") == "repositories"), {})
        return step.get("detail") or {}
    base_branches = None
    if args.branch:
        base_branches = dict(_step_detail().get("base_branches") or {})
        for pair in args.branch:
            repo, sep, branch = pair.partition("=")
            if not sep or repo.count("/") != 1:
                raise CliError("invalid_branch", f"Use --branch owner/name=branch, not {pair!r}.")
            base_branches[repo.strip().lower()] = branch.strip() or None
    runtimes_map = None
    if args.runtime:
        runtimes_map = dict(_step_detail().get("repo_runtimes") or {})
        for pair in args.runtime:
            repo, sep, runtime = pair.partition("=")
            if not sep or repo.count("/") != 1:
                raise CliError("invalid_runtime", f"Use --runtime owner/name=runtime, not {pair!r}.")
            runtimes_map[repo.strip().lower()] = runtime.strip() or None
    components_map = None
    if args.component:
        components_map = dict(_step_detail().get("components") or {})
        for spec in args.component:
            if ":" not in spec or "=" not in spec:
                raise CliError("invalid_component",
                               f"Use --component owner/name:comp=path[@runtime], not {spec!r}.")
            repo_comp, _, path_runtime = spec.partition("=")
            repo, _, comp = repo_comp.partition(":")
            repo = repo.strip().lower()
            if not path_runtime.strip():
                # owner/name:comp= clears the component
                if repo in components_map and comp in components_map[repo]:
                    del components_map[repo][comp]
                    if not components_map[repo]:
                        del components_map[repo]
            else:
                if "@" in path_runtime:
                    path, _, rt = path_runtime.rpartition("@")
                else:
                    path, rt = path_runtime, None
                components_map.setdefault(repo, {})
                cdef: dict = {"path": path.strip()}
                if rt:
                    cdef["runtime"] = rt.strip()
                components_map[repo][comp.strip()] = cdef
    doc = setup.put_repositories(api, None if args.clear_default_repo else args.default_repo,
                                 set_default=set_default, base_branches=base_branches,
                                 runtimes=runtimes_map, components=components_map)
    _emit(args, doc, lambda: setup.repositories_text(doc, _style()))
    return 0


def cmd_setup_channel(args) -> int:
    """`setup notifications slack|discord`: the API sends a test message before it stores the
    channel; `--remove` sends null. The other channel stays as it is."""
    channel = args.channel_kind
    name = setup.CHANNEL_NAME[channel]
    if args.remove:
        values = None
    elif args.webhook:
        values = {"webhook_url": _required_secret(f"{name} webhook URL: ", f"{name} webhook URL")}
    else:
        token = _required_secret(f"{name} bot token: ", f"{name} bot token")
        values = ({"bot_token": token, "channel": args.channel} if channel == "slack"
                  else {"bot_token": token, "channel_id": args.channel_id})
    doc = setup.put_notifications(_api(), channel, values)
    _emit(args, doc, lambda: setup.notifications_text(channel, doc, _style()))
    return 0


def cmd_setup_test(args) -> int:
    """Every channel's result is printed, also when one failed; the exit code is 1 then."""
    doc = setup.send_test_message(_api())
    _emit(args, doc, lambda: setup.test_result_text(doc, _style()))
    return 0 if all(r["ok"] for r in doc["results"]) else 1


def cmd_setup_webhooks(args) -> int:
    doc = setup.post_webhooks(_api(), args.rotate)
    _emit(args, doc, lambda: setup.webhooks_text(doc, _style()))
    return 0


def cmd_setup_assistant(args) -> int:
    """The Slack app-level token comes on stdin (first line) or a hidden prompt; empty keeps
    the stored one."""
    body = setup.assistant_body(slack_channels=args.slack_channel or [],
                                discord_channels=args.discord_channel or [],
                                repos=args.repo, app_token=None)
    if "slack" in body:
        token = _read_secret("Slack app-level token (xapp-…, empty keeps the stored one): ")
        if token:
            body["slack"]["app_token"] = token
    doc = setup.put_assistant(_api(), body)
    _emit(args, doc, lambda: setup.assistant_text(doc, _style()))
    return 0


def cmd_setup_skills(args) -> int:
    body: dict = {"repo": args.repo}
    if args.remove:
        body["remove"] = True
    else:
        if args.path:
            body["path"] = args.path
        if args.ref:
            body["ref"] = args.ref
        if args.role:
            body["roles"] = args.role
        if args.for_repo:
            body["repos"] = args.for_repo
    doc = setup.put_skills(_api(), body)
    _emit(args, doc, lambda: setup.skills_text(doc, _style()))
    return 0


# -- raw API for the Tern plugin -------------------------------------------------

def cmd_api(args) -> int:
    body = None
    if args.data is not None:
        raw = _read_stdin_value() if args.data == "-" else args.data
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError as exc:
            raise CliError("invalid_json",
                           f"--data is not JSON (line {exc.lineno}, column {exc.colno}).") from exc
    headers = {}
    for item in args.header or []:
        key, sep, value = item.partition(":")
        if not sep or not key.strip():
            raise CliError("invalid_header", f"Use --header Name:value, not {item!r}.")
        headers[key.strip()] = value.strip()
    path = args.path if args.path.startswith("/") else "/" + args.path
    resp = _api().raw(args.method, path, json_body=body, headers=headers)
    sys.stdout.write(json.dumps(resp.body, ensure_ascii=False) + "\n")
    return 0


# -- local machine ---------------------------------------------------------------

def cmd_omp_sync(args) -> int:
    result = local.omp_sync(_api())
    omp = result["omp"]
    lines = [
        f"omp profile `{result['profile']}` updated to generation {result['generation']}.",
        f"  config: {result['config']}",
        f"  default model: {result['model']}",
        f"  use: omp --profile {result['profile']}",
    ]
    if not omp["path"]:
        lines.append("  warning: omp was not found. Install it through Tern or set SYMPHONY_OMP.")
    elif omp["team_version"] and not omp["matches"]:
        lines.append(f"  warning: {omp['path']} is {omp['version'] or 'an unknown version'}. "
                     f"The team uses {omp['team_version']}.")
    _emit(args, result, "\n".join(lines))
    return 0


def cmd_tern_install(args) -> int:
    result = local.tern_install(creds.current(), plugin_dir=args.plugin,
                                install_plugin=not args.no_plugin)
    verbs = {"added": "added", "updated": "updated", "unchanged": "already correct"}
    plugin = (f"installed from {result['plugin']['source']}" if result["plugin"]
              else "not installed (--no-plugin)")
    lines = [
        f"Tern configured ({result['settings']}, 0600):",
        f"  ACP agent `symphony`: {verbs[result['agent']]} ({' '.join(result['command'])})",
        f"  MCP server `symphony`: {verbs[result['mcp']]}",
        f"  plugin: {plugin}",
        "",
        "Open sessions with `tern acp symphony --sessions` or `symphony chat ENG-123`.",
    ]
    _emit(args, result, "\n".join(lines))
    return 0


def cmd_acp(args) -> int:
    from symphony.cli.acp import serve

    return serve()


# -- backup (docs/backup.md) --------------------------------------------------------------

def _long_api() -> Api:
    """Backups, restores and the nightly run can take minutes."""
    return Api(creds.current(), timeout=3600.0)


def _attachment(headers) -> str | None:
    match = re.search(r'filename="([A-Za-z0-9._-]+)"', headers.get("content-disposition", ""))
    return match.group(1) if match else None


def cmd_backup_status(args) -> int:
    doc = _api().get("/v1/backup")
    _emit(args, doc, lambda: render.backup_status(doc))
    return 0


def cmd_backup_export(args) -> int:
    """Download, check the whole file (`backup.verify`), then move it into place."""
    import tempfile

    from symphony import backup

    wanted = Path(args.output) if args.output else None
    folder = wanted.parent if wanted else Path.cwd()
    with tempfile.NamedTemporaryFile(dir=folder, prefix=".symphony-backup-", delete=False) as out:
        try:
            headers = _long_api().download("/v1/backup/export", out)
            out.flush()
            info = backup.verify(out)
        except backup.BackupError as exc:
            os.unlink(out.name)
            raise CliError(exc.code, f"The download is not a valid backup: {exc.message}") from None
        except BaseException:
            os.unlink(out.name)
            raise
    path = wanted or folder / (_attachment(headers) or "symphony-backup.ndjson.gz")
    os.replace(out.name, path)
    doc = {"file": str(path), "bytes": path.stat().st_size, **info}
    _emit(args, doc, f"Wrote {path} ({doc['bytes']} bytes): {info['records']} keys of namespace "
                     f"{info['namespace']}, taken {info['created_at']}.")
    return 0


def cmd_backup_restore(args) -> int:
    if not args.confirm:
        raise CliError("confirm_required", "A restore replaces keys. Repeat it with --confirm.")
    params = {"confirm": "true", "namespace_from": args.namespace_from}
    api = _long_api()
    if args.object:
        doc = api.raw("POST", f"/v1/backup/backups/{sid_path(args.object)}/restore",
                      params=params).body
    else:
        with open(args.file, "rb") as source:
            doc = api.upload("/v1/backup/restore", source, params)
    _emit(args, doc, lambda: render.backup_restore(doc))
    return 0


def cmd_backup_run(args) -> int:
    doc = _long_api().post("/v1/backup/run", {})
    _emit(args, doc, lambda: render.backup_run(doc))
    return 0


def cmd_backup_list(args) -> int:
    doc = _api().get("/v1/backup/backups")
    _emit(args, doc, lambda: render.backup_objects(doc, "backups"))
    return 0


def cmd_backup_archive_list(args) -> int:
    doc = _api().get("/v1/backup/archive")
    _emit(args, doc, lambda: render.backup_objects(doc, "archive objects"))
    return 0


def cmd_backup_archive_get(args) -> int:
    """The archive object as JSONL (decompressed), to stdout or `-o FILE`."""
    import gzip
    import shutil
    import tempfile

    with tempfile.TemporaryFile() as spool:
        _long_api().download(f"/v1/backup/archive/{sid_path(args.name)}", spool)
        spool.seek(0)
        with gzip.GzipFile(fileobj=spool, mode="rb") as lines:
            if args.output:
                with open(args.output, "wb") as out:
                    shutil.copyfileobj(lines, out)
                _emit(args, {"file": args.output, "name": args.name},
                      f"Wrote {args.output} (JSONL).")
            else:
                shutil.copyfileobj(lines, sys.stdout.buffer)
    return 0


# -- parser -------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print JSON")
    parser = argparse.ArgumentParser(
        prog="symphony", parents=[common],
        description="Symphony: tracker tickets worked by agents on Kubernetes, operated from Tern.")
    parser.add_argument("--version", action="version", version=f"symphony {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, func: Callable, help_text: str, parent=sub) -> argparse.ArgumentParser:
        p = parent.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    p = add("login", cmd_login, "log in with a URL and a token (stored in the keychain)")
    p.add_argument("--url")
    p.add_argument("--token", help="the token; `-` reads it from stdin")
    add("logout", cmd_logout, "remove the saved credential")
    add("whoami", cmd_whoami, "show who the current token belongs to")

    add("status", cmd_status, "overview: daemon, counts, today's cost, merger")
    p = add("sessions", cmd_sessions, "list sessions")
    p.add_argument("--state", choices=["starting", "running", "waiting_input", "idle", "ended"])
    p.add_argument("--limit", type=int, default=50)
    p = add("watch", cmd_watch, "follow a session live")
    p.add_argument("ref", metavar="SESSION|TICKET")
    p = add("chat", cmd_chat, "open the session as an ACP chat in Tern")
    p.add_argument("ref", metavar="SESSION|TICKET")
    p = add("say", cmd_say, "send a message to the agent")
    p.add_argument("ref", metavar="SESSION|TICKET")
    p.add_argument("text", nargs="+")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--steer", action="store_true", help="deliver in the current turn")
    mode.add_argument("--follow-up", action="store_true", help="deliver after the turn")
    p = add("interrupt", cmd_interrupt, "stop the current turn")
    p.add_argument("ref", metavar="SESSION|TICKET")
    p.add_argument("--reason", default="interrupted from the CLI")
    p = add("answer", cmd_answer, "answer the agent's open question")
    p.add_argument("ref", metavar="SESSION|TICKET")
    p.add_argument("text", nargs="+")
    p = add("takeover", cmd_takeover, "take over the session on this machine with omp")
    p.add_argument("ticket")
    p.add_argument("--dir", default=".", help="local clone of the repository (default: here)")
    p.add_argument("--no-hold", action="store_true",
                   help="do not hold the ticket or interrupt the agent (only download and open)")
    p.add_argument("--no-launch", action="store_true", help="only print the omp command")
    add("issues", cmd_issues, "tracked tickets")
    add("repos", _projection("/v1/repos", render.repos), "repositories Symphony works on")
    add("infra", _projection("/v1/infra", render.infra), "services and Kubernetes resources")
    p = add("telemetry", _projection("/v1/telemetry", render.telemetry, limit=lambda a: a.limit),
            "tokens, cost, models and monthly counters")
    p.add_argument("--limit", type=int, default=200, help="sessions to include (1-500)")
    add("learning", _projection("/v1/learning", render.learning), "learning proposals (reflector)")
    p = add("audit", _projection("/v1/audit", render.audit, cursor=lambda a: a.cursor,
                                 limit=lambda a: a.limit), "audit events, newest first")
    p.add_argument("--cursor", help="the `nextCursor` of the previous page")
    p.add_argument("--limit", type=int, default=50, help="events per page (1-200)")
    for action, text in (("pause", "stop taking new tickets"), ("resume", "take tickets again"),
                         ("refresh", "reread the tracker now")):
        p = add(action, _command(action), f"all of Symphony: {text}")
        p.add_argument("--reason", default=f"{action} from the CLI")
    p = add("reevaluate", _command("reevaluate"), "reevaluate a blocked or retrying ticket")
    p.add_argument("ticket")
    p.add_argument("--reason", default="reevaluation from the CLI")
    p = add("release", cmd_release, "return a held ticket to Symphony")
    p.add_argument("ticket")
    p.add_argument("--reason", default=None, help="default: returned by <your name>")

    token = sub.add_parser("token", help="API tokens").add_subparsers(dest="sub", metavar="ACTION", required=True)
    add("print", cmd_token_print, "print the URL and token (for the Tern plugin)", token)
    p = add("create", cmd_token_create, "create a token (admin)", token)
    p.add_argument("name")
    p.add_argument("--role", choices=["viewer", "operator", "admin"], default="viewer")
    p.add_argument("--expires", default="90d", help="Nd or never (default 90d)")
    add("list", cmd_token_list, "list tokens (admin)", token)
    p = add("revoke", cmd_token_revoke, "revoke a token (admin)", token)
    p.add_argument("id")

    admin = sub.add_parser("admin", help="administration").add_subparsers(dest="sub", metavar="AREA", required=True)
    config = admin.add_parser("config", help="team config").add_subparsers(dest="action", metavar="ACTION", required=True)
    add("show", cmd_config_show, "show the published config", config)
    p = add("set", cmd_config_set, "publish role=provider/model[:thinking]", config)
    p.add_argument("assignments", nargs="+", metavar="ROLE=PROVIDER/MODEL[:THINKING]")
    p.add_argument("--reason", required=True)
    add("observed", cmd_config_observed, "the generation each consumer applied", config)
    provider = admin.add_parser("provider", help="providers and API keys").add_subparsers(
        dest="action", metavar="ACTION", required=True)
    add("list", cmd_provider_list, "configured providers (the key is never shown)", provider)
    p = add("key", cmd_provider_key,
            "store the API key of an omp built-in provider (read from stdin or a hidden prompt)",
            provider)
    p.add_argument("id", metavar="PROVIDER", help="an omp catalog id, for example anthropic or openai")
    mcp = admin.add_parser("mcp", help="MCP servers for agents, per role").add_subparsers(
        dest="action", metavar="ACTION", required=True)
    add("list", cmd_mcp_list, "MCP servers, their roles and the names of stored headers", mcp)
    p = add("set", cmd_mcp_set, "add or replace an MCP server (all fields; headers stay)", mcp)
    p.add_argument("name", metavar="NAME")
    p.add_argument("--preset", choices=["seiji"],
                   help="fill the fields that the other flags leave out (for example seiji)")
    p.add_argument("--transport", choices=["http", "sse", "stdio"],
                   help="required without --preset")
    p.add_argument("--url", help="http and sse: the server URL")
    p.add_argument("--command", help="stdio: the program to run")
    p.add_argument("--arg", action="append", metavar="ARG", help="stdio: one argument (repeat)")
    p.add_argument("--env", action="append", metavar="NAME=VALUE",
                   help="stdio: a non-secret variable (repeat)")
    p.add_argument("--role", action="append", required=True, metavar="ROLE",
                   help="a role that gets this server (repeat)")
    p.add_argument("--disabled", action="store_true", help="save it turned off")
    p.add_argument("--timeout-ms", type=int, metavar="N")
    p.add_argument("--instructions", action="store_true",
                   help="give the server's instructions to the agent")
    p.add_argument("--http-base", metavar="URL", help="http and sse: the server's HTTP API root")
    p.add_argument("--agent-env", action="append", metavar="NAME=TEMPLATE",
                   help="a variable for the agent; {token}, {http_url}, {mcp_url} (repeat)")
    p.add_argument("--rewrite-origin", action="append", metavar="ORIGIN",
                   help="another origin whose links point at the gateway (repeat)")
    p.add_argument("--cleanup", metavar="ACQUIRE_TOOL:ID_FIELD:RELEASE_TOOL:RELEASE_ARG",
                   help="release what a session still holds when it ends")
    p = add("remove", cmd_mcp_remove, "remove an MCP server and its stored headers", mcp)
    p.add_argument("name", metavar="NAME")
    p = add("headers", cmd_mcp_headers,
            "store secret headers: JSON on stdin, {name: value}; null removes one", mcp)
    p.add_argument("name", metavar="NAME")
    p = add("test", cmd_mcp_test, "connect through the gateway and list the tools", mcp)
    p.add_argument("name", metavar="NAME")

    servers = sub.add_parser("mcp", help="MCP servers of agent sessions").add_subparsers(
        dest="sub", metavar="ACTION", required=True)
    p = add("viewer", cmd_mcp_viewer,
            "a viewer link to a device that a live session holds (operator)", servers)
    p.add_argument("name", metavar="NAME")
    p.add_argument("lease", metavar="LEASE")
    p.add_argument("--control", action="store_true",
                   help="the link may drive the device (everyone who watches sees it)")

    assistant = sub.add_parser("assistant", help="the Slack and Discord assistant"
                               ).add_subparsers(dest="sub", metavar="ACTION", required=True)
    add("status", cmd_assistant_status, "heartbeat, transports and active threads", assistant)
    memory = assistant.add_parser("memory", help="what the assistant remembers about each person (admin)"
                                  ).add_subparsers(dest="action", metavar="ACTION", required=True)
    add("list", cmd_assistant_memory_list, "the soul in use and the people with memory", memory)
    p = add("show", cmd_assistant_memory_show, "one person's items", memory)
    p.add_argument("login", metavar="LOGIN")
    p = add("forget", cmd_assistant_memory_forget, "remove one item", memory)
    p.add_argument("login", metavar="LOGIN")
    p.add_argument("id", metavar="ID")
    p = add("wipe", cmd_assistant_memory_wipe, "remove every item of one person", memory)
    p.add_argument("login", metavar="LOGIN")

    merger = sub.add_parser("merger", help="merger").add_subparsers(dest="sub", metavar="ACTION", required=True)
    p = add("status", cmd_merger_status, "reviews, queue and train, per repository", merger)
    p.add_argument("--repo", help="show only this repository (owner/name)")
    for action, text in (("bypass", "skip the merger review of a PR"),
                         ("requeue", "put a PR back in the merger queue"),
                         ("release", "release a PR from the merger")):
        p = add(action, _merger_action(action), text, merger)
        p.add_argument("pr", type=int, metavar="PR")
        p.add_argument("--reason", required=True)
        p.add_argument("--repo", help="the PR's repository (owner/name); required when more than "
                                      "one repository has a merger")

    backups = sub.add_parser("backup", help="backup, restore and archive of the Redis state (admin)"
                             ).add_subparsers(dest="sub", metavar="ACTION", required=True)
    add("status", cmd_backup_status, "the target, the retention and the last runs", backups)
    p = add("export", cmd_backup_export, "download a backup file and check it", backups)
    p.add_argument("-o", "--output", metavar="FILE",
                   help="default: the server's name (symphony-<ns>-<time>.ndjson.gz) here")
    p = add("restore", cmd_backup_restore,
            "restore a backup file or a stored backup (pause the daemon first)", backups)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("file", nargs="?", metavar="FILE", help="a file from `backup export`")
    source.add_argument("--object", metavar="NAME", help="a backup from `backup list`")
    p.add_argument("--confirm", action="store_true", help="required: the restore replaces keys")
    p.add_argument("--namespace-from", metavar="NS",
                   help="restore a backup taken in another namespace")
    add("run", cmd_backup_run, "archive, back up to the target and prune old backups now",
        backups)
    add("list", cmd_backup_list, "backups stored on the target", backups)
    archive = backups.add_parser("archive", help="ended sessions and old audit entries"
                                 ).add_subparsers(dest="action", metavar="ACTION", required=True)
    add("list", cmd_backup_archive_list, "archive objects on the target", archive)
    p = add("get", cmd_backup_archive_get, "print one archive object as JSONL", archive)
    p.add_argument("name", metavar="NAME")
    p.add_argument("-o", "--output", metavar="FILE", help="write it to FILE instead of stdout")

    p = add("api", cmd_api, "raw API call (JSON; used by the Tern plugin)")
    p.add_argument("method", type=str.upper)
    p.add_argument("path")
    p.add_argument("--data", help="JSON body; `-` reads it from stdin")
    p.add_argument("--header", action="append", metavar="NAME:VALUE")

    omp = sub.add_parser("omp", help="local omp").add_subparsers(dest="sub", metavar="ACTION", required=True)
    add("sync", cmd_omp_sync, "apply the team config to the `symphony` omp profile", omp)
    tern = sub.add_parser("tern", help="local Tern").add_subparsers(dest="sub", metavar="ACTION", required=True)
    p = add("install", cmd_tern_install, "set up the ACP agent, the MCP server and the plugin in Tern", tern)
    p.add_argument("--plugin", help="plugin directory (default: <repo>/tern-plugin)")
    p.add_argument("--no-plugin", action="store_true")

    add("acp", cmd_acp, "ACP agent over stdio (Tern starts it)")

    p = add("setup", cmd_setup, "first-run setup: a wizard on a terminal, or one step at a time")
    p.add_argument("--status", action="store_true", help="print the setup checklist")
    steps = p.add_subparsers(dest="sub", metavar="STEP")
    p = add("tracker", cmd_setup_tracker,
            "set the tracker (Linear: the API key on stdin or a hidden prompt)", steps)
    p.add_argument("--kind", choices=["linear", "github"], required=True)
    p.add_argument("--team", metavar="KEY", help="Linear team key")
    p.add_argument("--project", metavar="SLUG", help="Linear project slug")
    p.add_argument("--owner", help="GitHub: the organization or user that owns the project")
    p.add_argument("--project-number", type=int, metavar="N", help="GitHub project number")
    p.add_argument("--status-field", metavar="F", help="GitHub project field with the states "
                                                       "(default Status)")
    p = add("github-app", cmd_setup_github_app,
            "set the GitHub App (the private key on stdin or from --private-key-file)", steps)
    p.add_argument("--app-id", required=True)
    p.add_argument("--installation-id", required=True)
    p.add_argument("--private-key-file", metavar="PATH")
    p = add("repositories", cmd_setup_repositories, "set or clear the default repository", steps)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--default-repo", metavar="OWNER/NAME")
    group.add_argument("--clear-default-repo", action="store_true",
                       help="no default: each ticket names its repository with a `repo:` label")
    p.add_argument("--branch", action="append", metavar="OWNER/NAME=BRANCH",
                   help="set the base branch for a repository (repeat for multiple; "
                        "owner/name= clears that repo)")
    p.add_argument("--runtime", action="append", metavar="OWNER/NAME=RUNTIME",
                   help="set the default runtime for a repository (repeat; owner/name= clears)")
    p.add_argument("--component", action="append", metavar="OWNER/NAME:COMP=PATH[@RUNTIME]",
                   help="define a repository component (repeat; owner/name:comp= removes)")
    notify = steps.add_parser("notifications", help="Slack, Discord and a test message"
                              ).add_subparsers(dest="channel_kind", metavar="CHANNEL", required=True)
    for channel, target, target_help in (
            ("slack", "--channel", "with a bot token: the channel ID or name"),
            ("discord", "--channel-id", "with a bot token: the channel ID")):
        name = setup.CHANNEL_NAME[channel]
        p = add(channel, cmd_setup_channel, f"set {name} (the token or URL on stdin or a hidden "
                                            "prompt); a test message is sent first", notify)
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument(target, help=target_help)
        group.add_argument("--webhook", action="store_true", help="use a webhook URL")
        group.add_argument("--remove", action="store_true", help=f"stop posting to {name}")
    add("test", cmd_setup_test, "send a test message to each configured channel", notify)
    p = add("webhooks", cmd_setup_webhooks,
            "create the Linear and GitHub webhook secrets (shown only once)", steps)
    p.add_argument("--rotate", action="store_true", help="replace the existing secrets")
    p = add("assistant", cmd_setup_assistant,
            "turn on the Slack and Discord assistant (the Slack app token on stdin or a hidden "
            "prompt)", steps)
    p.add_argument("--slack-channel", action="append", metavar="C…",
                   help="a Slack channel ID where it answers @mentions (repeat)")
    p.add_argument("--discord-channel", action="append", metavar="ID",
                   help="a Discord channel ID where it answers @mentions (repeat)")
    p.add_argument("--repo", action="append", metavar="OWNER/NAME",
                   help="a repository it may read (repeat; default: all)")
    p = add("skills", cmd_setup_skills,
            "add or remove a central skill source", steps)
    p.add_argument("--repo", required=True, metavar="OWNER/NAME",
                   help="the source repository (owner/name)")
    p.add_argument("--path", default=None, metavar="DIR",
                   help="directory in the source repo (default: skills)")
    p.add_argument("--ref", default=None, metavar="REF",
                   help="branch or tag (default: the repo's default branch)")
    p.add_argument("--role", action="append", metavar="ROLE",
                   help="restrict to this role (repeat; default: all)")
    p.add_argument("--for-repo", action="append", metavar="OWNER/NAME",
                   help="restrict to this target repository (repeat; default: global)")
    p.add_argument("--remove", action="store_true",
                   help="remove the source instead of adding it")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.json = getattr(args, "json", False)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except ApiError as exc:
        if args.json or args.func is cmd_api:
            sys.stdout.write(json.dumps(exc.body, ensure_ascii=False) + "\n")
        else:
            sys.stderr.write(f"error: {render.error_text(exc.code, exc.status, exc.message)}\n")
        return 1
    except (CliError, setup.SetupError, creds.NotLoggedIn, local.LocalError) as exc:
        code = getattr(exc, "code", "not_logged_in" if isinstance(exc, creds.NotLoggedIn) else "local_error")
        message = getattr(exc, "message", str(exc))
        if args.json or args.func is cmd_api:
            sys.stdout.write(json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False) + "\n")
        else:
            sys.stderr.write(f"error: {message}\n")
        return 1
    except KeyboardInterrupt:
        return 130


def _check_reason(kind: str, reason: str) -> None:
    problem = reason_problem(kind, reason)
    if problem:
        raise CliError("invalid_reason", problem)


if __name__ == "__main__":
    sys.exit(main())
