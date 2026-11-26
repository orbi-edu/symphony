"""deploy/, docker/ and scripts/ cross-checked against the code and internals §9.

A manifest that drifts from the code fails silently in a cluster (a pod waiting on a
missing Secret key, a Service on the wrong port, a Job on an image the daemon does not
run); these tests make the drift fail here instead.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from symphony import edge, kube
from symphony.config import parse_workflow

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
DOCKER = ROOT / "docker"
ORCHESTRATOR_IMAGE = "symphony-orchestrator"

# internals §9: which Secret carries each variable, and which workloads must see it.
SECRET_OF = {
    "LINEAR_API_KEY": "linear-api-key",
    "LINEAR_WEBHOOK_SECRET": "linear-api-key",
    "GITHUB_APP_ID": "github-app",
    "GITHUB_APP_PRIVATE_KEY": "github-app",
    "GITHUB_APP_INSTALLATION_ID": "github-app",
    "GITHUB_WEBHOOK_SECRET": "symphony-api",
    "SYMPHONY_BOOTSTRAP_TOKEN": "symphony-api",
    "SLACK_BOT_TOKEN": "symphony-slack",
    "SLACK_CHANNEL": "symphony-slack",
    "SLACK_WEBHOOK_URL": "symphony-slack",
    "SLACK_APP_TOKEN": "symphony-slack",
    "DISCORD_BOT_TOKEN": "symphony-discord",
    "DISCORD_CHANNEL_ID": "symphony-discord",
    "DISCORD_WEBHOOK_URL": "symphony-discord",
    "AWS_REGION": "model-provider",
    "AWS_BEARER_TOKEN_BEDROCK": "model-provider",
    "SYMPHONY_BACKUP_ACCESS_KEY_ID": "symphony-backup",
    "SYMPHONY_BACKUP_SECRET_ACCESS_KEY": "symphony-backup",
}
CONSUMERS = {
    "linear-api-key": {"symphony-api", "symphony-daemon", "symphony-merger", "symphony-nightly-sweep", "symphony-assistant"},
    "github-app": {"symphony-api", "symphony-daemon", "symphony-merger", "symphony-nightly-sweep", "symphony-assistant"},
    "symphony-api": {"symphony-api"},
    "symphony-slack": {"symphony-api", "symphony-daemon", "symphony-merger", "symphony-nightly-sweep", "symphony-assistant"},
    "symphony-discord": {"symphony-api", "symphony-daemon", "symphony-merger", "symphony-nightly-sweep", "symphony-assistant"},
    "model-provider": {"symphony-merger", "symphony-assistant"},
    "symphony-backup": {"symphony-api", "symphony-backup"},
}
# First-run setup: every integration is optional so pods start before setup configured it;
# the API's own Secret (bootstrap token) is the one Secret a minimal install needs.
OPTIONAL_SECRETS = set(CONSUMERS) - {"symphony-api"}
# The integration Secrets setup writes; they ship empty in deploy/integrations.yaml.
INTEGRATION_SECRETS = {"linear-api-key", "github-app", "symphony-slack", "symphony-discord", "symphony-mcp"}
# Workloads with no credential in their environment and no ServiceAccount token. The gateway's
# one credential is the `symphony-mcp` volume (test_gateway_mounts_only_the_mcp_secret_read_only).
CREDENTIAL_FREE = {"symphony-gateway"}


def _documents() -> list[dict]:
    kustomization = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text())
    docs: list[dict] = []
    for resource in kustomization["resources"]:
        docs.extend(d for d in yaml.safe_load_all((DEPLOY / resource).read_text()) if d)
    return docs


DOCS = _documents()


def _by_kind(kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in DOCS if d["kind"] == kind}


def _pod_spec(doc: dict) -> dict:
    if doc["kind"] == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return doc["spec"]["template"]["spec"]


def _pod_labels(doc: dict) -> dict:
    if doc["kind"] == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    return doc["spec"]["template"]["metadata"]["labels"]


WORKLOADS = {**_by_kind("Deployment"), **_by_kind("CronJob")}
SYMPHONY_WORKLOADS = {
    name: doc for name, doc in WORKLOADS.items() if name != "symphony-redis"
}


def _container(doc: dict) -> dict:
    [container] = _pod_spec(doc)["containers"]
    return container


def _secret_refs(container: dict) -> dict[str, bool]:
    return {
        ref["secretRef"]["name"]: bool(ref["secretRef"].get("optional"))
        for ref in container.get("envFrom", [])
        if "secretRef" in ref
    }


def _env(container: dict) -> dict:
    return {e["name"]: e for e in container.get("env", [])}


def _contract_section() -> str:
    text = (ROOT / "docs" / "internals.md").read_text(encoding="utf-8")
    return text.split("## 9.", 1)[1].split("\n## ", 1)[0]


def _contract_env_vars() -> set[str]:
    """The credential/config variables of the `| Variable | Use |` table of internals §9."""
    table = _contract_section().split("| Variable |", 1)[1].split("\n\n", 1)[0]
    return {token for token in re.findall(r"`([^`]+)`", table) if re.fullmatch(r"[A-Z][A-Z0-9_]+", token)}


def _contract_merger_env() -> set[str]:
    paragraph = _contract_section().split("**Merger:**", 1)[1].split("\n\n", 1)[0]
    return {token for token in re.findall(r"`([^`]+)`", paragraph) if re.fullmatch(r"SYMPHONY_[A-Z0-9_]+", token)}


def _secret_template() -> dict[str, dict[str, str]]:
    """Both templates: secrets.example.yaml (created by the operator) and
    integrations.example.yaml (filled by setup, or by hand with `kubectl replace`)."""
    out = {}
    for name in ("secrets.example.yaml", "integrations.example.yaml"):
        for doc in yaml.safe_load_all((DEPLOY / name).read_text()):
            if doc and doc["kind"] == "Secret":
                out[doc["metadata"]["name"]] = doc["stringData"]
    return out


# --------------------------------------------------------------------------- env/secrets


def test_contract_env_vars_are_all_mapped():
    contract = _contract_env_vars()
    assert "REDIS_URL" in contract and "SYMPHONY_BOOTSTRAP_TOKEN" in contract
    assert contract - {"REDIS_URL"} == set(SECRET_OF)


def test_secret_template_carries_every_contract_variable():
    template = _secret_template()
    # generated by setup and shown once: documented, never a placeholder value an operator
    # could leave behind (a weak webhook secret that looks configured)
    generated = {"GITHUB_WEBHOOK_SECRET"}
    text = (DEPLOY / "secrets.example.yaml").read_text()
    for variable, secret in SECRET_OF.items():
        if variable in generated:
            assert variable not in template[secret] and f"# {variable}:" in text, variable
        else:
            assert variable in template.get(secret, {}), f"{variable} missing from Secret {secret}"


def test_secrets_reach_exactly_the_right_workloads():
    for secret, consumers in CONSUMERS.items():
        holders = {
            name for name, doc in SYMPHONY_WORKLOADS.items() if secret in _secret_refs(_container(doc))
        }
        assert holders == consumers, f"Secret {secret}"
        for name in holders:
            optional = _secret_refs(_container(SYMPHONY_WORKLOADS[name]))[secret]
            assert optional == (secret in OPTIONAL_SECRETS), f"{name} optional flag for {secret}"


def test_credential_free_workloads_hold_nothing():
    for name in CREDENTIAL_FREE:
        container = _container(WORKLOADS[name])
        assert _secret_refs(container) == {}
        assert not any("valueFrom" in e and "secretKeyRef" in e["valueFrom"] for e in container.get("env", []))
        assert _pod_spec(WORKLOADS[name])["automountServiceAccountToken"] is False


def test_gateway_mounts_only_the_mcp_secret_read_only():
    """A3: the gateway adds the stored MCP headers itself, so it reads `symphony-mcp`, as a
    read-only volume at SYMPHONY_MCP_DIR, and no other Secret."""
    spec = _pod_spec(WORKLOADS["symphony-gateway"])
    secrets = [v for v in spec["volumes"] if "secret" in v]
    assert [v["secret"]["secretName"] for v in secrets] == ["symphony-mcp"]
    assert secrets[0]["secret"]["optional"] is True
    [mount] = [m for m in _container(WORKLOADS["symphony-gateway"])["volumeMounts"] if m["name"] == secrets[0]["name"]]
    assert mount["readOnly"] is True
    assert _env(_container(WORKLOADS["symphony-gateway"]))["SYMPHONY_MCP_DIR"]["value"] == mount["mountPath"]


def test_every_symphony_workload_reaches_redis_by_its_service():
    service = _by_kind("Service")["symphony-redis"]
    port = service["spec"]["ports"][0]["port"]
    for name, doc in SYMPHONY_WORKLOADS.items():
        url = _env(_container(doc))["REDIS_URL"]["value"]
        assert url.startswith(f"redis://symphony-redis:{port}/"), name


def test_runner_jobs_get_the_host_secrets_and_notifiers(monkeypatch):
    """The daemon builds runner/reflector Jobs in code; they must carry the same Secrets."""
    monkeypatch.setenv("SYMPHONY_ORCHESTRATOR_IMAGE", "symphony-orchestrator:test")
    cfg = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    captured = {}

    class Batch:
        def create_namespaced_job(self, ns, manifest):
            captured["manifest"] = manifest

    kube.spawn_runner_job(Batch(), "symphony", cfg, "issue-1", "ENG-1", 1, 1)
    [container] = captured["manifest"]["spec"]["template"]["spec"]["containers"]
    refs = _secret_refs(container)
    assert {"linear-api-key", "github-app"} <= set(refs)
    # optional like everywhere else: a Job never waits on a missing integration Secret
    assert all(refs.values()), refs
    assert captured["manifest"]["spec"]["template"]["spec"]["serviceAccountName"] == "symphony-runner"
    template = _secret_template()
    assert {
        kube.SECRET_TRACKER, kube.SECRET_GITHUB, kube.SECRET_PROVIDER, kube.SECRET_SLACK,
        kube.SECRET_DISCORD,
    } <= set(template)


def _spawned_runner(monkeypatch, workflow: str) -> dict:
    monkeypatch.setenv("SYMPHONY_ORCHESTRATOR_IMAGE", "symphony-orchestrator:test")
    captured = {}

    class Batch:
        def create_namespaced_job(self, ns, manifest):
            captured["manifest"] = manifest

    kube.spawn_runner_job(Batch(), "symphony", parse_workflow(workflow), "issue-1", "ENG-1", 1, 1)
    return captured["manifest"]["spec"]["template"]


def _quantity(value: str) -> float:
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "m": 1e-3}
    for suffix, factor in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * factor
    return float(value)


def test_runner_pods_are_not_best_effort_and_not_safe_to_evict(monkeypatch):
    """A BestEffort runner is the kubelet's first eviction under node pressure, and the
    autoscaler removes its node freely: either kills the attempt in flight."""
    template = _spawned_runner(monkeypatch, "---\ntracker: {kind: memory}\n---\nx")
    [container] = template["spec"]["containers"]
    assert container["name"] == "runner"
    requests, limits = container["resources"]["requests"], container["resources"].get("limits", {})
    assert _quantity(requests["cpu"]) > 0
    assert _quantity(requests["memory"]) >= 256 * 2**20
    assert _quantity(limits["memory"]) >= _quantity(requests["memory"])
    assert template["metadata"]["annotations"]["cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"


def test_runner_and_reflector_pods_are_not_disrupted_by_karpenter(monkeypatch):
    """Karpenter consolidation reads only `karpenter.sh/do-not-disrupt`; without it, it
    evicts a runner on a nearly empty node and the Job fails (backoffLimit 0)."""
    monkeypatch.setenv("SYMPHONY_ORCHESTRATOR_IMAGE", "symphony-orchestrator:test")
    cfg = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    captured = []

    class Batch:
        def create_namespaced_job(self, ns, manifest):
            captured.append(manifest)

    kube.spawn_runner_job(Batch(), "symphony", cfg, "issue-1", "ENG-1", 1, 1)
    kube.spawn_reflector_job(Batch(), "symphony", cfg, "issue-1", {"identifier": "ENG-1"})
    assert [m["spec"]["template"]["metadata"]["labels"]["component"] for m in captured] == [
        "runner", "reflector",
    ]
    for manifest in captured:
        assert manifest["spec"]["template"]["metadata"]["annotations"]["karpenter.sh/do-not-disrupt"] == "true"


class _RunningPod:
    class status:
        phase = "Running"


class _WorkspaceCore:
    def __init__(self):
        self.pods: list[dict] = []
        self.patches: list[dict] = []

    def create_namespaced_persistent_volume_claim(self, ns, body):
        pass

    def create_namespaced_pod(self, ns, body):
        self.pods.append(body)

    def read_namespaced_pod(self, name, ns):
        return _RunningPod()

    def patch_namespaced_pod(self, name, ns, body):
        self.patches.append(body)


def test_a_new_workspace_pod_is_protected_from_eviction():
    core = _WorkspaceCore()
    cfg = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    assert kube.create_workspace(core, "symphony", cfg, "ws-1") is True
    [pod] = core.pods
    assert pod["metadata"]["annotations"]["karpenter.sh/do-not-disrupt"] == "true"
    assert pod["metadata"]["annotations"]["cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"


def test_a_released_workspace_drops_its_eviction_guards_and_a_reused_one_regains_them():
    """An idle workspace must not hold a node; a busy one must not lose its working tree."""
    core = _WorkspaceCore()
    kube.set_idle_since(core, "symphony", "ws-1", 123)
    kube.set_idle_since(core, "symphony", "ws-1", None)
    released, busy = (p["metadata"]["annotations"] for p in core.patches)
    assert released == {
        "symphony.dev/idle-since": "123",
        "karpenter.sh/do-not-disrupt": None,
        "cluster-autoscaler.kubernetes.io/safe-to-evict": None,
    }
    assert busy == {
        "symphony.dev/idle-since": None,
        "karpenter.sh/do-not-disrupt": "true",
        "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
    }


# One replica each, and an eviction loses work in flight or stops every component. The API
# is left movable: it keeps no state and its clients reconnect (deploy/api.yaml).
NOT_DISRUPTED = {
    "symphony-merger", "symphony-redis", "symphony-daemon", "symphony-gateway",
    "symphony-assistant",
}


def test_single_replica_control_plane_pods_are_not_disrupted_by_karpenter():
    deployments = _by_kind("Deployment")
    for name in NOT_DISRUPTED:
        assert deployments[name]["spec"]["replicas"] == 1
        annotations = deployments[name]["spec"]["template"]["metadata"].get("annotations") or {}
        assert annotations.get(kube.DO_NOT_DISRUPT_ANNOTATION) == "true", name
    # A PodDisruptionBudget at 0 on one replica would block every node drain.
    assert not _by_kind("PodDisruptionBudget")


def test_runner_pod_sizes_come_from_the_workflow(monkeypatch):
    template = _spawned_runner(
        monkeypatch,
        "---\ntracker: {kind: memory}\nkubernetes:\n  runner: {cpu: 0.5, memory_mib: 1024}\n---\nx",
    )
    [container] = template["spec"]["containers"]
    assert container["resources"]["requests"] == {"cpu": "0.5", "memory": "1024Mi"}
    assert container["resources"]["limits"]["memory"] == "1024Mi"


# --------------------------------------------------------------------------- ports


def test_service_ports_match_code():
    services = _by_kind("Service")
    expected = {"symphony-api": edge.API_PORT, "symphony-gateway": edge.GATEWAY_PORT}
    assert expected == {"symphony-api": 8080, "symphony-gateway": 8090}  # internals §9
    for name, port in expected.items():
        [service_port] = services[name]["spec"]["ports"]
        assert service_port["port"] == port
        # The service port; the gateway's health port (8081) is not in its Service.
        [container_port] = [p for p in _container(WORKLOADS[name])["ports"] if p["name"] == "http"]
        assert container_port["containerPort"] == port
        assert service_port["targetPort"] in (port, container_port["name"])
        assert services[name]["spec"]["selector"] == _pod_labels(WORKLOADS[name])


def test_gateway_service_name_is_what_workspaces_dial(monkeypatch):
    monkeypatch.delenv("SYMPHONY_GATEWAY_URL", raising=False)
    monkeypatch.setenv("POD_NAMESPACE", "symphony")
    cfg = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    service = _by_kind("Service")["symphony-gateway"]
    port = service["spec"]["ports"][0]["port"]
    assert edge.gateway_url(cfg) == f"http://symphony-gateway.symphony.svc.cluster.local:{port}"


def test_health_probes_use_the_daemon_health_port():
    from symphony.entry import daemon

    for name in ("symphony-daemon", "symphony-merger", "symphony-assistant"):
        container = _container(WORKLOADS[name])
        ports = {p["name"]: p["containerPort"] for p in container["ports"]}
        assert ports[container["livenessProbe"]["httpGet"]["port"]] == daemon.HEALTH_PORT == 8081
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"


# --------------------------------------------------------------------------- entrypoints


def test_entry_commands_exist_as_modules():
    commands = {name: _container(doc)["command"] for name, doc in SYMPHONY_WORKLOADS.items()}
    expected = {
        "symphony-api": ["python", "-m", "symphony.entry.serve", "api"],
        "symphony-gateway": ["python", "-m", "symphony.entry.serve", "gateway"],
        "symphony-daemon": ["python", "-m", "symphony.entry.daemon"],
        "symphony-merger": ["python", "-m", "symphony.entry.merger"],
        "symphony-assistant": ["python", "-m", "symphony.entry.assistant"],
        "symphony-nightly-sweep": ["python", "-m", "symphony.entry.sweep"],
        "symphony-backup": ["python", "-m", "symphony.entry.backup"],
    }
    assert commands == expected
    for module in {c[2] for c in commands.values()} | {"symphony.entry.run_issue", "symphony.entry.reflect"}:
        assert importlib.util.find_spec(module) is not None, f"{module} does not exist"


def test_serve_handles_the_deployed_roles():
    tree = ast.parse((ROOT / "src/symphony/entry/serve.py").read_text())
    compared = {
        node.comparators[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "role"
        and isinstance(node.comparators[0], ast.Constant)
    }
    assert {"api", "gateway"} <= compared


# --------------------------------------------------------------------------- identities


def test_service_accounts_and_rbac():
    accounts = _by_kind("ServiceAccount")
    roles = _by_kind("Role")
    bindings = _by_kind("RoleBinding")
    for name, doc in SYMPHONY_WORKLOADS.items():
        account = _pod_spec(doc)["serviceAccountName"]
        assert account in accounts, name
    # the runner identity kube.py assigns to Jobs exists and is bound
    assert "symphony-runner" in accounts and bindings["symphony-runner"]["roleRef"]["name"] == "symphony-runner"
    assert accounts["symphony-gateway"]["automountServiceAccountToken"] is False
    assert "symphony-gateway" not in bindings

    def allows(role: str, resource: str, verb: str) -> bool:
        return any(
            resource in rule["resources"] and verb in rule["verbs"] for rule in roles[role]["rules"]
        )

    assert allows("symphony-daemon", "jobs", "create")
    assert allows("symphony-daemon", "pods/exec", "create")
    assert allows("symphony-runner", "pods", "create")
    assert allows("symphony-runner", "persistentvolumeclaims", "create")
    # kube.discard_workspace, run by the runner when the repository set changed
    assert allows("symphony-runner", "pods", "delete")
    assert allows("symphony-runner", "persistentvolumeclaims", "delete")
    assert allows("symphony-runner", "pods/exec", "create")
    runner_secrets = next(r for r in roles["symphony-runner"]["rules"] if "secrets" in r["resources"])
    assert set(runner_secrets["resourceNames"]) == {"symphony-providers", kube.SECRET_PROVIDER, "symphony-mcp"}
    assert not any("secrets" in r["resources"] for r in roles["symphony-daemon"]["rules"])


def test_merger_reads_only_the_admin_provider_keys_and_mcp_servers():
    """internals §13: each merger agent runs on its role's provider, with the key saved in
    the Admin screen (Secret symphony-providers), and gets its role's MCP servers
    (symphony-mcp): read access to those Secrets and nothing else."""
    roles = _by_kind("Role")
    bindings = _by_kind("RoleBinding")
    providers = _by_kind("Secret")["symphony-providers"]
    assert "data" not in providers and "stringData" not in providers  # applies never clobber keys
    binding = bindings["symphony-merger"]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "symphony-merger"}]
    assert roles[binding["roleRef"]["name"]]["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["symphony-providers", "symphony-mcp"],
         "verbs": ["get"]}
    ]
    merger = WORKLOADS["symphony-merger"]
    assert _pod_spec(merger)["serviceAccountName"] == "symphony-merger"
    assert "automountServiceAccountToken" not in _pod_spec(merger)
    assert _env(_container(merger))["SYMPHONY_SECRET_PROVIDERS"]["value"] == "symphony-providers"
    assert _env(_container(WORKLOADS["symphony-api"]))["SYMPHONY_SECRET_PROVIDERS"]["value"] == "symphony-providers"


def _role_rules(role: str) -> list[dict]:
    return _by_kind("Role")[role]["rules"]


def _granted(role: str, resource: str) -> dict[str | None, set[str]]:
    """resourceName (None = every object) → verbs, for one resource of one Role."""
    out: dict[str | None, set[str]] = {}
    for rule in _role_rules(role):
        if resource in rule["resources"]:
            for name in rule.get("resourceNames") or [None]:
                out.setdefault(name, set()).update(rule["verbs"])
    return out


def test_api_role_scopes_every_setup_write_to_named_objects():
    """First-run setup writes integrations and restarts their consumers. Kubernetes cannot
    scope `create` to a name, so the API may only get/patch/update objects that already
    exist, by name; nothing namespace-wide beyond reads and `list deployments`."""
    secrets = _granted("symphony-api", "secrets")
    assert None not in secrets  # never every Secret of the namespace
    assert {name for name, verbs in secrets.items() if "patch" in verbs} == INTEGRATION_SECRETS | {"symphony-api"}
    assert secrets["model-provider"] == {"get"}  # key names for the models step, never written
    assert all("create" not in verbs and "delete" not in verbs for verbs in secrets.values())
    assert _granted("symphony-api", "configmaps") == {"symphony-workflow": {"get", "update"}}
    deployments = _granted("symphony-api", "deployments")
    assert deployments.pop(None) == {"list"}
    assert all(verbs == {"get", "patch"} for verbs in deployments.values())
    # every consumer the setup restarts can be patched: the daemon, the API itself, the
    # assistant and every merger Deployment (label component=merger)
    mergers = {name for name, doc in _by_kind("Deployment").items() if _pod_labels(doc).get("component") == "merger"}
    assert mergers and set(deployments) == {"symphony-api", "symphony-daemon", "symphony-assistant"} | mergers


def test_setup_finds_every_merger_deployment_by_its_labels():
    """Setup lists merger Deployments with `MERGER_SELECTOR` to restart them after a secret
    change. The selector matches Deployment labels, not pod labels, so a merger Deployment
    without them is never restarted and keeps the old credentials (found on kind)."""
    from symphony.setup_secrets import MERGER_SELECTOR

    wanted = dict(pair.split("=") for pair in MERGER_SELECTOR.split(","))
    mergers = {name: doc for name, doc in _by_kind("Deployment").items()
               if _pod_labels(doc).get("component") == "merger"}
    assert mergers
    for name, doc in mergers.items():
        labels = doc["metadata"].get("labels") or {}
        assert wanted.items() <= labels.items(), name


def test_setup_restarts_every_deployment_that_reads_a_secret_it_writes():
    """A setup write must restart each Deployment whose envFrom reads the changed Secret."""
    from symphony.setup_secrets import CONSUMERS, DEPLOYMENTS, SECRET_NAMES

    role_of = {deployment: role for role, deployment in DEPLOYMENTS.items()}
    for group, secret in SECRET_NAMES.items():
        if group not in CONSUMERS:
            continue
        for name, doc in _by_kind("Deployment").items():
            if secret not in _secret_refs(_container(doc)):
                continue
            role = "merger" if _pod_labels(doc).get("component") == "merger" else role_of.get(name)
            if role is None:
                continue  # not restarted by setup (e.g. the gateway reads no setup Secret)
            assert role in CONSUMERS[group], (group, name)


def test_setup_targets_exist_empty_from_the_first_apply():
    """The objects setup writes ship empty, so a later `kubectl apply -k deploy/` never
    erases what setup stored, and the API never needs `create`."""
    secrets = _by_kind("Secret")
    for name in INTEGRATION_SECRETS | {"symphony-providers"}:
        assert name in secrets, name
        assert "data" not in secrets[name] and "stringData" not in secrets[name], name
    workflow = _by_kind("ConfigMap")["symphony-workflow"]
    assert "data" not in workflow
    assert "symphony-api" not in secrets  # the operator's own Secret (bootstrap token)
    # every pod mounts the workflow optionally: a fresh install has none yet
    for name, doc in SYMPHONY_WORKLOADS.items():
        volumes = [v for v in _pod_spec(doc).get("volumes", []) if v.get("configMap", {}).get("name") == "symphony-workflow"]
        assert volumes and all(v["configMap"].get("optional") is True for v in volumes), name


def test_api_reads_the_public_url_from_its_config_map():
    container = _container(WORKLOADS["symphony-api"])
    refs = [ref["configMapRef"]["name"] for ref in container.get("envFrom", []) if "configMapRef" in ref]
    assert refs == ["symphony-api-config"]
    assert _by_kind("ConfigMap")["symphony-api-config"]["data"] == {"SYMPHONY_PUBLIC_URL": ""}


def _required_refs(pod: dict) -> set[tuple[str, str]]:
    """(kind, name) of every object a pod cannot start without."""
    out: set[tuple[str, str]] = set()
    for container in pod["containers"]:
        for ref in container.get("envFrom", []):
            for key, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
                if key in ref and not ref[key].get("optional"):
                    out.add((kind, ref[key]["name"]))
        for env in container.get("env", []):
            source = env.get("valueFrom", {})
            for key, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
                if key in source and not source[key].get("optional"):
                    out.add((kind, source[key]["name"]))
    for volume in pod.get("volumes", []):
        for key, kind, field in (("configMap", "ConfigMap", "name"), ("secret", "Secret", "secretName"),
                                 ("persistentVolumeClaim", "PersistentVolumeClaim", "claimName")):
            if key in volume and not volume[key].get("optional"):
                out.add((kind, volume[key][field]))
    return out


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")
def test_minimal_install_needs_only_symphony_api_and_redis():
    """`kubectl apply -k deploy/` + the `symphony-api` Secret is a complete first install:
    every object any pod needs to start is in the rendered deploy, except that one Secret,
    and only the API needs it. Integrations come later from the setup flow."""
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(DEPLOY)], capture_output=True, text=True, check=True
    ).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    present = {(d["kind"], d["metadata"]["name"]) for d in docs}
    missing: dict[tuple[str, str], set[str]] = {}
    for doc in docs:
        if doc["kind"] in ("Deployment", "CronJob"):
            for ref in _required_refs(_pod_spec(doc)) - present:
                missing.setdefault(ref, set()).add(doc["metadata"]["name"])
    assert missing == {("Secret", "symphony-api"): {"symphony-api"}}
    assert ("Deployment", "symphony-redis") in present and ("Service", "symphony-redis") in present


def test_no_oauth_or_subscription_credentials_anywhere(monkeypatch):
    """API keys only: no subscription Secret in the template, the manifests or the Jobs."""
    resources = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text())["resources"]
    text = "\n".join(
        (DEPLOY / name).read_text()
        for name in [*resources, "secrets.example.yaml", "integrations.example.yaml"]
    )
    for word in (
        "anthropic-subscription", "ANTHROPIC_REFRESH_TOKEN", "provider-token", "oauth-native",
        "openai-codex", "OPENAI_CODEX", "auth.openai.com", "platform.claude.com",
    ):
        assert word not in text, word
    assert not hasattr(kube, "SECRET_SUBSCRIPTION")
    monkeypatch.setenv("SYMPHONY_ORCHESTRATOR_IMAGE", "symphony-orchestrator:test")
    cfg = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    captured = {}

    class Batch:
        def create_namespaced_job(self, ns, manifest):
            captured["manifest"] = manifest

    kube.spawn_runner_job(Batch(), "symphony", cfg, "issue-1", "ENG-1", 1, 1)
    [container] = captured["manifest"]["spec"]["template"]["spec"]["containers"]
    assert set(_secret_refs(container)) == {
        kube.SECRET_TRACKER, kube.SECRET_GITHUB, kube.SECRET_SLACK, kube.SECRET_DISCORD,
    }
    assert not (DOCKER / "oauth-native").exists()


def test_merger_has_a_persistent_workdir():
    merger = WORKLOADS["symphony-merger"]
    container = _container(merger)
    workdir = _env(container)["SYMPHONY_MERGER_WORKDIR"]["value"]
    mount = next(m for m in container["volumeMounts"] if m["mountPath"] == workdir)
    volume = next(v for v in _pod_spec(merger)["volumes"] if v["name"] == mount["name"])
    assert volume["persistentVolumeClaim"]["claimName"] in _by_kind("PersistentVolumeClaim")
    assert merger["spec"]["strategy"]["type"] == "Recreate"
    # internals §9 "Merger": the per-repo knobs are explicit in the manifest; the binary and
    # the health port keep their defaults (omp on PATH, 8081 = the probe's port)
    contract = _contract_merger_env()
    assert {"SYMPHONY_MERGER_REPO", "SYMPHONY_MERGER_WORKDIR", "SYMPHONY_MERGER_REVIEWERS"} <= contract
    assert contract - {"SYMPHONY_OMP_BIN", "SYMPHONY_HEALTH_PORT"} <= set(_env(container))
    assert {"SYMPHONY_OMP_BIN", "SYMPHONY_HEALTH_PORT"}.isdisjoint(_env(container))


def test_redis_is_durable():
    redis = WORKLOADS["symphony-redis"]
    args = _container(redis)["args"]
    assert args[args.index("--appendonly") + 1] == "yes"
    [volume] = _pod_spec(redis)["volumes"]
    assert volume["persistentVolumeClaim"]["claimName"] in _by_kind("PersistentVolumeClaim")


# --------------------------------------------------------------------------- network


def _workspace_labels() -> dict:
    cfg = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    return kube._workspace_manifest(cfg, "ws", "img")["metadata"]["labels"]


def test_workspaces_reach_only_gateway_dns_and_public_internet():
    policies = _by_kind("NetworkPolicy")
    labels = _workspace_labels()
    [workspace] = [p for p in policies.values() if p["spec"]["podSelector"].get("matchLabels") == labels]
    assert set(workspace["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert workspace["spec"]["ingress"] == []
    peers = [peer for rule in workspace["spec"]["egress"] for peer in rule["to"]]
    pods = [p["podSelector"]["matchLabels"] for p in peers if "podSelector" in p and "namespaceSelector" not in p]
    assert pods == [_pod_labels(WORKLOADS["symphony-gateway"])]
    [block] = [p["ipBlock"] for p in peers if "ipBlock" in p]
    assert block["cidr"] == "0.0.0.0/0"
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"} <= set(block["except"])


def test_gateway_and_redis_ingress_fences():
    policies = {p["spec"]["podSelector"].get("matchLabels", {}).get("component"): p for p in _by_kind("NetworkPolicy").values()}
    gateway = policies["gateway"]["spec"]["ingress"]
    # agents only: workspaces and distill pods (traces, MCP), merger agents and the assistant (MCP)
    assert [peer["podSelector"]["matchLabels"] for rule in gateway for peer in rule["from"]] == [
        _workspace_labels(),
        {"app": "symphony", "component": "distill"},
        {"app": "symphony", "component": "merger"},
        {"app": "symphony", "component": "assistant"},
    ]
    merger = _pod_labels(WORKLOADS["symphony-merger"])
    assert {"app": merger["app"], "component": merger["component"]} == {"app": "symphony", "component": "merger"}
    [redis_rule] = policies["redis"]["spec"]["ingress"]
    [expression] = redis_rule["from"][0]["podSelector"]["matchExpressions"]
    allowed = set(expression["values"])
    components = {_pod_labels(doc)["component"] for doc in SYMPHONY_WORKLOADS.values()}
    assert components | {"runner", "reflector"} == allowed  # kube.py's Job components
    assert "workspace" not in allowed


def test_gateway_egress_is_redis_dns_and_public_internet_only():
    """A3: remote MCP servers are on the public internet; a stored header must never reach a
    private address (the API, the node, the cloud metadata endpoint)."""
    policies = {p["spec"]["podSelector"].get("matchLabels", {}).get("component"): p for p in _by_kind("NetworkPolicy").values()}
    spec = policies["gateway"]["spec"]
    assert set(spec["policyTypes"]) == {"Ingress", "Egress"}
    peers = [peer for rule in spec["egress"] for peer in rule["to"]]
    pods = [p["podSelector"]["matchLabels"] for p in peers if "podSelector" in p and "namespaceSelector" not in p]
    assert pods == [{"app": "symphony", "component": "redis"}]
    [block] = [p["ipBlock"] for p in peers if "ipBlock" in p]
    assert block["cidr"] == "0.0.0.0/0"
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8"} <= set(block["except"])


# --------------------------------------------------------------------------- images


def test_symphony_workloads_run_the_orchestrator_image():
    kustomization = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text())
    assert [i["name"] for i in kustomization["images"]] == [ORCHESTRATOR_IMAGE]
    for name, doc in SYMPHONY_WORKLOADS.items():
        assert _container(doc)["image"] == ORCHESTRATOR_IMAGE, name


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")
@pytest.mark.parametrize("path", ["deploy", "scripts/e2e", "deploy/cloudflared"])
def test_kustomize_renders_and_jobs_follow_the_daemon_image(path):
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / path)], capture_output=True, text=True, check=True
    ).stdout
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    assert all(d["metadata"].get("namespace") == "symphony" for d in docs if d["kind"] != "Namespace")
    if path == "deploy/cloudflared":
        return
    workloads = {d["metadata"]["name"]: d for d in docs if d["kind"] in ("Deployment", "CronJob")}
    daemon_image = _container(workloads["symphony-daemon"])["image"]
    assert daemon_image.startswith(ORCHESTRATOR_IMAGE + ":")
    for name in ("symphony-daemon", "symphony-nightly-sweep"):
        container = _container(workloads[name])
        assert container["image"] == daemon_image
        assert _env(container)["SYMPHONY_ORCHESTRATOR_IMAGE"]["value"] == daemon_image


def test_agent_images_match_build_script():
    script = (ROOT / "scripts/build_images.sh").read_text()
    runtimes = re.search(r'RUNTIMES="\$\{RUNTIMES:-([^}]*)\}"', script).group(1).split()
    assert set(runtimes) == set(kube.DEFAULT_AGENT_IMAGES)
    for runtime, image in kube.DEFAULT_AGENT_IMAGES.items():
        assert image == f"symphony-agent:{runtime}"
        assert re.search(rf"^\s+{runtime}\) echo ", script, re.M), runtime


# --------------------------------------------------------------------------- engine pin


def test_dockerfiles_pin_the_code_engine_version():
    for dockerfile in ("orchestrator.Dockerfile", "agent.Dockerfile"):
        text = (DOCKER / dockerfile).read_text()
        assert re.search(rf"^ARG OMP_VERSION={re.escape(edge.OMP_VERSION)}$", text, re.M), dockerfile
        assert "install-omp.sh" in text


def test_omp_installer_has_pinned_checksums_for_the_version():
    installer = (DOCKER / "install-omp.sh").read_text()
    for arch in ("x64", "arm64"):
        assert re.search(
            rf"^\s+{re.escape(edge.OMP_VERSION)}/{arch}\) sha256=[0-9a-f]{{64}} ;;$", installer, re.M
        ), arch


def test_catalog_installer_pins_the_engine_version():
    installer = (DOCKER / "install-omp-catalog.sh").read_text()
    assert re.search(rf"^\s+{re.escape(edge.OMP_VERSION)}\) sha256=[0-9a-f]{{64}} ;;$", installer, re.M)
    dockerfile = (DOCKER / "orchestrator.Dockerfile").read_text()
    assert 'install-omp-catalog.sh "${OMP_VERSION}" /opt/symphony/omp-catalog' in dockerfile
    # the API's catalog default path is where the image puts it
    from symphony.api import catalog

    assert catalog.DEFAULT_PATH == "/opt/symphony/omp-catalog/models.json"


def test_image_bakes_the_default_workflow_setup_seeds_from():
    """A fresh install has an empty symphony-workflow ConfigMap; setup seeds it from the
    image's copy of the repository's WORKFLOW.md, which must be a valid workflow."""
    dockerfile = (DOCKER / "orchestrator.Dockerfile").read_text()
    assert "COPY WORKFLOW.md /opt/symphony/WORKFLOW.default.md" in dockerfile
    assert "ENV SYMPHONY_DEFAULT_WORKFLOW=/opt/symphony/WORKFLOW.default.md" in dockerfile
    parse_workflow((ROOT / "WORKFLOW.md").read_text(encoding="utf-8"))


def test_dockerignore_admits_every_copy_source():
    for dockerfile in ("orchestrator.Dockerfile", "agent.Dockerfile"):
        text = (DOCKER / dockerfile).read_text()
        ignore = (DOCKER / f"{dockerfile}.dockerignore").read_text().split()
        allowed = [line[1:].rstrip("/") for line in ignore if line.startswith("!")]
        for match in re.finditer(r"^COPY (?!--from)(.+) \S+$", text, re.M):
            for source in match.group(1).split():
                assert (ROOT / source).exists(), source
                assert any(source == a or source.startswith(a + "/") for a in allowed), source


# --------------------------------------------------------------------------- kind certification


def _certify_module():
    spec = importlib.util.spec_from_file_location("kind_certify", ROOT / "scripts/e2e/certify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_certification_workflow_moves_one_ticket_through_the_seed():
    """The kind certification moves its ticket by republishing WORKFLOW.md: every state it
    publishes must parse, and the seeded ticket must be dispatchable on its own repo label."""
    certify = _certify_module()
    boards = {}
    for state in ("none", "Todo", "Done"):
        cfg = parse_workflow(certify.render(state, "amazon-bedrock", "global.anthropic.claude-opus-5-5"))
        boards[state] = edge.make_tracker(cfg).fetch_issues_by_ids([certify.ISSUE_ID])
        assert cfg.tracker_kind == "memory" and cfg.max_turns == 1 and not cfg.learning_enabled
        assert cfg.agent_images == {"node": "symphony-agent:node"}
        # the identity remote keeps the second dispatch on the same workspace
        assert "git remote add origin" in cfg.hooks["after_create"]
    assert boards["none"] == []
    [todo] = boards["Todo"]
    assert todo.dispatchable and todo.target_repo == certify.TARGET_REPO
    [done] = boards["Done"]
    assert done.state_type == "terminal"
