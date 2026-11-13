"""Actual shell/file behavior at the worker's credential boundary (internals §13).

Every model is reached by API key: a built-in omp provider's key (an override entry plus the
host env), a custom provider (its own definition and key), or nothing managed (the host
`model-provider` env alone). The managed Secret is read per attempt.
"""

import base64
import copy
import json
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
from kubernetes import client

from symphony import kube, provider_runtime, workspace
from symphony.config import Config
from symphony.providers import ProviderError

KEY = "dummy-provider-credential-not-a-real-key"
OTHER_KEY = "dummy-other-provider-credential-not-a-real-key"
BUILTIN_KEY = "dummy-anthropic-key-not-real"


def definition(**changes):
    return {
        "name": "Managed provider",
        "api": "anthropic-messages",
        "baseUrl": "https://api.anthropic.com",
        "auth": "apiKey",
        "models": [{"id": "model-one"}],
        "apiKey": KEY,
    } | changes


def _encoded(providers):
    return base64.b64encode(
        json.dumps({"schemaVersion": 1, "providers": providers}).encode()
    ).decode()


class ShellSandbox(kube.PodSandbox):
    """Execute the real private-file writer and exports without a Kubernetes transport."""

    def __init__(self):
        super().__init__(None, "symphony", "shell")
        self.commands = []

    def exec_script(self, script, timeout_s=None, *, stdin=None):
        command = ["bash", "-c", self._exports() + script]
        self.commands.append(command)
        result = subprocess.run(
            command, input=stdin, text=True, capture_output=True, env={}, timeout=timeout_s or 10
        )
        return result.returncode, result.stdout + result.stderr


class Core:
    """The two Secrets a worker reads: the managed providers and the host `model-provider`."""

    def __init__(self, providers=None, host=None):
        self.providers = client.V1Secret(
            metadata=client.V1ObjectMeta(name="symphony-providers", resource_version="1"),
            data={"providers.json": _encoded(providers or {})},
        )
        self.host = client.V1Secret(
            data={name: base64.b64encode(v.encode()).decode() for name, v in (host or {}).items()}
        )
        self.reads = 0

    def save(self, providers):
        """What an Admin save does to the Secret between two attempts."""
        self.providers.data["providers.json"] = _encoded(providers)
        self.providers.metadata.resource_version = str(int(self.providers.metadata.resource_version) + 1)

    def read_namespaced_secret(self, name=None, namespace=None, **kwargs):
        if name == kube.SECRET_PROVIDER:
            return self.host
        self.reads += 1
        return copy.deepcopy(self.providers)


@pytest.fixture
def private_files(tmp_path, monkeypatch):
    directory = tmp_path / "private" / "engine"
    environment = directory.parent / "engine.env"
    monkeypatch.setattr(provider_runtime, "AGENT_DIR", str(directory))
    monkeypatch.setattr(provider_runtime, "ENV_FILE", str(environment))
    return directory, environment


def prepare(sandbox, core, provider="custom", model="model-one"):
    provider_runtime.prepare_provider(
        sandbox,
        core,
        "symphony",
        Config(tracker_kind="memory", engine_provider=provider, engine_model=model),
    )


def staged(sandbox, name):
    code, output = sandbox.exec_script(f"printf '%s' \"${{{name}-unset}}\"")
    assert code == 0
    return output


# -- built-in provider keys ----------------------------------------------------------
def test_a_builtin_key_is_staged_as_an_override_on_top_of_the_host_env(private_files):
    directory, environment = private_files
    core = Core(
        {"anthropic": {"builtin": True, "apiKey": BUILTIN_KEY}, "custom": definition(apiKey=OTHER_KEY)},
        host={"AWS_REGION": "us-east-1"},
    )
    sandbox = ShellSandbox()
    prepare(sandbox, core, "anthropic", "claude-sonnet-4-5")

    assert json.loads((directory / "models.yml").read_text()) == {
        "providers": {"anthropic": {"apiKey": "SYMPHONY_PROVIDER_API_KEY"}}
    }
    assert staged(sandbox, "SYMPHONY_PROVIDER_API_KEY") == BUILTIN_KEY
    assert staged(sandbox, "AWS_REGION") == "us-east-1"  # the host env still applies
    assert BUILTIN_KEY not in (directory / "models.yml").read_text()
    assert OTHER_KEY not in environment.read_text()
    assert BUILTIN_KEY not in json.dumps(sandbox.commands)  # never in argv
    assert stat.S_IMODE(environment.stat().st_mode) == 0o600


def test_a_key_saved_after_one_attempt_is_used_by_the_next(private_files):
    """The Secret is read per attempt: an Admin save needs no restart."""
    core = Core({"anthropic": {"builtin": True, "apiKey": "first-key"}})
    sandbox = ShellSandbox()
    prepare(sandbox, core, "anthropic", "claude-sonnet-4-5")
    assert staged(sandbox, "SYMPHONY_PROVIDER_API_KEY") == "first-key"
    core.save({"anthropic": {"builtin": True, "apiKey": "rotated-key"}})
    prepare(sandbox, core, "anthropic", "claude-sonnet-4-5")
    assert staged(sandbox, "SYMPHONY_PROVIDER_API_KEY") == "rotated-key"
    assert core.reads == 2


def test_without_a_managed_item_only_the_host_env_applies(private_files):
    directory, environment = private_files
    core = Core(
        {"anthropic": {"builtin": True, "apiKey": BUILTIN_KEY}},
        host={"AWS_BEARER_TOKEN_BEDROCK": "host-bedrock", "AWS_REGION": "us-east-1"},
    )
    sandbox = ShellSandbox()
    prepare(sandbox, core, "amazon-bedrock", "global.anthropic.claude-opus-5-5")
    assert json.loads((directory / "models.yml").read_text()) == {"providers": {}}
    assert staged(sandbox, "AWS_BEARER_TOKEN_BEDROCK") == "host-bedrock"
    assert staged(sandbox, "SYMPHONY_PROVIDER_API_KEY") == "unset"
    assert BUILTIN_KEY not in environment.read_text()


def test_host_only_credentials_in_the_model_secret_stop_the_attempt(private_files):
    sandbox = ShellSandbox()
    with pytest.raises(provider_runtime.ProviderRuntimeError, match="host-only"):
        prepare(sandbox, Core(host={"LINEAR_API_KEY": "lin"}), "amazon-bedrock")
    assert sandbox.environment_file is None


def test_a_builtin_item_reads_back_without_its_key_and_ignores_the_model():
    from symphony.providers import ProviderRepository

    repository = ProviderRepository(Core({"anthropic": {"builtin": True, "apiKey": BUILTIN_KEY}}), "symphony")
    assert repository.read()["items"] == [
        {"builtin": True, "id": "anthropic", "credentialConfigured": True}
    ]
    runtime = repository.runtime("anthropic", "any-catalog-model")  # never provider_model_missing
    assert runtime.builtin and runtime.environment == {"SYMPHONY_PROVIDER_API_KEY": BUILTIN_KEY}
    assert BUILTIN_KEY not in repr(runtime)


@pytest.mark.parametrize(
    "stored",
    [
        {"builtin": True},  # a stored built-in item IS its key
        {"builtin": True, "apiKey": BUILTIN_KEY, "baseUrl": "https://evil.example"},
        {"builtin": False, "apiKey": BUILTIN_KEY},
        definition(api="openai-codex-responses"),  # the subscription protocol is gone
        definition(auth="oauth"),
    ],
)
def test_malformed_or_retired_stored_items_fail_closed(stored):
    from symphony.providers import ProviderRepository

    with pytest.raises(ProviderError) as exc:
        ProviderRepository(Core({"anthropic": stored}), "symphony").runtime("anthropic", "model-one")
    assert exc.value.code == "providers_invalid"
    assert BUILTIN_KEY not in str(exc.value)


# -- custom providers ------------------------------------------------------------------
def test_selected_key_is_private_shell_safe_and_absent_from_process_arguments(
    private_files, tmp_path
):
    directory, environment = private_files
    injected = tmp_path / "injected"
    key = f"é'; $(touch {injected}); '"
    core = Core({"custom": definition(apiKey=key), "other": definition(apiKey=OTHER_KEY)})
    sandbox = ShellSandbox()
    prepare(sandbox, core)
    assert staged(sandbox, "SYMPHONY_PROVIDER_API_KEY") == key
    assert not injected.exists()
    assert key not in json.dumps(sandbox.commands)
    assert OTHER_KEY not in environment.read_text()
    assert key not in (directory / "models.yml").read_text()
    assert stat.S_IMODE(environment.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_custom_provider_carries_only_its_own_definition_and_key(private_files):
    directory, _ = private_files
    core = Core({"custom": definition()}, host={"AWS_BEARER_TOKEN_BEDROCK": "host-bedrock"})
    sandbox = ShellSandbox()
    prepare(sandbox, core)
    models = json.loads((directory / "models.yml").read_text())
    assert models == {"providers": {"custom": {
        "api": "anthropic-messages", "baseUrl": "https://api.anthropic.com", "auth": "apiKey",
        "models": [{"id": "model-one"}], "apiKey": "SYMPHONY_PROVIDER_API_KEY",
    }}}
    assert staged(sandbox, "AWS_BEARER_TOKEN_BEDROCK") == "unset"


def test_switch_to_keyless_provider_removes_the_previous_key_and_vault(private_files):
    directory, environment = private_files
    keyless = definition(api="openai-completions", auth="none")
    keyless.pop("apiKey")
    core = Core({"custom": definition(), "keyless": keyless})
    sandbox = ShellSandbox()
    prepare(sandbox, core)
    (directory / "agent.db").write_text("stale credential vault")
    prepare(sandbox, core, "keyless")
    assert staged(sandbox, "SYMPHONY_PROVIDER_API_KEY") == "unset"
    assert not (directory / "agent.db").exists()
    assert "SYMPHONY_PROVIDER_API_KEY" not in environment.read_text()


def test_a_custom_provider_without_the_selected_model_stops_the_attempt(private_files):
    with pytest.raises(ProviderError, match="not configured"):
        prepare(ShellSandbox(), Core({"custom": definition()}), "custom", "other-model")


# -- failure discipline -------------------------------------------------------------------
def test_failed_lookup_cannot_leave_an_old_credential_available(private_files):
    _, environment = private_files
    sandbox = ShellSandbox()
    core = Core({"custom": definition()})
    prepare(sandbox, core)
    core.providers.data["providers.json"] = "invalid-base64"
    with pytest.raises(ProviderError):
        prepare(sandbox, core)
    assert not environment.exists()
    assert sandbox.environment_file is None


def test_failed_private_write_never_activates_the_environment(private_files, monkeypatch):
    _, environment = private_files
    sandbox = ShellSandbox()
    writer = sandbox.write_private_file

    def block_environment(path, content):
        if path == str(environment):
            Path(f"{path}.pending").mkdir()
        return writer(path, content)

    monkeypatch.setattr(sandbox, "write_private_file", block_environment)
    with pytest.raises(provider_runtime.ProviderRuntimeError):
        prepare(sandbox, Core({"custom": definition()}))
    assert sandbox.environment_file is None
    assert not environment.exists()


def test_missing_staged_environment_stops_instead_of_using_ambient_auth(private_files):
    _, environment = private_files
    sandbox = ShellSandbox()
    prepare(sandbox, Core({"custom": definition()}))
    environment.unlink()
    marker = environment.parent / "engine-started"
    code, _ = sandbox.exec_script(f"touch {shlex.quote(str(marker))}")
    assert code != 0 and not marker.exists()


def test_attempt_repository_wins_over_the_provider_environment_in_clone_hooks(
    private_files, tmp_path
):
    sandbox = ShellSandbox()
    sandbox.env["SYMPHONY_TARGET_REPO"] = "org/current"
    core = Core(host={"SYMPHONY_TARGET_REPO": "wrong/repository"})
    prepare(sandbox, core, "openai")
    checkout = tmp_path / "checkout"
    destination = shlex.quote(str(checkout))
    cfg = Config(
        tracker_kind="memory",
        hooks={
            "after_create": f"git init -q {destination} && git -C {destination} "
            'remote add origin "https://github.com/$SYMPHONY_TARGET_REPO.git"'
        },
    )
    workspace.run_hook(sandbox, "after_create", cfg)
    remote = subprocess.check_output(
        ["git", "-C", str(checkout), "remote", "get-url", "origin"], text=True
    ).strip()
    assert remote == "https://github.com/org/current.git"
