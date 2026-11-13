"""Model-provider mock (internals §13/§14): Anthropic Messages and OpenAI Responses, streamed.

The framing tests always run: they drive the mock over HTTP and check every SSE event and
JSON body against the vendored specs. The engine tests are opt-in (`SYMPHONY_REAL_OMP=1`):
the real omp 18.20.2 completes a two-step turn (a `bash` tool call that writes a file, then
the codeword) against each mock, with the key staged exactly as Symphony stages an Admin-saved
built-in provider key (`ProviderRepository.runtime` → `prepare_provider`).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from scripts.mocks.models import schemas, server
from scripts.mocks.models.script import DEFAULT_KEYS, default_script

ROOT = Path(__file__).resolve().parents[2]
CODEWORD = "TEAL-HERON-4417"
COMMAND = f"printf '%s\\n' {CODEWORD} > codeword.txt"
SCRIPT = {
    "steps": [
        {"tool": {"name": "bash", "input": {"i": "Writing the codeword file", "command": COMMAND}}},
        {"text": f"The codeword is {CODEWORD}."},
    ]
}
ANTHROPIC_HEADERS = {
    "x-api-key": DEFAULT_KEYS["anthropic"],
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}
OPENAI_HEADERS = {"authorization": f"Bearer {DEFAULT_KEYS['openai']}"}


@pytest.fixture
def mock(tmp_path):
    srv = server.serve(0, tmp_path / "mock")
    srv.state.configure(script=SCRIPT)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def sse(response: httpx.Response) -> list[tuple[str, dict]]:
    """Parse an SSE body strictly: every frame is `event: <name>` + one `data:` JSON line."""
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.read().decode()
    assert text.endswith("\n\n")
    events = []
    for frame in text[:-2].split("\n\n"):
        lines = frame.split("\n")
        assert len(lines) == 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "), frame
        name, data = lines[0][7:], json.loads(lines[1][6:])
        assert data["type"] == name  # both vendors repeat the event name in the payload
        events.append((name, data))
    return events


def stream(base: str, path: str, headers: dict, body: dict) -> list[tuple[str, dict]]:
    with httpx.stream("POST", base + path, headers=headers, json=body, timeout=10) as response:
        assert response.status_code == 200, response.read()
        return sse(response)


def valid(validator, instance) -> None:
    failure = schemas.first_error(validator, instance)
    assert failure is None, f"{schemas.dotted(failure)}: {failure.message}"


# --- Anthropic ---------------------------------------------------------------------------

BASH_SCHEMA = {
    "type": "object",
    "properties": {"i": {"type": "string"}, "command": {"type": "string"}, "cwd": {"type": "string"}},
    "required": ["command", "i"],
    "additionalProperties": False,
}


def anthropic_body(messages, **changes) -> dict:
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 4096,
        "stream": True,
        "thinking": {"type": "enabled", "budget_tokens": 1024, "display": "summarized"},
        "system": [{"type": "text", "text": "You are a coding agent."}],
        "tools": [{"name": "bash", "description": "Run a command.", "input_schema": BASH_SCHEMA}],
        "messages": messages,
    } | changes


def assemble_anthropic(events) -> dict:
    """Rebuild the Message the documented flow describes, checking the flow as it goes."""
    event_validator = schemas.anthropic_validator("RawMessageStreamEvent")
    for _, data in events:
        valid(event_validator, data)
    names = [name for name, _ in events if name != "ping"]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    message = dict(events[0][1]["message"])
    assert message["content"] == [] and message["stop_reason"] is None
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    open_index = None
    for name, data in events[1:]:
        if name == "content_block_start":
            assert open_index is None and data["index"] == len(blocks)
            open_index = data["index"]
            blocks[open_index] = dict(data["content_block"])
            partial[open_index] = ""
        elif name == "content_block_delta":
            assert data["index"] == open_index
            block, delta = blocks[open_index], data["delta"]
            kind = delta["type"]
            if kind == "text_delta":
                block["text"] += delta["text"]
            elif kind == "thinking_delta":
                block["thinking"] += delta["thinking"]
            elif kind == "signature_delta":
                block["signature"] = delta["signature"]
            else:
                assert kind == "input_json_delta" and block["type"] == "tool_use"
                partial[open_index] += delta["partial_json"]
        elif name == "content_block_stop":
            assert data["index"] == open_index
            if blocks[open_index]["type"] == "tool_use":
                blocks[open_index]["input"] = json.loads(partial[open_index])
            open_index = None
        elif name == "message_delta":
            message["stop_reason"] = data["delta"]["stop_reason"]
            message["usage"] = {**message["usage"], **data["usage"]}
    message["content"] = [blocks[index] for index in sorted(blocks)]
    return message


def test_anthropic_streams_the_documented_tool_use_then_text_flow(mock):
    first = [{"role": "user", "content": "Write the codeword file, then tell me the codeword."}]
    events = stream(mock, "/anthropic/v1/messages", ANTHROPIC_HEADERS, anthropic_body(first))
    assert ("ping", {"type": "ping"}) in events
    message = assemble_anthropic(events)
    assert message["stop_reason"] == "tool_use"
    assert [block["type"] for block in message["content"]] == ["thinking", "tool_use"]
    thinking, tool_use = message["content"]
    assert thinking["thinking"] and thinking["signature"]
    assert tool_use["name"] == "bash" and tool_use["input"]["command"] == COMMAND
    assert tool_use["id"].startswith("toolu_")
    assert message["usage"]["input_tokens"] > 0 and message["usage"]["output_tokens"] > 1

    second = first + [
        {"role": "assistant", "content": message["content"]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use["id"], "content": "(no output)"}]},
    ]
    final = assemble_anthropic(stream(mock, "/anthropic/v1/messages", ANTHROPIC_HEADERS, anthropic_body(second)))
    assert final["stop_reason"] == "end_turn"
    assert final["content"][-1] == {"type": "text", "text": f"The codeword is {CODEWORD}."}

    # Non-streamed, the same request answers with the documented Message object.
    plain = httpx.post(
        mock + "/anthropic/v1/messages", headers=ANTHROPIC_HEADERS, json=anthropic_body(second, stream=False)
    )
    assert plain.status_code == 200 and plain.headers["request-id"].startswith("req_")
    valid(schemas.anthropic_validator("Message"), plain.json())

    recorded = httpx.get(mock + "/_mock/requests", params={"vendor": "anthropic"}).json()["requests"]
    assert [entry["headers"]["x-api-key"] for entry in recorded] == [DEFAULT_KEYS["anthropic"]] * 3


@pytest.mark.parametrize(
    ("headers", "body_changes", "status", "kind", "message"),
    [
        ({"x-api-key": None}, {}, 401, "authentication_error", "x-api-key header is required"),
        ({"x-api-key": "sk-ant-wrong"}, {}, 401, "authentication_error", "invalid x-api-key"),
        ({"anthropic-version": None}, {}, 400, "invalid_request_error", "anthropic-version: header is required"),
        ({}, {"temperature": 0.5, "unknown": 1}, 400, "invalid_request_error", "unknown: Extra inputs are not permitted"),
        ({"anthropic-version": "2026-10-21"}, {"temperature": 0.5}, 400, "invalid_request_error", "temperature: Extra inputs"),
        ({}, {"context_management": {"edits": []}}, 400, "invalid_request_error", "context_management: Extra inputs"),
        ({}, {"max_tokens": 1024}, 400, "invalid_request_error", "`max_tokens` must be greater than"),
        ({}, {"model": "claude-unknown"}, 404, "not_found_error", "model: claude-unknown"),
    ],
)
def test_anthropic_rejects_what_the_docs_reject(mock, headers, body_changes, status, kind, message):
    sent = {name: value for name, value in (ANTHROPIC_HEADERS | headers).items() if value is not None}
    body = anthropic_body([{"role": "user", "content": "hi"}]) | body_changes
    response = httpx.post(mock + "/anthropic/v1/messages", headers=sent, json=body)
    assert response.status_code == status
    error = response.json()
    valid(schemas.anthropic_validator("ErrorResponse"), error)
    assert error["error"]["type"] == kind and error["error"]["message"].startswith(message), error


def test_anthropic_beta_fields_need_the_beta_header_and_signatures_are_checked(mock):
    first = [{"role": "user", "content": "go"}]
    beta = ANTHROPIC_HEADERS | {"anthropic-beta": "context-management-2025-06-27"}
    body = anthropic_body(first, context_management={"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]})
    message = assemble_anthropic(stream(mock, "/anthropic/v1/messages", beta, body))

    tampered = [dict(block) for block in message["content"]]
    tampered[0]["thinking"] += " (edited)"
    tool_id = tampered[1]["id"]
    replay = first + [
        {"role": "assistant", "content": tampered},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]},
    ]
    response = httpx.post(mock + "/anthropic/v1/messages", headers=ANTHROPIC_HEADERS, json=anthropic_body(replay))
    assert response.status_code == 400
    assert "Invalid `signature` in `thinking` block" in response.json()["error"]["message"]

    unanswered = first + [{"role": "assistant", "content": message["content"]}, {"role": "user", "content": "next"}]
    response = httpx.post(mock + "/anthropic/v1/messages", headers=ANTHROPIC_HEADERS, json=anthropic_body(unanswered))
    assert response.status_code == 400 and "without `tool_result`" in response.json()["error"]["message"]


# --- OpenAI ------------------------------------------------------------------------------

NULLABLE = {"anyOf": [{"type": "string"}, {"type": "null"}]}
BASH_FUNCTION = {
    "type": "function",
    "name": "bash",
    "description": "Run a command.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {"i": {"type": "string"}, "command": {"type": "string"}, "cwd": NULLABLE},
        "required": ["i", "command", "cwd"],
        "additionalProperties": False,
    },
}


def openai_body(items, **changes) -> dict:
    return {
        "model": "gpt-5.2",
        "instructions": "You are a coding agent.",
        "input": items,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "low", "summary": "auto"},
        "tools": [BASH_FUNCTION],
    } | changes


def assemble_openai(events) -> dict:
    event_validator = schemas.openai_component("ResponseStreamEvent")
    for _, data in events:
        valid(event_validator, data)
    assert [data["sequence_number"] for _, data in events] == list(range(len(events)))
    names = [name for name, _ in events]
    assert names[:2] == ["response.created", "response.in_progress"] and names[-1] == "response.completed"
    assert events[0][1]["response"]["status"] == "in_progress"
    items: dict[int, dict] = {}
    for name, data in events:
        if name == "response.output_item.added":
            assert data["output_index"] == len(items)
            items[data["output_index"]] = dict(data["item"], arguments="", text="")
        elif name == "response.output_text.delta":
            items[data["output_index"]]["text"] += data["delta"]
        elif name == "response.function_call_arguments.delta":
            items[data["output_index"]]["arguments"] += data["delta"]
        elif name == "response.output_item.done":
            done, built = data["item"], items[data["output_index"]]
            if done["type"] == "function_call":
                assert built["arguments"] == done["arguments"]
            if done["type"] == "message":
                assert built["text"] == done["content"][0]["text"]
    final = events[-1][1]["response"]
    valid(schemas.openai_component("Response"), final)
    assert final["status"] == "completed" and len(final["output"]) == len(items)
    usage = final["usage"]
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"] > 0
    return final


def test_openai_streams_the_documented_function_call_then_text_flow(mock):
    first = [{"role": "user", "content": [{"type": "input_text", "text": "Write the file, then the codeword."}]}]
    response = assemble_openai(stream(mock, "/openai/v1/responses", OPENAI_HEADERS, openai_body(first)))
    reasoning, call = response["output"]
    assert reasoning["type"] == "reasoning" and reasoning["encrypted_content"]
    assert call["type"] == "function_call" and call["name"] == "bash"
    # Strict function calling emits every declared property.
    assert json.loads(call["arguments"]) == {"i": "Writing the codeword file", "command": COMMAND, "cwd": None}

    # A stateless replay, as omp sends it: the reasoning item by encrypted content, no id.
    replayed = {key: reasoning[key] for key in ("type", "summary", "encrypted_content")}
    call_item = {key: call[key] for key in ("type", "call_id", "name", "arguments")}
    output = {"type": "function_call_output", "call_id": call["call_id"], "output": "(no output)"}
    second = first + [replayed, call_item, output]
    final = assemble_openai(stream(mock, "/openai/v1/responses", OPENAI_HEADERS, openai_body(second)))
    assert final["output"][-1]["content"][0]["text"] == f"The codeword is {CODEWORD}."

    plain = httpx.post(mock + "/openai/v1/responses", headers=OPENAI_HEADERS, json=openai_body(second, stream=False))
    assert plain.status_code == 200
    valid(schemas.openai_component("Response"), plain.json())

    models = httpx.get(mock + "/openai/v1/models", headers=OPENAI_HEADERS)
    valid(schemas.openai_component("ListModelsResponse"), models.json())
    assert "gpt-5.2" in [model["id"] for model in models.json()["data"]]

    recorded = httpx.get(mock + "/_mock/requests", params={"vendor": "openai"}).json()["requests"]
    assert {entry["headers"]["authorization"] for entry in recorded} == {f"Bearer {DEFAULT_KEYS['openai']}"}


@pytest.mark.parametrize(
    ("headers", "body_changes", "status", "code", "message"),
    [
        ({"authorization": None}, {}, 401, None, "You didn't provide an API key."),
        ({"authorization": "Bearer sk-proj-wrong-0000"}, {}, 401, "invalid_api_key", "Incorrect API key provided: sk-"),
        ({}, {"input": 7}, 400, "invalid_value", "Invalid value for 'input'"),
        ({}, {"unknown_field": True}, 400, "unknown_parameter", "Unknown parameter: 'unknown_field'."),
        ({}, {"model": "gpt-unknown"}, 404, "model_not_found", "The model 'gpt-unknown' does not exist"),
        ({}, {"previous_response_id": "resp_x"}, 400, "previous_response_not_found", "Previous response with id"),
    ],
)
def test_openai_rejects_what_the_spec_rejects(mock, headers, body_changes, status, code, message):
    sent = {name: value for name, value in (OPENAI_HEADERS | headers).items() if value is not None}
    body = openai_body([{"role": "user", "content": "hi"}]) | body_changes
    response = httpx.post(mock + "/openai/v1/responses", headers=sent, json=body)
    assert response.status_code == status
    error = response.json()
    valid(schemas.openai_component("ErrorResponse"), error)
    assert error["error"]["code"] == code and error["error"]["message"].startswith(message), error


def test_openai_checks_replayed_tool_outputs_and_reasoning(mock):
    orphan = [{"role": "user", "content": "hi"}, {"type": "function_call_output", "call_id": "call_x", "output": "x"}]
    response = httpx.post(mock + "/openai/v1/responses", headers=OPENAI_HEADERS, json=openai_body(orphan))
    assert response.status_code == 400 and "No tool call found" in response.json()["error"]["message"]

    forged = [{"role": "user", "content": "hi"}, {"type": "reasoning", "summary": [], "encrypted_content": "forged"}]
    response = httpx.post(mock + "/openai/v1/responses", headers=OPENAI_HEADERS, json=openai_body(forged))
    assert response.json()["error"]["code"] == "invalid_encrypted_content"

    # Stored (default) requests keep the description's rule: a reasoning item needs its id.
    stored = openai_body(forged, store=True)
    stored["input"][1]["encrypted_content"] = None
    response = httpx.post(mock + "/openai/v1/responses", headers=OPENAI_HEADERS, json=stored)
    assert response.status_code == 400


# --- the mock itself ---------------------------------------------------------------------


def test_the_mock_starts_fast_and_keeps_its_state(tmp_path):
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "scripts.mocks.models", "--port", "0", "--state", str(tmp_path), "--codeword", "X-1"],
        cwd=ROOT, stdout=subprocess.PIPE, text=True,
    )
    try:
        line = proc.stdout.readline()
        assert time.monotonic() - started < 2, "the mock must be ready within 2 s"
        base = re.search(r"http://[\d.]+:\d+", line).group(0)
        assert httpx.get(base + "/_mock/health").json() == {"ok": True}
        assert httpx.get(base + "/_mock/state").json()["script"] == default_script("X-1")
        assert httpx.put(base + "/_mock/keys", json={"openai": "sk-other"}).status_code == 200
        assert httpx.put(base + "/_mock/script", json={"steps": []}).status_code == 400
    finally:
        proc.terminate()
        proc.wait(5)
    saved = json.loads((tmp_path / "state.json").read_text())
    assert saved["keys"]["openai"] == "sk-other" and saved["script"] == default_script("X-1")


def test_vendored_files_match_sources():
    specs = ROOT / "scripts" / "mocks" / "specs"
    rows = re.findall(r"^\| `([^`]+)` \|.*\| `([0-9a-f]{64})` \|$", (specs / "SOURCES.md").read_text(), re.M)
    mine = {name: sha for name, sha in rows if name.startswith(("openai", "anthropic"))}
    assert {"openai.openapi.yaml.gz", "anthropic_messages.schema.json"} <= set(mine)
    for name, sha in mine.items():
        data = (specs / name).read_bytes()
        if name.endswith(".gz"):
            data = gzip.decompress(data)
        assert hashlib.sha256(data).hexdigest() == sha, name


# --- the real engine (opt-in) ------------------------------------------------------------

OMP = os.environ.get(
    "SYMPHONY_OMP_BIN",
    os.path.expanduser("~/Library/Application Support/Tern/omp/versions/18.20.2"),
)
ENGINE = {"anthropic": "claude-sonnet-4-5", "openai": "gpt-5.2"}
WRONG = {"anthropic": "invalid x-api-key", "openai": "Incorrect API key provided"}
real_omp = pytest.mark.skipif(os.environ.get("SYMPHONY_REAL_OMP") != "1", reason="real engine runs are opt-in")


def _run_engine(vendor, key, mock, tmp_path, monkeypatch, redis_client):
    """One runner attempt with the Admin item `{builtin: true, apiKey: key}` for `vendor`,
    staged by `prepare_provider` exactly as in production. Only the endpoint is redirected,
    and only here: Anthropic through omp's `ANTHROPIC_BASE_URL`; the built-in `openai`
    provider ignores `OPENAI_BASE_URL` in 18.20.2, so its models.yml entry gets `baseUrl`
    beside the staged `apiKey` after the production file has been checked."""
    from tests.conftest import FakeClock, FakeKV
    from tests.test_provider_runtime import Core

    from symphony import provider_runtime, state
    from symphony.config import parse_workflow
    from symphony.entry.run_issue import engine_env, engine_launch
    from symphony.omp_rpc import OmpClient
    from symphony.runner import RunnerEnv, run_attempt
    from symphony.sessions import SessionStore
    from symphony.teamconfig import TeamConfig
    from symphony.trackers.memory import MemoryTracker
    from symphony.workspace import LocalEngine, LocalSandbox

    version = subprocess.run([OMP, "--version"], capture_output=True, text=True, check=True)
    assert "18.20.2" in version.stdout + version.stderr
    agent_dir, env_file = tmp_path / "engine", tmp_path / "engine.env"
    monkeypatch.setattr(provider_runtime, "AGENT_DIR", str(agent_dir))
    monkeypatch.setattr(provider_runtime, "ENV_FILE", str(env_file))
    work = tmp_path / "ws"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    cfg = parse_workflow(
        "---\ntracker: {kind: memory}\nagent: {max_turns: 1}\n"
        f"workspace: {{root: {json.dumps(str(work))}}}\n"
        f"engine:\n  command: {json.dumps(shlex.quote(OMP) + ' --mode rpc')}\n"
        f"  provider: {vendor}\n  model: {ENGINE[vendor]}\n  thinking: low\n"
        "  read_timeout_ms: 20000\ntelemetry: {enabled: false}\n---\n"
        "Write the project codeword into codeword.txt, then tell me the codeword.\n"
    )
    core = Core({vendor: {"builtin": True, "apiKey": key}})
    sandbox = LocalSandbox(str(work), name=f"ws-mock-{vendor}")
    # Nothing ambient may supply a credential or an endpoint: only the staged files do.
    ambient = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("ANTHROPIC_", "OPENAI_", "PI_", "AWS_"))
    }
    ambient["HOME"] = str(tmp_path / "home")
    redirect = {"ANTHROPIC_BASE_URL": f"{mock}/anthropic"} if vendor == "anthropic" else {}

    def ensure(issue, bound):
        provider_runtime.prepare_provider(sandbox, core, "mock", bound)
        staged = json.loads((agent_dir / "models.yml").read_text())
        assert staged == {"providers": {vendor: {"apiKey": "SYMPHONY_PROVIDER_API_KEY"}}}
        if vendor == "openai":
            staged["providers"]["openai"]["baseUrl"] = f"{mock}/openai/v1"
            (agent_dir / "models.yml").write_text(json.dumps(staged))
        return sandbox

    def open_client(sb, token, issue, bound):
        launch = f". {shlex.quote(str(env_file))} && " + engine_launch(bound, str(tmp_path / "sessions"))
        proc = LocalEngine(
            ["bash", "-c", launch], cwd=str(work),
            env={**ambient, **redirect, **engine_env(bound, "i1", 1, token, ("acme/mock",))},
            stderr_path=str(tmp_path / "omp.stderr"),
        )
        return OmpClient(proc, proc, proc)

    kv, clock, tracker = FakeKV(), FakeClock(), MemoryTracker()
    tracker.add_issue("i1", "MOCK-1", state="Todo", labels=("repo:acme/mock",))
    kv.put(state.issue_key("i1"), {"status": "claimed", "identifier": "MOCK-1", "attempt": 1,
                                   "dispatch_seq": 1, "runs": 1, "tokens": {}, "session_path": None})
    kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "mock"})
    sessions = SessionStore(redis_client, "mock")
    env = RunnerEnv(
        kv=kv, tracker=tracker, cfg=cfg, now_ms=clock.now_ms, call_id=f"mock-{vendor}",
        ensure_sandbox=ensure, open_client=open_client, kill_agent=lambda sb: None,
        release_sandbox=lambda sb: None, wall_deadline_ms=clock.now_ms() + 600_000,
        sessions=sessions, team=TeamConfig(redis_client, "mock"),
    )
    outcome = run_attempt(env, "i1", 1, 1)
    requests = httpx.get(mock + "/_mock/requests", params={"vendor": vendor}).json()["requests"]
    return outcome, kv.get(state.issue_key("i1")), sessions.meta("i1:1"), work, requests


def _credential(vendor: str, entry: dict) -> str:
    headers = entry["headers"]
    return headers["x-api-key"] if vendor == "anthropic" else headers["authorization"].removeprefix("Bearer ")


@real_omp
@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
def test_real_omp_completes_the_scripted_turn_with_the_admin_key(vendor, mock, tmp_path, monkeypatch, redis_client):
    key = f"{DEFAULT_KEYS[vendor]}-admin-saved"
    httpx.put(mock + "/_mock/keys", json={vendor: key}).raise_for_status()
    outcome, issue, meta, work, requests = _run_engine(vendor, key, mock, tmp_path, monkeypatch, redis_client)
    assert outcome == "continuation", issue.get("failure_summary")
    assert (work / "codeword.txt").read_text() == f"{CODEWORD}\n"
    assert CODEWORD in meta["last_message"], meta["last_message"]
    turns = [entry for entry in requests if entry["path"].endswith(("/messages", "/responses"))]
    assert len(turns) >= 2 and all(entry["status"] == 200 for entry in turns)
    assert {_credential(vendor, entry) for entry in requests} == {key}
    replay = json.dumps(turns[-1]["body"])
    assert ("tool_result" if vendor == "anthropic" else "function_call_output") in replay


@real_omp
@pytest.mark.parametrize("vendor", ["anthropic", "openai"])
def test_real_omp_surfaces_the_providers_401_for_a_wrong_key(vendor, mock, tmp_path, monkeypatch, redis_client):
    wrong = f"{DEFAULT_KEYS[vendor]}-not-the-saved-one"
    outcome, issue, _, work, requests = _run_engine(vendor, wrong, mock, tmp_path, monkeypatch, redis_client)
    assert outcome == "failed"
    summary = issue["failure_summary"] or ""
    assert "401" in summary and WRONG[vendor] in summary, summary
    assert requests and all(entry["status"] == 401 for entry in requests)
    assert {_credential(vendor, entry) for entry in requests} == {wrong}
    assert not (work / "codeword.txt").exists()
