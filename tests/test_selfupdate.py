"""`symphony version`, `symphony update` and the update check after a command (docs/cli.md),
against the in-process fake API. Processes (`uv`, `tern`, the new CLI) are faked: each test
says what they answer and checks the commands the CLI runs."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from symphony import __version__
from symphony.cli import client, local, main, selfupdate
from test_cli_commands import home, keychain, login, run  # noqa: F401

pytest_plugins = ["test_cli_fake_api"]

REPO = "https://example.com/acme/symphony.git"
MAJOR, MINOR, PATCH = (int(x) for x in __version__.split("-")[0].split(".")[:3])
NEXT = f"{MAJOR}.{MINOR}.{PATCH + 1}"
CHECKOUT = Path(__file__).resolve().parent.parent


class Processes:
    """`subprocess.run` and `shutil.which` for `uv`, `tern` and the CLI `uv` writes."""

    def __init__(self, tmp: Path) -> None:
        self.calls: list[list[str]] = []
        self.bindir = tmp / "tool-bin"
        self.bindir.mkdir()
        (self.bindir / "symphony").write_text("#!/bin/sh\n")
        self.tern_plugin: str | None = "0.0.1"  # the plugin version Tern has (None: none)
        self.uv_status = 0

    def which(self, name: str) -> str | None:
        return f"/opt/bin/{name}" if name in ("uv", "tern") else None

    def run(self, argv, **_kw) -> subprocess.CompletedProcess:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        prog, rest = Path(argv[0]).name, argv[1:]

        def done(stdout: str = "", code: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, code, stdout, stderr)

        if prog == "uv" and rest[:2] == ["tool", "install"]:
            return done(code=self.uv_status, stderr="error: no tag v99" if self.uv_status else "")
        if prog == "uv" and rest[:2] == ["tool", "dir"]:
            return done(f"{self.bindir}\n")
        if prog == "tern" and rest[:2] == ["plugin", "list"]:
            plugins = [{"id": "symphony", "version": self.tern_plugin}] if self.tern_plugin else []
            return done(json.dumps({"plugins": [{"id": "other", "version": "9.9.9"}, *plugins]}))
        if prog == "tern" and rest[:2] == ["plugin", "install"]:
            self.tern_plugin = local.plugin_version(Path(rest[2]))
            return done('{"installed":"symphony"}')
        if argv[0] == str(self.bindir / "symphony"):
            return done(json.dumps({"plugin": {"source": "/new/symphony/tern_plugin", "version": NEXT,
                                               "status": "installed", "result": {}}}))
        raise AssertionError(f"unexpected command {argv}")

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def procs(tmp_path, monkeypatch) -> Processes:
    p = Processes(tmp_path)
    monkeypatch.setattr(subprocess, "run", p.run)
    monkeypatch.setattr(shutil, "which", p.which)
    return p


@pytest.fixture
def packaged(tmp_path, monkeypatch) -> Path:
    """The installed package's plugin (`symphony/tern_plugin`), and no source checkout."""
    plugin = tmp_path / "site-packages" / "symphony" / "tern_plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(f'schema = 1\nid = "symphony"\nversion = "{__version__}"\n')
    monkeypatch.setattr(local, "REPO_ROOT", tmp_path / "site-packages")
    monkeypatch.setattr(local, "PACKAGED_PLUGIN", plugin)
    return plugin


@pytest.fixture
def tool(tmp_path, monkeypatch, packaged) -> None:
    """This CLI as `uv tool install git+<REPO>@v<version>`: a uv tool environment, no checkout."""
    prefix = tmp_path / "tool-env"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(selfupdate, "SOURCE_ROOT", None)
    monkeypatch.setattr(selfupdate, "_direct_url", lambda: {
        "url": REPO, "vcs_info": {"vcs": "git", "requested_revision": f"v{__version__}", "commit_id": "0" * 40}})


@pytest.fixture
def api(home, keychain, fake_api, capsys):  # noqa: F811
    """The fake API, logged in to (with the update check off: tests/conftest.py)."""
    login(capsys, fake_api)
    return fake_api


@pytest.fixture
def auto(api, monkeypatch) -> list[dict]:
    """The update check on, outside CI. The list records the detached processes the CLI starts."""
    for var in ("SYMPHONY_AUTO_UPDATE", "CI", "SYMPHONY_CLIENT"):
        monkeypatch.delenv(var, raising=False)
    started: list[dict] = []

    def popen(argv, **kw):
        started.append({"argv": list(argv), **kw})

    monkeypatch.setattr(subprocess, "Popen", popen)
    return started


# -- versions ------------------------------------------------------------------------------

@pytest.mark.parametrize(("a", "b", "result"), [
    ("0.2.9", "0.2.10", True), ("0.2.10", "0.2.9", False), ("0.2.27", "0.2.27", False),
    ("1.0.0-rc.1", "1.0.0", True), ("1.0.0", "1.0.0-rc.1", False), ("1.0.0-alpha", "1.0.0-alpha.1", True),
    ("1.0.0-2", "1.0.0-beta", True), ("1.0.0-rc.2", "1.0.0-rc.10", True), ("v0.1.0", "0.2.0", True),
    ("1.0.0+build.5", "1.0.0", False), ("garbage", "0.2.0", False), ("0.2.0", None, False),
])
def test_versions_compare_as_semver(a, b, result):
    assert selfupdate.older(a, b) is result


# -- symphony version ------------------------------------------------------------------------

def test_version_reports_the_cli_the_plugin_tern_has_and_the_server(api, capsys,
                                                                    procs, tool):
    api.server_version = NEXT
    procs.tern_plugin = __version__
    code, out, err = run(capsys, "version", "--json")
    assert code == 0, err
    assert json.loads(out) == {
        "cli": __version__, "plugin_installed": __version__, "server": NEXT, "install": "tool",
        "source": f"git+{REPO}@v{__version__}", "command": "symphony update", "server_error": None}


def test_version_of_a_checkout_names_it(api, capsys, procs):
    code, out, _ = run(capsys, "version", "--json")
    doc = json.loads(out)
    assert (doc["install"], doc["source"], doc["cli"]) == ("editable", str(CHECKOUT), __version__)
    assert doc["command"] is None


# -- symphony update ---------------------------------------------------------------------------

def test_update_installs_the_servers_release_then_the_new_plugin(api, capsys,
                                                                 procs, tool):
    api.server_version = NEXT
    code, out, err = run(capsys, "update", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["status"] == "updated"
    assert (result["cli"], result["server"]) == (__version__, NEXT)
    assert result["command"] == f"uv tool install --force git+{REPO}@v{NEXT}"
    assert procs.ran("/opt/bin/uv", "tool", "install") == [
        ["/opt/bin/uv", "tool", "install", "--force", f"git+{REPO}@v{NEXT}"]]
    # The new CLI installs the plugin of the new package; this process still runs the old one.
    assert procs.ran(str(procs.bindir / "symphony")) == [
        [str(procs.bindir / "symphony"), "tern", "install", "--plugin-only", "--json"]]
    assert result["plugin"]["status"] == "installed"
    assert not (local.cache_dir() / "update.lock").exists()


def test_update_check_changes_nothing(api, capsys, procs, tool):
    api.server_version = NEXT
    code, out, _ = run(capsys, "update", "--check", "--json")
    assert code == 0
    assert json.loads(out)["status"] == "available"
    assert procs.ran("/opt/bin/uv", "tool", "install") == []
    code, out, _ = run(capsys, "update", "--check")
    assert f"uv tool install --force git+{REPO}@v{NEXT}" in out


def test_a_failed_update_says_the_old_cli_still_works(api, capsys, procs, tool):
    api.server_version = NEXT
    procs.uv_status = 2
    code, out, _ = run(capsys, "update", "--json")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "update_failed"
    assert f"symphony {__version__} still works" in error["message"] and "no tag v99" in error["message"]
    assert procs.ran(str(procs.bindir / "symphony")) == []
    assert not (local.cache_dir() / "update.lock").exists()


def test_a_current_cli_brings_an_older_plugin_up_from_the_package(api, capsys,
                                                                   procs, tool, packaged):
    code, out, err = run(capsys, "update", "--json")
    assert code == 0, err
    result = json.loads(out)
    assert result["status"] == "current"
    assert procs.ran("/opt/bin/uv") == []
    assert procs.ran("/opt/bin/tern", "plugin", "install") == [
        ["/opt/bin/tern", "plugin", "install", str(packaged), "--force", "--json"]]
    assert (result["plugin"]["installed"], result["plugin"]["version"]) == ("0.0.1", __version__)
    # Run again: Tern has this version now, so nothing is reinstalled.
    code, out, _ = run(capsys, "update", "--json")
    assert json.loads(out)["plugin"]["status"] == "current"
    assert len(procs.ran("/opt/bin/tern", "plugin", "install")) == 1


def test_update_never_touches_a_checkout(api, capsys, procs):
    api.server_version = NEXT
    code, out, _ = run(capsys, "update", "--json")
    assert code == 0
    result = json.loads(out)
    assert (result["status"], result["install"]) == ("manual", "editable")
    assert result["command"] == f"git -C {CHECKOUT} pull && uv tool install --editable {CHECKOUT}"
    assert procs.calls == []


def test_a_current_checkout_reinstalls_an_older_plugin_from_the_checkout(api, capsys, procs):
    code, out, _ = run(capsys, "update", "--json")
    assert json.loads(out)["plugin"]["status"] == "installed"
    assert procs.ran("/opt/bin/tern", "plugin", "install") == [
        ["/opt/bin/tern", "plugin", "install", str(CHECKOUT / "tern-plugin"), "--force", "--json"]]


def test_a_server_without_a_version_is_no_target(api, capsys, procs, tool):
    api.server_version = None
    code, out, _ = run(capsys, "update", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "no_server_version"


# -- the packaged plugin ----------------------------------------------------------------------

def test_tern_install_uses_the_packaged_plugin_without_a_checkout(home, capsys, procs,  # noqa: F811
                                                                   packaged):
    code, out, err = run(capsys, "tern", "install", "--plugin-only", "--json")
    assert code == 0, err
    assert json.loads(out)["plugin"]["source"] == str(packaged)
    assert procs.ran("/opt/bin/tern", "plugin", "install") == [
        ["/opt/bin/tern", "plugin", "install", str(packaged), "--force", "--json"]]
    # Tern has this version now: the plugin that may be drawing the caller is not reloaded.
    code, out, _ = run(capsys, "tern", "install", "--plugin-only", "--json")
    assert json.loads(out)["plugin"]["status"] == "unchanged"
    assert len(procs.ran("/opt/bin/tern", "plugin", "install")) == 1


# -- the check after a command ------------------------------------------------------------------

def overview_reads(api) -> int:
    return sum(1 for method, path, _ in api.requests if (method, path) == ("GET", "/v1/overview"))


def test_an_older_tool_cli_updates_in_the_background_at_most_every_6_hours(
        api, capsys, procs, tool, auto):
    api.server_version = NEXT
    code, out, err = run(capsys, "status", "--json")
    assert code == 0
    json.loads(out)  # stdout keeps only the command's own output
    assert err == f"Updating symphony {__version__} → {NEXT} in the background\n"
    assert overview_reads(api) == 1  # the version came with the overview `status` read
    assert len(auto) == 1
    assert auto[0]["argv"] == [sys.executable, "-m", "symphony.cli", "update", "--json"]
    assert auto[0]["start_new_session"] is True and auto[0]["env"]["SYMPHONY_AUTO_UPDATE"] == "0"
    assert auto[0]["stdout"].name == str(selfupdate.log_path())

    code, _, err = run(capsys, "status", "--json")
    assert err == "" and len(auto) == 1

    check = local.cache_dir() / "update-check.json"
    check.write_text(json.dumps({"checked_at": time.time() - selfupdate.CHECK_EVERY_S - 1}))
    run(capsys, "status", "--json")
    assert len(auto) == 2


def test_a_command_without_an_overview_reads_the_version_once(api, capsys, procs,
                                                              tool, auto):
    api.server_version = NEXT
    code, _, err = run(capsys, "whoami")
    assert code == 0 and "in the background" in err
    assert overview_reads(api) == 1
    run(capsys, "whoami")
    assert overview_reads(api) == 1


def test_a_current_cli_starts_nothing(api, capsys, procs, tool, auto):
    code, _, err = run(capsys, "status")
    assert code == 0 and err == "" and auto == []


def test_a_command_that_does_not_reach_the_api_checks_nothing(capsys, procs, tool, auto):
    run(capsys, "logout")
    assert auto == [] and not (local.cache_dir() / "update-check.json").exists()


@pytest.mark.parametrize(("var", "value"), [
    ("CI", "true"), ("SYMPHONY_AUTO_UPDATE", "0"), ("SYMPHONY_CLIENT", "tern-plugin")])
def test_no_auto_update_in_ci_when_opted_out_or_for_the_plugin(api, capsys, procs,
                                                                tool, auto, monkeypatch, var, value):
    api.server_version = NEXT
    monkeypatch.setenv(var, value)
    code, _, err = run(capsys, "status")
    assert code == 0 and err == "" and auto == []


def test_a_checkout_only_says_what_to_run(api, capsys, procs, auto):
    api.server_version = NEXT
    code, _, err = run(capsys, "status")
    assert code == 0 and auto == []
    assert err == (f"symphony {__version__} is older than the server ({NEXT}). Update it: "
                   f"git -C {CHECKOUT} pull && uv tool install --editable {CHECKOUT}\n")
    assert procs.calls == []


def test_the_acp_bridge_never_checks(api, capsys, procs, tool, auto, monkeypatch):
    api.server_version = NEXT

    def serve() -> int:
        client.Api(main.creds.current()).get(client.OVERVIEW)
        return 0

    monkeypatch.setattr("symphony.cli.acp.serve", serve)
    code, out, err = run(capsys, "acp")
    assert (code, out, err) == (0, "", "")
    assert auto == [] and not (local.cache_dir() / "update-check.json").exists()


def test_a_failed_check_does_not_fail_the_command(api, capsys, procs, tool, auto,
                                                  monkeypatch):
    api.server_version = NEXT

    def broken(*_a, **_kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(subprocess, "Popen", broken)
    code, out, _ = run(capsys, "status", "--json")
    assert code == 0 and json.loads(out)["version"] == NEXT
    assert "update check failed: no space left on device" in selfupdate.log_path().read_text()
