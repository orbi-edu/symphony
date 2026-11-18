"""MCP servers for agents: definitions per role, short-lived tokens, staging, and the
upstream half of the gateway proxy.

Credentials go through the gateway. A remote (`http`/`sse`) server is staged as
`{gateway}/v1/mcp/<name>` with the agent's own MCP token; the gateway checks the token and
adds the stored headers itself. So a stored header value never reaches an agent's
environment, files or command line. A `stdio` server runs where the agent runs and carries
only non-secret `env`.

Storage is the Secret `symphony-mcp` (pre-created empty by `deploy/integrations.yaml`):

- `servers.json`: `{name: definition}`;
- `headers.<name>.json`: `{header: value}`, write-only.

Local mode keeps the same keys in the 0600 file `<root>/mcp.json`. The gateway reads the
Secret as a mounted volume (`mount_dir`), so it needs no Kubernetes token.

Tokens: `sym:<ns>:mcp:token:<sha256(token)>` → `{role, session, exp}`, with a TTL. Held ids of
a server with `cleanup` live under `sym:<ns>:mcp:held:<sha256(token)>` (internals §18). Redis
never holds the token itself or any header value.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
import structlog

from . import metrics, teamconfig

_log = structlog.get_logger()

SERVERS_KEY = "servers.json"
TRANSPORTS = ("http", "sse", "stdio")
REMOTE = ("http", "sse")
TOKEN_TTL_S = 4 * 3600
TOKEN_PREFIX = "mcp_"
PROTOCOL_VERSION = "2025-11-25"
# The private agent dir's settings file (omp reads `$PI_CODING_AGENT_DIR/config.yml`).
# omp 18.20.2 keeps MCP servers on with a `--tools` allowlist and, with the default
# `tools.xdev: true`, mounts their tools as xd:// devices and adds `write` to call them.
# `xdev: false` keeps MCP tools as plain tools, so the allowlist gains no file tool.
AGENT_CONFIG = "mcp:\n  enableProjectConfig: false\ntools:\n  xdev: false\n"
# The gateway forwards only these request headers: the MCP transport headers, and the body's
# type and length (a known length keeps the body unchunked, which some servers require).
FORWARDED = ("accept", "content-type", "content-length", "mcp-session-id", "mcp-protocol-version",
             "last-event-id")
# The gateway never passes these response headers on.
_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade", "set-cookie", "content-length",
})
_RESERVED_HEADERS = frozenset({*FORWARDED, *_HOP, "host", "content-encoding", "accept-encoding",
                               "cookie", "user-agent"})
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}\Z")
_HEADER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
# A stdio server's env lands in the agent's files: names that say "credential" are refused.
_SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSW|API_?KEY|PRIVATE|CREDENTIAL|AUTH|COOKIE|SESSION",
                         re.IGNORECASE)
_FIELDS = frozenset({"transport", "url", "command", "args", "env", "roles", "enabled",
                     "timeout_ms", "instructions", "http_base", "agent_env", "rewrite_origins",
                     "cleanup"})
# `agent_env` templates: the session MCP token and the gateway URLs, nothing else.
PLACEHOLDERS = ("{token}", "{http_url}", "{mcp_url}")
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")
# A literal that looks like a key: a long run of token characters.
_TOKENISH = re.compile(r"[A-Za-z0-9_\-+=]{24,}")
_TOOL = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_ID_FIELD = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
CLEANUP_KEYS = ("acquire_tool", "id_field", "release_tool", "release_arg")
_MAX_AGENT_ENV = 16
_MAX_REWRITE_ORIGINS = 8
# The gateway rewrites links only in bodies up to this size; larger ones pass unchanged.
REWRITE_LIMIT = 8 * 1024 * 1024
CLEANUP_TIMEOUT_S = 60.0
# A release that cannot reach its server is retried by the sweep for this long.
CLEANUP_GIVE_UP_S = 24 * 3600
EV_MCP_LEASE = "symphony.mcp_lease"
# omp's per-server tool timeout. Seiji runs take up to an hour, plus the evidence.
MAX_TIMEOUT_MS = 7_200_000
_MAX_SERVERS = 50
_MAX_DOCUMENT = 256 * 1024
_MAX_HEADERS = 20
_MAX_HEADER_VALUE = 8 * 1024
_REQUEST_TIMEOUT = (3, 10)
CONNECT_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 20.0


class McpError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field = field


def _invalid(field: str, message: str) -> McpError:
    return McpError("invalid_mcp_server", message, 422, field)


def _not_found() -> McpError:
    return McpError("not_found", "mcp server not found", 404)


def _allowed_http_hosts() -> set[str]:
    raw = os.environ.get("SYMPHONY_MCP_ALLOW_HTTP_HOSTS", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise _invalid("name", "name must match ^[a-z0-9][a-z0-9_-]{0,47}$")
    return name


def validate(definition: Any) -> dict:
    """The stored form of one definition. `headers_set` (read side only) is ignored, and
    `preset` fills the fields that the body leaves out."""
    if not isinstance(definition, dict):
        raise _invalid("definition", "the definition must be a JSON object")
    definition = apply_preset(definition)
    body = {key: value for key, value in definition.items() if key not in ("name", "headers_set")}
    if "headers" in body:
        raise _invalid("headers", "secret headers are set with PUT /v1/admin/mcp/{name}/headers")
    for key in body:
        if key not in _FIELDS:
            raise _invalid(key, f"unknown field {key}")
    transport = body.get("transport")
    if transport not in TRANSPORTS:
        raise _invalid("transport", "transport must be http, sse or stdio")
    out: dict[str, Any] = {"transport": transport}
    if transport in REMOTE:
        for key in ("command", "args", "env"):
            if key in body:
                raise _invalid(key, f"{key} is for stdio servers only")
        out["url"] = _url(body.get("url"))
    else:
        if "url" in body:
            raise _invalid("url", "url is for http and sse servers only")
        command = body.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 512 or "\x00" in command:
            raise _invalid("command", "stdio servers need a command")
        out["command"] = command
        args = body.get("args", [])
        if (not isinstance(args, list) or len(args) > 64
                or not all(isinstance(a, str) and len(a) <= 1024 and "\x00" not in a for a in args)):
            raise _invalid("args", "args must be a list of at most 64 strings")
        out["args"] = list(args)
        env = body.get("env", {})
        if not isinstance(env, dict) or len(env) > 64:
            raise _invalid("env", "env must be an object of at most 64 variables")
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
                raise _invalid("env", "env names must be environment variable names")
            if _SECRET_ENV.search(key):
                raise _invalid(f"env.{key}", "env holds non-secret values only; this name looks "
                               "like a credential")
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise _invalid(f"env.{key}", "env values must be strings")
        out["env"] = dict(env)
    roles = body.get("roles", [])
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise _invalid("roles", "roles must be a list of role names")
    unknown = [r for r in roles if r not in teamconfig.ROLES]
    if unknown:
        raise _invalid("roles", f"unknown role {unknown[0]}; roles are {', '.join(teamconfig.ROLES)}")
    out["roles"] = [r for r in teamconfig.ROLES if r in roles]
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _invalid("enabled", "enabled must be true or false")
    out["enabled"] = enabled
    if "timeout_ms" in body:
        timeout = body["timeout_ms"]
        if type(timeout) is not int or not 0 <= timeout <= MAX_TIMEOUT_MS:
            raise _invalid("timeout_ms", f"timeout_ms must be an integer in 0..{MAX_TIMEOUT_MS}")
        out["timeout_ms"] = timeout
    if "instructions" in body:
        if not isinstance(body["instructions"], bool):
            raise _invalid("instructions", "instructions must be true or false")
        out["instructions"] = body["instructions"]
    companion = ("http_base", "agent_env", "rewrite_origins", "cleanup")
    if transport not in REMOTE:
        for key in companion:
            if key in body:
                raise _invalid(key, f"{key} is for http and sse servers only")
        return out
    if "http_base" in body:
        out["http_base"] = _url(body["http_base"], "http_base").rstrip("/")
        if urlsplit(out["http_base"]).query or urlsplit(out["http_base"]).fragment:
            raise _invalid("http_base", "http_base takes no query or fragment")
    if "agent_env" in body:
        out["agent_env"] = _agent_env(body["agent_env"], "http_base" in out)
    if "rewrite_origins" in body:
        if "http_base" not in out:
            raise _invalid("rewrite_origins", "rewrite_origins needs http_base")
        out["rewrite_origins"] = _origins(body["rewrite_origins"])
    if "cleanup" in body:
        if transport != "http":
            raise _invalid("cleanup", "cleanup is for http servers only")
        out["cleanup"] = _cleanup(body["cleanup"])
    return out


def _agent_env(value: Any, has_http_base: bool) -> dict[str, str]:
    """Templates for the agent's environment. A value holds only placeholders and harmless
    text: a credential-like name needs a placeholder, and no literal may look like a key."""
    if not isinstance(value, dict) or len(value) > _MAX_AGENT_ENV:
        raise _invalid("agent_env", f"agent_env must be an object of at most {_MAX_AGENT_ENV} "
                       "variables")
    out: dict[str, str] = {}
    for key, template in value.items():
        field = f"agent_env.{key}"
        if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
            raise _invalid("agent_env", "agent_env names must be environment variable names")
        if not isinstance(template, str) or len(template) > 1024 or any(c in template for c in "\x00\r\n"):
            raise _invalid(field, "agent_env values must be single-line strings")
        found = _PLACEHOLDER.findall(template)
        unknown = [p for p in found if p not in PLACEHOLDERS]
        if unknown:
            raise _invalid(field, f"unknown placeholder {unknown[0]}; use {', '.join(PLACEHOLDERS)}")
        if "{http_url}" in found and not has_http_base:
            raise _invalid(field, "{http_url} needs http_base")
        literal = _PLACEHOLDER.sub(" ", template)
        if not found and _SECRET_ENV.search(key):
            raise _invalid(field, "agent_env holds no literal secrets; this name looks like a "
                           "credential, so its value must use a placeholder")
        if _TOKENISH.search(literal) or "bearer" in literal.lower():
            raise _invalid(field, "agent_env holds no literal secrets; this value looks like a "
                           "credential")
        out[key] = template
    return out


def _origins(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_REWRITE_ORIGINS:
        raise _invalid("rewrite_origins", f"rewrite_origins must be a list of at most "
                       f"{_MAX_REWRITE_ORIGINS} origins")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 512:
            raise _invalid("rewrite_origins", "an origin is a string like https://host[:port]")
        parts = urlsplit(item.rstrip("/"))
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.path
                or parts.query or parts.fragment or parts.username or parts.password):
            raise _invalid("rewrite_origins", f"{item!r} is not an origin like https://host[:port]")
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        if origin not in out:
            out.append(origin)
    return out


def _cleanup(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(CLEANUP_KEYS):
        raise _invalid("cleanup", f"cleanup needs exactly {', '.join(CLEANUP_KEYS)}")
    for key in ("acquire_tool", "release_tool"):
        if not isinstance(value[key], str) or not _TOOL.fullmatch(value[key]):
            raise _invalid(f"cleanup.{key}", "a tool name has 1-128 characters of [A-Za-z0-9_.-]")
    for key in ("id_field", "release_arg"):
        if not isinstance(value[key], str) or not _ID_FIELD.fullmatch(value[key]):
            raise _invalid(f"cleanup.{key}", "a field name has 1-64 characters of [A-Za-z0-9_.-]")
    return {key: value[key] for key in CLEANUP_KEYS}


# -- presets (C) --------------------------------------------------------------------------
@dataclass(frozen=True)
class Preset:
    """Defaults for one kind of server, and what Symphony adds for it. A preset is not
    stored: a definition matches it when its `agent_env` has `marker`."""

    fields: Mapping[str, Any]
    marker: str
    # The admin test reads this path of `http_base` and reports its JSON object as `me`.
    identity_path: str
    # A paragraph for the attempt prompt of every role that has a matching server.
    guidance: str
    # `POST {http_base}{viewer_path}` (`{id}`: the held id) returns a viewer link.
    viewer_path: str | None = None


SEIJI_GUIDANCE = """\
Seiji device lab (MCP server `{name}`): Seiji lends you an iOS simulator or an Android emulator \
and records evidence of each run.
- Upload a build with `curl -sS -H "Authorization: Bearer $SEIJI_TOKEN" -H "X-Filename: MyApp.zip" \
--data-binary @MyApp.zip "$SEIJI_URL/v1/apps"`. Use `$SEIJI_URL` for every Seiji link.
- Always call `release_device` when you are done. A held device costs budget.
- Put the Markdown of `export_evidence` in the pull request body.
- Download `$SEIJI_URL/v1/runs/<run_id>/evidence.zip` with the same header and attach it with \
`symphony_attach_evidence` when the tracker accepts files (Linear does). GitHub issues take text \
only: attach `report.md` from `$SEIJI_URL/v1/runs/<run_id>/report.md` instead.
- Reviewers cannot open Seiji links. Attach the files they need."""

PRESETS: dict[str, Preset] = {
    "seiji": Preset(
        fields={
            "transport": "http",
            "timeout_ms": 3_900_000,
            "agent_env": {"SEIJI_TOKEN": "{token}", "SEIJI_URL": "{http_url}"},
            "cleanup": {"acquire_tool": "acquire_device", "id_field": "lease_id",
                        "release_tool": "release_device", "release_arg": "lease_id"},
        },
        marker="SEIJI_URL",
        identity_path="/v1/me",
        guidance=SEIJI_GUIDANCE,
        viewer_path="/v1/leases/{id}/viewer",
    ),
}


def apply_preset(definition: Mapping[str, Any]) -> dict:
    """The body with `preset` replaced by the preset's fields; given fields win. For `seiji`,
    `http_base` is the url without its `/mcp` path."""
    body = dict(definition)
    name = body.pop("preset", None)
    if name is None:
        return body
    preset = PRESETS.get(name) if isinstance(name, str) else None
    if preset is None:
        raise _invalid("preset", f"unknown preset; presets are {', '.join(PRESETS)}")
    for key, value in preset.fields.items():
        body.setdefault(key, copy.deepcopy(value))
    url = body.get("url")
    if "http_base" not in body and isinstance(url, str):
        parts = urlsplit(url)
        if not parts.path.rstrip("/").endswith("/mcp") or parts.query or parts.fragment:
            raise _invalid("http_base", "the url does not end with /mcp; give http_base")
        body["http_base"] = urlunsplit((parts.scheme, parts.netloc,
                                        parts.path.rstrip("/")[: -len("/mcp")], "", ""))
    return body


def preset_of(definition: Mapping[str, Any]) -> Preset | None:
    """The preset whose marker the definition's `agent_env` carries, with an `http_base`."""
    if not definition.get("http_base"):
        return None
    agent_env = definition.get("agent_env") or {}
    return next((p for p in PRESETS.values() if p.marker in agent_env), None)


def prompt_guidance(definitions: Iterable[Mapping[str, Any]]) -> str:
    """The preset paragraphs for these definitions (a role's servers), in name order."""
    parts = []
    for definition in definitions:
        preset = preset_of(definition)
        if preset is not None:
            parts.append(preset.guidance.format(name=definition.get("name", "")))
    return "\n\n".join(parts)


def render_agent_env(definition: Mapping[str, Any], token: str, gateway_url: str) -> dict[str, str]:
    """`agent_env` with its placeholders filled: only the session token and gateway URLs."""
    mcp_url = f"{gateway_url.rstrip('/')}/v1/mcp/{definition['name']}"
    values = {"{token}": token, "{mcp_url}": mcp_url, "{http_url}": f"{mcp_url}/http"}
    return {
        key: _PLACEHOLDER.sub(lambda m: values[m.group(0)], template)
        for key, template in (definition.get("agent_env") or {}).items()
    }


def _url(value: Any, field: str = "url") -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in "\x00\r\n "):
        raise _invalid(field, f"http and sse servers need a {field}")
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None:
        raise _invalid(field, f"the {field} must not hold credentials; store them as headers")
    host = (parts.hostname or "").lower()
    if not host:
        raise _invalid(field, f"the {field} needs a host")
    if parts.scheme == "https":
        return value
    if parts.scheme == "http" and host in _allowed_http_hosts():
        return value
    raise _invalid(field, f"the {field} must use https://")


def _validate_headers(changes: Any) -> dict[str, str | None]:
    if not isinstance(changes, dict) or not changes:
        raise _invalid("headers", "headers must be a non-empty object of name → value or null")
    out: dict[str, str | None] = {}
    for name, value in changes.items():
        if not isinstance(name, str) or not _HEADER.fullmatch(name) or name.lower() in _RESERVED_HEADERS:
            raise _invalid("headers", f"header {name!r} cannot be stored")
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > _MAX_HEADER_VALUE
            or any(c in value for c in "\x00\r\n")
        ):
            raise _invalid(f"headers.{name}", "a header value must be a non-empty single-line string")
        out[name] = value
    return out


def headers_key(name: str) -> str:
    return f"headers.{name}.json"


# -- storage backends --------------------------------------------------------------------
class _SecretBackend:
    """The Secret `symphony-mcp` through the Kubernetes API (`get`, `patch`)."""

    def __init__(self, core, namespace: str, name: str) -> None:
        self.core, self.namespace, self.name = core, namespace, name

    def load(self) -> tuple[dict[str, Any], str | None, bool]:
        """(documents by key, resourceVersion, exists)."""
        try:
            secret = self.core.read_namespaced_secret(
                name=self.name, namespace=self.namespace, _request_timeout=_REQUEST_TIMEOUT
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return {}, None, False
            raise McpError("mcp_unavailable", "MCP server storage is unavailable.", 503) from None
        docs = {}
        for key, encoded in (secret.data or {}).items():
            try:
                docs[key] = json.loads(base64.b64decode(encoded, validate=True))
            except (ValueError, binascii.Error, TypeError):
                raise McpError("mcp_invalid", "MCP server storage is malformed.", 503) from None
        return docs, secret.metadata.resource_version, True

    def write(self, changes: Mapping[str, Any], revision: str | None, exists: bool) -> None:
        if not exists:
            raise McpError("mcp_not_bootstrapped", "The symphony-mcp Secret does not exist. "
                           "Apply deploy/integrations.yaml.", 503)
        data = {
            key: None if value is None else base64.b64encode(_encode(value)).decode("ascii")
            for key, value in changes.items()
        }
        body = {"metadata": {"resourceVersion": revision}, "data": data}
        try:
            self.core.patch_namespaced_secret(
                name=self.name, namespace=self.namespace, body=body, _request_timeout=_REQUEST_TIMEOUT
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 409:
                raise McpError("mcp_conflict", "MCP servers changed meanwhile; retry.", 409) from None
            raise McpError("mcp_unavailable", "MCP server storage rejected the write.", 503) from None


class _FileBackend:
    """Local mode: one 0600 JSON file with the Secret's keys."""

    _lock = threading.Lock()

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)

    def load(self) -> tuple[dict[str, Any], str | None, bool]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}, None, True
        if self.path.stat().st_mode & 0o077:
            os.chmod(self.path, 0o600)
        try:
            docs = json.loads(raw)
            if not isinstance(docs, dict):
                raise ValueError
        except ValueError:
            raise McpError("mcp_invalid", f"{self.path} is not valid JSON.", 503) from None
        return docs, None, True

    def write(self, changes: Mapping[str, Any], revision: str | None, exists: bool) -> None:
        with self._lock:
            docs, _, _ = self.load()
            for key, value in changes.items():
                if value is None:
                    docs.pop(key, None)
                else:
                    docs[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(_encode(docs))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)


class _MountBackend:
    """The gateway: the Secret mounted read-only as files (one file per key)."""

    def __init__(self, directory: str | os.PathLike) -> None:
        self.directory = Path(directory)

    def load(self) -> tuple[dict[str, Any], str | None, bool]:
        docs = {}
        try:
            entries = [p for p in self.directory.iterdir() if p.name.endswith(".json") and p.is_file()]
        except FileNotFoundError:
            return {}, None, True
        for path in entries:
            try:
                docs[path.name] = json.loads(path.read_bytes())
            except (OSError, ValueError):
                raise McpError("mcp_invalid", "MCP server storage is malformed.", 503) from None
        return docs, None, True

    def write(self, changes, revision, exists) -> None:
        raise McpError("mcp_read_only", "This process cannot change MCP servers.", 503)


def _encode(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()


class McpRepository:
    """MCP server definitions (A1). `core` is a Kubernetes CoreV1Api; with `core=None`,
    `local_path` (the local `<root>/mcp.json`) or `mount_dir` (the gateway's mounted Secret)
    is the store. With none of them the store is empty and read-only."""

    def __init__(self, core, namespace: str, local_path: str | os.PathLike | None = None, *,
                 mount_dir: str | os.PathLike | None = None) -> None:
        self.namespace = namespace
        if core is not None:
            self._backend: Any = _SecretBackend(
                core, namespace, os.environ.get("SYMPHONY_SECRET_MCP", "symphony-mcp")
            )
        elif local_path is not None:
            self._backend = _FileBackend(local_path)
        elif mount_dir is not None:
            self._backend = _MountBackend(mount_dir)
        else:
            self._backend = None

    def _load(self) -> tuple[dict, dict[str, Any], str | None, bool]:
        if self._backend is None:
            return {}, {}, None, False
        docs, revision, exists = self._backend.load()
        servers = docs.get(SERVERS_KEY) or {}
        if not isinstance(servers, dict):
            raise McpError("mcp_invalid", "MCP server storage is malformed.", 503)
        return servers, docs, revision, exists

    def _write(self, changes: Mapping[str, Any], revision: str | None, exists: bool) -> None:
        if self._backend is None:
            raise McpError("mcp_unavailable", "MCP server storage is not configured.", 503)
        encoded = _encode(changes.get(SERVERS_KEY) or {})
        if len(encoded) > _MAX_DOCUMENT:
            raise McpError("mcp_full", "MCP server storage has reached its size limit.")
        self._backend.write(changes, revision, exists)

    @staticmethod
    def _public(name: str, definition: dict, docs: Mapping[str, Any]) -> dict:
        stored = docs.get(headers_key(name)) or {}
        return {"name": name, **copy.deepcopy(definition), "headers_set": sorted(stored)}

    def list(self) -> list[dict]:
        servers, docs, _, _ = self._load()
        return [self._public(name, servers[name], docs) for name in sorted(servers)]

    def get(self, name: str) -> dict | None:
        servers, docs, _, _ = self._load()
        definition = servers.get(name)
        return None if definition is None else self._public(name, definition, docs)

    def put(self, name: str, definition: Any) -> dict:
        validate_name(name)
        stored = validate(definition)
        servers, docs, revision, exists = self._load()
        if stored["transport"] == "stdio" and docs.get(headers_key(name)):
            raise _invalid("transport", "stdio servers cannot have secret headers; remove them first")
        if name not in servers and len(servers) >= _MAX_SERVERS:
            raise McpError("mcp_full", f"at most {_MAX_SERVERS} MCP servers")
        servers = {**servers, name: stored}
        self._write({SERVERS_KEY: servers}, revision, exists)
        return self._public(name, stored, docs)

    def remove(self, name: str) -> None:
        servers, docs, revision, exists = self._load()
        if name not in servers:
            raise _not_found()
        servers = {key: value for key, value in servers.items() if key != name}
        changes: dict[str, Any] = {SERVERS_KEY: servers}
        if headers_key(name) in docs:
            changes[headers_key(name)] = None
        self._write(changes, revision, exists)

    def set_headers(self, name: str, changes: Any) -> list[str]:
        """Write-only: `{header: value}` stores a value, `{header: None}` removes it. Returns
        the names of the stored headers, never a value."""
        updates = _validate_headers(changes)
        servers, docs, revision, exists = self._load()
        definition = servers.get(name)
        if definition is None:
            raise _not_found()
        if definition["transport"] == "stdio":
            raise _invalid("headers", "stdio servers cannot have secret headers")
        stored = dict(docs.get(headers_key(name)) or {})
        lowered = {key.lower(): key for key in stored}
        for header, value in updates.items():
            stored.pop(lowered.get(header.lower(), header), None)
            if value is not None:
                stored[header] = value
        if len(stored) > _MAX_HEADERS:
            raise _invalid("headers", f"at most {_MAX_HEADERS} headers per server")
        self._write({headers_key(name): stored or None}, revision, exists)
        return sorted(stored)

    def for_role(self, role: str) -> list[dict]:
        """The enabled definitions for `role`, each with its `name`."""
        servers, _, _, _ = self._load()
        return [
            {"name": name, **copy.deepcopy(servers[name])}
            for name in sorted(servers)
            if servers[name].get("enabled", True) and role in servers[name].get("roles", ())
        ]

    def headers(self, name: str) -> dict[str, str]:
        """Gateway only: the stored header values of one server."""
        _, docs, _, _ = self._load()
        stored = docs.get(headers_key(name)) or {}
        return {str(key): str(value) for key, value in stored.items()}


# -- tokens (A2) --------------------------------------------------------------------------
def token_key(namespace: str, token: str) -> str:
    return f"sym:{namespace}:mcp:token:{hashlib.sha256(token.encode()).hexdigest()}"


def mint_token(client, namespace: str, role: str, session_id: str, ttl_s: int = TOKEN_TTL_S) -> str:
    """One token per agent launch. Redis keeps only its hash; revoke it when the session ends."""
    if role not in teamconfig.ROLES:
        raise ValueError(f"unknown role {role}")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    record = {"role": role, "session": session_id, "exp": int(time.time()) + int(ttl_s)}
    client.set(token_key(namespace, token), json.dumps(record), ex=int(ttl_s))
    return token


def revoke_token(client, namespace: str, token: str) -> None:
    client.delete(token_key(namespace, token))


def token_record(client, namespace: str, token: str | None) -> dict | None:
    """The live record of `token`, or None (unknown, revoked or expired)."""
    if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
        return None
    raw = client.get(token_key(namespace, token))
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict) or not isinstance(record.get("exp"), int) or record["exp"] <= time.time():
        return None
    return record


def count_call(client, namespace: str, server: str, role: str) -> None:
    """Telemetry: MCP calls per server and role (bounded by servers × roles)."""
    client.hincrby(f"sym:{namespace}:mcp:calls", f"{server}:{role}", 1)


def call_counts(client, namespace: str) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for field, value in (client.hgetall(f"sym:{namespace}:mcp:calls") or {}).items():
        server, _, role = field.partition(":")
        counts.setdefault(server, {})[role] = int(value)
    return counts


# -- staging (A1, A6) ---------------------------------------------------------------------
def write_private_file(path: str, content: str) -> None:
    """A `write_file` for a local agent dir: owner-only and atomic."""
    pending = f"{path}.pending"
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(pending, 0o600)
    os.replace(pending, path)


def _entries(definitions: Iterable[Mapping[str, Any]], token: str, gateway_url: str
             ) -> dict[str, dict]:
    """omp server entries: remote servers through the gateway, stdio as stored."""
    staged: dict[str, dict] = {}
    for definition in definitions:
        name = definition["name"]
        if definition["transport"] in REMOTE:
            entry: dict[str, Any] = {
                "type": definition["transport"],
                "url": f"{gateway_url.rstrip('/')}/v1/mcp/{name}",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        else:
            entry = {
                "type": "stdio",
                "command": definition["command"],
                "args": list(definition.get("args", [])),
                "env": dict(definition.get("env", {})),
            }
        if "timeout_ms" in definition:
            entry["timeout"] = definition["timeout_ms"]
        if "instructions" in definition:
            entry["instructions"] = definition["instructions"]
        staged[name] = entry
    return staged


def agent_environment(definitions: Iterable[Mapping[str, Any]], token: str, gateway_url: str
                      ) -> dict[str, str]:
    """The rendered `agent_env` of these servers: the session token and gateway URLs only."""
    environment: dict[str, str] = {}
    for definition in definitions:
        if definition["transport"] in REMOTE:
            environment.update(render_agent_env(definition, token, gateway_url))
    return environment


# The engine env file sources this file of the private agent dir when it exists.
AGENT_ENV_FILE = "mcp.env"


def stage_mcp(write_file: Callable[[str, str], None], agent_dir: str, role: str, token: str,
              gateway_url: str, *, repository: McpRepository) -> list[str]:
    """Write `<agent_dir>/mcp.json` and `<agent_dir>/config.yml`, and `<agent_dir>/mcp.env`
    when a server has `agent_env`; return the staged server names. `models.yml` is left
    alone. Remote servers point at the gateway with the agent's token; stdio servers keep
    their non-secret env."""
    definitions = repository.for_role(role)
    staged = _entries(definitions, token, gateway_url)
    base = agent_dir.rstrip("/")
    write_file(f"{base}/mcp.json", json.dumps({"mcpServers": staged}, ensure_ascii=True))
    write_file(f"{base}/config.yml", AGENT_CONFIG)
    environment = agent_environment(definitions, token, gateway_url)
    if environment:
        write_file(f"{base}/{AGENT_ENV_FILE}", "".join(
            f"export {name}={shlex.quote(value)}\n" for name, value in sorted(environment.items())
        ))
    return sorted(staged)


# Local mode keeps the host's omp agent dir (its models and logins). omp 18.20.2 then takes
# the servers as a Claude-format plugin (`--plugin-dir`; tools `mcp__symphony_<server>_<tool>`)
# and the settings as a `--config` overlay, which wins over the host's own config.yml.
PLUGIN = "symphony"


def stage_overlay(directory: str | os.PathLike, role: str, token: str, gateway_url: str, *,
                  repository: McpRepository) -> list[str]:
    """Write the plugin and the settings overlay under the private `directory`; return the
    staged server names. `overlay_args(directory)` is what the engine command line adds."""
    base = Path(directory)
    (base / PLUGIN / ".claude-plugin").mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    staged = _entries(repository.for_role(role), token, gateway_url)
    write_private_file(str(base / PLUGIN / ".claude-plugin" / "plugin.json"),
                       json.dumps({"name": PLUGIN, "version": "1.0.0"}))
    write_private_file(str(base / PLUGIN / ".mcp.json"),
                       json.dumps({"mcpServers": staged}, ensure_ascii=True))
    write_private_file(str(base / "config.yml"), AGENT_CONFIG)
    return sorted(staged)


def overlay_args(directory: str | os.PathLike) -> list[str]:
    base = Path(directory)
    return ["--plugin-dir", str(base / PLUGIN), "--config", str(base / "config.yml")]


@dataclass
class McpLaunch:
    """What an agent launcher needs: mint a token, stage the role's servers, revoke later."""

    client: Any
    namespace: str
    repository: McpRepository
    gateway_url: str

    def stage(self, write_file: Callable[[str, str], None], agent_dir: str, role: str,
              session_id: str, ttl_s: int = TOKEN_TTL_S) -> str:
        """Returns the token to revoke when the agent session ends."""
        token = mint_token(self.client, self.namespace, role, session_id, ttl_s)
        try:
            stage_mcp(write_file, agent_dir, role, token, self.gateway_url, repository=self.repository)
        except BaseException:
            revoke_token(self.client, self.namespace, token)
            raise
        return token

    def stage_overlay(self, directory: str | os.PathLike, role: str, session_id: str,
                      ttl_s: int = TOKEN_TTL_S) -> str:
        """Local mode: `stage_overlay` with a fresh token; returns it, to revoke later."""
        token = mint_token(self.client, self.namespace, role, session_id, ttl_s)
        try:
            stage_overlay(directory, role, token, self.gateway_url, repository=self.repository)
        except BaseException:
            revoke_token(self.client, self.namespace, token)
            raise
        return token

    def agent_env(self, role: str, token: str) -> dict[str, str]:
        """Local mode: the variables to add to the engine's own environment."""
        return agent_environment(self.repository.for_role(role), token, self.gateway_url)

    def prompt_guidance(self, role: str) -> str:
        """The preset paragraphs for `role`'s servers; empty when none matches a preset or
        the store cannot be read (the prompt never fails on it)."""
        try:
            return prompt_guidance(self.repository.for_role(role))
        except McpError:
            return ""

    def end(self, token: str | None) -> None:
        """Revoke the token, then release what the session still holds (best effort)."""
        if not token:
            return
        revoke_token(self.client, self.namespace, token)
        try:
            release_held(self.client, self.namespace, self.repository, token_hash(token))
        except Exception as exc:  # noqa: BLE001 — the gateway's sweep tries again
            _log.warning("mcp_cleanup_failed", error=type(exc).__name__)


# -- held resources (B) -------------------------------------------------------------------
# `sym:<ns>:mcp:held:<sha256(token)>`: hash `<server>/<id>` → `{server, id, session, model?,
# platform?}`, one entry per id that `cleanup.acquire_tool` returned and the session has not
# released. `sym:<ns>:mcp:held` indexes the token hashes that hold something, for the sweep.
# `sym:<ns>:mcp:session-held:<sid>` has the same entries per session, for the session list.
HELD_TTL_S = 30 * 24 * 3600


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _held_key(namespace: str, digest: str) -> str:
    return f"sym:{namespace}:mcp:held:{digest}"


def _held_index(namespace: str) -> str:
    return f"sym:{namespace}:mcp:held"


def _session_held_key(namespace: str, sid: str) -> str:
    return f"sym:{namespace}:mcp:session-held:{sid}"


def _field(server: str, lease_id: str) -> str:
    return f"{server}/{lease_id}"


def _public_lease(entry: Mapping[str, Any]) -> dict:
    return {key: entry[key] for key in ("server", "id", "model", "platform") if key in entry}


def _emit_lease(client, namespace: str, sid: str | None, entry: Mapping[str, Any], held: bool) -> None:
    """`symphony.mcp_lease` on the session's event stream, when that session exists."""
    from .sessions import SessionStore

    if not sid:
        return
    store = SessionStore(client, namespace)
    if store.meta(sid) is None:
        return
    store.append_event(sid, EV_MCP_LEASE, {**_public_lease(entry), "held": held})


def record_held(client, namespace: str, token: str, session: str | None, server: str,
                lease_id: str, extra: Mapping[str, str] | None = None) -> None:
    digest = token_hash(token)
    entry = {"server": server, "id": lease_id, "session": session or "", "at": int(time.time()),
             **(extra or {})}
    raw = json.dumps(entry, separators=(",", ":"))
    pipe = client.pipeline()
    pipe.hset(_held_key(namespace, digest), _field(server, lease_id), raw)
    pipe.expire(_held_key(namespace, digest), HELD_TTL_S)
    pipe.sadd(_held_index(namespace), digest)
    if session:
        pipe.hset(_session_held_key(namespace, session), _field(server, lease_id), raw)
        pipe.expire(_session_held_key(namespace, session), HELD_TTL_S)
    pipe.execute()
    _emit_lease(client, namespace, session, entry, True)


def _held_entries(client, namespace: str, digest: str) -> list[dict]:
    out = []
    for raw in (client.hgetall(_held_key(namespace, digest)) or {}).values():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("server"), str) and isinstance(entry.get("id"), str):
            out.append(entry)
    return out


def forget_held(client, namespace: str, token: str, server: str, lease_id: str | None) -> bool:
    """Drop one held id after a successful release. Without an id, the session's only id on
    `server` (a server that releases "the one you hold"). True when something was dropped."""
    digest = token_hash(token)
    entries = [e for e in _held_entries(client, namespace, digest) if e["server"] == server]
    if lease_id is not None:
        entries = [e for e in entries if e["id"] == lease_id]
    if len(entries) != 1:
        return False
    _drop(client, namespace, digest, entries[0])
    return True


def _drop(client, namespace: str, digest: str, entry: Mapping[str, Any]) -> None:
    field = _field(entry["server"], entry["id"])
    pipe = client.pipeline()
    pipe.hdel(_held_key(namespace, digest), field)
    if entry.get("session"):
        pipe.hdel(_session_held_key(namespace, entry["session"]), field)
    pipe.execute()
    if not client.exists(_held_key(namespace, digest)):
        client.srem(_held_index(namespace), digest)
    _emit_lease(client, namespace, entry.get("session"), entry, False)


def session_leases(client, namespace: str, sids: Iterable[str]) -> dict[str, list[dict]]:
    """The held ids of each session, `[{server, id, model?, platform?}]`, in one round trip."""
    ids = list(sids)
    if not ids:
        return {}
    pipe = client.pipeline()
    for sid in ids:
        pipe.hgetall(_session_held_key(namespace, sid))
    out: dict[str, list[dict]] = {}
    for sid, found in zip(ids, pipe.execute(), strict=True):
        leases = []
        for field in sorted(found or {}):
            try:
                leases.append(_public_lease(json.loads(found[field])))
            except (ValueError, KeyError, TypeError):
                continue
        out[sid] = leases
    return out


def held_by_live_session(client, namespace: str, server: str, lease_id: str) -> bool:
    """True when a session whose MCP token is still live holds `lease_id` on `server`."""
    digests = sorted(client.smembers(_held_index(namespace)) or ())
    if not digests:
        return False
    pipe = client.pipeline()
    for digest in digests:
        pipe.hexists(_held_key(namespace, digest), _field(server, lease_id))
        pipe.exists(f"sym:{namespace}:mcp:token:{digest}")
    answers = pipe.execute()
    return any(answers[i] and answers[i + 1] for i in range(0, len(answers), 2))


def release_held(client, namespace: str, repository: McpRepository, digest: str, *,
                 http: httpx.Client | None = None) -> dict[str, int]:
    """Call `cleanup.release_tool` for every id the token still holds, through the server's
    own url and stored headers, 60 s each, best effort. An id whose server answered is
    forgotten; one whose server could not be reached stays for the sweep, up to
    `CLEANUP_GIVE_UP_S` after it was recorded. Logs `mcp_cleanup` with counts only."""
    entries = _held_entries(client, namespace, digest)
    counts = {"held": len(entries), "released": 0, "failed": 0, "kept": 0}
    if not entries:
        client.srem(_held_index(namespace), digest)
        return counts
    owned = http is None
    http = http or httpx.Client(timeout=httpx.Timeout(CLEANUP_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                                follow_redirects=False)
    try:
        for entry in entries:
            ok, answered = False, True
            try:
                definition = repository.get(entry["server"])
                cleanup = (definition or {}).get("cleanup")
                if definition is not None and cleanup:
                    ok = _release_one(http, definition, repository.headers(entry["server"]),
                                      cleanup, entry["id"])
            except httpx.HTTPError:
                answered = False
            except Exception as exc:  # noqa: BLE001 — best effort: the server's reaper is the backstop
                # The type only, never the message: it may echo a stored header or lease id.
                _log.warning("mcp_cleanup_release_failed", server=entry.get("server"),
                             error=type(exc).__name__)
                ok = False
            counts["released" if ok else "failed"] += 1
            fresh = time.time() - entry.get("at", 0) < CLEANUP_GIVE_UP_S
            if answered or not fresh:
                _drop(client, namespace, digest, entry)
            else:
                counts["kept"] += 1
    finally:
        if owned:
            http.close()
    if not client.exists(_held_key(namespace, digest)):
        client.srem(_held_index(namespace), digest)
    _log.info("mcp_cleanup", **counts)
    metrics.count_mcp_cleanup(client, namespace, counts["released"], counts["failed"])
    return counts


def _release_one(http: httpx.Client, definition: Mapping[str, Any], stored: Mapping[str, str],
                 cleanup: Mapping[str, str], lease_id: str) -> bool:
    """One JSON-RPC `tools/call` of the release tool, after an `initialize` (a stateful
    server needs the session it returns; a stateless one ignores it)."""
    base = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

    def post(message: dict, extra: Mapping[str, str]) -> httpx.Response:
        return http.post(definition["url"], content=json.dumps(message),
                         headers=upstream_headers({**base, **extra}, stored))

    first = post(_initialize(1), {})
    if first.status_code >= 300:
        _log.warning("mcp_cleanup_release_refused", step="initialize", status=first.status_code)
        return False
    initialized = _sync_result(first, 1) or {}
    extra = {"mcp-protocol-version": str(initialized.get("protocolVersion") or PROTOCOL_VERSION)}
    if first.headers.get("mcp-session-id"):
        extra["mcp-session-id"] = first.headers["mcp-session-id"]
    post(_INITIALIZED, extra)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": cleanup["release_tool"], "arguments": {cleanup["release_arg"]: lease_id}}}
    answer = post(call, extra)
    result = _sync_result(answer, 2) if answer.status_code < 300 else None
    if "mcp-session-id" in extra:
        with contextlib.suppress(httpx.HTTPError):
            http.delete(definition["url"], headers=upstream_headers(extra, stored))
    ok = isinstance(result, dict) and result.get("isError") is False
    if not ok:
        _log.warning("mcp_cleanup_release_refused", step="tools/call", status=answer.status_code,
                     result=None if result is None else ("error" if result.get("isError") else "unknown"))
    return ok


def _sync_result(response: httpx.Response, rpc_id: int) -> dict | None:
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if media == "text/event-stream":
        candidates = [data for _, data in _sse_messages(response.text.splitlines())]
    else:
        candidates = [response.text]
    for data in candidates:
        try:
            message = json.loads(data)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == rpc_id and isinstance(message.get("result"), dict):
            return message["result"]
    return None


def sweep_held(client, namespace: str, repository: McpRepository) -> int:
    """Release what sessions with an expired or revoked token still hold (a crash left
    them). Returns how many tokens were swept."""
    swept = 0
    for digest in sorted(client.smembers(_held_index(namespace)) or ()):
        if client.exists(f"sym:{namespace}:mcp:token:{digest}"):
            continue
        release_held(client, namespace, repository, digest)
        swept += 1
    return swept


def observe_call(request: Any, response: Any, cleanup: Mapping[str, str]
                 ) -> tuple[str, str | None, dict] | None:
    """What a successful `tools/call` did to held ids: `("acquired", id, extra)`,
    `("released", id or None, {})`, or None. `request` and `response` are JSON-RPC messages."""
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    if not isinstance(response, dict) or response.get("id") != request.get("id"):
        return None
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError") is not False:
        return None
    name = params.get("name")
    if name == cleanup["release_tool"]:
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        value = arguments.get(cleanup["release_arg"])
        return "released", value if isinstance(value, str) and value else None, {}
    if name != cleanup["acquire_tool"]:
        return None
    content = result.get("content")
    first = next((block for block in content if isinstance(block, dict) and block.get("type") == "text"),
                 None) if isinstance(content, list) else None
    try:
        payload = json.loads(first["text"]) if first else None
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    lease_id = payload.get(cleanup["id_field"])
    if not isinstance(lease_id, str) or not lease_id or len(lease_id) > 256:
        return None
    extra = {key: payload[key] for key in ("model", "platform")
             if isinstance(payload.get(key), str) and len(payload[key]) <= 128}
    return "acquired", lease_id, extra


# -- the upstream half of the proxy (A3, A4) ----------------------------------------------
def http_client() -> httpx.AsyncClient:
    """Connect within 10 s, then no read deadline: an SSE stream may stay quiet."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=CONNECT_TIMEOUT_S, pool=CONNECT_TIMEOUT_S),
        follow_redirects=False,
    )


def upstream_url(definition: Mapping[str, Any], rest: str = "", query: str = "") -> str:
    """`/v1/mcp/<name>` is the server's url; `/v1/mcp/<name>/<rest>` is the absolute path
    `/<rest>` on the same origin (the legacy SSE transport posts to such a path)."""
    url = definition["url"]
    if rest:
        parts = urlsplit(url)
        url = f"{parts.scheme}://{parts.netloc}/{rest.lstrip('/')}"
        return f"{url}?{query}" if query else url
    if query:
        return f"{url}{'&' if '?' in url else '?'}{query}"
    return url


def upstream_headers(incoming: Mapping[str, str] | None, stored: Mapping[str, str]) -> dict[str, str]:
    """Only the MCP transport headers pass; the agent's Authorization never does."""
    out = {"user-agent": "symphony-gateway", "accept-encoding": "identity"}
    for name in FORWARDED:
        value = (incoming or {}).get(name)
        if value is not None:
            out[name] = value
    out.update(stored)
    return out


def response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP}


async def open_upstream(client: httpx.AsyncClient, definition: Mapping[str, Any],
                        stored: Mapping[str, str], method: str, *, rest: str = "", query: str = "",
                        incoming: Mapping[str, str] | None = None, content=None) -> httpx.Response:
    """Send one request to the server, streaming; the caller must `aclose()` the response."""
    request = client.build_request(
        method, upstream_url(definition, rest, query),
        headers=upstream_headers(incoming, stored), content=content,
    )
    return await client.send(request, stream=True)


# The companion HTTP API (`/v1/mcp/<name>/http/<path>`) forwards these request headers too:
# the upload's file name and conditional or partial downloads.
HTTP_FORWARDED = (*FORWARDED, "x-filename", "range", "if-range", "if-none-match",
                  "if-modified-since")
# No viewer route is proxied: it takes no token, so the gateway would hand out the device.
_UNPROXIED_PREFIXES = ("view",)
_REWRITABLE = re.compile(r"application/(?:[a-z0-9.+-]*\+)?json|application/x-ndjson|text/[a-z0-9.+-]+")
_PATH_SAFE = "/:@!$&'()*+,;=-._~"


def http_api_path(rest: str) -> str | None:
    """The forwarded path below `http_base`, or None when it must not be forwarded: a dot
    segment, an empty segment, or a viewer route."""
    segments = rest.split("/")
    if not rest or any(s in ("", ".", "..") for s in segments):
        return None
    if segments[0].lower() in _UNPROXIED_PREFIXES:
        return None
    return rest


async def open_http_api(client: httpx.AsyncClient, definition: Mapping[str, Any],
                        stored: Mapping[str, str], method: str, path: str, query: str,
                        incoming: Mapping[str, str], content=None) -> httpx.Response:
    """One streamed request to `{http_base}/{path}`; the caller must `aclose()` it."""
    url = f"{definition['http_base']}/{quote(path, safe=_PATH_SAFE)}" + (f"?{query}" if query else "")
    headers = {"user-agent": "symphony-gateway", "accept-encoding": "identity"}
    for name in HTTP_FORWARDED:
        value = incoming.get(name)
        if value is not None:
            headers[name] = value
    headers.update(stored)
    request = client.build_request(method, url, headers=headers, content=content)
    return await client.send(request, stream=True)


def link_rewriter(definition: Mapping[str, Any], http_url: str) -> Callable[[str], str] | None:
    """Replace `http_base` and each `rewrite_origins` origin by the gateway's `http_url`, so
    the agent can follow every link. None when the server has no `http_base`."""
    base = definition.get("http_base")
    if not base:
        return None
    sources = sorted({base, *definition.get("rewrite_origins", ())}, key=len, reverse=True)
    pattern = re.compile("(?:" + "|".join(re.escape(s) for s in sources) + r")(?![A-Za-z0-9_:-]|\.[A-Za-z0-9])")
    target = http_url.rstrip("/")
    return lambda text: pattern.sub(lambda _m: target, text)


def rewritable(upstream: httpx.Response) -> bool:
    """A JSON, text or Markdown body without a content encoding."""
    media = upstream.headers.get("content-type", "").split(";")[0].strip().lower()
    encoding = upstream.headers.get("content-encoding", "identity").strip().lower()
    return (bool(_REWRITABLE.fullmatch(media)) and media != "text/event-stream"
            and encoding in ("", "identity"))


async def read_limited(upstream: httpx.Response, limit: int = REWRITE_LIMIT
                       ) -> tuple[bytes, AsyncIterator[bytes] | None]:
    """The whole body when it fits in `limit` (rest None), else what was read and the rest."""
    declared = upstream.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return b"", upstream.aiter_raw()
    chunks, size = [], 0
    raw = upstream.aiter_raw()
    async for chunk in raw:
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return b"".join(chunks), raw
    return b"".join(chunks), None


def rewrite_bytes(body: bytes, rewrite: Callable[[str], str]) -> bytes:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    return rewrite(text).encode("utf-8")


def rewrite_endpoint(definition: Mapping[str, Any], name: str, data: str) -> str | None:
    """Legacy SSE: the server's `endpoint` event, as a gateway path. None for another origin."""
    target = urlsplit(urljoin(definition["url"], data.strip()))
    origin = urlsplit(definition["url"])
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        return None
    path = f"/v1/mcp/{name}/{target.path.lstrip('/')}"
    return f"{path}?{target.query}" if target.query else path


async def sse_rewritten(lines: AsyncIterator[str], definition: Mapping[str, Any], name: str
                        ) -> AsyncIterator[bytes]:
    """Re-emit an SSE stream event by event, pointing `endpoint` events at the gateway."""
    event: list[str] = []
    async for line in lines:
        if line:
            event.append(line)
            continue
        yield _rewrite_event(event, definition, name)
        event = []
    if event:
        yield _rewrite_event(event, definition, name)


def _rewrite_event(lines: list[str], definition, name) -> bytes:
    if any(line.split(":", 1)[0] == "event" and line.split(":", 1)[1].strip() == "endpoint"
           for line in lines if ":" in line):
        out = []
        for line in lines:
            if line.startswith("data:"):
                path = rewrite_endpoint(definition, name, line[5:])
                if path is None:
                    return b": endpoint on another origin dropped\n\n"
                line = f"data: {path}"
            out.append(line)
        lines = out
    return ("\n".join(lines) + "\n\n").encode()


def _sse_messages(text_lines: Iterable[str]) -> list[tuple[str, str]]:
    events, kind, data = [], "message", []
    for line in [*text_lines, ""]:
        if not line:
            if data:
                events.append((kind, "\n".join(data)))
            kind, data = "message", []
        elif line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    return events


class _ProbeFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


async def _rpc_result(response: httpx.Response, rpc_id: int) -> dict:
    """The JSON-RPC result for `rpc_id` from a JSON or SSE response (closes it)."""
    try:
        if response.status_code in (401, 403):
            raise _ProbeFailed("rejected", f"the server answered HTTP {response.status_code}")
        if response.status_code >= 300:
            raise _ProbeFailed("protocol_error", f"the server answered HTTP {response.status_code}")
        media = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if media == "text/event-stream":
            lines: list[str] = []
            async for line in response.aiter_lines():
                lines.append(line)
                if not line:
                    for _, data in _sse_messages(lines):
                        found = _match(data, rpc_id)
                        if found is not None:
                            return found
                    lines = []
            for _, data in _sse_messages(lines):
                found = _match(data, rpc_id)
                if found is not None:
                    return found
            raise _ProbeFailed("protocol_error", "the stream ended without a response")
        found = _match((await response.aread()).decode("utf-8", "replace"), rpc_id)
        if found is None:
            raise _ProbeFailed("protocol_error", "the server sent no JSON-RPC response")
        return found
    finally:
        await response.aclose()


def _match(data: str, rpc_id: int) -> dict | None:
    try:
        message = json.loads(data)
    except ValueError:
        return None
    if not isinstance(message, dict) or message.get("id") != rpc_id:
        return None
    if "error" in message:
        error = message["error"] if isinstance(message["error"], dict) else {}
        raise _ProbeFailed("protocol_error", str(error.get("message", "JSON-RPC error"))[:200])
    result = message.get("result")
    if not isinstance(result, dict):
        raise _ProbeFailed("protocol_error", "the response has no result object")
    return result


def _initialize(rpc_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
        "clientInfo": {"name": "symphony-admin-test", "version": "1.0.0"},
    }}


_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def _tools_list(rpc_id: int, cursor: str | None) -> dict:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/list"}
    if cursor:
        message["params"] = {"cursor": cursor}
    return message


async def _probe_streamable(client, definition, stored) -> dict:
    accept = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

    async def post(message: dict, extra: dict) -> httpx.Response:
        return await open_upstream(client, definition, stored, "POST",
                                   incoming={**accept, **extra}, content=json.dumps(message))

    response = await post(_initialize(1), {})
    session = response.headers.get("mcp-session-id")
    result = await _rpc_result(response, 1)
    version = result.get("protocolVersion")
    extra = {"mcp-protocol-version": str(version)} if version else {}
    if session:
        extra["mcp-session-id"] = session
    response = await post(_INITIALIZED, extra)
    status = response.status_code
    await response.aclose()
    if status in (401, 403):
        raise _ProbeFailed("rejected", f"the server answered HTTP {status}")
    if status >= 300:
        raise _ProbeFailed("protocol_error", f"notifications/initialized got HTTP {status}")
    tools, cursor = [], None
    for page in range(10):
        listed = await _rpc_result(await post(_tools_list(2 + page, cursor), extra), 2 + page)
        tools += [t.get("name") for t in listed.get("tools", []) if isinstance(t, dict)]
        cursor = listed.get("nextCursor")
        if not cursor:
            break
    if session:
        try:
            ended = await open_upstream(client, definition, stored, "DELETE", incoming=extra)
            await ended.aclose()
        except httpx.HTTPError:
            pass
    return {"tools": tools, "protocol_version": version}


async def _probe_sse(client, definition, stored) -> dict:
    stream = await open_upstream(client, definition, stored, "GET",
                                 incoming={"accept": "text/event-stream"})
    try:
        if stream.status_code in (401, 403):
            raise _ProbeFailed("rejected", f"the server answered HTTP {stream.status_code}")
        if stream.status_code >= 300:
            raise _ProbeFailed("protocol_error", f"the server answered HTTP {stream.status_code}")
        lines = stream.aiter_lines()

        async def next_event() -> tuple[str, str]:
            buffer: list[str] = []
            async for line in lines:
                if line:
                    buffer.append(line)
                    continue
                events = _sse_messages(buffer)
                buffer = []
                if events:
                    return events[0]
            raise _ProbeFailed("protocol_error", "the stream ended early")

        kind, data = await next_event()
        if kind != "endpoint":
            raise _ProbeFailed("protocol_error", "the stream did not start with an endpoint event")
        target = urlsplit(urljoin(definition["url"], data.strip()))
        rest = target.path.lstrip("/")

        async def call(message: dict) -> dict | None:
            sent = await open_upstream(client, definition, stored, "POST", rest=rest,
                                       query=target.query, content=json.dumps(message),
                                       incoming={"content-type": "application/json"})
            status = sent.status_code
            await sent.aclose()
            if status in (401, 403):
                raise _ProbeFailed("rejected", f"the server answered HTTP {status}")
            if status >= 300:
                raise _ProbeFailed("protocol_error", f"the server answered HTTP {status}")
            if "id" not in message:
                return None
            while True:
                _, payload = await next_event()
                found = _match(payload, message["id"])
                if found is not None:
                    return found

        result = await call(_initialize(1)) or {}
        await call(_INITIALIZED)
        listed = await call(_tools_list(2, None)) or {}
        tools = [t.get("name") for t in listed.get("tools", []) if isinstance(t, dict)]
        return {"tools": tools, "protocol_version": result.get("protocolVersion")}
    finally:
        await stream.aclose()


async def probe(definition: Mapping[str, Any], stored: Mapping[str, str], *,
                client: httpx.AsyncClient | None = None, timeout_s: float = PROBE_TIMEOUT_S) -> dict:
    """The Admin Test: `initialize`, `notifications/initialized`, then `tools/list`, through
    the proxy's own upstream code. A server that matches a preset with an `identity_path`
    also gets `GET {http_base}{identity_path}`, reported as `me`. Error messages never carry
    a header value."""
    import asyncio

    if definition["transport"] == "stdio":
        return {"ok": None, "tools": [], "protocol_version": None,
                "note": "stdio servers are tested when an agent starts"}
    owned = client is None
    client = client or http_client()
    try:
        async with asyncio.timeout(timeout_s):
            if definition["transport"] == "sse":
                found = await _probe_sse(client, definition, stored)
            else:
                found = await _probe_streamable(client, definition, stored)
            preset = preset_of(definition)
            if preset is not None:
                found["me"] = await _identity(client, definition, stored, preset.identity_path)
        return {"ok": True, **found}
    except _ProbeFailed as exc:
        return _failed(exc.code, exc.message)
    except (TimeoutError, httpx.TimeoutException):
        return _failed("unreachable", "the server did not answer in time")
    except httpx.HTTPError as exc:
        return _failed("unreachable", f"the server could not be reached ({type(exc).__name__})")
    finally:
        if owned:
            await client.aclose()


async def _identity(client: httpx.AsyncClient, definition: Mapping[str, Any],
                    stored: Mapping[str, str], path: str) -> dict:
    response = await open_http_api(client, definition, stored, "GET", path.lstrip("/"), "",
                                   {"accept": "application/json"})
    try:
        if response.status_code in (401, 403):
            raise _ProbeFailed("rejected", f"{path} answered HTTP {response.status_code}")
        if response.status_code >= 300:
            raise _ProbeFailed("protocol_error", f"{path} answered HTTP {response.status_code}")
        try:
            body = json.loads(await response.aread())
        except ValueError:
            raise _ProbeFailed("protocol_error", f"{path} did not answer JSON") from None
        if not isinstance(body, dict):
            raise _ProbeFailed("protocol_error", f"{path} did not answer a JSON object")
        return {key: body[key] for key in ("user", "quota") if key in body}
    finally:
        await response.aclose()


def _failed(code: str, message: str) -> dict:
    return {"ok": False, "tools": [], "protocol_version": None,
            "error": {"code": code, "message": message}}


_LEASE_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


async def request_viewer(definition: Mapping[str, Any], stored: Mapping[str, str], lease_id: str,
                         control: bool, *, client: httpx.AsyncClient | None = None) -> dict:
    """`{url, expires_in, control}` from the preset's viewer route, with the stored headers.
    A relative url becomes absolute against `http_base`. Raises McpError."""
    preset = preset_of(definition)
    if preset is None or preset.viewer_path is None:
        raise McpError("viewer_unsupported", "this MCP server has no viewer", 404)
    if not _LEASE_ID.fullmatch(lease_id or ""):
        raise _invalid("lease_id", "lease_id has 1-128 characters of [A-Za-z0-9_.-]")
    owned = client is None
    client = client or http_client()
    path = preset.viewer_path.format(id=lease_id).lstrip("/")
    try:
        response = await open_http_api(
            client, definition, stored, "POST", path, "",
            {"accept": "application/json", "content-type": "application/json"},
            content=json.dumps({"control": bool(control)}),
        )
        try:
            raw = await response.aread()
        finally:
            await response.aclose()
    except httpx.HTTPError:
        raise McpError("mcp_unreachable", "the MCP server could not be reached", 502) from None
    finally:
        if owned:
            await client.aclose()
    if response.status_code in (401, 403):
        raise McpError("rejected", "the MCP server refused the stored credential", 502)
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if response.status_code >= 300:
        detail = body.get("error") if isinstance(body, dict) else None
        message = detail if isinstance(detail, str) else f"the server answered HTTP {response.status_code}"
        raise McpError("viewer_refused", message[:500], 409)
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        raise McpError("protocol_error", "the viewer answer has no url", 502)
    url = body["url"]
    if url.startswith("/"):
        url = definition["http_base"] + url
    expires = body.get("expires_in")
    return {"url": url, "expires_in": expires if isinstance(expires, int) else None,
            "control": bool(body.get("control", control))}
