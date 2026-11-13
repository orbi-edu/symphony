"""Per-attempt OMP provider configuration and credential staging on the trusted host.

Every model is reached by API key (internals §13): a key the team saved for one of omp's
built-in providers, a provider of the team's own, or — with neither — the host-provisioned
`model-provider` Secret's environment (e.g. Bedrock). Nothing staged here can mint or
refresh a credential; the worker only ever sees the key itself, in a private file.
"""

from __future__ import annotations

import base64
import json
import re
import shlex

from . import kube
from .mcp_servers import AGENT_ENV_FILE
from .providers import ProviderRepository

AGENT_DIR = "/run/symphony/engine"
ENV_FILE = "/run/symphony/engine.env"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HOST_ONLY = frozenset(
    {
        "LINEAR_API_KEY",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_ID",
        "GITHUB_APP_INSTALLATION_ID",
    }
)


class ProviderRuntimeError(Exception):
    """A safe failure which must stop the attempt before the engine starts."""


def _host_environment(core, namespace: str) -> dict[str, str]:
    """The explicitly host-provisioned provider environment, without pod envFrom snapshots."""
    try:
        secret = core.read_namespaced_secret(
            kube.SECRET_PROVIDER, namespace, _request_timeout=(3, 10)
        )
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return {}
        raise ProviderRuntimeError("Host-managed model credentials could not be read.") from None
    data = secret.data or {}
    if len(data) > 100 or sum(len(value) for value in data.values()) > 256 * 1024:
        raise ProviderRuntimeError("Host-managed model credentials exceed the supported bounds.")
    environment = {}
    try:
        for name, encoded in data.items():
            # Non-environment Secret entries are not credentials for the engine.
            if not _ENV_NAME.fullmatch(name):
                continue
            if name in _HOST_ONLY or name.startswith("SYMPHONY_ADMIN_"):
                raise ProviderRuntimeError(
                    "The model credential Secret contains host-only credentials."
                )
            value = base64.b64decode(encoded, validate=True).decode("utf-8")
            if "\x00" in value:
                raise ValueError("Invalid environment value")
            environment[name] = value
    except ProviderRuntimeError:
        raise
    except (ValueError, UnicodeError, TypeError):
        raise ProviderRuntimeError("Host-managed model credentials are malformed.") from None
    return environment


def prepare_provider(sandbox, core, namespace: str, cfg, stage_mcp=None) -> None:
    """Stage the attempt's private provider configuration for `cfg.engine_provider/model`.

    The managed providers Secret is read on every call, so a key saved in the Admin screen
    reaches the next attempt. A built-in provider key rides on top of the host environment;
    a custom provider carries only its own key; with no managed item, the host environment
    alone applies.

    `stage_mcp(write_file, agent_dir)` (an `mcp_servers.McpLaunch.stage` partial) then writes
    the role's `mcp.json` and `config.yml` into the same private agent dir.
    """
    sandbox.environment_file = None
    # This reserved, ephemeral directory belongs to the launcher. Wipe the previous
    # config before lookup, so even a failed switch leaves no reusable old key.
    directory = shlex.quote(AGENT_DIR)
    parent = shlex.quote(AGENT_DIR.rsplit("/", 1)[0])
    code, _ = sandbox.exec_script(
        f"umask 077; rm -rf -- {directory} && mkdir -p {directory} && "
        f"chmod 700 {parent} {directory} && rm -f -- {shlex.quote(ENV_FILE)}",
        timeout_s=30,
    )
    if code != 0:
        raise ProviderRuntimeError("The private engine directory could not be prepared.")
    selected = ProviderRepository(core, namespace).runtime(cfg.engine_provider, cfg.engine_model)
    if selected is None:
        environment, models = _host_environment(core, namespace), {"providers": {}}
    elif selected.builtin:
        environment = {**_host_environment(core, namespace), **selected.environment}
        models = selected.models_config
    else:
        environment, models = dict(selected.environment), selected.models_config
    environment["PI_CODING_AGENT_DIR"] = AGENT_DIR
    contents = "".join(
        f"export {name}={shlex.quote(value)}\n" for name, value in sorted(environment.items())
    )
    # `stage_mcp` writes the MCP servers' `agent_env` here; the dir is wiped above, so an
    # earlier attempt's token never survives.
    mcp_env = shlex.quote(f"{AGENT_DIR}/{AGENT_ENV_FILE}")
    contents += f"if [ -f {mcp_env} ]; then . {mcp_env}; fi\n"
    for path, content in (
        (f"{AGENT_DIR}/models.yml", json.dumps(models, ensure_ascii=True)),
        (ENV_FILE, contents),
    ):
        code, _ = sandbox.write_private_file(path, content)
        if code != 0:
            raise ProviderRuntimeError("The provider configuration could not be staged privately.")
    if stage_mcp is not None:

        def write_file(path: str, content: str) -> None:
            code, _ = sandbox.write_private_file(path, content)
            if code != 0:
                raise ProviderRuntimeError("The MCP configuration could not be staged privately.")

        try:
            stage_mcp(write_file, AGENT_DIR)
        except ProviderRuntimeError:
            raise
        except Exception:
            raise ProviderRuntimeError("The MCP servers could not be read.") from None
    sandbox.environment_file = ENV_FILE
