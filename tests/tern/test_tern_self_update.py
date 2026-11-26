"""The Tern plugin's self-update (tern-plugin/lib/update.luau), run outside Tern.

`self_update.luau` runs the module under the `luau` CLI with the plugin's modules copied next to
a stub of the `tern` global (`tern_stub.luau`): `plugin.toml` and `tern.kv` are in memory, and
each CLI command the module starts gets the answer the test gives, as Tern would deliver it.
Skipped when `luau` is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tern_views import luau  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("luau") is None, reason="the luau CLI is not installed")

VERSION = ["version", "--json"]
UPDATE = ["update", "--json"]
PLUGIN = ["tern", "install", "--plugin-only", "--json"]


@pytest.fixture(scope="module")
def runner(luau_plugin) -> Path:
    return luau_plugin("self_update", "self_update.luau")


@pytest.fixture
def run(runner: Path):
    def go(case: dict) -> dict:
        proc = subprocess.run(["luau", str(runner), "-a", luau(case)], capture_output=True, text=True,
                              timeout=60, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)
    return go


def version_doc(cli: str, install: str = "tool", command: str | None = None) -> dict:
    return {"status": 0, "doc": {"cli": cli, "install": install, "server": "0.2.28", "command": command,
                                 "plugin_installed": cli, "source": None}}


def test_versions_compare_as_semver(run):
    pairs = [("0.2.9", "0.2.10"), ("0.2.10", "0.2.9"), ("0.2.27", "0.2.27"), ("1.0.0-rc.1", "1.0.0"),
             ("1.0.0-alpha", "1.0.0-alpha.1"), ("1.0.0-2", "1.0.0-beta"), ("v0.1.0", "0.2.0"),
             ("garbage", "0.2.0"), ("0.2.0", None)]
    assert run({"older": [list(p) for p in pairs]})["older"] == [
        True, False, False, True, True, True, True, False, False]


@pytest.mark.parametrize(("own", "info", "server", "tried", "action", "version"), [
    ("0.2.27", {"cli": "0.2.27", "install": "tool"}, "0.2.28", {}, "update_cli", "0.2.28"),
    ("0.2.26", {"cli": "0.2.27", "install": "tool"}, "0.2.27", {}, "install_plugin", "0.2.27"),
    ("0.2.27", {"cli": "0.2.27", "install": "tool"}, "0.2.27", {}, "none", None),
    # A CLI ahead of the server (a newer clone, a rollback) is left alone.
    ("0.2.28", {"cli": "0.2.28", "install": "tool"}, "0.2.27", {}, "none", None),
    ("0.2.27", {"cli": "0.2.27", "install": "tool"}, "0.2.28", {"cli:0.2.28": True}, "none", None),
    ("0.2.26", {"cli": "0.2.27", "install": "tool"}, "0.2.27", {"plugin:0.2.27": True}, "none", None),
    ("0.2.27", {"cli": "0.2.27", "install": "editable"}, "0.2.28", {}, "banner", "0.2.28"),
    ("0.2.26", {"cli": "0.2.27", "install": "editable"}, "0.2.27", {}, "banner", "0.2.27"),
    ("0.2.27", {"cli": "0.2.27", "install": "other"}, "0.2.28", {}, "banner", "0.2.28"),
    ("0.2.27", {"cli": "0.2.27", "install": "tool"}, None, {}, "none", None),
])
def test_decision(run, own, info, server, tried, action, version):
    d = run({"decide": {"own": own, "info": info, "server": server, "tried": tried}})
    assert (d["action"], d.get("version")) == (action, version)


def test_dev_banner_names_the_command(run):
    clone = "git -C /src/symphony pull && uv tool install --editable /src/symphony"
    d = run({"decide": {"own": "0.2.27", "server": "0.2.28",
                        "info": {"cli": "0.2.27", "install": "editable", "command": clone}}})
    assert clone in d["text"]
    d = run({"decide": {"own": "0.2.26", "server": "0.2.27", "info": {"cli": "0.2.27", "install": "editable"}}})
    assert "symphony tern install --plugin-only" in d["text"]


def test_an_older_cli_updates_once_in_the_background(run):
    out = run({"own": "0.2.27", "server": "0.2.28", "answers": [version_doc("0.2.27")], "polls": 3})
    # The update keeps running; later overviews start nothing else.
    assert out["runs"] == [VERSION, UPDATE]
    assert out["notice"]["text"] == "Updating Symphony to 0.2.28…"
    assert list(out["kv"]["self_update.tried"]) == ["cli:0.2.28"]


def test_an_updated_cli_says_it_reloads(run):
    done = {"status": 0, "doc": {"status": "updated", "cli": "0.2.27", "server": "0.2.28",
                                 "plugin": {"status": "installed", "version": "0.2.28"}}}
    out = run({"own": "0.2.27", "server": "0.2.28", "answers": [version_doc("0.2.27"), done]})
    assert out["runs"] == [VERSION, UPDATE]
    assert out["notice"]["text"] == "Updated Symphony to 0.2.28; reloading"


def test_a_failed_update_shows_its_error_once_and_is_not_retried(run):
    failed = {"status": 1, "doc": {"error": {"code": "update_failed",
                                             "message": "`uv tool install` failed; symphony 0.2.27 still works."}}}
    out = run({"own": "0.2.27", "server": "0.2.28", "answers": [version_doc("0.2.27"), failed], "polls": 4})
    assert out["runs"] == [VERSION, UPDATE]
    assert out["notice"]["tone"] == "error"
    assert "still works" in out["notice"]["text"]


def test_an_older_plugin_installs_the_clis_copy(run):
    out = run({"own": "0.2.26", "server": "0.2.27", "answers": [version_doc("0.2.27")]})
    assert out["runs"] == [VERSION, PLUGIN]
    assert out["notice"]["text"] == "Updating the Symphony plugin to 0.2.27…"


def test_a_dev_install_gets_a_banner_and_no_action(run):
    out = run({"own": "0.2.27", "server": "0.2.28", "polls": 3,
               "answers": [version_doc("0.2.27", "editable", "git -C /src pull && uv tool install --editable /src")]})
    assert out["runs"] == [VERSION]
    assert out["notice"]["tone"] == "warning" and "uv tool install --editable /src" in out["notice"]["text"]


def test_a_target_tried_before_a_reload_is_not_tried_again(run):
    # The reload that follows an update starts a new VM: the attempt survives it in tern.kv.
    kv = {"self_update.tried": {"cli:0.2.28": 9e15}}
    out = run({"own": "0.2.27", "server": "0.2.28", "kv": kv, "answers": [version_doc("0.2.27")]})
    assert out["runs"] == [VERSION]


def test_the_new_plugin_announces_the_update_after_the_reload(run):
    out = run({"own": "0.2.28", "server": "0.2.28", "kv": {"self_update.pending": "0.2.28"}})
    # A plugin as new as the server asks the CLI nothing.
    assert out["runs"] == []
    assert out["notice"]["text"] == "Symphony updated to 0.2.28"


def test_a_plugin_as_new_as_the_server_asks_the_cli_nothing(run):
    out = run({"own": "0.2.28", "server": "0.2.28", "polls": 3})
    assert out["runs"] == [] and out["notice"] is None


def test_a_cli_without_the_version_command_changes_nothing(run):
    old_cli = {"status": 1, "doc": {"error": {"code": "usage", "message": "invalid choice: 'version'"}}}
    out = run({"own": "0.2.27", "server": "0.2.28", "answers": [old_cli], "polls": 3})
    assert out["runs"] == [VERSION]
    assert out["notice"] is None
