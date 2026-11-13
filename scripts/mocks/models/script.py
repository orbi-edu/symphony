"""The scripted conversation and the mock's persistent state.

A script is an ordered list of steps; the step a request gets is the number of tool results
already in its conversation (clamped to the last step), so a retried or replayed request
always gets the same answer. A step is `{"text": …}`, `{"tool": {"name", "input"}}` or both
(text first). The default script is the certification's two-step turn: write a file with the
codeword through omp's `bash` tool, then answer with the codeword.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

DEFAULT_CODEWORD = "MOCK-CODEWORD-4400"
DEFAULT_KEYS = {"anthropic": "sk-ant-api03-mock-0000", "openai": "sk-proj-mock-0000"}
DEFAULT_MODELS = {
    "anthropic": ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"],
    "openai": ["gpt-5.2", "gpt-5.2-codex", "gpt-5.1", "gpt-5-mini"],
}
VENDORS = tuple(DEFAULT_KEYS)
# Signs thinking/reasoning blocks so the mock can tell its own from altered ones, as the
# vendors do; fixed, so a restarted mock still accepts a conversation it started.
_SECRET = b"symphony-models-mock"


@dataclass(frozen=True)
class Step:
    text: str | None = None
    tool: str | None = None
    input: dict | None = None


@dataclass
class Reply:
    """A vendor answer: a JSON body, or SSE `events` as (event name, data) pairs."""

    status: int
    body: dict | None = None
    events: list[tuple[str, dict]] | None = None
    headers: dict[str, str] = field(default_factory=dict)


class ScriptError(Exception):
    """The script asks for something this request does not allow (a tool it did not offer,
    or input outside the tool's declared schema): a broken certification, not a model."""


def tool_input(step: Step, declared: dict[str, dict]) -> dict:
    """The step's tool input, checked against the schema the request declared for it."""
    if step.tool not in declared:
        raise ScriptError(f"mock script: tool {step.tool!r} is not among the request's tools")
    error = best_match(Draft202012Validator(declared[step.tool]).iter_errors(step.input))
    if error is not None:
        raise ScriptError(f"mock script: input for {step.tool!r} breaks its schema: {error.message}")
    return step.input


def pieces(text: str, size: int) -> list[str]:
    """Deterministic stream deltas."""
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def tokens(raw) -> int:
    """A deterministic token count: one per four bytes, at least one."""
    size = len(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
    return max(1, size // 4)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def default_script(codeword: str = DEFAULT_CODEWORD) -> dict:
    return {
        "steps": [
            {
                "tool": {
                    "name": "bash",
                    "input": {
                        "i": "Writing the codeword file",
                        "command": f"printf '%s\\n' {codeword} > codeword.txt",
                    },
                }
            },
            {"text": f"The codeword is {codeword}."},
        ]
    }


def parse_script(document) -> list[Step]:
    if not isinstance(document, dict) or not isinstance(document.get("steps"), list):
        raise ValueError("script must be {steps: [...]}")
    steps = []
    for raw in document["steps"]:
        if not isinstance(raw, dict) or not raw or set(raw) - {"text", "tool"}:
            raise ValueError("each step has `text`, `tool` or both")
        text, tool = raw.get("text"), raw.get("tool")
        if text is not None and not isinstance(text, str):
            raise ValueError("step text must be a string")
        if tool is not None and (
            not isinstance(tool, dict)
            or not isinstance(tool.get("name"), str)
            or not isinstance(tool.get("input", {}), dict)
        ):
            raise ValueError("step tool must be {name, input}")
        steps.append(
            Step(text, tool and tool["name"], tool and dict(tool.get("input", {})))
        )
    if not steps:
        raise ValueError("script needs at least one step")
    return steps


def sign(text: str) -> str:
    return base64.b64encode(hmac.new(_SECRET, text.encode(), hashlib.sha256).digest()).decode()


def signed(text: str, signature) -> bool:
    return isinstance(signature, str) and hmac.compare_digest(sign(text), signature)


class MockState:
    """Keys, script and served models (persisted in `state.json`) and the request log
    (`requests.jsonl`, every request with its headers, credentials included: these are
    the mock's own test keys, and certification reads them back)."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self.lock = threading.Lock()
        self.keys = dict(DEFAULT_KEYS)
        self.models = {vendor: list(ids) for vendor, ids in DEFAULT_MODELS.items()}
        self.script = default_script()
        self.requests: list[dict] = []
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            saved = directory / "state.json"
            if saved.exists():
                document = json.loads(saved.read_text())
                self.keys.update(document.get("keys", {}))
                self.models.update(document.get("models", {}))
                self.script = document.get("script", self.script)
            log = directory / "requests.jsonl"
            if log.exists():
                self.requests = [json.loads(line) for line in log.read_text().splitlines() if line]
        self.steps = parse_script(self.script)

    def save(self) -> None:
        if self.directory is not None:
            document = {"keys": self.keys, "models": self.models, "script": self.script}
            (self.directory / "state.json").write_text(json.dumps(document, indent=2))

    def configure(self, *, keys=None, models=None, script=None) -> None:
        with self.lock:
            if keys is not None:
                if not isinstance(keys, dict) or not all(
                    vendor in VENDORS and isinstance(key, str) and key for vendor, key in keys.items()
                ):
                    raise ValueError("keys must map anthropic/openai to non-empty strings")
                self.keys.update(keys)
            if models is not None:
                if not isinstance(models, dict) or not all(
                    vendor in VENDORS and isinstance(ids, list) and all(isinstance(i, str) for i in ids)
                    for vendor, ids in models.items()
                ):
                    raise ValueError("models must map anthropic/openai to lists of ids")
                self.models.update({vendor: list(ids) for vendor, ids in models.items()})
            if script is not None:
                self.steps = parse_script(script)
                self.script = script
            self.save()

    def step(self, tool_results: int) -> Step:
        with self.lock:
            return self.steps[min(tool_results, len(self.steps) - 1)]

    def record(self, entry: dict) -> None:
        with self.lock:
            entry = {"seq": len(self.requests) + 1, **entry}
            self.requests.append(entry)
            if self.directory is not None:
                with (self.directory / "requests.jsonl").open("a") as log:
                    log.write(json.dumps(entry) + "\n")

    def reset_requests(self) -> None:
        with self.lock:
            self.requests = []
            if self.directory is not None:
                (self.directory / "requests.jsonl").unlink(missing_ok=True)
