"""The run's session trace on the ticket (`sessiontrace`, internals §3): `omp --export` in the
workspace, read back through the sandbox, redacted, attached, and linked in the session
report. A real shell sandbox runs a stand-in `omp` that writes a page shaped like omp's
export (the session base64-encoded in a `session-data` script)."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest
from tests.conftest import FakeClock, FakeKV
from tests.omp_fakes import FakeEngine, runner_env

from symphony import sessiontrace, state, telemetry
from symphony.config import ConfigError, parse_workflow
from symphony.runner import run_attempt
from symphony.trackers.memory import MemoryTracker
from symphony.workspace import LocalSandbox

GITHUB_TOKEN = "ghp_" + "A1b2C3d4E5" * 3 + "F6g7H8"  # the shape the merger precheck flags
KNOWN = "acme-tracker-credential-0123456789"  # matches no pattern: redacted by value
PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKC\n-----END RSA PRIVATE KEY-----"
SESSION = {
    "header": {"type": "session", "id": "s-1", "cwd": "/workspace"},
    "entries": [
        {"type": "message", "message": {"role": "user", "content": "Fix the login page"}},
        {"type": "message", "message": {"role": "toolResult", "content": [
            {"type": "text", "text": f"GITHUB_TOKEN={GITHUB_TOKEN}\nkey={KNOWN}\n{PRIVATE_KEY}"}]}},
        {"type": "message", "message": {"role": "assistant", "content": "Done, 3 files changed"}},
    ],
    "trace": [{"name": "turn", "ms": 1200}],
}


def export_page(session: dict) -> str:
    data = base64.b64encode(json.dumps(session).encode()).decode()
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head><title>Session Export</title></head>\n<body>\n'
        f'  <script id="session-data" type="application/json">{data}</script>\n'
        '  <script defer src="https://cdn.example/export-viewer.js"></script>\n</body>\n</html>\n'
    )


def session_of(page: str) -> dict:
    m = re.search(r'<script id="session-data"[^>]*>([^<]*)</script>', page)
    return json.loads(base64.b64decode(m.group(1)))


class _Binary(str):
    """The stand-in's path, with the page it writes and the log of its calls."""

    page: Path
    calls: Path


@pytest.fixture
def omp(tmp_path):
    """A stand-in `omp` that records its arguments and writes `page` to the output path."""
    page, calls = tmp_path / "page.html", tmp_path / "omp-calls"
    page.write_text(export_page(SESSION))
    path = tmp_path / "omp"
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\n'
        f'[ "$1" = --export ] || exit 1\ncp {page} "$3"\n'
    )
    path.chmod(0o755)
    binary = _Binary(path)
    binary.page, binary.calls = page, calls
    return binary


class Tracker(MemoryTracker):
    """Records every `attach_evidence` call; `fail` makes the upload raise."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.add_issue("i1", "ENG-1")
        self.uploads: list[dict] = []
        self.fail = fail

    def attach_evidence(self, issue, **kw):
        if self.fail:
            raise RuntimeError("evidence upload failed with HTTP 403")
        self.uploads.append({"issue": issue, **kw})
        return super().attach_evidence(issue, **kw)


def attach(sandbox, tracker, omp, **kw):
    return sessiontrace.attach(sandbox, tracker, "i1", omp=str(omp), session_path="/s.jsonl",
                               run=kw.pop("run", 2), credentials=kw.pop("credentials", [KNOWN]),
                               **kw)


# -- export, read, redact, upload ------------------------------------------------------
def test_the_export_runs_in_the_sandbox_and_the_redacted_page_is_attached(tmp_path, omp):
    tracker = Tracker()
    record = attach(LocalSandbox(str(tmp_path)), tracker, omp)

    (call,) = omp.calls.read_text().splitlines()
    flag, session_path, output = call.split(" ")
    assert (flag, session_path) == ("--export", "/s.jsonl")
    assert output.endswith("/session-trace.html") and not Path(output).parent.exists(), \
        "a temporary directory, removed after the read"
    (upload,) = tracker.uploads
    assert upload["title"] == "Session trace (run 2)"
    assert upload["filename"] == "session-trace-run-2.html"
    assert (upload["kind"], upload["content_type"]) == ("report", "text/html")
    assert record == {"run": 2, "url": "memory://evidence/ENG-1/session-trace-run-2.html",
                      "redactions": 3}


def test_planted_tokens_and_the_runs_credentials_are_redacted(tmp_path, omp):
    tracker = Tracker()
    attach(LocalSandbox(str(tmp_path)), tracker, omp)
    page = tracker.uploads[0]["data"].decode()
    for secret in (GITHUB_TOKEN, KNOWN, "MIIEow", "BEGIN RSA PRIVATE KEY"):
        assert secret not in page and secret not in json.dumps(session_of(page))
    text = session_of(page)["entries"][1]["message"]["content"][0]["text"]
    assert text == "GITHUB_TOKEN=[REDACTED]\nkey=[REDACTED]\n[REDACTED]"
    assert session_of(page)["entries"][2] == SESSION["entries"][2], "the rest is untouched"
    assert 'src="https://cdn.example/export-viewer.js"' in page


def test_short_values_are_not_treated_as_credentials():
    page, count = sessiontrace.redact_export(export_page(SESSION), ["123456", "", None])
    assert count == 2  # the GitHub token and the private key block, by pattern
    assert "Done, 3 files changed" in json.dumps(session_of(page))


# The session JSON as omp's JSON.stringify writes it, which Python's json.dumps would not
# reproduce: `0.000016` (Python: 1.6e-05), a raw U+2028, a lone surrogate escape.
OMP_JSON = (
    '{"header":{"type":"session","id":"s-1"},"entries":[{"type":"message","message":'
    '{"role":"toolResult","content":[{"type":"text","text":"say \\"hi\\"\\n\u00e9\u2028\\ud800 '
    'C:\\\\tmp"}],"usage":{"cost":{"input":0.000016,"output":0.0021000000000000003}}}},'
    '{"type":"message","message":{"role":"toolResult","content":[{"type":"text","text":'
    f'"GITHUB_TOKEN={GITHUB_TOKEN} \\u00e9"}}]}}}}],"trace":[]}}'
)


def _omp_page(text: str) -> str:
    data = base64.b64encode(text.encode("utf-8")).decode()
    return f'<html><body><script id="session-data" type="application/json">{data}</script></body></html>'


def test_an_unredacted_page_round_trips_byte_for_byte():
    clean = OMP_JSON.replace(GITHUB_TOKEN, "nothing-secret")
    page = _omp_page(clean)
    assert sessiontrace.redact_export(page, ["acme-not-in-this-page-123"]) == (page, 0)


def test_redaction_changes_only_the_literal_that_held_the_secret():
    page, count = sessiontrace.redact_export(_omp_page(OMP_JSON), [])
    assert count == 1
    text = base64.b64decode(re.search(r'json">([^<]*)<', page).group(1)).decode()
    head, _, rest = OMP_JSON.partition(f'"GITHUB_TOKEN={GITHUB_TOKEN} \\u00e9"')
    assert text == head + '"GITHUB_TOKEN=[REDACTED] \\u00e9"' + rest
    assert '"input":0.000016' in text and "\u2028\\ud800" in text


def test_a_page_without_session_data_is_never_attached(tmp_path, omp):
    omp.page.write_text("<html><body>session data elsewhere</body></html>")
    tracker = Tracker()
    record = attach(LocalSandbox(str(tmp_path)), tracker, omp)
    assert record == {"run": 2,
                      "note": "trace not attached: the export has no session data to redact"}
    assert tracker.uploads == []


def test_a_trace_over_25_mb_is_skipped_with_its_size(tmp_path, omp):
    with open(omp.page, "wb") as fh:
        fh.truncate(26 * 1024 * 1024)
    tracker = Tracker()
    record = attach(LocalSandbox(str(tmp_path)), tracker, omp)
    assert record == {"run": 2, "note": "trace too large to attach: 26.0 MB"}
    assert tracker.uploads == []


def test_a_failed_export_and_a_failed_upload_are_report_lines(tmp_path, omp):
    broken = tmp_path / "broken-omp"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    assert attach(LocalSandbox(str(tmp_path)), Tracker(), broken)["note"].startswith(
        "trace not attached: the export failed")
    logged = []
    record = attach(LocalSandbox(str(tmp_path)), Tracker(fail=True), omp,
                    log=lambda event, **kw: logged.append(event))
    assert record == {"run": 2, "note": "trace not attached: the upload failed"}
    assert logged == ["session_trace_upload_failed"]


# -- the session report --------------------------------------------------------------
def test_the_session_report_links_each_runs_trace_or_says_why_not():
    entry = {"runs": 2, "tokens": {}, "traces": [
        {"run": 1, "url": "https://uploads.example/t1.html", "redactions": 1},
        {"run": 2, "note": "trace not attached: the upload failed"},
    ]}
    report = telemetry.render_session_report(entry)
    assert "- **Session trace**: https://uploads.example/t1.html (run 1, 1 redaction)" in report
    assert "- Session trace (run 2): trace not attached: the upload failed" in report


# -- the runner ----------------------------------------------------------------------
def _workflow(omp, extra=""):
    return parse_workflow(
        f"---\ntracker: {{kind: memory}}\nagent: {{max_turns: 1{extra}}}\n"
        f"engine: {{command: '{omp} --mode rpc', provider: acme, model: acme-1}}\n---\nx\n"
    )


class _Ordered(LocalSandbox):
    def __init__(self, root, order):
        super().__init__(root)
        self.order = order

    def exec_script(self, script, timeout_s=None, *, stdin=None):
        if "--export" in script:
            self.order.append("export")
        return super().exec_script(script, timeout_s, stdin=stdin)


def _run(redis_client, tmp_path, cfg, tracker, order):
    kv = FakeKV()
    kv.put(state.issue_key("i1"), {"status": "claimed", "identifier": "ENG-1", "attempt": 1,
                                   "dispatch_seq": 1, "runs": 1, "tokens": {},
                                   "failure_summary": None})
    kv.put(state.dispatch_key("i1", 1), {"daemon_owner": "d1"})
    engine = FakeEngine()
    engine.run()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = runner_env(redis_client, kv=kv, tracker=tracker, cfg=cfg, clock=FakeClock(),
                     engine=engine, sandbox=_Ordered(str(workspace), order),
                     kill_agent=lambda sb: order.append("kill"),
                     release_sandbox=lambda sb: order.append("release"),
                     credentials=lambda: [KNOWN])
    outcome = run_attempt(env, "i1", 1, 1)
    return outcome, kv.get(state.issue_key("i1"))


def test_the_runner_attaches_the_trace_after_the_engine_stops(redis_client, tmp_path, omp):
    order: list[str] = []
    tracker = Tracker()
    outcome, entry = _run(redis_client, tmp_path, _workflow(omp), tracker, order)
    assert outcome == "continuation"
    assert order == ["kill", "export", "release"]
    assert entry["traces"] == [{"run": 1, "url": "memory://evidence/ENG-1/session-trace-run-1.html",
                                "redactions": 3}]
    assert "**Session trace**: memory://evidence/ENG-1/session-trace-run-1.html" in \
        telemetry.render_session_report(entry)


def test_a_failed_upload_leaves_the_run_and_its_report_intact(redis_client, tmp_path, omp):
    outcome, entry = _run(redis_client, tmp_path, _workflow(omp), Tracker(fail=True), [])
    assert outcome == "continuation"
    assert entry["traces"] == [{"run": 1, "note": "trace not attached: the upload failed"}]
    assert "Runs: **1**" in telemetry.render_session_report(entry)


def test_agent_attach_trace_false_exports_nothing(redis_client, tmp_path, omp):
    order: list[str] = []
    tracker = Tracker()
    cfg = _workflow(omp, ", attach_trace: false")
    assert cfg.attach_trace is False
    outcome, entry = _run(redis_client, tmp_path, cfg, tracker, order)
    assert outcome == "continuation" and order == ["kill", "release"]
    assert "traces" not in entry and tracker.uploads == [] and not omp.calls.exists()


def test_attach_trace_is_a_boolean_defaulting_on(omp):
    assert _workflow(omp).attach_trace is True
    with pytest.raises(ConfigError, match="attach_trace"):
        _workflow(omp, ", attach_trace: 'no'")
