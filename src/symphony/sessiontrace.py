"""The run's session trace on the ticket (internals §3): at the end of a run the runner has
omp export the session file to its self-contained HTML viewer (`omp --export <session>
<out>`: the transcript and the trace timeline; the page loads only the viewer code from a
CDN), reads it back through the sandbox, redacts it and attaches it to the ticket. The
session report links it.

omp does not redact the export, so this module does, before anything leaves the runner:
every match of the merger precheck's secret patterns (`merger.rules.SECRETS`) and every
credential value the run holds (tracker key, App tokens, gateway tokens) becomes
`[REDACTED]`. The session sits base64-encoded in the page's `session-data` script; it is
decoded and redacted string by string, so a match never breaks the JSON. A page without
that element is not attached: redaction could not be checked.

Nothing here fails a run: every outcome is a record for the session report.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from collections.abc import Callable, Iterable

from .hosttools import EVIDENCE_MAX_BYTES, FileTooLarge, read_sandbox_file
from .merger.rules import SECRETS
from .trackers.base import TrackerAdapter
from .workspace import SandboxLike

REDACTED = "[REDACTED]"
# Shorter values are ids (a GitHub App id, an installation id), not secrets, and would
# blank ordinary numbers all over the transcript.
MIN_CREDENTIAL_CHARS = 12
TRACE_MAX_BYTES = EVIDENCE_MAX_BYTES  # the tracker's attachment limit
_EXPORT_FILE = "session-trace.html"
_PAYLOAD = re.compile(r'(<script id="session-data"[^>]*>)([A-Za-z0-9+/=\s]*)(</script>)')


class TraceError(Exception):
    """The trace cannot be attached; the message is the session report's line."""


class Redactor:
    """Replaces secrets with `[REDACTED]`, counting every replacement."""

    def __init__(self, credentials: Iterable[str | None]) -> None:
        values = sorted(
            {v for v in credentials if isinstance(v, str) and len(v.strip()) >= MIN_CREDENTIAL_CHARS},
            key=len, reverse=True,
        )
        self._known = re.compile("|".join(map(re.escape, values))) if values else None
        self.count = 0

    def text(self, value: str) -> str:
        if self._known is not None:
            value, n = self._known.subn(REDACTED, value)
            self.count += n
        for _, pattern in SECRETS:
            value, n = pattern.subn(REDACTED, value)
            self.count += n
        return value

    def walk(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.walk(item) for item in value]
        if isinstance(value, dict):
            return {key: self.walk(item) for key, item in value.items()}
        return value


def redact_export(html: str, credentials: Iterable[str | None]) -> tuple[str, int]:
    """The export with every secret replaced, and the number of replacements."""
    found = _PAYLOAD.search(html)
    if found is None:
        raise TraceError("trace not attached: the export has no session data to redact")
    try:
        session = json.loads(base64.b64decode(found.group(2)))
    except (binascii.Error, ValueError) as exc:
        raise TraceError("trace not attached: the export's session data is unreadable") from exc
    redactor = Redactor(credentials)
    session = redactor.walk(session)
    encoded = base64.b64encode(
        json.dumps(session, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    page = (redactor.text(html[: found.start(2)]) + encoded + redactor.text(html[found.end(2):]))
    return page, redactor.count


def export(sandbox: SandboxLike, omp: str, session_path: str) -> str:
    """Run `omp --export` in the sandbox into a fresh temporary directory; returns it."""
    script = (
        "d=$(mktemp -d) || exit 2\n"
        f'{shlex.quote(omp)} --export {shlex.quote(session_path)} "$d/{_EXPORT_FILE}" '
        '>/dev/null 2>&1 || { echo "symphony:export-failed"; rm -rf -- "$d"; exit 3; }\n'
        'echo "symphony:dir $d"\n'
    )
    code, out = sandbox.exec_script(script, timeout_s=300)
    directory = next((line.split(" ", 1)[1].strip() for line in out.splitlines()
                      if line.startswith("symphony:dir ")), None)
    if code != 0 or not directory:
        raise TraceError(f"trace not attached: the export failed (exit {code})")
    return directory


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def attach(
    sandbox: SandboxLike,
    tracker: TrackerAdapter,
    issue_id: str,
    *,
    omp: str,
    session_path: str,
    run: int,
    credentials: Iterable[str | None],
    log: Callable[..., None] = lambda event, **kw: None,
) -> dict:
    """Export, redact and attach run `run`'s trace. Returns the session report's record:
    `{run, url, redactions}` when attached, `{run, note}` otherwise. Never raises."""
    try:
        directory = export(sandbox, omp, session_path)
        try:
            data = read_sandbox_file(sandbox, directory, _EXPORT_FILE, TRACE_MAX_BYTES)
        finally:
            sandbox.exec_script(f"rm -rf -- {shlex.quote(directory)}", timeout_s=60)
        page, redactions = redact_export(data.decode("utf-8"), credentials)
        body = page.encode("utf-8")
        if len(body) > TRACE_MAX_BYTES:
            raise FileTooLarge(_EXPORT_FILE, len(body), TRACE_MAX_BYTES)
    except FileTooLarge as exc:
        log("session_trace_too_large", run=run, size=exc.size)
        return {"run": run, "note": f"trace too large to attach: {_mb(exc.size)}"}
    except TraceError as exc:
        log("session_trace_failed", run=run, error=str(exc))
        return {"run": run, "note": str(exc)}
    except Exception as exc:  # noqa: BLE001 — the trace is advice; the run and report stand
        log("session_trace_failed", run=run, error=str(exc)[:300])
        return {"run": run, "note": "trace not attached: the export could not be read"}
    try:
        attached = tracker.attach_evidence(
            issue_id, title=f"Session trace (run {run})", kind="report",
            filename=f"session-trace-run-{run}.html", content_type="text/html", data=body,
        )
    except Exception as exc:  # noqa: BLE001 — same: a failed upload is one report line
        log("session_trace_upload_failed", run=run, error=str(exc)[:300])
        return {"run": run, "note": "trace not attached: the upload failed"}
    return {"run": run, "url": attached["url"], "redactions": redactions}
