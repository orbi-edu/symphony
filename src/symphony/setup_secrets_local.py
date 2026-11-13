"""Local backend of the integration secret store: `<root>/secrets.env`.

`symphony.entry.local` creates one `LocalSecretStore` per process, loads the file at start
and hands the store to the API (`build_services(secrets=...)`), so `/v1/setup` writes land
here. A write:

1. rewrites the file atomically, mode 0600, one `NAME='value'` assignment per variable
   (POSIX shell quoting, so `set -a; . secrets.env` reads it back; a multi-line value such
   as a PEM key stays inside its quotes);
2. updates this process's environment, which every consumer reads at call time (the
   tracker is built per tick, notifications read `SLACK_*`/`DISCORD_*` per message, the
   webhook routes read their secrets per request);
3. calls `on_change(names)` (at once, or at `restart(groups)` when the write passed
   `restart=False`), where the stack drops what it caches (the GitHub App installation
   credentials) and wakes the tick, so the next tick uses the new value without a restart.
   No process restarts, so `put`/`remove`/`restart` return no consumers (`[]`).

Precedence, local mode only: a variable already set in the host environment when the
process started wins over the file. Loading skips it, and a write or removal of it fails
with `host_env_overrides` (409) instead of storing a value that would be ignored. In
Kubernetes there is one source, the Secrets read through `envFrom`.

Values are never logged, returned or passed in argv; errors name variables only.
"""

from __future__ import annotations

import os
import shlex
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from .edge import log
from .setup_secrets import GROUPS, READ_ONLY_GROUPS, SecretStoreError, check_names

_MAX_VALUE = 64 * 1024


class LocalSecretStore:
    mode = "local"

    def __init__(
        self, path: str | os.PathLike, *, on_change: Callable[[set[str]], None] | None = None
    ) -> None:
        self.path = Path(path)
        self.on_change = on_change
        self._lock = threading.Lock()
        known = {name for names in GROUPS.values() for name in names}
        # Snapshot before loading: these came from the host and keep winning.
        self.host_names = frozenset(n for n in known if os.environ.get(n, "").strip())
        self._values = self._read()
        self._pending: set[str] = set()
        for name, value in self._values.items():
            if name not in self.host_names:
                os.environ[name] = value

    # -- the file --------------------------------------------------------------
    def _read(self) -> dict[str, str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        if self.path.stat().st_mode & 0o077:
            os.chmod(self.path, 0o600)
        known = {name for names in GROUPS.values() for name in names}
        values: dict[str, str] = {}
        try:
            words = shlex.split(text, comments=True, posix=True)
        except ValueError:
            raise SecretStoreError(
                "secrets_file_invalid", f"{self.path} is not valid shell assignments.", 500
            ) from None
        for word in words:
            name, sep, value = word.partition("=")
            if not sep or name not in known:
                log.warning("secrets_file_entry_ignored", path=str(self.path), name=name if sep else "?")
                continue
            values[name] = value
        return values

    def _write(self, values: Mapping[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "# Symphony integration secrets (written by setup). Mode 0600.\n" + "".join(
            f"{name}={shlex.quote(values[name])}\n" for name in sorted(values)
        )
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # -- the store -------------------------------------------------------------
    def _names(self, group: str, names: Iterable[str]) -> list[str]:
        names = list(names)
        check_names(group, names)
        overridden = sorted(set(names) & self.host_names)
        if overridden:
            raise SecretStoreError(
                "host_env_overrides",
                f"{', '.join(overridden)} is set in the host environment, which wins in local "
                "mode. Change or unset it there and restart.",
                409,
            )
        return names

    def put(self, group: str, values: Mapping[str, str], *, restart: bool = True) -> list[str]:
        names = self._names(group, values)
        for name in names:
            value = values[name]
            if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > _MAX_VALUE:
                raise SecretStoreError("invalid_secret", f"{name} needs a nonempty bounded value.", 422)
        with self._lock:
            merged = {**self._values, **{name: values[name] for name in names}}
            self._write(merged)
            self._values = merged
            os.environ.update({name: values[name] for name in names})
            self._pending.update(names)
        return self.restart([group]) if restart else []

    def remove(self, group: str, names: Iterable[str], *, restart: bool = True) -> list[str]:
        names = self._names(group, names)
        with self._lock:
            remaining = {k: v for k, v in self._values.items() if k not in names}
            self._write(remaining)
            self._values = remaining
            for name in names:
                os.environ.pop(name, None)
            self._pending.update(names)
        return self.restart([group]) if restart else []

    def restart(self, groups: Iterable[str]) -> list[str]:
        """The environment already holds the values; tell the stack (`on_change`) about the
        names of `groups` written since the last restart. No process restarts: returns []."""
        wanted = {name for group in groups for name in GROUPS.get(group, ())}
        with self._lock:
            names = self._pending & wanted
            self._pending -= names
        if names:
            log.info("setup_secrets_changed", names=sorted(names))
            if self.on_change is not None:
                self.on_change(names)
        return []

    def has(self, group: str, name: str) -> bool:
        if group not in READ_ONLY_GROUPS and name not in GROUPS.get(group, ()):
            return False
        return bool(os.environ.get(name, "").strip())
