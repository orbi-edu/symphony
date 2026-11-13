"""GitHub App installation credentials (FR-016..FR-018, SC-004).

Contract: specs/002-pi-agent-github-app/contracts/github-credentials.md

Pure: assertion construction, expiry arithmetic and the refresh decision are tested with
an injected clock and a fake exchange. No network.
"""

import os
import subprocess

import pytest
from tests.conftest import FakeClock

from symphony import state
from symphony.ghapp import (
    REFRESH_MARGIN_MS,
    CredentialError,
    InstallationCredential,
    build_assertion,
    credential_helper_script,
)
from symphony.workspace import CREDENTIAL_DIR, TOKEN_FILE, install_credential_helper

# A throwaway key generated for this test only; never used against a real installation.
TEST_KEY = None


def _key():
    global TEST_KEY
    if TEST_KEY is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        TEST_KEY = pk.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    return TEST_KEY


def _cred(clock, exchange=None, **kw):
    return InstallationCredential(
        app_id="12345",
        private_key=_key(),
        installation_id="99",
        now_ms=clock.now_ms,
        exchange=exchange or (lambda assertion, inst, repos, body: {"token": "ghs_x", "expires_at_ms": clock.now_ms() + 3_600_000}),
        **kw,
    )


# -- assertion (T031) ------------------------------------------------------
def test_assertion_carries_issuer_and_is_backdated_for_clock_skew():
    import jwt

    now_s = 1_700_000_000
    token = build_assertion("12345", _key(), now_s)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "12345"
    assert claims["iat"] < now_s, "iat must be backdated to tolerate clock skew"
    assert claims["exp"] > now_s
    assert claims["exp"] - claims["iat"] <= 600 + 60  # inside the accepted maximum


def test_assertion_is_signed_rs256():
    import jwt

    header = jwt.get_unverified_header(build_assertion("1", _key(), 1_700_000_000))
    assert header["alg"] == "RS256"


def test_expires_at_from_the_response_wins_over_any_assumed_lifetime():
    """Never assume the nominal hour — the API is the authority."""
    clock = FakeClock()
    short = clock.now_ms() + 60_000
    cred = _cred(clock, exchange=lambda a, i, r, b: {"token": "ghs_x", "expires_at_ms": short})
    cred.token()
    assert cred.expires_at_ms == short
    assert cred.needs_refresh() is True  # 60s left is inside the margin


# -- refresh arithmetic (T032) ---------------------------------------------
def test_refresh_fires_before_expiry_with_margin_and_not_earlier():
    clock = FakeClock()
    cred = _cred(clock)
    cred.token()
    assert cred.needs_refresh() is False
    clock.advance(3_600_000 - REFRESH_MARGIN_MS - 1_000)
    assert cred.needs_refresh() is False  # still outside the margin
    clock.advance(2_000)
    assert cred.needs_refresh() is True


def test_margin_leaves_room_for_a_failed_mint_and_a_retry():
    """FR-018's whole point: the failure must surface while there is still time to act,
    not as a push rejection after the credential has already died."""
    assert REFRESH_MARGIN_MS >= 5 * 60 * 1000


def test_four_hour_attempt_against_one_hour_tokens_refreshes_as_expected():
    clock = FakeClock()
    mints = []

    def exchange(assertion, inst, repos, body):
        mints.append(clock.now_ms())
        return {"token": f"ghs_{len(mints)}", "expires_at_ms": clock.now_ms() + 3_600_000}

    cred = _cred(clock, exchange=exchange)
    cred.token()
    for _ in range(4 * 60):  # four hours, one check per simulated minute
        clock.advance(60_000)
        if cred.needs_refresh():
            cred.token(force_refresh=True)
    assert 4 <= len(mints) <= 6, f"expected ~one mint per hour plus margin, got {len(mints)}"


def test_token_is_reused_while_valid():
    clock = FakeClock()
    calls = []
    cred = _cred(clock, exchange=lambda a, i, r, b: (calls.append(1), {"token": "ghs_x", "expires_at_ms": clock.now_ms() + 3_600_000})[1])
    assert cred.token() == cred.token() == "ghs_x"
    assert len(calls) == 1


# -- failure paths (T033) --------------------------------------------------
def test_mint_failure_raises_a_distinct_diagnosable_error():
    clock = FakeClock()

    def boom(assertion, inst, repos, body):
        raise RuntimeError("404 installation not found")

    cred = _cred(clock, exchange=boom)
    with pytest.raises(CredentialError) as exc:
        cred.token()
    assert "installation" in str(exc.value)


def test_refresh_exhaustion_is_distinct_from_a_first_mint_failure():
    clock = FakeClock()
    calls = {"n": 0}

    def flaky(assertion, inst, repos, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"token": "ghs_x", "expires_at_ms": clock.now_ms() + 3_600_000}
        raise RuntimeError("503")

    cred = _cred(clock, exchange=flaky)
    cred.token()
    clock.advance(3_600_000)
    with pytest.raises(CredentialError) as exc:
        cred.token(force_refresh=True)
    assert "renew" in str(exc.value).lower()


def test_missing_configuration_fails_before_any_network_call():
    with pytest.raises(CredentialError):
        InstallationCredential.from_env({}, lambda: 0).token()


def test_token_never_reaches_shared_state():
    """The Dict is multi-writer shared state, not a credential store."""
    from tests.conftest import FakeKV

    kv = FakeKV()
    clock = FakeClock()
    cred = _cred(clock)
    secret = cred.token()
    kv.put(state.issue_key("i1"), {"bridge_token": "unrelated", "status": "running"})
    assert secret not in repr(kv.data)


# -- credential helper delivery (T034) -------------------------------------
class FakeSandbox:
    name = "ws-1"

    def __init__(self):
        self.scripts = []

    def exec_script(self, script, timeout_s=None):
        self.scripts.append(script)
        return 0, ""


def test_helper_script_emits_gits_credential_format():
    script = credential_helper_script()
    assert "username=x-access-token" in script
    assert "password=" in script
    assert TOKEN_FILE in script


def test_install_writes_the_token_to_a_file_not_into_a_url():
    """Embedding the credential in the remote — the previous approach — persists it in
    .git/config and ties its lifetime to clone time. A helper decouples both."""
    sb = FakeSandbox()
    install_credential_helper(sb, "ghs_secret")
    joined = "\n".join(sb.scripts)
    assert TOKEN_FILE in joined
    assert "credential.helper" in joined
    assert "https://x-access-token:" not in joined  # never in a URL


def test_refresh_only_rewrites_the_token_file():
    """No git surgery on refresh, so nothing races with an in-flight operation."""
    sb = FakeSandbox()
    install_credential_helper(sb, "ghs_1")
    first = len(sb.scripts)
    install_credential_helper(sb, "ghs_2", helper_installed=True)
    assert len(sb.scripts) == first + 1
    assert "credential.helper" not in sb.scripts[-1]


def test_a_system_wide_helper_is_never_asked_before_ours(tmp_path):
    """Homebrew's git ships `credential.helper=osxkeychain` system-wide, and git asks system
    helpers before global ones: without the reset it would send the keychain's password for
    the host and store the attempt's token there. Real git, real config files."""
    sb = FakeSandbox()
    install_credential_helper(sb, "ghs_secret")
    script = "\n".join(sb.scripts).replace(CREDENTIAL_DIR, str(tmp_path / "cred"))
    marker = tmp_path / "system-helper-asked"
    system = tmp_path / "gitconfig-system"
    system.write_text(f"[credential]\n\thelper = \"!touch {marker}; echo password=from-keychain; true\"\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_SYSTEM": str(system),
           "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig-global"), "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["bash", "-c", script], env=env, check=True)
    filled = subprocess.run(["git", "credential", "fill"], input="url=https://git.example/acme/app.git\n\n",
                            env=env, capture_output=True, text=True, check=True).stdout
    assert "password=ghs_secret" in filled.splitlines()
    assert not marker.exists()


# -- repository-scoped credentials -----------------------------------------
def test_mint_request_carries_the_issues_repository_and_no_other():
    clock = FakeClock()
    seen = []

    def exchange(assertion, inst, repos, body):
        seen.append(body)
        return {"token": "ghs_x", "expires_at_ms": clock.now_ms() + 3_600_000}

    cred = _cred(clock, exchange=exchange, target_repos=("acme/backend",))
    cred.token()
    assert seen == [{"repositories": ["backend"]}]


def test_a_multi_repo_ticket_gets_one_token_scoped_to_all_its_repositories():
    """A ticket spanning repositories pushes to each of them, so the token must cover
    exactly that set — every one of them, and nothing else on the installation."""
    clock = FakeClock()
    seen = []

    def exchange(assertion, inst, repos, body):
        seen.append((repos, body))
        return {"token": "ghs_x", "expires_at_ms": clock.now_ms() + 3_600_000}

    cred = _cred(clock, exchange=exchange, target_repos=("acme/api", "Acme/Web", "acme/api"))
    cred.token()
    assert seen == [(("acme/api", "acme/web"), {"repositories": ["api", "web"]})]


def test_renewal_carries_the_same_repository_scope():
    """A renewal that silently widened scope would defeat the whole point — and
    renewals happen unattended, hours into an attempt."""
    clock = FakeClock()
    seen = []

    def exchange(assertion, inst, repos, body):
        seen.append(body)
        return {"token": f"ghs_{len(seen)}", "expires_at_ms": clock.now_ms() + 3_600_000}

    cred = _cred(clock, exchange=exchange, target_repos=("acme/api", "acme/web"))
    cred.token()
    clock.advance(3_600_000)
    cred.token(force_refresh=True)
    assert seen == [{"repositories": ["api", "web"]}] * 2


def test_a_cross_owner_scope_is_refused_before_any_mint():
    """One installation belongs to one account: no request could cover both owners, and
    an unscoped token would be wider than the ticket."""
    from symphony.ghapp import NotAuthorizedForRepo

    clock = FakeClock()
    calls = []
    cred = _cred(clock, exchange=lambda *a: calls.append(a), target_repos=("acme/api", "other/web"))
    with pytest.raises(NotAuthorizedForRepo):
        cred.token()
    assert calls == []


def test_unauthorized_repository_is_its_own_reason():
    """Distinct from a configuration failure: one means "install the application on that
    repository", the other means "the application's credentials are wrong"."""
    from symphony.ghapp import NOT_AUTHORIZED_REASON, NotAuthorizedForRepo

    clock = FakeClock()

    def refuse(assertion, inst, repos, body):
        raise NotAuthorizedForRepo(", ".join(repos))

    cred = _cred(clock, exchange=refuse, target_repos=("acme/nope",))
    with pytest.raises(NotAuthorizedForRepo) as exc:
        cred.token()
    assert exc.value.reason == NOT_AUTHORIZED_REASON
    assert "acme/nope" in str(exc.value)
    assert isinstance(exc.value, CredentialError)  # still a credential failure


# -- permission-scoped credentials (internals §11) ----------------------------
def _recording(clock, seen):
    def exchange(assertion, inst, repos, body):
        seen.append(body)
        return {"token": f"ghs_{len(seen)}", "expires_at_ms": clock.now_ms() + 3_600_000}

    return exchange


def test_read_only_is_exactly_read_on_code_pull_requests_and_metadata():
    from symphony.ghapp import READ_ONLY

    assert READ_ONLY == {"contents": "read", "pull_requests": "read", "metadata": "read"}


def test_the_workspace_scope_can_ship_a_pull_request_but_never_touch_tickets():
    from symphony.ghapp import WORKSPACE

    assert WORKSPACE == {
        "contents": "write", "pull_requests": "write", "metadata": "read",
        "checks": "read", "actions": "read", "statuses": "read",
    }
    # tickets, boards and CI definitions stay out of agent reach
    assert not {"issues", "repository_projects", "organization_projects", "workflows"} & set(WORKSPACE)


def test_a_workspace_token_requests_its_explicit_scope_on_every_renewal():
    from symphony.ghapp import WORKSPACE

    clock = FakeClock()
    seen = []
    cred = InstallationCredential.from_env(
        {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": _key(), "GITHUB_APP_INSTALLATION_ID": "9"},
        clock.now_ms, target_repos=("acme/api", "acme/web"), permissions=WORKSPACE,
    )
    cred._exchange = _recording(clock, seen)
    cred.token()
    clock.advance(3_600_000)
    cred.token(force_refresh=True)
    expected = {"repositories": ["api", "web"], "permissions": WORKSPACE}
    assert seen == [expected, expected]


def test_an_investigation_token_requests_read_only_on_mint_and_on_every_renewal():
    """An investigation delivers a report, so its token must not be able to push or open a
    pull request — and a renewal hours later must not quietly restore write access."""
    from symphony.ghapp import READ_ONLY

    clock = FakeClock()
    seen = []
    cred = InstallationCredential.from_env(
        {"GITHUB_APP_ID": "1", "GITHUB_APP_PRIVATE_KEY": _key(), "GITHUB_APP_INSTALLATION_ID": "9"},
        clock.now_ms, target_repos=("acme/api",), permissions=READ_ONLY,
    )
    cred._exchange = _recording(clock, seen)
    cred.token()
    clock.advance(3_600_000)
    cred.token(force_refresh=True)
    expected = {"repositories": ["api"], "permissions": READ_ONLY}
    assert seen == [expected, expected]
    assert seen[0]["permissions"] is not READ_ONLY  # the shared constant is never handed out


def test_without_permissions_the_request_keeps_the_installation_scope():
    clock = FakeClock()
    seen = []
    _cred(clock, exchange=_recording(clock, seen), target_repos=("acme/api",)).token()
    assert seen == [{"repositories": ["api"]}]
    assert "permissions" not in seen[0]
