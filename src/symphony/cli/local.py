"""Commands that change the person's own machine: `omp sync`, `tern install`, `takeover`."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ruamel.yaml import YAML

from symphony.cli import creds
from symphony.cli.client import Api

OMP_PROFILE = "symphony"
TERN_AGENT_ID = "symphony"
MCP_NAME = "symphony"
REPO_ROOT = Path(__file__).resolve().parents[3]


class LocalError(Exception):
    pass


# -- omp ---------------------------------------------------------------------

def omp_binary() -> str | None:
    """`SYMPHONY_OMP`, else the omp Tern manages, else `omp` on PATH. The PATH one comes last
    on purpose: workstations were found running an old `~/.local/bin/omp` ahead of Tern's."""
    explicit = os.environ.get("SYMPHONY_OMP")
    if explicit:
        return explicit
    for managed in (Path.home() / "Library/Application Support/Tern/omp/bin/omp",):
        if managed.is_file() and os.access(managed, os.X_OK):
            return str(managed)
    return shutil.which("omp")


def omp_version(binary: str) -> str | None:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", out.stdout + out.stderr)
    return match.group(1) if match else None


def omp_profile_dir() -> Path:
    """`~/.omp/profiles/symphony/agent` (omp `dirs.ts`: `<home>/<PI_CONFIG_DIR|.omp>/profiles/<name>`)."""
    return Path.home() / (os.environ.get("PI_CONFIG_DIR") or ".omp") / "profiles" / OMP_PROFILE / "agent"


def _deep_merge(dst, src: dict) -> None:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value


def omp_sync(api: Api) -> dict:
    """Write the team's omp settings and developer model into the dedicated `symphony` omp
    profile (`omp --profile symphony`), never the person's default profile, then report the
    generation applied as `member:<token name>`."""
    doc = api.get("/v1/admin/config")
    generation = int(doc.get("generation", 0))
    developer = (doc.get("models") or {}).get("developer") or {}
    if not developer.get("provider") or not developer.get("model"):
        raise LocalError("The team config does not set a model for the developer role.")
    thinking = developer.get("thinking", "medium")
    selector = f"{developer['provider']}/{developer['model']}:{thinking}"
    omp = doc.get("omp") or {}

    path = omp_profile_dir() / "config.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.preserve_quotes = True
    current = yaml.load(path.read_text()) if path.exists() else None
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise LocalError(f"{path} is not a YAML mapping. Fix or delete the file.")
    _deep_merge(current, dict(omp.get("settings") or {}))
    roles = current.get("modelRoles")
    if not isinstance(roles, dict):
        roles = current["modelRoles"] = {}
    roles["default"] = selector
    current["defaultThinkingLevel"] = thinking
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as fh:
        yaml.dump(current, fh)
    os.replace(tmp, path)

    binary = omp_binary()
    local_version = omp_version(binary) if binary else None
    wanted = omp.get("version")
    api.post("/v1/admin/config/observed", {"generation": generation})
    state = {"url": api.cred.url, "generation": generation, "at": int(time.time() * 1000)}
    creds.config_dir().mkdir(parents=True, exist_ok=True)
    (creds.config_dir() / "omp-sync.json").write_text(json.dumps(state) + "\n")
    return {
        "profile": OMP_PROFILE,
        "config": str(path),
        "generation": generation,
        "model": selector,
        "omp": {"path": binary, "version": local_version, "team_version": wanted,
                "matches": bool(wanted) and local_version == wanted},
    }


# -- Tern --------------------------------------------------------------------

def tern_settings_path() -> Path:
    explicit = os.environ.get("TERN_CONFIG_DIR")
    if explicit:
        return Path(explicit) / "settings.json"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Tern/settings.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Tern" / "settings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "tern" / "settings.json"


def _acp_command() -> tuple[str, list[str]]:
    """The bridge's command for Tern, as an absolute path: Tern 0.8.3 resolves an agent's
    `command` on its own PATH, not the PATH of the shell that ran `symphony tern install`
    (headless check: a bare `symphony` "wasn't found on PATH" though the shell found it)."""
    found = shutil.which("symphony")
    if found:
        return str(Path(found).absolute()), ["acp"]
    argv0 = Path(sys.argv[0]).absolute() if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name == "symphony" and argv0.is_file():
        return str(argv0), ["acp"]
    return sys.executable, ["-m", "symphony.cli", "acp"]


def _upsert(items: list, key: str, value: str, entry: dict) -> str:
    for i, item in enumerate(items):
        if isinstance(item, dict) and item.get(key) == value:
            merged = {**item, **entry}
            if merged == item:
                return "unchanged"
            items[i] = merged
            return "updated"
    items.append(entry)
    return "added"


def tern_install(cred: creds.Credential, *, plugin_dir: str | None, install_plugin: bool) -> dict:
    path = tern_settings_path()
    try:
        doc = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    except json.JSONDecodeError as exc:
        raise LocalError(f"{path} is not valid JSON (line {exc.lineno}, column {exc.colno}). "
                         "Nothing was changed.") from exc
    if not isinstance(doc, dict):
        raise LocalError(f"{path} is not a JSON object. Nothing was changed.")
    agents = doc.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise LocalError(f"{path}: `agents` is not an object. Nothing was changed.")
    custom = agents.setdefault("custom", [])
    mcp = agents.setdefault("mcp", [])
    if not isinstance(custom, list) or not isinstance(mcp, list):
        raise LocalError(f"{path}: `agents.custom` and `agents.mcp` must be lists.")
    command, args = _acp_command()
    agent = _upsert(custom, "id", TERN_AGENT_ID, {
        "id": TERN_AGENT_ID, "name": "Symphony", "command": command, "args": args,
    })
    # Literal header: Tern 0.8.3 did not expand `${VAR}` in MCP headers (spike S6), hence 0600.
    server = _upsert(mcp, "name", MCP_NAME, {
        "name": MCP_NAME, "transport": "http", "url": f"{cred.url}/mcp",
        "headers": {"Authorization": f"Bearer {cred.token}"},
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    if agent != "unchanged" or server != "unchanged" or not path.exists():
        creds._write_private(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    os.chmod(path, 0o600)
    result = {"settings": str(path), "agent": agent, "mcp": server,
              "command": [command, *args], "plugin": None}
    if install_plugin:
        result["plugin"] = _install_plugin(Path(plugin_dir) if plugin_dir else None)
    return result


def _install_plugin(source: Path | None) -> dict:
    if source is None:
        # Only a source checkout (`uv tool install --editable .`) has the plugin next to the CLI;
        # a regular install has only the Python package.
        source = REPO_ROOT / "tern-plugin"
        if not (source / "plugin.toml").is_file():
            raise LocalError("This symphony install has no Tern plugin source (it is not a source "
                             "checkout). Use --plugin DIR, or --no-plugin if the plugin is already "
                             "installed in Tern.")
    if not (source / "plugin.toml").is_file():
        raise LocalError(f"The Tern plugin was not found in {source}. Use --plugin DIR.")
    tern = shutil.which("tern")
    if not tern:
        raise LocalError("The `tern` command is not on PATH. Install Tern or use --no-plugin.")
    out = subprocess.run([tern, "plugin", "install", str(source), "--force", "--json"],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise LocalError(f"`tern plugin install` failed: {(out.stderr or out.stdout).strip()}")
    try:
        detail = json.loads(out.stdout) if out.stdout.strip() else {}
    except ValueError:
        detail = {"output": out.stdout.strip()}
    return {"source": str(source), "result": detail}


# -- takeover ----------------------------------------------------------------

def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "symphony"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def fetch_branch(meta: dict, cwd: Path) -> dict:
    """Fetch the attempt's WIP branch into the repository at `cwd` and switch to it when the
    working tree is clean. Never discards local changes."""
    branch = meta.get("branch")
    if not branch:
        return {"status": "no_branch", "message": "the session recorded no branch"}
    top = _git("rev-parse", "--show-toplevel", cwd=cwd)
    if top.returncode != 0:
        return {"status": "not_a_repo", "branch": branch,
                "message": f"{cwd} is not a git repository; run takeover inside the clone"}
    repo = meta.get("repo")
    origin = _git("remote", "get-url", "origin", cwd=cwd).stdout.strip()
    if repo and origin and repo.lower().removesuffix(".git") not in origin.lower():
        return {"status": "other_repo", "branch": branch,
                "message": f"this clone ({origin}) is not {repo}"}
    fetched = _git("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=cwd)
    if fetched.returncode != 0:
        return {"status": "fetch_failed", "branch": branch, "message": fetched.stderr.strip()}
    if _git("status", "--porcelain", cwd=cwd).stdout.strip():
        return {"status": "fetched_dirty", "branch": branch,
                "message": f"origin/{branch} fetched; local changes exist, so the branch did not change"}
    local = _git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=cwd).returncode == 0
    if local:
        switched = _git("switch", branch, cwd=cwd)
        if switched.returncode == 0:
            merged = _git("merge", "--ff-only", f"origin/{branch}", cwd=cwd)
            if merged.returncode != 0:
                return {"status": "diverged", "branch": branch,
                        "message": f"the local {branch} diverged from origin/{branch}; resolve it first"}
    else:
        switched = _git("switch", "-c", branch, "--track", f"origin/{branch}", cwd=cwd)
    if switched.returncode != 0:
        return {"status": "switch_failed", "branch": branch, "message": switched.stderr.strip()}
    return {"status": "switched", "branch": branch, "message": f"on branch {branch}"}


def omp_resume_argv(transcript: Path) -> list[str]:
    binary = omp_binary() or "omp"
    argv = [binary]
    if (omp_profile_dir() / "config.yml").exists():
        argv += ["--profile", OMP_PROFILE]
    return argv + ["--resume", str(transcript)]
