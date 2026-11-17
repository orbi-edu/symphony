"""`symphony setup`: detect an unconfigured backend and configure it, step by step.

Every write goes through the setup API (`/v1/setup/*`), which verifies a value against the
real service before it stores it. Secrets reach this process on stdin or a hidden prompt,
never in argv, and leave it only in the request body.

Two ways in:
- the wizard (`symphony setup` on a terminal): banner, step progress, spinners while the API
  verifies, hidden prompts for secrets, a summary at the end;
- one subcommand per step (`symphony setup tracker …`), for scripts and the Tern plugin.
"""

from __future__ import annotations

import getpass
import itertools
import os
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from symphony.cli import render
from symphony.cli.client import Api, ApiError, sid_path

# The wizard sets up the GitHub App first: the GitHub tracker and the repository check use it.
WIZARD_ORDER = ("github_app", "tracker", "repositories", "models", "notifications", "assistant",
                "webhooks")


class SetupError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# -- terminal style -----------------------------------------------------------

class Style:
    """ANSI color only on a terminal, and never with `NO_COLOR` set or `TERM=dumb`."""

    CODES = {"bold": "1", "dim": "2", "green": "32", "red": "31", "yellow": "33"}

    def __init__(self, stream: IO[str]) -> None:
        tty = hasattr(stream, "isatty") and stream.isatty()
        self.tty = tty
        self.color = tty and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"

    def __call__(self, text: str, *names: str) -> str:
        if not self.color or not names:
            return text
        codes = ";".join(self.CODES[n] for n in names)
        return f"\x1b[{codes}m{text}\x1b[0m"


BANNER = r"""
   ___                 _
  / __|_  _ _ __  _ __| |_  ___ _ _ _  _
  \__ \ || | '  \| '_ \ ' \/ _ \ ' \ || |
  |___/\_, |_|_|_| .__/_||_\___/_||_\_, |
       |__/      |_|                |__/
"""

_MARK = {"done": ("✓", "green"), "todo": ("○", "yellow"), "error": ("✗", "red"),
         "skipped": ("–", "dim")}
_STATUS_LABEL = {"done": "done", "todo": "to do", "error": "error", "skipped": "skipped"}


@contextmanager
def spinner(style: Style, out: IO[str], label: str) -> Iterator[None]:
    """A spinner on a terminal while a request runs; a plain line elsewhere."""
    if not style.tty:
        out.write(f"{label}…\n")
        out.flush()
        yield
        return
    stop = threading.Event()

    def spin() -> None:
        for frame in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            out.write(f"\r{frame} {label}…")
            out.flush()
            if stop.wait(0.08):
                break

    thread = threading.Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        out.write("\r\x1b[2K")
        out.flush()


# -- reading the state ----------------------------------------------------------

def get_status(api: Api) -> dict:
    return api.get("/v1/setup")


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def _test_text(last: dict | None) -> str:
    if not last:
        return "no test message yet"
    if last.get("ok"):
        return f"test message sent {render.ago(last.get('at'))}"
    return f"test message failed ({last.get('error')}) {render.ago(last.get('at'))}"


def _tracker_summary(d: dict) -> str:
    if d.get("kind") == "linear":
        scope = f"team {d['team_key']}" if d.get("team_key") else (
            f"project {d['project_slug']}" if d.get("project_slug") else "no team or project")
        key = "" if d.get("credential") else ", no API key"
        return f"Linear, {scope}{key}"
    if d.get("kind") == "github":
        if not (d.get("owner") and d.get("project_number")):
            return "GitHub Projects, no project"
        app = "" if d.get("credential") else ", no GitHub App"
        return (f"GitHub project {d['owner']}#{d['project_number']}, field "
                f"{d.get('status_field') or 'Status'}{app}")
    return "No tracker is configured."


def _summaries(step_id: str, d: dict) -> str:
    if step_id == "tracker":
        return _tracker_summary(d)
    if step_id == "github_app":
        if not d.get("configured"):
            return "No GitHub App is configured."
        repos = d.get("repositories") or []
        return f"Installed on {_plural(len(repos), 'repository', 'repositories')}" + (
            f": {', '.join(repos)}" if repos else "")
    if step_id == "repositories":
        parts = [f"default {d['default_repo']}" if d.get("default_repo") else "no default"]
        bb = d.get("base_branches") or {}
        if bb:
            parts.append(f"base branch for {', '.join(sorted(bb))}")
        if d.get("mergers"):
            parts.append(f"merger for {', '.join(d['mergers'])}")
        if not d.get("repositories"):
            return "No default repository and no ticket names one."
        return ", ".join(parts)
    if step_id == "models":
        roles = d.get("roles") or []
        providers = sorted({r.get("provider") for r in roles if r.get("provider")})
        return f"{_plural(len(roles), 'role')} on {', '.join(providers) or 'no provider'}"
    if step_id == "notifications":
        names = {"slack": "Slack", "discord": "Discord"}
        parts = [f"{names[c]}: {_test_text(d[c].get('last_test'))}"
                 for c in ("slack", "discord") if (d.get(c) or {}).get("configured")]
        return "; ".join(parts) or "No channel is configured."
    if step_id == "assistant":
        return assistant_summary(d)
    if step_id == "webhooks":
        names = {"linear": "Linear", "github": "GitHub"}
        parts = []
        for source in ("linear", "github"):
            hook = d.get(source) or {}
            if not hook.get("in_use"):
                continue
            if not hook.get("secret"):
                parts.append(f"{names[source]}: no secret")
            elif hook.get("last_delivery_at"):
                parts.append(f"{names[source]}: last delivery {render.ago(hook['last_delivery_at'])}")
            else:
                parts.append(f"{names[source]}: no signed delivery yet")
        return "; ".join(parts)
    return ""


def step_summary(step: dict) -> str:
    """One line for a `GET /v1/setup` step: its error when it has one, else its detail."""
    error = step.get("error")
    if error:
        code, message = str(error.get("code")), str(error.get("message") or "")
        if code in render.ERROR_TEXT:
            return render.error_text(code, 0, message)
        return message[:1].upper() + message[1:]
    detail = step.get("detail")
    return _summaries(str(step.get("id")), detail) if isinstance(detail, dict) else ""


def _restarted(doc: dict) -> list[str]:
    names = doc.get("restarted") or []
    return [f"Restarted to apply it: {', '.join(names)}."] if names else []


def status_text(doc: dict, style: Style) -> str:
    steps = doc.get("steps") or []
    left = [s for s in steps if s.get("required") and s.get("status") != "done"]
    if doc.get("complete"):
        head = style("Symphony setup is complete.", "bold", "green")
    else:
        head = style(f"Symphony setup is not complete: {len(left)} required "
                     f"step{'s' if len(left) != 1 else ''} left.", "bold", "yellow")
    lines = [f"{head} (mode: {doc.get('mode', '?')})", ""]
    titles = [s.get("title") or s.get("id", "") for s in steps]
    width = max([len(t) + (0 if s.get("required") else len(" (optional)"))
                 for t, s in zip(titles, steps, strict=True)] + [4])
    for step, title in zip(steps, titles, strict=True):
        mark, color = _MARK.get(step.get("status"), ("?", "dim"))
        status = _STATUS_LABEL.get(step.get("status"), str(step.get("status")))
        summary = step_summary(step)
        name = title if step.get("required") else f"{title}{style(' (optional)', 'dim')}"
        pad = " " * (width - len(title) - (0 if step.get("required") else len(" (optional)")))
        line = f"  {style(mark, color)} {name}{pad}  {status.ljust(7)}"
        lines.append((f"{line}  {summary}" if summary else line).rstrip())
    if not doc.get("complete"):
        lines += ["", "Run `symphony setup` on a terminal to finish, or one step at a time with "
                      "`symphony setup <step> --help`."]
    return "\n".join(lines)


# -- one step at a time -----------------------------------------------------------

def tracker_body(*, kind: str, team: str | None = None, project: str | None = None,
                 owner: str | None = None, project_number: int | None = None,
                 status_field: str | None = None) -> dict:
    """The `PUT /v1/setup/tracker` body without the Linear API key, checked before any secret is
    asked for."""
    if kind == "linear":
        if owner or project_number or status_field:
            raise SetupError("invalid_tracker",
                             "--owner, --project-number and --status-field are for GitHub.")
        if bool(team) == bool(project):
            raise SetupError("invalid_tracker", "Use exactly one of --team KEY or --project SLUG.")
        return {"kind": "linear", ("team_key" if team else "project_slug"): team or project}
    if team or project:
        raise SetupError("invalid_tracker", "--team and --project are for Linear.")
    if not owner or not project_number or project_number < 1:
        raise SetupError("invalid_tracker", "GitHub needs --owner and --project-number.")
    body = {"kind": "github", "owner": owner, "project_number": project_number}
    if status_field:
        body["status_field"] = status_field
    return body


def put_tracker(api: Api, body: dict, api_key: str | None = None) -> dict:
    if body["kind"] == "linear":
        if not api_key:
            raise SetupError("missing_secret", "Give the Linear API key on stdin or at the prompt.")
        body = {**body, "api_key": api_key}
    return api.raw("PUT", "/v1/setup/tracker", json_body=body).body


def tracker_text(doc: dict, style: Style) -> str:
    name = {"linear": "Linear", "github": "GitHub Projects"}.get(doc.get("kind"), doc.get("kind"))
    lines = [style(f"Tracker saved: {name}.", "green")]
    if doc.get("states"):
        lines.append(f"States on the board: {', '.join(doc['states'])}.")
    for warning in doc.get("warnings") or []:
        if warning.get("code") == "state_missing":
            lines.append(style(f"Warning: the WORKFLOW state {warning.get('state')!r} is not on "
                               "the board.", "yellow"))
        else:
            lines.append(style(f"Warning: {warning.get('code')}.", "yellow"))
    return "\n".join(lines + _restarted(doc))


def put_github_app(api: Api, *, app_id: str, installation_id: str, private_key: str) -> dict:
    if "PRIVATE KEY" not in private_key:
        raise SetupError("invalid_private_key",
                         "The private key must be the PEM file GitHub gave you for the App.")
    return api.raw("PUT", "/v1/setup/github-app", json_body={
        "app_id": str(app_id), "installation_id": str(installation_id),
        "private_key": private_key}).body


def github_app_text(doc: dict, style: Style) -> str:
    repos = doc.get("repositories") or []
    lines = [style("GitHub App saved.", "green"),
             f"The installation can reach {len(repos)} repositor{'y' if len(repos) == 1 else 'ies'}"
             + (f": {', '.join(repos)}." if repos else ".")]
    missing = doc.get("missing_permissions") or []
    if missing:
        lines.append(style(f"Warning: the App lacks these permissions: {', '.join(missing)}. "
                           "Grant them in the App settings, then accept them on the installation.",
                           "yellow"))
    return "\n".join(lines + _restarted(doc))


def put_repositories(api: Api, default_repo: str | None,
                     base_branches: dict[str, str | None] | None = None) -> dict:
    body: dict = {}
    if default_repo is not None:
        repo = default_repo.strip().lower()
        if repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
            raise SetupError("invalid_repo", f"Use owner/name, not {default_repo!r}.")
        body["default_repo"] = repo
    if base_branches is not None:
        body["base_branches"] = base_branches
    return api.raw("PUT", "/v1/setup/repositories", json_body=body).body


def repositories_text(doc: dict, style: Style) -> str:
    repo = doc.get("default_repo")
    if repo:
        lines = [style(f"Default repository: {repo}.", "green")]
    else:
        lines = [style("No default repository. Each ticket must name its repository with a "
                       "`repo:` label.", "green")]
    bb = doc.get("base_branches") or {}
    if bb:
        for r, b in sorted(bb.items()):
            lines.append(f"  Base branch for {r}: {b}")
    without = doc.get("without_merger") or []
    if without:
        lines.append(style(f"Warning: no merger is registered for {', '.join(without)} yet. "
                           "Deploy a merger for each repository.", "yellow"))
    return "\n".join(lines + _restarted(doc))


def put_notifications(api: Api, channel: str, values: dict | None) -> dict:
    """`values` None removes the channel; the other channel stays as it is."""
    return api.raw("PUT", "/v1/setup/notifications", json_body={channel: values}).body


CHANNEL_NAME = {"slack": "Slack", "discord": "Discord"}


def notifications_text(channel: str, doc: dict, style: Style) -> str:
    name = CHANNEL_NAME[channel]
    if not (doc.get(channel) or {}).get("configured"):
        lines = [style(f"{name} removed.", "green")]
    else:
        lines = [style(f"{name} saved. The test message was sent.", "green")]
    return "\n".join(lines + _restarted(doc))


def _transport_text(name: str, t: dict | None, *, running: bool) -> str | None:
    if not t or not t.get("channels"):
        return None
    channels = _plural(len(t["channels"]), "channel")
    state = ("connected" if t.get("connected") else "not connected") if running else "set"
    return f"{name}: {channels}, {state}"


def assistant_summary(d: dict) -> str:
    if not d.get("enabled"):
        return "The assistant is off."
    running = bool(d.get("running"))
    parts = [p for p in (_transport_text("Slack", d.get("slack"), running=running),
                         _transport_text("Discord", d.get("discord"), running=running)) if p]
    parts.append(f"running, {_plural(int(d.get('threads') or 0), 'thread')}" if running
                 else "not running")
    return "; ".join(parts)


def assistant_body(*, slack_channels: list[str], discord_channels: list[str],
                   repos: list[str] | None, app_token: str | None) -> dict:
    """The `PUT /v1/setup/assistant` body. A transport with no channel is not sent; `repos`
    None keeps the published list."""
    if not slack_channels and not discord_channels:
        raise SetupError("invalid_assistant",
                         "Give at least one --slack-channel or --discord-channel.")
    body: dict = {}
    if slack_channels:
        body["slack"] = {"channels": slack_channels}
        if app_token:
            body["slack"]["app_token"] = app_token
    if discord_channels:
        body["discord"] = {"channels": discord_channels}
    if repos is not None:
        cleaned = []
        for repo in repos:
            name = repo.strip().lower()
            if name.count("/") != 1 or name.startswith("/") or name.endswith("/"):
                raise SetupError("invalid_repo", f"Use owner/name, not {repo!r}.")
            cleaned.append(name)
        body["repositories"] = cleaned
    return body


def put_assistant(api: Api, body: dict) -> dict:
    return api.raw("PUT", "/v1/setup/assistant", json_body=body).body


def assistant_text(doc: dict, style: Style) -> str:
    lines = [style("Assistant saved. It answers in:", "green")]
    for name, key in (("Slack", "slack"), ("Discord", "discord")):
        t = doc.get(key)
        if t:
            extra = " (limited message content)" if t.get("intent") == "limited" else ""
            lines.append(f"  {name}: {', '.join(t.get('channels') or [])}{extra}")
    repos = doc.get("repositories")
    lines.append(f"Repositories: {', '.join(repos)}." if repos else
                 "Repositories: every repository Symphony works on.")
    return "\n".join(lines + _restarted(doc))


def assistant_status(api: Api) -> dict:
    """The `assistant` step of `GET /v1/setup`: its detail with `status` and `error`."""
    for step in get_status(api).get("steps") or []:
        if step.get("id") == "assistant":
            return {**(step.get("detail") or {}), "status": step.get("status"),
                    "error": step.get("error")}
    raise SetupError("assistant_unavailable", "This Symphony has no assistant setup step.")


def assistant_status_text(doc: dict, style: Style) -> str:
    if not doc.get("enabled"):
        return "The assistant is off. Turn it on with `symphony setup assistant`."
    running = bool(doc.get("running"))
    head = (style("The assistant is running.", "green") if running
            else style("The assistant is not running.", "yellow"))
    lines = [head]
    if doc.get("heartbeat_at"):
        lines.append(f"  last heartbeat: {render.ago(doc['heartbeat_at'])}")
    for name, key in (("Slack", "slack"), ("Discord", "discord")):
        t = doc.get(key) or {}
        if not t.get("channels"):
            continue
        state = ("connected" if t.get("connected") else "not connected") if running else "-"
        lines.append(f"  {name}: {state} · channels {', '.join(t['channels'])}")
    lines.append(f"  active threads: {int(doc.get('threads') or 0)}")
    repos = doc.get("repositories")
    lines.append(f"  repositories: {', '.join(repos) if repos else 'all'}")
    if doc.get("error"):
        lines.append(style("  ✗ " + step_summary({"id": "assistant", "error": doc["error"]}),
                           "red"))
    return "\n".join(lines)


def store_provider_key(api: Api, pid: str, key: str) -> dict:
    """Store the API key of an omp built-in provider (`{builtin: true, apiKey}`), against the
    providers revision just read."""
    revision = api.get("/v1/admin/providers").get("revision")
    if not revision:
        raise SetupError("providers_not_bootstrapped",
                         render.ERROR_TEXT["providers_not_bootstrapped"])
    return api.raw("PUT", f"/v1/admin/providers/{sid_path(pid)}",
                   json_body={"builtin": True, "apiKey": key},
                   headers={"If-Match": f'"{revision}"'}).body


_TEST_ERROR = {
    "rejected": "{name} refused the test message. Check the token, the channel or the URL.",
    "unreachable": "{name} could not be reached. Try again in a moment.",
}


def send_test_message(api: Api) -> dict:
    """`POST /v1/setup/notifications/test` (`{results: {slack?: {ok, error, at}, …}}`, `error` a
    code) as one entry per channel, Slack first: `{results: [{channel, ok, at, error: null |
    {code, message}}]}`."""
    results = api.post("/v1/setup/notifications/test", {}).get("results") or {}
    out = []
    for channel in [c for c in CHANNEL_NAME if c in results]:
        result = results[channel]
        error = None
        if not result.get("ok"):
            code = str(result.get("error") or "failed")
            template = _TEST_ERROR.get(code, "{name} did not accept the test message ({code}).")
            error = {"code": code,
                     "message": template.format(name=CHANNEL_NAME[channel], code=code)}
        out.append({"channel": channel, "ok": bool(result.get("ok")), "at": result.get("at"),
                    "error": error})
    return {"results": out}


def test_result_text(doc: dict, style: Style) -> str:
    lines = []
    for result in doc.get("results") or []:
        name = CHANNEL_NAME.get(result["channel"], result["channel"])
        if result["ok"]:
            lines.append(f"  {style('✓', 'green')} {name}: test message sent")
        else:
            lines.append(f"  {style('✗', 'red')} {name}: {result['error']['message']}")
    return "\n".join(lines)


def post_webhooks(api: Api, rotate: bool) -> dict:
    return api.post("/v1/setup/webhooks", {"rotate": True} if rotate else {})


def webhooks_text(doc: dict, style: Style) -> str:
    lines = [style("Save these secrets now. Symphony shows them only once.", "bold", "yellow"),
             "Paste each one into the webhook settings of its service. To get new ones, run "
             "`symphony setup webhooks --rotate`.", ""]
    names = {"linear": "Linear", "github": "GitHub"}
    for source in ("linear", "github"):
        hook = doc.get(source)
        if not hook:
            continue
        lines += [style(names[source], "bold"), f"  URL:    {hook.get('url')}",
                  f"  Secret: {hook.get('secret')}"]
        if hook.get("events"):
            lines.append(f"  Events: {', '.join(hook['events'])}")
        lines.append("")
    return "\n".join(lines + _restarted(doc)).rstrip()


# -- the wizard --------------------------------------------------------------------

Ask = Callable[[str], str]


class Wizard:
    """The interactive flow. `ask` reads a visible answer, `secret` a hidden one; both are
    injected so the flow is testable without a terminal."""

    def __init__(self, api: Api, out: IO[str], *, ask: Ask, secret: Ask, style: Style) -> None:
        self.api = api
        self.out = out
        self.ask = ask
        self.secret = secret
        self.style = style
        self.repositories: list[str] = []

    # -- io -----------------------------------------------------------------------
    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def choose(self, question: str, options: list[str], default: str) -> str:
        hint = "/".join(o.upper() if o == default else o for o in options)
        while True:
            answer = self.ask(f"{question} [{hint}] ").strip().lower() or default
            if answer in options:
                return answer
            self.say(f"Answer one of: {', '.join(options)}.")

    def yes(self, question: str, default: bool) -> bool:
        return self.choose(question, ["y", "n"], "y" if default else "n") == "y"

    def text(self, question: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        return self.ask(f"{question}{suffix}: ").strip() or default

    def attempt(self, label: str, call: Callable[[], dict]) -> dict | None:
        """Run a verifying request under a spinner. On failure explain and offer a retry of the
        whole step (None means the person moved on)."""
        try:
            with spinner(self.style, self.out, label):
                return call()
        except ApiError as exc:
            self.say(self.style("✗ " + render.error_text(exc.code, exc.status, exc.message), "red"))
        except SetupError as exc:
            self.say(self.style("✗ " + exc.message, "red"))
        return None

    # -- flow -----------------------------------------------------------------------
    def run(self) -> int:
        self.say(BANNER.rstrip("\n"))
        self.say("Setup walks you through every step Symphony needs. Each value is verified "
                 "against the real service before it is saved.")
        self.say()
        doc = get_status(self.api)
        if self.api.me().get("role") != "admin":
            self.say(status_text(doc, self.style))
            self.say()
            self.say("Only an admin can change the setup. Ask an admin to run `symphony setup`.")
            return 0 if doc.get("complete") else 1
        steps = {s["id"]: s for s in doc.get("steps") or []}
        order = [i for i in WIZARD_ORDER if i in steps]
        self.repositories = list((steps.get("github_app") or {}).get("detail", {})
                                 .get("repositories") or [])
        for n, step_id in enumerate(order, 1):
            step = steps[step_id]
            title = step.get("title") or step_id
            optional = "" if step.get("required") else self.style(" (optional)", "dim")
            bar = "■" * n + "□" * (len(order) - n)
            self.say(f"{bar} " + self.style(f"Step {n} of {len(order)}: {title}", "bold")
                     + optional)
            summary = step_summary(step)
            if step.get("status") == "done":
                self.say(self.style(f"  ✓ done · {summary}" if summary else "  ✓ done", "green"))
                if not self.yes("  Change it?", False):
                    self.say()
                    continue
            elif step.get("status") == "error":
                self.say(self.style(f"  ✗ {summary}", "red"))
            getattr(self, f"step_{step_id}")(step.get("detail") or {})
            self.say()
        final = get_status(self.api)
        self.say(status_text(final, self.style))
        self.say()
        if final.get("complete"):
            self.say(self.style("Symphony is ready.", "bold", "green")
                     + " Create your first ticket in the tracker and move it to an active state. "
                     "Then follow it with `symphony status` or in Tern.")
            return 0
        self.say("Some required steps are not done yet. Run `symphony setup` again to finish.")
        return 1

    def _retry(self, step: Callable[[], bool]) -> None:
        while not step():
            if not self.yes("  Try this step again?", True):
                return

    def step_tracker(self, detail: dict) -> None:
        def once() -> bool:
            kind = self.choose("  Tracker", ["linear", "github"], detail.get("kind") or "linear")
            try:
                if kind == "linear":
                    team = self.text("  Linear team key (leave empty to use a project instead)",
                                     detail.get("team_key") or "")
                    project = "" if team else self.text("  Linear project slug",
                                                        detail.get("project_slug") or "")
                    body = tracker_body(kind="linear", team=team or None, project=project or None)
                    key = self.secret("  Linear API key (hidden): ").strip()
                else:
                    owner = self.text("  Project owner (organization or user)",
                                      detail.get("owner") or "")
                    number = self.text("  Project number", str(detail.get("project_number") or ""))
                    field = self.text("  Status field", detail.get("status_field") or "Status")
                    if not number.isdigit():
                        raise SetupError("invalid_tracker", "The project number must be a number.")
                    body = tracker_body(kind="github", owner=owner, project_number=int(number),
                                        status_field=field)
                    key = None
            except SetupError as exc:
                self.say(self.style("✗ " + exc.message, "red"))
                return False
            doc = self.attempt("Verifying the tracker", lambda: put_tracker(self.api, body, key))
            if doc is None:
                return False
            self.say(tracker_text(doc, self.style))
            return True
        self._retry(once)

    def step_github_app(self, detail: dict) -> None:
        def once() -> bool:
            app_id = self.text("  GitHub App ID")
            installation = self.text("  Installation ID")
            path = self.text("  Path to the App private key (.pem)")
            try:
                pem = Path(path).expanduser().read_text()
            except OSError:
                self.say(self.style(f"✗ Cannot read {path}.", "red"))
                return False
            doc = self.attempt("Verifying the GitHub App", lambda: put_github_app(
                self.api, app_id=app_id, installation_id=installation, private_key=pem))
            if doc is None:
                return False
            self.repositories = list(doc.get("repositories") or [])
            self.say(github_app_text(doc, self.style))
            return True
        self._retry(once)

    def step_repositories(self, detail: dict) -> None:
        def once() -> bool:
            if self.repositories:
                self.say(f"  The App can reach: {', '.join(self.repositories)}.")
            default = detail.get("default_repo") or (
                self.repositories[0] if len(self.repositories) == 1 else "")
            repo = self.text("  Default repository owner/name (none: each ticket names its "
                             "repository with a `repo:` label)", default or "none")
            repo = "" if repo.lower() == "none" else repo
            existing_bb = detail.get("base_branches") or {}
            bb_default = " ".join(f"{r}={b}" for r, b in sorted(existing_bb.items())) if existing_bb else ""
            bb_raw = self.text("  Base branches (owner/name=branch, space-separated; empty: auto)",
                               bb_default)
            base_branches: dict[str, str | None] | None = None
            if bb_raw.strip():
                base_branches = {}
                for pair in bb_raw.strip().split():
                    if "=" not in pair:
                        self.say(self.style(f"  ✗ Expected owner/name=branch, got {pair!r}.", "red"))
                        return False
                    r, _, b = pair.partition("=")
                    base_branches[r.lower()] = b or None
            elif existing_bb:
                base_branches = {}
            doc = self.attempt("Saving the repository", lambda: put_repositories(
                self.api, repo or None, base_branches=base_branches))
            if doc is None:
                return False
            self.say(repositories_text(doc, self.style))
            return True
        self._retry(once)

    def step_models(self, detail: dict) -> None:
        where = {"stored": "API key stored", "host": "credential from the host environment",
                 None: "no credential"}
        for role in detail.get("roles") or []:
            line = (f"  {role.get('role')}: {role.get('provider')}/{role.get('model')} · "
                    f"{where.get(role.get('credential'), role.get('credential'))}")
            self.say(self.style(line, "red") if role.get("credential") is None else line)
        self.say("  A provider needs a stored API key unless the host environment has one "
                 "(for example AWS credentials for amazon-bedrock). To change the models, run "
                 "`symphony admin config set`.")
        while True:
            pid = self.text("  Provider to store an API key for (leave empty to continue)")
            if not pid:
                return
            key = self.secret(f"  API key for {pid} (hidden): ").strip()
            if not key:
                self.say(self.style("✗ The key is empty.", "red"))
                continue
            if self.attempt(f"Saving the {pid} key",
                            lambda pid=pid, key=key: store_provider_key(self.api, pid, key)):
                self.say(self.style(f"API key for {pid} saved.", "green"))

    def step_notifications(self, detail: dict) -> None:
        def once() -> bool:
            channel = self.choose("  Channel", ["slack", "discord", "skip"], "skip")
            if channel == "skip":
                return True
            webhook = self.choose("  Use a webhook URL or a bot token?", ["webhook", "bot"],
                                  "webhook") == "webhook"
            name = CHANNEL_NAME[channel]
            if webhook:
                values = {"webhook_url": self.secret(f"  {name} webhook URL (hidden): ").strip()}
            elif channel == "slack":
                values = {"bot_token": self.secret("  Slack bot token (hidden): ").strip(),
                          "channel": self.text("  Slack channel ID or name")}
            else:
                values = {"bot_token": self.secret("  Discord bot token (hidden): ").strip(),
                          "channel_id": self.text("  Discord channel ID")}
            doc = self.attempt(f"Sending a test message to {name}",
                               lambda: put_notifications(self.api, channel, values))
            if doc is None:
                return False
            self.say(notifications_text(channel, doc, self.style))
            return True
        self._retry(once)

    def step_assistant(self, detail: dict) -> None:
        def channels(text: str) -> list[str]:
            return [c.strip() for c in text.replace(",", " ").split() if c.strip()]

        def once() -> bool:
            where = self.choose("  Answer on", ["slack", "discord", "both", "skip"], "skip")
            if where == "skip":
                return True
            slack = discord = []
            token = None
            if where in ("slack", "both"):
                token = self.secret("  Slack app-level token xapp-… (hidden, empty keeps the "
                                    "stored one): ").strip() or None
                slack = channels(self.text("  Slack channel IDs (comma separated)", ",".join(
                    (detail.get("slack") or {}).get("channels") or [])))
            if where in ("discord", "both"):
                discord = channels(self.text("  Discord channel IDs (comma separated)", ",".join(
                    (detail.get("discord") or {}).get("channels") or [])))
            repos = channels(self.text("  Repositories it may read, owner/name (empty: all)",
                                       ",".join(detail.get("repositories") or [])))
            try:
                body = assistant_body(slack_channels=slack, discord_channels=discord,
                                      repos=repos, app_token=token)
            except SetupError as exc:
                self.say(self.style("✗ " + exc.message, "red"))
                return False
            doc = self.attempt("Verifying the assistant", lambda: put_assistant(self.api, body))
            if doc is None:
                return False
            self.say(assistant_text(doc, self.style))
            return True
        self._retry(once)

    def step_webhooks(self, detail: dict) -> None:
        exists = any((detail.get(s) or {}).get("secret") for s in ("linear", "github"))
        if exists:
            question = ("  The secrets exist and cannot be shown again. Replace them? The old "
                        "ones stop working.")
        else:
            question = "  Create the webhook secrets now?"
        if not self.yes(question, not exists):
            return
        doc = self.attempt("Creating the webhook secrets", lambda: post_webhooks(self.api, exists))
        if doc is not None:
            self.say(webhooks_text(doc, self.style))


def terminal_wizard(api: Api) -> int:
    """The wizard on the real terminal."""
    return Wizard(api, sys.stdout, ask=input, secret=getpass.getpass, style=Style(sys.stdout)).run()
