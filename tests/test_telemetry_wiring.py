"""How telemetry reaches the sandbox.

Every assertion here guards a SILENT failure: get one of these wrong and there is no error,
no log, and no telemetry — the feature simply does not exist, and looks like it does.
Evidence for each is in specs/004-agent-session-telemetry/research.md.
"""

from symphony.config import ConfigError, parse_workflow
from symphony.edge import otlp_endpoint, telemetry_env

# -- the endpoint ----------------------------------------------------------


def test_the_endpoint_carries_the_signal_path():
    """The per-signal endpoint is used verbatim. Without the path every batch is posted
    to `/`."""
    assert otlp_endpoint("https://telemetry.example.com") == "https://telemetry.example.com/v1/traces"
    assert otlp_endpoint("http://localhost:4318/") == "http://localhost:4318/v1/traces"


def test_no_url_yields_no_endpoint():
    assert otlp_endpoint("") == ""


# -- the attempt's exporter environment --------------------------------------


def _cfg(block: str = ""):
    return parse_workflow(_workflow_with(block))


def test_the_engine_exports_traces_only_over_protobuf_to_the_verbatim_endpoint():
    """OMP disables a signal whose protocol is not `http/protobuf`, and appends `/v1/traces`
    to the COMMON endpoint — either mistake means no telemetry and no error."""
    env = telemetry_env(
        _cfg("telemetry:\n  endpoint: https://otel.example.com/v1/traces\n"), "i1", 7, "tok"
    )
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == "https://otel.example.com/v1/traces"
    assert env["OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"] == "http/protobuf"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert env["OTEL_LOGS_EXPORTER"] == env["OTEL_METRICS_EXPORTER"] == "none"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "symphony.issue_id=i1,symphony.dispatch_seq=7"
    assert "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" not in env


def test_the_bearer_header_survives_percent_decoding():
    """OMP percent-decodes header values and splits on `,`/`=`: a raw space or a `=` in
    the token would corrupt the Authorization header."""
    env = telemetry_env(_cfg(), "i1", 1, "a=b,c")
    assert env["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] == "Authorization=Bearer%20a%3Db%2Cc"


def test_disabled_telemetry_is_the_sdk_off_switch():
    assert telemetry_env(_cfg("telemetry:\n  enabled: false\n"), "i1", 1, "t") == {
        "OTEL_SDK_DISABLED": "true"
    }


def test_a_capture_level_reaches_the_engine():
    env = telemetry_env(_cfg("telemetry:\n  capture_content: summary\n"), "i1", 1, "t")
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "summary"


def test_by_default_spans_go_to_the_sandbox_gateway(monkeypatch):
    """The gateway (internals §9, port 8090) is the only Symphony service a workspace reaches."""
    from symphony.edge import GATEWAY_PORT

    monkeypatch.delenv("SYMPHONY_GATEWAY_URL", raising=False)
    monkeypatch.setenv("POD_NAMESPACE", "sym")
    env = telemetry_env(_cfg(), "i1", 1, "t")
    base = f"http://symphony-gateway.sym.svc.cluster.local:{GATEWAY_PORT}"
    assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == f"{base}/v1/traces"


def test_the_gateway_override_moves_the_traces_route(monkeypatch):
    monkeypatch.setenv("SYMPHONY_GATEWAY_URL", "http://127.0.0.1:8090/")
    assert telemetry_env(_cfg(), "i1", 1, "t")["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] == (
        "http://127.0.0.1:8090/v1/traces"
    )


# -- configuration ---------------------------------------------------------


def test_a_relative_operator_endpoint_is_rejected_at_load():
    text = _workflow_with("telemetry:\n  endpoint: /v1/traces\n")
    try:
        parse_workflow(text)
    except ConfigError as exc:
        assert "absolute URL" in str(exc)
    else:
        raise AssertionError("a relative operator endpoint must not be accepted")


def test_a_valid_operator_endpoint_is_accepted():
    text = _workflow_with("telemetry:\n  endpoint: https://otel.example.com/v1/traces\n")
    assert parse_workflow(text).telemetry_endpoint == "https://otel.example.com/v1/traces"


def test_an_unknown_capture_level_is_rejected():
    text = _workflow_with("telemetry:\n  capture_content: everything\n")
    try:
        parse_workflow(text)
    except ConfigError as exc:
        assert "capture_content" in str(exc)
    else:
        raise AssertionError("an unknown capture level must not be accepted")


def _workflow_with(block: str) -> str:
    return f"---\ntracker:\n  kind: memory\nengine:\n  provider: p\n  model: m\n{block}---\nbody\n"
