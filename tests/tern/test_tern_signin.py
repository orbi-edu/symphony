"""The sign-in block's "Install in Tern" step and the missing-CLI error, run outside Tern.

`signin_install.luau` presses Enter on the block's install step under the `luau` CLI, with the
plugin's modules copied next to a stub of the `tern` global (`tern_stub.luau`) that records each
spawn. The test then runs the recorded argv as Tern's daemon would, with the daemon's PATH, and a
fake CLI where `uv tool install` puts the real one. `cli_missing.luau` shows the error text for a
CLI that cannot start. Skipped when `luau` is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

# launchd's PATH: what a Tern started from the Dock gets, without the login shell's additions.
DAEMON_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
INSTALL = ["tern", "install", "--no-plugin", "--json"]

pytestmark = pytest.mark.skipif(shutil.which("luau") is None, reason="the luau CLI is not installed")


@pytest.fixture(scope="module")
def runner(luau_plugin) -> Path:
    return luau_plugin("signin", "signin_install.luau")


@pytest.fixture(scope="module")
def missing(luau_plugin) -> Path:
    return luau_plugin("missing", "cli_missing.luau")


def env_literal(getenv: dict[str, str]) -> str:
    return "{" + ",".join(f"[{json.dumps(k)}]={json.dumps(v)}" for k, v in getenv.items()) + "}"


def spawned_argv(runner: Path, getenv: dict[str, str]) -> list[str]:
    """The argv the block hands to `tern.process.run` when `tern.getenv` returns `getenv`."""
    proc = subprocess.run(["luau", str(runner), "-a", env_literal(getenv)], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def fake_cli(bindir: Path) -> Path:
    """A `symphony` that records its arguments in `<bindir>/args` and prints a JSON result."""
    bindir.mkdir(parents=True, exist_ok=True)
    cli = bindir / "symphony"
    cli.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$(dirname "$0")/args"\necho \'{}\'\n')
    cli.chmod(0o755)
    return cli


def recorded(bindir: Path) -> list[str] | None:
    args = bindir / "args"
    return args.read_text().splitlines() if args.exists() else None


def daemon_run(argv: list[str], **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, env={"PATH": DAEMON_PATH, **env}, capture_output=True, text=True,
                          timeout=30, check=False)


@pytest.mark.parametrize("where", ["home", "uv_tool_bin_dir"])
def test_install_finds_a_uv_cli_missing_from_the_daemon_path(runner, tmp_path, where):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home)}
    if where == "home":
        bindir = home / ".local" / "bin"
    else:
        bindir = tmp_path / "tools"
        env["UV_TOOL_BIN_DIR"] = str(bindir)
    fake_cli(bindir)

    proc = daemon_run(spawned_argv(runner, env), **env)

    assert proc.returncode == 0, proc.stderr
    # Configures the chat agent and the MCP server; the running plugin is not reinstalled.
    assert recorded(bindir) == INSTALL


def test_install_prefers_the_cli_on_the_daemon_path(runner, tmp_path):
    home = tmp_path / "home"
    fake_cli(home / ".local" / "bin")
    on_path = tmp_path / "path-bin"
    fake_cli(on_path)

    proc = subprocess.run(spawned_argv(runner, {"HOME": str(home)}), capture_output=True, text=True,
                          env={"PATH": f"{on_path}:{DAEMON_PATH}", "HOME": str(home)}, timeout=30)

    assert proc.returncode == 0, proc.stderr
    assert recorded(on_path) == INSTALL
    assert recorded(home / ".local" / "bin") is None


def test_install_keeps_an_explicit_symphony_bin(runner, tmp_path):
    home = tmp_path / "home"
    fake_cli(home / ".local" / "bin")
    explicit = fake_cli(tmp_path / "custom")

    argv = spawned_argv(runner, {"HOME": str(home), "SYMPHONY_BIN": str(explicit)})
    proc = daemon_run(argv, HOME=str(home))

    assert argv[0] == str(explicit)
    assert proc.returncode == 0, proc.stderr
    assert recorded(tmp_path / "custom") == INSTALL
    assert recorded(home / ".local" / "bin") is None


def test_install_without_any_cli_exits_127(runner, tmp_path):
    # 127 is what `cli.run` reports as `cli_missing`.
    system = [Path(d, "symphony") for d in [*DAEMON_PATH.split(":"), "/opt/homebrew/bin", "/usr/local/bin"]]
    if any(p.exists() for p in system):
        pytest.skip("this machine has a symphony CLI in a system directory")
    home = tmp_path / "home"
    home.mkdir()
    proc = daemon_run(spawned_argv(runner, {"HOME": str(home)}), HOME=str(home))
    assert proc.returncode == 127


@pytest.mark.parametrize("how", ["exit127", "throws"])
def test_missing_cli_error_names_what_tern_tried(missing, how):
    def shown(getenv: dict[str, str]) -> str:
        proc = subprocess.run(["luau", str(missing), "-a", env_literal(getenv), how], capture_output=True,
                              text=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout.strip()

    # Without SYMPHONY_BIN: the directories the lookup searched, and how to name the CLI instead.
    lookup = shown({"HOME": "/home/alice"})
    assert "$UV_TOOL_BIN_DIR" in lookup and "~/.local/bin" in lookup and "set SYMPHONY_BIN" in lookup
    # An explicit SYMPHONY_BIN skips the lookup: the error names that path, not the directories.
    explicit = shown({"HOME": "/home/alice", "SYMPHONY_BIN": "/opt/acme/symphony"})
    assert "/opt/acme/symphony" in explicit
    assert "~/.local/bin" not in explicit and "set SYMPHONY_BIN" not in explicit
