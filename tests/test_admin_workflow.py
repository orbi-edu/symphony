"""Policy review, credential boundaries and optimistic ConfigMap publication."""

import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from kubernetes.client import V1ConfigMap, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from symphony.admin_commands import CommandError
from symphony.admin_workflow import WorkflowError, WorkflowRepository
from symphony.config import parse_workflow

POLICY = """---
# operator's comment
tracker:
  kind: memory
engine: {provider: acme, model: acme-1}
custom_policy:
  retained: 'do not edit' # unknown extension
---

Issue {{ issue.identifier }}.
{% if attempt %}Retry {{ attempt }}.{% endif %}
"""


class ConfigMaps:
    def __init__(self, text=POLICY):
        self.current = V1ConfigMap(
            metadata=V1ObjectMeta(
                name="symphony-workflow",
                namespace="symphony",
                resource_version="10",
                annotations={"operator": "preserved"},
            ),
            data={"WORKFLOW.md": text, "unrelated": "preserved"},
        )
        self.writes = 0
        self.before_replace = None
        self.lose_response = False

    def read_namespaced_config_map(self, *, name, namespace, _request_timeout):
        return copy.deepcopy(self.current)

    def replace_namespaced_config_map(self, *, name, namespace, body, _request_timeout):
        if self.before_replace:
            self.before_replace(self)
        if body.metadata.resource_version != self.current.metadata.resource_version:
            raise ApiException(status=409, reason="private Kubernetes diagnostic")
        self.writes += 1
        self.current = copy.deepcopy(body)
        self.current.metadata.resource_version = str(int(body.metadata.resource_version) + 1)
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("private transport diagnostic")
        return copy.deepcopy(self.current)


def reviewed(repository, patch):
    revision = repository.read()["revision"]
    review = repository.validate(revision, patch)
    return {"baseRevision": revision, "patch": patch, "documentHash": review["documentHash"]}


def test_publication_preserves_unknown_yaml_comments_prompt_and_unrelated_configmap_data():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    payload = reviewed(repository, {"agent.max_turns": 7})
    revision = repository.publish(payload, "command-1", before_write=lambda: None)
    text = core.current.data["WORKFLOW.md"]
    assert parse_workflow(text).max_turns == 7
    assert "# operator's comment" in text
    assert "retained: 'do not edit' # unknown extension" in text
    assert text.split("---\n", 2)[2] == POLICY.split("---\n", 2)[2]
    assert core.current.data["unrelated"] == "preserved"
    assert core.current.metadata.annotations["operator"] == "preserved"
    assert hashlib.sha256(text.encode()).hexdigest() == payload["documentHash"]
    assert repository.publish(payload, "command-1", before_write=lambda: None) == revision
    assert core.writes == 1


def test_crash_after_write_is_recovered_without_republishing():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    payload = reviewed(repository, {"engine.model": "acme-2"})
    authorized = True

    def authorize():
        if not authorized:
            raise CommandError(
                "authorization_revoked", "The publisher no longer has permission.", 403
            )

    core.lose_response = True
    with pytest.raises(WorkflowError) as error:
        repository.publish(payload, "command-2", before_write=authorize)
    assert error.value.status_code == 503
    authorized = False
    assert (
        repository.publish(payload, "command-2", before_write=authorize)
        == core.current.metadata.resource_version
    )
    assert core.writes == 1


def test_revocation_during_config_read_prevents_publication(monkeypatch):
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    payload = reviewed(repository, {"agent.max_turns": 8})
    authorized = True
    original_read = core.read_namespaced_config_map
    revoked = CommandError("authorization_revoked", "The publisher no longer has permission.", 403)

    def read_and_revoke(**kwargs):
        nonlocal authorized
        result = original_read(**kwargs)
        authorized = False
        return result

    def authorize():
        if not authorized:
            raise revoked

    monkeypatch.setattr(core, "read_namespaced_config_map", read_and_revoke)
    with pytest.raises(CommandError) as error:
        repository.publish(payload, "revoked-command", before_write=authorize)
    assert error.value is revoked
    assert core.writes == 0
    assert core.current.data["WORKFLOW.md"] == POLICY
    assert core.current.metadata.resource_version == "10"


def test_cas_race_preserves_intervening_policy():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    payload = reviewed(repository, {"agent.max_turns": 8})

    def concurrent_write(source):
        source.current.metadata.resource_version = "11"
        source.current.data["WORKFLOW.md"] = POLICY.replace("acme-1", "new-policy")

    core.before_replace = concurrent_write
    with pytest.raises(WorkflowError) as error:
        repository.publish(payload, "command-3", before_write=lambda: None)
    assert error.value.status_code == 412
    assert parse_workflow(core.current.data["WORKFLOW.md"]).engine_model == "new-policy"
    assert core.writes == 0


def test_review_digest_binds_exact_patch_and_base_revision():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    payload = reviewed(repository, {"agent.max_turns": 8})
    payload["patch"] = {"agent.max_turns": 9}
    with pytest.raises(WorkflowError) as changed_review:
        repository.publish(payload, "command-4", before_write=lambda: None)
    assert changed_review.value.status_code == 412
    payload["patch"] = {"agent.max_turns": 8}
    core.current.metadata.resource_version = "11"
    with pytest.raises(WorkflowError) as stale_review:
        repository.publish(payload, "command-4", before_write=lambda: None)
    assert stale_review.value.status_code == 412
    assert core.writes == 0


def test_replay_cannot_claim_success_after_manual_document_change():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    payload = reviewed(repository, {"agent.max_turns": 8})
    repository.publish(payload, "command-5", before_write=lambda: None)
    core.current.data["WORKFLOW.md"] += "\nManual policy change\n"
    with pytest.raises(WorkflowError) as error:
        repository.publish(payload, "command-5", before_write=lambda: None)
    assert error.value.status_code == 412
    assert core.writes == 1


def test_legacy_credential_and_copies_never_enter_values_or_review_but_survive_publication():
    secret = "legacy-credential-not-a-known-token-format"
    policy = POLICY.replace(
        "  kind: memory", "  kind: linear\n  provider:\n    team_key: ENG\n    api_key: " + secret
    )
    policy = policy.replace(
        "Issue {{ issue.identifier }}.", "Issue {{ issue.identifier }}. " + secret
    )
    core = ConfigMaps(policy)
    repository = WorkflowRepository(core, "symphony")
    public = repository.read()
    assert secret not in json.dumps(public)
    assert set(public["redactedKeys"]) == {"tracker.provider.api_key", "prompt_body"}
    assert public["values"]["tracker.provider.team_key"] == "ENG"
    review = repository.validate("10", {"agent.max_turns": 8})
    assert secret not in json.dumps(review)
    repository.publish(
        {
            "baseRevision": "10",
            "patch": {"agent.max_turns": 8},
            "documentHash": review["documentHash"],
        },
        "command-6",
        before_write=lambda: None,
    )
    assert parse_workflow(core.current.data["WORKFLOW.md"]).provider["api_key"] == secret
    assert core.current.data["WORKFLOW.md"].endswith(policy.split("---\n", 2)[2])
    with pytest.raises(WorkflowError) as error:
        repository.validate("11", {"tracker.provider.api_key": "$REPLACEMENT"})
    assert error.value.status_code == 422
    assert secret not in str(error.value)


def test_nested_credential_containers_withhold_copies_but_preserve_references():
    primary = "opaque-primary-legacy-value"
    secondary = "opaque-secondary-legacy-value"
    reference = "$HOST_CREDENTIAL"
    nested = {
        "credentials": {"unknown": [{"primary": primary}, {"deeper": [secondary, reference]}]}
    }
    policy = POLICY.replace(
        "custom_policy:", "custom_extension: " + json.dumps(nested) + "\ncustom_policy:"
    )
    policy = policy.replace("acme-1}", "acme-1, command: " + secondary + "}")
    policy = policy.replace("Issue {{ issue.identifier }}.", primary)
    repository = WorkflowRepository(ConfigMaps(policy), "symphony")

    public = repository.read()
    assert public["values"]["prompt_body"] == ""
    assert public["values"]["engine.command"] == ""
    assert {"prompt_body", "engine.command"} <= set(public["redactedKeys"])
    for secret in (primary, secondary):
        assert secret not in json.dumps(public)
        with pytest.raises(WorkflowError) as error:
            repository.validate("10", {"hooks.before_run": secret})
        assert error.value.status_code == 422
        assert secret not in json.dumps(error.value.diagnostics)

    review = repository.validate("10", {"hooks.before_run": "echo " + reference})
    assert review["diff"][0]["after"] == "echo " + reference
    assert primary not in json.dumps(review)
    assert secondary not in json.dumps(review)


@pytest.mark.parametrize(
    "field, section, container",
    [
        ("tracker.required_labels", "tracker", "list"),
        ("agent.max_concurrent_agents_by_state", "agent", "keys"),
    ],
)
def test_escaped_credential_copies_in_structured_values_are_withheld_and_rejected(
    field, section, container
):
    secret = 'opaque"credential\\segment\nlast'
    value = [secret] if container == "list" else {secret: 2}
    encoded = json.dumps(value)
    policy = POLICY.replace(
        "custom_policy:", "legacy_secret: " + json.dumps(secret) + "\ncustom_policy:"
    )
    property_name = field.split(".")[-1]
    if section == "tracker":
        existing = policy.replace(
            "  kind: memory", "  kind: memory\n  " + property_name + ": " + encoded
        )
    else:
        existing = policy.replace(
            "engine:", "agent:\n  " + property_name + ": " + encoded + "\nengine:"
        )
    repository = WorkflowRepository(ConfigMaps(existing), "symphony")

    public = repository.read()
    assert field in public["redactedKeys"]
    assert public["values"][field] == ""
    review = repository.validate("10", {"agent.max_turns": 8})
    assert all(change["key"] != field for change in review["diff"])
    with pytest.raises(WorkflowError) as locked:
        repository.validate("10", {field: "[]" if container == "list" else "{}"})
    assert locked.value.status_code == 422

    clean = WorkflowRepository(ConfigMaps(policy), "symphony")
    with pytest.raises(WorkflowError) as copied:
        clean.validate("10", {field: encoded})
    assert copied.value.status_code == 422
    assert secret not in str(copied.value)
    assert all(secret not in diagnostic["message"] for diagnostic in copied.value.diagnostics)


def test_credential_patterns_are_checked_in_decoded_structured_values():
    credential = "Bearer\nabcdefgh12345678"
    encoded = json.dumps([credential])
    policy = POLICY.replace("  kind: memory", "  kind: memory\n  required_labels: " + encoded)
    repository = WorkflowRepository(ConfigMaps(policy), "symphony")
    public = repository.read()
    assert public["values"]["tracker.required_labels"] == ""
    assert "tracker.required_labels" in public["redactedKeys"]
    clean = WorkflowRepository(ConfigMaps(), "symphony")
    with pytest.raises(WorkflowError) as error:
        clean.validate("10", {"tracker.required_labels": encoded})
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "patch",
    [
        {"tracker.provider.api_key": "literal-credential-value"},
        {"telemetry.endpoint": "https://name:password@collector:4318"},
        {"hooks.before_run": "export API_KEY=literalcredentialvalue"},
        {"kubernetes.namespace": "another-namespace"},
        {"kubernetes.sandbox.memory_mib": True},
        {"agent.max_concurrent_agents_by_state": '{" Rework ": 1, "rework": 2}'},
        {"kubernetes.image_pull_secrets": '[{"name":"secret","password":"private"}]'},
        {"kubernetes.runner.wallclock_guard_s": 14400},
        {"review_watch.answer_prompt": "{% if issue %}"},
        {"tracker.provider.password": "not-allowlisted"},
    ],
)
def test_unsafe_or_invalid_patches_are_rejected_before_writes(patch):
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    with pytest.raises(WorkflowError) as error:
        repository.validate("10", patch)
    assert error.value.status_code == 422
    assert core.writes == 0


def test_complete_validation_rejects_existing_invalid_limits_not_just_patch():
    core = ConfigMaps(
        POLICY.replace("engine:", "agent:\n  max_concurrent_agents_by_state: {Todo: 0}\nengine:")
    )
    repository = WorkflowRepository(core, "symphony")
    with pytest.raises(WorkflowError) as error:
        repository.validate("10", {"engine.model": "acme-2"})
    assert error.value.status_code == 422
    assert error.value.diagnostics[0]["key"] == "agent.max_concurrent_agents_by_state"


def test_prompt_only_edit_preserves_front_matter_bytes_and_empty_body():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    repository.publish(
        reviewed(repository, {"prompt_body": ""}), "command-7", before_write=lambda: None
    )
    assert core.current.data["WORKFLOW.md"] == POLICY.rsplit("---\n", 1)[0] + "---\n"


def test_shared_yaml_alias_cannot_extend_a_patch_to_an_unknown_policy_section():
    policy = POLICY.replace(
        "engine: {provider: acme, model: acme-1}",
        "engine: &engine {provider: acme, model: acme-1}\nother_engine: *engine",
    )
    repository = WorkflowRepository(ConfigMaps(policy), "symphony")
    with pytest.raises(WorkflowError) as error:
        repository.validate("10", {"engine.model": "acme-2"})
    assert error.value.status_code == 422


def test_unused_credential_reference_can_clear_but_active_tracker_cannot():
    policy = POLICY.replace(
        "  kind: memory",
        "  kind: linear\n  provider: {team_key: ENG, api_key: $HOST_TRACKER_KEY}",
    )
    repository = WorkflowRepository(ConfigMaps(policy), "symphony")
    with pytest.raises(WorkflowError) as required:
        repository.validate("10", {"tracker.provider.api_key": ""})
    assert required.value.status_code == 422
    repository.publish(
        reviewed(repository, {"tracker.kind": "memory", "tracker.provider.api_key": ""}),
        "remove-unused-reference",
        before_write=lambda: None,
    )
    current = repository.read()
    assert current["values"]["tracker.provider.api_key"] == ""
    with pytest.raises(WorkflowError) as incomplete:
        repository.validate(current["revision"], {"tracker.kind": "linear"})
    assert incomplete.value.status_code == 422


def test_github_board_fields_are_validated_and_publishable():
    core = ConfigMaps()
    repository = WorkflowRepository(core, "symphony")
    for patch in ({"tracker.kind": "github"},  # no owner nor project yet
                  {"tracker.kind": "github", "tracker.provider.owner": "acme/app",
                   "tracker.provider.project_number": 7},
                  {"tracker.kind": "github", "tracker.provider.owner": "acme",
                   "tracker.provider.project_number": 0}):
        with pytest.raises(WorkflowError) as invalid:
            repository.validate("10", patch)
        assert invalid.value.status_code == 422
    repository.publish(
        reviewed(repository, {"tracker.kind": "github", "tracker.provider.owner": "acme",
                              "tracker.provider.project_number": 7}),
        "use-github-board",
        before_write=lambda: None,
    )
    cfg = parse_workflow(core.current.data["WORKFLOW.md"])
    assert cfg.tracker_kind == "github"
    assert cfg.provider == {"owner": "acme", "project_number": 7}
    values = repository.read()["values"]
    assert values["tracker.provider.project_number"] == 7
    assert values["tracker.provider.status_field"] == ""
    repository.publish(
        reviewed(repository, {"tracker.kind": "memory", "tracker.provider.owner": "",
                              "tracker.provider.project_number": ""}),
        "back-to-memory",
        before_write=lambda: None,
    )
    assert parse_workflow(core.current.data["WORKFLOW.md"]).tracker_kind == "memory"


def test_kubernetes_secret_names_remain_publishable_while_literals_stay_redacted():
    secret = "private-inline-credential"
    policy = POLICY.replace("  retained:", f"  credentials: {{legacy: {secret}}}\n  retained:")
    policy = policy.replace(
        "Issue {{ issue.identifier }}.", f"Issue {{{{ issue.identifier }}}}. {secret}"
    )
    repository = WorkflowRepository(ConfigMaps(policy), "symphony")
    repository.publish(
        reviewed(repository, {"kubernetes.image_pull_secrets": '["registry-secret"]'}),
        "command-8",
        before_write=lambda: None,
    )
    visible = repository.read()
    assert json.loads(visible["values"]["kubernetes.image_pull_secrets"]) == ["registry-secret"]
    assert "prompt_body" in visible["redactedKeys"]
    assert secret not in json.dumps(visible)


def test_missing_source_and_parser_errors_never_expose_raw_diagnostics():
    secret = "private-kubernetes-server-body"
    core = SimpleNamespace(
        read_namespaced_config_map=lambda **kwargs: (_ for _ in ()).throw(
            ApiException(status=403, reason=secret)
        )
    )
    with pytest.raises(WorkflowError) as unavailable:
        WorkflowRepository(core, "symphony").read()
    assert unavailable.value.status_code == 503
    assert secret not in str(unavailable.value)
    repository = WorkflowRepository(
        ConfigMaps(POLICY.replace("kind: memory", f"kind: {secret}")), "symphony"
    )
    with pytest.raises(WorkflowError) as invalid:
        repository.read()
    assert secret not in str(invalid.value)
    assert secret not in json.dumps(invalid.value.diagnostics)


def test_the_shipped_workflow_prompt_is_editable_through_the_api():
    # Its shell snippets (`sed -n 's/^password=//p'`, `Authorization: Bearer $TOKEN`) are not
    # credentials: the prompt must not be locked as one.
    from pathlib import Path

    shipped = (Path(__file__).resolve().parents[1] / "WORKFLOW.md").read_text()
    repository = WorkflowRepository(ConfigMaps(shipped), "symphony")
    assert "prompt_body" not in repository.read()["redactedKeys"]
    body = shipped.split("---\n", 2)[2]
    payload = reviewed(repository, {"prompt_body": body + "\nOne more line.\n"})
    assert payload["documentHash"]
