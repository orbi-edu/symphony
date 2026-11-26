"""Headless Tern harness for the Symphony plugin.

Runs `tern serve` with an isolated `TERN_CONFIG_DIR`, the plugin loaded as a Tern test fixture
(`STENCIL_FIXTURE_ROOT=<root>/crates/app`, plugin copied to `<root>/crates/plugins/fixtures/
symphony`, then `tern ctl plugins fixtures`), the fake API (`fake_api.py`) and the stub CLI
(`bin/symphony`) first on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PLUGIN = REPO / "tern-plugin"
TOKEN = "sym_tern_7f3a9c2e5b1d4e8f"

# Lines Tern logs when a plugin handler raises or a view is rejected.
PLUGIN_ERRORS = ("plugin handler failed", "plugin failed", "stencil_plugins: ", "unknown kind", "duplicate child key")


# Node classes whose text is what a person reads (headers, rows, chips, code).
TEXT_LEAVES = (
    ".sf-text", ".sf-item", ".sf-tbl-row", ".sf-card-head", ".sf-badge", ".sf-tab", ".sf-kv",
    ".sf-code", ".sf-input", ".sf-overlay", ".sf-shimmer",
)


class TernError(RuntimeError):
    pass


def tern_binary() -> str | None:
    return shutil.which("tern")


class Harness:
    def __init__(
        self, *, state: str = "team", logged_in: bool = True, role: str = "admin", size: str = "1440 960",
        server_version: str | None = None,
    ) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="symtern-", dir="/tmp"))
        self.state = state
        self.logged_in = logged_in
        self.role = role
        self.size = size
        # The `version` the fake API's overview reports (default: this checkout's, as the stub CLI's).
        self.server_version = server_version
        self.sock = self.tmp / "ctl.sock"
        self.out = self.tmp / "out"
        self.stub_home = self.tmp / "stub"
        self.writes = self.tmp / "writes.jsonl"
        self.serve_log = self.tmp / "serve.log"
        self.api: subprocess.Popen | None = None
        self.redis_proc: subprocess.Popen | None = None
        self.serve: subprocess.Popen | None = None
        self.url = ""

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self) -> Harness:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        root = self.tmp / "root"
        (root / "crates" / "app").mkdir(parents=True)
        shutil.copytree(PLUGIN, root / "crates" / "plugins" / "fixtures" / "symphony")
        env = dict(os.environ)
        env.pop("SYMPHONY_URL", None)
        env.pop("SYMPHONY_TOKEN", None)
        env.update(
            TERN_CONFIG_DIR=str(self.tmp / "config"),
            STENCIL_FIXTURE_ROOT=str(root / "crates" / "app"),
            STENCIL_LOG="warn,tern::plugin=info",
        )
        self.start_backend(env)
        self.serve = subprocess.Popen(
            ["tern", "serve", "--control", str(self.sock), "--out", str(self.out)],
            stdout=self.serve_log.open("w"), stderr=subprocess.STDOUT, env=env, cwd=str(self.tmp),
        )
        deadline = time.monotonic() + 15
        while not self.sock.exists():
            if time.monotonic() > deadline or self.serve.poll() is not None:
                raise TernError("tern serve did not start:\n" + self.serve_log.read_text())
            time.sleep(0.05)
        self.ctl("size", self.size)
        self.ctl("ready")
        self.ctl("plugins", "fixtures")
        # Tern 0.9.26+ dropped `ctl status-bar` (the status line is always on there).
        self.ctl("status-bar", "on", check=False)

    def start_redis(self) -> str:
        """A disposable `redis-server` on a free port; returns its URL."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.redis_proc = subprocess.Popen(
            [shutil.which("redis-server") or "redis-server", "--port", str(port), "--save", "",
             "--appendonly", "no"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return f"redis://127.0.0.1:{port}/0"
            except OSError:
                if time.monotonic() > deadline or self.redis_proc.poll() is not None:
                    raise TernError("redis-server did not start") from None
                time.sleep(0.05)

    def start_backend(self, env: dict) -> None:
        """The fake API and the stub CLI; `env` is what `tern serve` (and so the plugin) gets."""
        ready = self.tmp / "api.url"
        extra = ["--server-version", self.server_version] if self.server_version else []
        self.api = subprocess.Popen(
            [str(REPO / ".venv" / "bin" / "python"), str(HERE / "fake_api.py"), "--port", "0", "--log", str(self.writes),
             "--ready", str(ready), "--state", self.state, "--redis-url", self.start_redis(), *extra],
            stdout=(self.tmp / "api.log").open("w"), stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while not ready.exists():
            if time.monotonic() > deadline or self.api.poll() is not None:
                raise TernError("fake API did not start:\n" + (self.tmp / "api.log").read_text())
            time.sleep(0.02)
        self.url = ready.read_text().strip()
        self.stub_home.mkdir()
        if self.logged_in:
            self.login()
        env.update(
            PATH=f"{HERE / 'bin'}:{env.get('PATH', '')}",
            SYMPHONY_STUB_HOME=str(self.stub_home),
            # Absolute, so shells the plugin opens (takeover tabs) find the stub too.
            SYMPHONY_BIN=str(HERE / "bin" / "symphony"),
        )

    def stop(self) -> None:
        if self.serve and self.serve.poll() is None:
            try:
                self.ctl("quit")
                self.serve.wait(5)
            except (TernError, subprocess.TimeoutExpired):
                self.serve.kill()
        if self.api and self.api.poll() is None:
            self.api.terminate()
            self.api.wait(5)
        if self.redis_proc and self.redis_proc.poll() is None:
            self.redis_proc.terminate()
            self.redis_proc.wait(5)
        # SYMPHONY_TERN_KEEP=1 keeps serve.log, the write log and raw shots for debugging.
        if not os.environ.get("SYMPHONY_TERN_KEEP"):
            shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self) -> None:
        (self.stub_home / "creds.json").write_text(
            json.dumps({"url": self.url, "token": TOKEN, "name": "alice", "role": self.role})
        )

    # -- control -------------------------------------------------------------
    def ctl(self, *words: str, check: bool = True) -> dict:
        proc = subprocess.run(
            ["tern", "ctl", "--control", str(self.sock), *words],
            capture_output=True, text=True, timeout=60,
        )
        line = (proc.stdout.strip().splitlines() or ["{}"])[-1]
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise TernError(f"ctl {' '.join(words)}: {proc.stdout}{proc.stderr}") from exc
        if check and not reply.get("ok"):
            raise TernError(f"ctl {' '.join(words)}: {reply.get('error')}")
        return reply

    def run(self, action: str) -> None:
        self.ctl("plugins", "run", action)

    def expect(self, text: str) -> None:
        """Waits (up to 20 s) until the focused pane's surface shows `text`."""
        self.ctl("plugins", "expect", json.dumps(text, ensure_ascii=False))

    def text(self, region: str = "sf-main") -> str:
        """The visible text of the focused pane's `region`, one line per leaf node (`tree` caps
        the text of a large node, so this asks for the leaves)."""
        selector = ", ".join(f".tn-pane.on .{region} {leaf}" for leaf in TEXT_LEAVES)
        reply = self.ctl("tree", selector)
        return "\n".join(n.get("text", "") for n in reply.get("nodes", []))

    def wait_text(self, needle: str, region: str = "sf-main", timeout: float = 10) -> str:
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            text = self.text(region)
            if needle in text:
                return text
            time.sleep(0.1)
        raise TernError(f"{needle!r} never appeared; last text:\n{text[:2000]}")

    def wait_gone(self, needle: str, region: str = "sf-main", timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle not in self.text(region):
                return
            time.sleep(0.1)
        raise TernError(f"{needle!r} is still shown")

    def picker(self) -> str:
        """The labels of Tern's native picker (`.tn-pick`), which lives outside the plugin surface."""
        reply = self.ctl("tree", ".tn-pick .ck-label")
        return "\n".join(n.get("text", "") for n in reply.get("nodes", []))

    def wait_picker(self, needle: str, timeout: float = 15) -> str:
        """Waits until the open picker lists `needle`: a picker the window opens after a host
        round trip (the model catalog is decoded host-side) takes longer under load."""
        deadline = time.monotonic() + timeout
        labels = ""
        while time.monotonic() < deadline:
            labels = self.picker()
            if needle in labels:
                return labels
            time.sleep(0.1)
        raise TernError(f"the picker never listed {needle!r}; it lists:\n{labels[:1000]}")

    def click(self, selector: str) -> None:
        self.ctl("click", selector)

    def key(self, combo: str) -> None:
        self.ctl("key", combo)

    def type(self, text: str) -> None:
        self.ctl("type", json.dumps(text, ensure_ascii=False))

    def shot(self, name: str, theme: str = "light") -> Path:
        self.ctl("appearance", theme)
        self.ctl("wait", "400")
        reply = self.ctl("shot", name)
        return Path(reply["png"])

    def shots(self, name: str, dest: Path | None = None) -> list[Path]:
        out = []
        for theme in ("light", "dark"):
            png = self.shot(f"{name}-{theme}", theme)
            if dest:
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / f"{name}-{theme}.png"
                shutil.copyfile(png, target)
                png = target
            out.append(png)
        self.ctl("appearance", "light")
        return out

    # -- assertions ------------------------------------------------------------
    def plugin_errors(self) -> list[str]:
        lines = self.serve_log.read_text(errors="replace").splitlines()
        return [ln for ln in lines if any(m in ln for m in PLUGIN_ERRORS)]

    def write_log(self) -> list[dict]:
        if not self.writes.exists():
            return []
        return [json.loads(ln) for ln in self.writes.read_text().splitlines() if ln.strip()]

    def calls(self) -> list[list[str]]:
        path = self.stub_home / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    def wait_write(self, method: str, path: str, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for w in self.write_log():
                if w["method"] == method and w["path"] == path:
                    return w
            time.sleep(0.05)
        raise TernError(f"no {method} {path}; writes: {self.write_log()}")
