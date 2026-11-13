"""FR-014/FR-015: deterministic workspace_key; per-hook semantics; confirmed pkill."""

import subprocess

import pytest

from symphony.config import parse_workflow
from symphony.workspace import (
    AGENT_PROCESS_PATTERN,
    HookFatal,
    run_hook,
    sandbox_name,
    workspace_key,
)


class FakeSandbox:
    name = "ws-test"

    def __init__(self, results=None):
        self.results = results or {}
        self.scripts: list[str] = []

    def exec_script(self, script, timeout_s=None):
        self.scripts.append(script)
        for needle, result in self.results.items():
            if needle in script:
                return result
        return (0, "")


def test_workspace_key_unchanged_when_safe():
    assert workspace_key("ENG-123") == "ENG-123"


def test_sandbox_name_is_dns_1123_safe():
    """Pod names reject what the upstream workspace_key charset allows (uppercase, '_',
    '.'), so the pod name folds the key — deterministically, hash-suffixed on ANY
    alteration, same discipline as workspace_key itself."""
    import re

    from symphony.workspace import dns_name

    # already-safe input passes through untouched
    assert dns_name("abc-123") == "abc-123"
    # folding appends a 64-bit hash of the ORIGINAL, so case variants never collide
    assert sandbox_name("ENG-123") != sandbox_name("eng-123")
    assert sandbox_name("ENG-123") == sandbox_name("ENG-123")
    for raw in ("ENG-123", "a_b.c", "Ação 42/x"):
        name = sandbox_name(raw)
        assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name), name
        assert len(name) <= 63


def test_workspace_key_sanitization_appends_64bit_hash_deterministically():
    key1 = workspace_key("ENG 123/á")
    key2 = workspace_key("ENG 123/á")
    assert key1 == key2  # deterministic, never by runtime collision
    base, _, suffix = key1.rpartition("-")
    assert len(suffix) == 16  # 8 bytes = 64 bits in hex
    assert all(c.isalnum() or c in "._-" for c in key1)


def test_sanitized_distinct_identifiers_do_not_collide():
    assert workspace_key("ENG 1") != workspace_key("ENG-1")
    assert workspace_key("a/b") != workspace_key("a b")


CFG = parse_workflow(
    "---\ntracker: {kind: memory}\nhooks:\n  after_create: 'setup'\n  before_run: 'pre'\n"
    "  after_run: 'post'\n  before_remove: 'cleanup'\n---\nx"
)


def test_fatal_hooks_raise_on_failure():
    for hook in ("after_create", "before_run"):
        sb = FakeSandbox(results={CFG.hooks[hook]: (1, "boom")})
        with pytest.raises(HookFatal):
            run_hook(sb, hook, CFG)


def test_nonfatal_hooks_logged_and_ignored():
    logged = []
    for hook in ("after_run", "before_remove"):
        sb = FakeSandbox(results={CFG.hooks[hook]: (2, "meh")})
        ok = run_hook(sb, hook, CFG, log=lambda e, **kw: logged.append((e, kw)))
        assert ok is False  # failed but did NOT raise — never blocks cleanup
    assert len(logged) == 2


def test_missing_hook_is_noop():
    cfg = parse_workflow("---\ntracker: {kind: memory}\n---\nx")
    sb = FakeSandbox()
    assert run_hook(sb, "after_create", cfg) is True
    assert sb.scripts == []


@pytest.mark.parametrize(
    ("command", "matches"),
    [
        ("/usr/local/bin/omp --mode rpc --session-dir /omp-sessions/sessions", True),
        ("omp --mode rpc", True),
        ("bun /opt/omp/src/cli.ts --mode rpc", True),
        ("bun run /opt/omp/src/cli.ts --mode rpc --model test", True),
        ("omp --print 'hello'", False),
        ("bash -lc 'omp --mode rpc'", False),
        ("pgrep -f -- '--mode rpc'", False),
        ("node unrelated-server.js --mode rpc", False),
        ("omp --mode rpc-other", False),
    ],
)
def test_rpc_process_matching_excludes_cleanup_shells(command, matches):
    """Use the shell's POSIX ERE matcher, the same regex dialect as pkill/pgrep."""
    result = subprocess.run(
        ["bash", "-c", '[[ "$1" =~ $2 ]]', "match", command, AGENT_PROCESS_PATTERN],
        capture_output=True,
        env={},
    )
    assert result.returncode == (0 if matches else 1)


# -- workspace / repository agreement --------------------------------------
# A sandbox outlives the attempt that built it, and the clone lives in `after_create`,
# which only runs on creation. Re-pointing a ticket at another repository therefore
# leaves a workspace holding the previous one — with a credential correctly scoped to the
# new one, so git reports "Repository not found" for a repository that exists.


class _FakeSandbox:
    name = "ws-X"

    def __init__(self, remote: str | None, code: int = 0):
        self._remote, self._code = remote, code
        self.scripts: list[str] = []

    def exec_script(self, script, timeout_s=None):
        self.scripts.append(script)
        return self._code, (self._remote or "")


def test_matching_remote_keeps_the_workspace():
    from symphony.workspace import workspace_matches_repo

    sb = _FakeSandbox("https://github.com/acme/backend.git\n")
    assert workspace_matches_repo(sb, "/workspace", "acme/backend")


def test_a_different_repository_is_detected():
    from symphony.workspace import workspace_matches_repo

    sb = _FakeSandbox("https://github.com/acme/other.git\n")
    assert not workspace_matches_repo(sb, "/workspace", "acme/backend")


def test_remote_spelling_variants_still_match():
    """The label is lower-cased by `repos.parse_designation`, while the remote keeps
    whatever git recorded — suffix, trailing slash and case must not force a needless
    (and expensive) sandbox rebuild."""
    from symphony.workspace import workspace_matches_repo

    for remote in (
        "https://github.com/Acme/Backend.git",
        "https://github.com/acme/backend",
        "https://github.com/acme/backend/",
        "git@github.com:acme/backend.git",
    ):
        assert workspace_matches_repo(_FakeSandbox(remote + "\n"), "/workspace", "acme/backend"), (
            remote
        )


def test_an_unreadable_workspace_is_treated_as_a_mismatch():
    """No workspace, or no remote, means nothing can be trusted about it — recreating is
    cheap and certain, while guessing is neither."""
    from symphony.workspace import workspace_matches_repo

    assert not workspace_matches_repo(_FakeSandbox("", code=1), "/workspace", "acme/backend")
    assert not workspace_matches_repo(_FakeSandbox(""), "/workspace", "acme/backend")
