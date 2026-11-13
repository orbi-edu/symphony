"""The person's Symphony credential: the CLI is its only owner (internals §7).

`~/.config/symphony/config.toml` keeps the URL, the token id and the identity `/v1/me` returned at
login; the token itself goes to the OS keychain (`keyring`) and, when no keychain works, to a
`0600` file beside the config. `SYMPHONY_URL` and `SYMPHONY_TOKEN` take precedence over both, so
CI and containers never touch the keychain. Everything else (the ACP bridge, the Tern plugin via
`symphony api`/`symphony token print`, `symphony omp sync`) reads the credential through here.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

KEYRING_SERVICE = "symphony"


class NotLoggedIn(Exception):
    pass


@dataclass(frozen=True)
class Credential:
    url: str
    token: str
    name: str | None = None
    role: str | None = None
    token_id: str | None = None
    source: str = "config"  # "config" or "env"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "symphony"


def config_path() -> Path:
    return config_dir() / "config.toml"


def token_file() -> Path:
    return config_dir() / "token"


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("The URL must start with http:// or https://.")
    return url


def load_config() -> dict:
    try:
        return tomllib.loads(config_path().read_text())
    except FileNotFoundError:
        return {}


def _toml_value(value: str) -> str:
    # A JSON string is a valid TOML basic string for everything we store (URLs, ids, names).
    return json.dumps(value, ensure_ascii=False)


def save_config(doc: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k} = {_toml_value(str(v))}\n" for k, v in doc.items() if v is not None)
    _write_private(path, body)


def _write_private(path: Path, body: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _keyring():
    import keyring

    return keyring


def store_token(url: str, token: str) -> str:
    """Keychain first; a keychain that refuses or silently drops the secret (the `null`/`fail`
    backends, a locked keychain) falls back to the `0600` file. Returns where it went."""
    try:
        kr = _keyring()
        kr.set_password(KEYRING_SERVICE, url, token)
        if kr.get_password(KEYRING_SERVICE, url) == token:
            token_file().unlink(missing_ok=True)
            return "keychain"
    except Exception:  # noqa: BLE001 - any keychain failure means "use the file"
        pass
    token_file().parent.mkdir(parents=True, exist_ok=True)
    _write_private(token_file(), token + "\n")
    return "file"


def read_token(url: str, storage: str | None) -> str | None:
    if storage != "file":
        try:
            token = _keyring().get_password(KEYRING_SERVICE, url)
            if token:
                return token
        except Exception:  # noqa: BLE001
            pass
    try:
        return token_file().read_text().strip() or None
    except FileNotFoundError:
        return None


def forget(url: str | None) -> None:
    if url:
        try:
            _keyring().delete_password(KEYRING_SERVICE, url)
        except Exception:  # noqa: BLE001 - nothing stored there
            pass
    token_file().unlink(missing_ok=True)
    config_path().unlink(missing_ok=True)


def save_login(url: str, token: str, me: dict) -> str:
    storage = store_token(url, token)
    save_config({
        "url": url,
        "token_id": me.get("token_id"),
        "name": me.get("name"),
        "role": me.get("role"),
        "storage": storage,
    })
    return storage


def current() -> Credential:
    """The credential in effect, env first. Raises `NotLoggedIn`."""
    cfg = load_config()
    env_url = os.environ.get("SYMPHONY_URL")
    env_token = os.environ.get("SYMPHONY_TOKEN")
    url = env_url or cfg.get("url")
    if not url:
        raise NotLoggedIn("You are not logged in. Run `symphony login`.")
    url = normalize_url(url)
    if env_token:
        # An env token's identity is unknown until `/v1/me` says it.
        return Credential(url=url, token=env_token, source="env")
    if cfg.get("url") != url:
        raise NotLoggedIn(f"There is no token for {url}. Set SYMPHONY_TOKEN or run `symphony login`.")
    token = read_token(url, cfg.get("storage"))
    if not token:
        raise NotLoggedIn("No token is saved. Run `symphony login`.")
    return Credential(
        url=url,
        token=token,
        name=cfg.get("name"),
        role=cfg.get("role"),
        token_id=cfg.get("token_id"),
    )
