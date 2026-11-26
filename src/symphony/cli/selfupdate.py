"""`symphony version`, `symphony update` and the update check after a command (docs/cli.md).

The update target is the version the API runs (`version` in `GET /v1/overview`), not the newest
release: the CLI and the Tern plugin match what is deployed. Only a `uv tool install` that is not
editable updates itself (`uv tool install --force git+<repo>@v<server>`, which replaces the tool
in one step, so a failure leaves the old CLI working). A checkout is never touched: the CLI says
what to run.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlparse

from symphony import SOURCE_ROOT, __version__
from symphony.cli import client, creds, local

CHECK_EVERY_S = 6 * 3600
# A lock older than this belongs to an update that died.
LOCK_STALE_S = 30 * 60
UV_TIMEOUT_S = 15 * 60

_SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
_SCP = re.compile(r"(?P<user>[^@/\s]+)@(?P<host>[^:/\s]+):(?P<path>[^\s]+)")


class UpdateError(local.LocalError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# -- versions ------------------------------------------------------------------------------

def parse_version(text: object) -> tuple | None:
    """A semantic version as a sortable key, or None. A pre-release sorts before its release,
    numeric identifiers before alphanumeric ones (SemVer §11); build metadata is ignored."""
    match = _SEMVER.fullmatch(str(text).strip()) if text is not None else None
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        tail: tuple = (1,)
    else:
        tail = (0, *((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return int(major), int(minor), int(patch), tail


def older(version: object, than: object) -> bool:
    """`version` is older than `than`; False when either is not a version, so garbage never
    triggers an update."""
    a, b = parse_version(version), parse_version(than)
    return a is not None and b is not None and a < b


# -- this install --------------------------------------------------------------------------

def _direct_url() -> dict:
    """PEP 610 `direct_url.json`: where the installer got this package (a git URL, a directory)."""
    try:
        text = distribution("symphony").read_text("direct_url.json")
        doc = json.loads(text) if text else {}
    except (PackageNotFoundError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def install() -> dict:
    """`kind`: `editable` (the CLI runs from a checkout), `tool` (`uv tool install`, not
    editable) or `other`; `source`: the checkout, or where the package was installed from."""
    if SOURCE_ROOT is not None:
        return {"kind": "editable", "source": str(SOURCE_ROOT)}
    doc = _direct_url()
    source = doc.get("url")
    vcs = doc.get("vcs_info")
    if isinstance(source, str) and isinstance(vcs, dict):
        rev = vcs.get("requested_revision") or vcs.get("commit_id")
        source = f"git+{source}" + (f"@{rev}" if rev else "")
    # uv writes its receipt into the root of each tool's environment.
    kind = "tool" if (Path(sys.prefix) / "uv-receipt.toml").is_file() else "other"
    return {"kind": kind, "source": source if isinstance(source, str) else None}


def _git_url(remote: str) -> str | None:
    """A git remote as a URL `uv` accepts after `git+`: `git@host:owner/repo` becomes
    `ssh://git@host/owner/repo`."""
    if "://" in remote:
        return remote
    match = _SCP.fullmatch(remote)
    if match:
        return f"ssh://{match['user']}@{match['host']}/{match['path']}"
    return None


def repo_url() -> str | None:
    """The git repository to install releases from: the one this package came from
    (`uv tool install git+URL@vX`), else the `origin` of the directory it was installed from
    (`uv tool install .` in a clone)."""
    doc = _direct_url()
    url = doc.get("url")
    if not isinstance(url, str):
        return None
    vcs = doc.get("vcs_info")
    if isinstance(vcs, dict):
        return url if vcs.get("vcs") == "git" else None
    if not url.startswith("file://"):
        return None
    try:
        out = subprocess.run(["git", "-C", unquote(urlparse(url).path), "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _git_url(out.stdout.strip()) if out.returncode == 0 else None


def tool_argv(url: str, target: str) -> list[str]:
    return ["uv", "tool", "install", "--force", f"git+{url}@v{target}"]


def manual_command(kind: str, source: str | None, target: str) -> str:
    """What the person runs to bring this CLI to `target`."""
    if kind == "editable" and source:
        clone = shlex.quote(source)
        return f"git -C {clone} pull && uv tool install --editable {clone}"
    return shlex.join(tool_argv(repo_url() or "<repository URL>", target))


# -- the server ------------------------------------------------------------------------------

def server_version(cred: creds.Credential | None = None, *, timeout: float = 15.0) -> str | None:
    """The version the API runs: the overview this process already read, else a fresh read.
    Raises `ApiError` and `NotLoggedIn`."""
    if client.seen["version"]:
        return client.seen["version"]
    api = client.Api(cred or creds.current(), timeout=timeout)
    try:
        body = api.get(client.OVERVIEW)
    finally:
        api.close()
    version = body.get("version") if isinstance(body, dict) else None
    return version if isinstance(version, str) else None


def report() -> dict:
    """`symphony version`: this CLI, the plugin Tern has and the server."""
    inst = install()
    result = {"cli": __version__, "plugin_installed": local.tern_plugin_version(), "server": None,
              "install": inst["kind"], "source": inst["source"], "command": None,
              "server_error": None}
    try:
        result["server"] = server_version(timeout=5.0)
    except client.ApiError as exc:
        result["server_error"] = exc.code
    except creds.NotLoggedIn:
        result["server_error"] = "not_logged_in"
    if older(__version__, result["server"]):
        result["command"] = ("symphony update" if inst["kind"] == "tool" and repo_url()
                             else manual_command(inst["kind"], inst["source"], result["server"]))
    return result


# -- symphony update ---------------------------------------------------------------------------

@contextmanager
def _lock():
    """One update at a time on this machine (a background one and the plugin's may meet)."""
    path = local.cache_dir() / "update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            break
        except FileExistsError:
            try:
                fresh = time.time() - path.stat().st_mtime < LOCK_STALE_S
            except FileNotFoundError:
                continue
            if fresh:
                raise UpdateError("update_running", "Another symphony update is running.") from None
            path.unlink(missing_ok=True)
    else:
        raise UpdateError("update_running", "Another symphony update is running.")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def _sync_plugin(check: bool) -> dict | None:
    """Brings the plugin Tern has up to this CLI's (the checkout's or the packaged copy). A Tern
    without the plugin is left alone: `symphony tern install` installs it."""
    source = local.plugin_source()
    have = local.tern_plugin_version()
    if source is None or have is None:
        return None
    want = local.plugin_version(source)
    if not older(have, want):
        return {"installed": have, "version": want, "status": "current"}
    if check:
        return {"installed": have, "version": want, "status": "available"}
    return {"installed": have, **local.install_tern_plugin(None)}


def _new_cli() -> str:
    """The `symphony` that `uv tool install` just wrote."""
    try:
        out = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=30)
        candidate = Path(out.stdout.strip()) / "symphony"
        if out.returncode == 0 and candidate.is_file():
            return str(candidate)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return shutil.which("symphony") or "symphony"


def _plugin_from_new_cli(had_plugin: bool) -> dict | None:
    """The new package's plugin, installed by the new CLI: this process still runs the old one."""
    if not had_plugin:
        return None
    try:
        out = subprocess.run([_new_cli(), "tern", "install", "--plugin-only", "--json"],
                             capture_output=True, text=True, timeout=120)
        doc = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"error": {"code": "plugin_failed", "message": str(exc)}}
    if out.returncode != 0 or not isinstance(doc, dict):
        message = (out.stderr or out.stdout).strip() or f"exit status {out.returncode}"
        return doc if isinstance(doc, dict) and "error" in doc else {
            "error": {"code": "plugin_failed", "message": message}}
    return doc.get("plugin")


def update(*, check: bool) -> dict:
    """`symphony update [--check]`: brings this CLI to the server's version, then the plugin Tern
    has to the CLI's."""
    target = server_version()
    if target is None:
        raise UpdateError("no_server_version",
                          "The server does not report its version: it runs a Symphony release "
                          "older than self-update. Nothing to update to.")
    inst = install()
    result = {"cli": __version__, "server": target, "install": inst["kind"],
              "source": inst["source"], "status": "current", "command": None, "plugin": None}
    if not older(__version__, target):
        result["plugin"] = _sync_plugin(check)
        return result
    url = repo_url() if inst["kind"] == "tool" else None
    if url is None:
        result["status"] = "manual"
        result["command"] = manual_command(inst["kind"], inst["source"], target)
        return result
    argv = tool_argv(url, target)
    result["command"] = shlex.join(argv)
    if check:
        result["status"] = "available"
        return result
    uv = shutil.which("uv")
    if uv is None:
        raise UpdateError("uv_missing", f"`uv` is not on PATH. Run: {result['command']}")
    with _lock():
        had_plugin = local.tern_plugin_version() is not None
        try:
            out = subprocess.run([uv, *argv[1:]], capture_output=True, text=True,
                                 timeout=UV_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise UpdateError("update_failed", f"`{result['command']}` did not finish in "
                              f"{UV_TIMEOUT_S // 60} minutes; symphony {__version__} still "
                              "works.") from exc
        if out.returncode != 0:
            tail = "\n".join((out.stderr or out.stdout).strip().splitlines()[-5:])
            raise UpdateError("update_failed", f"`{result['command']}` failed; symphony "
                              f"{__version__} still works.\n{tail}")
        result["status"] = "updated"
        result["plugin"] = _plugin_from_new_cli(had_plugin)
    return result


# -- the check after a command -------------------------------------------------------------------

def auto_disabled() -> str | None:
    """Why the check after a command does not run, or None."""
    if os.environ.get("CI", "").strip().lower() not in ("", "0", "false"):
        return "ci"
    if os.environ.get("SYMPHONY_AUTO_UPDATE", "").strip().lower() in ("0", "false", "no", "off"):
        return "opted_out"
    if os.environ.get("SYMPHONY_CLIENT"):
        # The Tern plugin runs the CLI with SYMPHONY_CLIENT=tern-plugin and updates by itself.
        return "client"
    return None


def _check_path() -> Path:
    return local.cache_dir() / "update-check.json"


def log_path() -> Path:
    return local.cache_dir() / "update.log"


def _due(now: float) -> bool:
    try:
        checked = float(json.loads(_check_path().read_text())["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return not 0 <= now - checked < CHECK_EVERY_S


def _log(line: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}\n")
    except OSError:
        pass


def _spawn_update(target: str) -> None:
    """`symphony update --json` in a detached process that outlives this command; its output
    goes to the log."""
    _log(f"updating symphony {__version__} to {target}")
    with log_path().open("a", encoding="utf-8") as fh:
        subprocess.Popen(
            [sys.executable, "-m", "symphony.cli", "update", "--json"],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, start_new_session=True,
            env={**os.environ, "SYMPHONY_AUTO_UPDATE": "0"},
        )


def after_command() -> None:
    """After a command that talked to the API, at most once every CHECK_EVERY_S: when the CLI is
    older than the server, update a `uv tool` install in the background, or say what to run.
    It writes one line on stderr, never on stdout, and never fails the command."""
    cred = client.seen["cred"]
    if cred is None or auto_disabled():
        return
    try:
        now = time.time()
        if not _due(now):
            return
        path = _check_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": now}))
        target = client.seen["version"] or server_version(cred, timeout=3.0)
        if not older(__version__, target):
            return
        inst = install()
        if inst["kind"] == "tool" and repo_url():
            _spawn_update(target)
            sys.stderr.write(f"Updating symphony {__version__} → {target} in the background\n")
        else:
            sys.stderr.write(f"symphony {__version__} is older than the server ({target}). "
                             f"Update it: {manual_command(inst['kind'], inst['source'], target)}\n")
    except Exception as exc:  # a failed check must not fail the command it follows
        _log(f"update check failed: {exc}")
