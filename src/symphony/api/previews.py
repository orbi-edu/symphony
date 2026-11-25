"""Preview links in the browser: open a PR build in Seiji from the PR (`preview_links`).

- `GET /v1/previews/open?repo=&pr=&sha=[&sig=]` → 302 to a fresh Seiji control viewer when the
  build runs on a device; otherwise it starts one (`PreviewManager.open_link`) and answers an
  HTML page that polls `status` every 2 s and reloads this URL when the device is ready. A
  reload after the viewer ticket expired mints a new one for the same lease. Each open is
  audited as `preview.opened` (actor: the person's GitHub login, target `repo#pr@sha12`).
- `GET /v1/previews/status?repo=&pr=&sha=[&sig=]` → `{state, step?, message}` with the same
  checks (`state`: ready, starting, error, stopped, refused).

No API token: a browser follows the link from the PR. With `api.access` the Cloudflare Access
JWT is verified (`access.AccessVerifier`, 401 when missing or refused) and its e-mail must be
mapped in `notify.users` (403); `sig` is optional there, but a present one must match when
the API holds the secret (403). Without Access, `previews.public_links: true` makes the signed
link the credential: the secret (503) and a matching `sig` (403) are required. Otherwise the
route refuses (403). Pages are `no-store` and send no referrer.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import threading
from collections.abc import Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import preview_links
from ..access import HEADER, AccessError, AccessVerifier
from ..config import Config
from ..previews import PreviewError, PreviewGone
from .backup import mounted_workflow
from .control import Control

log = logging.getLogger("symphony.api")

ACTION = "preview.opened"
PUBLIC_ACTOR = "public-link"
POLL_S = 2
_ACTOR_CHARS = re.compile(r"[^A-Za-z0-9 ._@-]")
_STEPS = {
    "queued": "Getting ready\u2026",
    "downloading": "Downloading build\u2026",
    "installing": "Installing\u2026 (the simulator boots first: up to two minutes)",
    "launching": "Launching\u2026",
}
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
             "X-Content-Type-Options": "nosniff"}


class _Refused(Exception):
    def __init__(self, status: int, title: str, text: str) -> None:
        super().__init__(text)
        self.status, self.title, self.text = status, title, text


def person_for_email(users: dict, email: object) -> str | None:
    """Who `notify.users` maps this e-mail to: the GitHub login mapped to the same chat
    user, else the e-mail itself. None when no service maps it."""
    email = str(email or "").strip().casefold()
    if "@" not in email:
        return None
    for service in sorted(users):
        mapped = users.get(service) or {}
        user_id = mapped.get(email)
        if user_id is None:
            continue
        logins = sorted(k for k, v in mapped.items() if v == user_id and "@" not in k)
        return logins[0] if logins else email
    return None


_POLL = """<script nonce="{nonce}">
(function () {{
  var el = document.getElementById("status");
  function poll() {{
    fetch("status" + location.search, {{credentials: "same-origin", cache: "no-store"}})
      .then(function (r) {{ return r.json(); }})
      .then(function (d) {{
        el.textContent = d.message || "";
        if (d.state === "ready") {{ location.reload(); return; }}
        if (d.state === "starting") {{ setTimeout(poll, {ms}); }}
      }})
      .catch(function () {{
        el.textContent = "Lost contact with Symphony. Trying again\\u2026";
        setTimeout(poll, {ms});
      }});
  }}
  setTimeout(poll, {ms});
}})();
</script>"""


def _page(status: int, title: str, text: str, *, poll: bool = False) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    script = _POLL.format(nonce=nonce, ms=POLL_S * 1000) if poll else ""
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} \u00b7 Symphony</title>"
        f'<style nonce="{nonce}">body{{font:16px/1.5 system-ui,sans-serif;max-width:36rem;'
        "margin:15vh auto;padding:0 1rem}</style></head><body>"
        f'<h1>{html.escape(title)}</h1><p id="status">{html.escape(text)}</p>{script}'
        "</body></html>"
    )
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
           "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return HTMLResponse(body, status, headers={**_NO_STORE, "Content-Security-Policy": csp})


def _sentence(exc: Exception) -> str:
    text = str(exc)
    return text[:1].upper() + text[1:]


def _message(progress: dict) -> str:
    state = progress["state"]
    if state == "ready":
        return "Ready. Opening the simulator\u2026"
    if state == "starting":
        return _STEPS.get(progress.get("step", ""), _STEPS["queued"])
    if state == "error":
        return f"Could not start the preview: {progress.get('error')}. Reload the page to try again."
    return "The preview is not running. Reload the page to start it again."


def build_router(
    control: Control,
    *,
    config: Callable[[], Config | None] = mounted_workflow,
    jwks_http: Callable[[], httpx.Client] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/previews")
    s = control.s
    verifiers: dict[tuple[str, str], AccessVerifier] = {}
    verifiers_lock = threading.Lock()

    def verifier(access: dict) -> AccessVerifier:
        key = (access["team_domain"], access["aud"])
        with verifiers_lock:
            found = verifiers.get(key)
            if found is None:
                found = verifiers[key] = AccessVerifier(
                    *key, **({"http": jwks_http} if jwks_http is not None else {}))
        return found

    def check(request: Request, repo: str, pr: str, sha: str, sig: str) -> tuple[str, int, str, str]:
        """`(repo, pr, sha12, actor)` of an authorized link, or `_Refused`."""
        repo = repo.strip().lower()
        if not (preview_links.REPO.fullmatch(repo) and pr.isdigit() and 0 < int(pr) < 10**9
                and preview_links.SHA12.fullmatch(sha)):
            raise _Refused(400, "Not a preview link", "This address is not a complete preview "
                           "link. Open it again from the pull request.")
        key = preview_links.secret()
        bad_sig = _Refused(403, "This link is not valid", "Its signature does not match. Open "
                           "the link again from the pull request.")
        if sig and key is not None and not preview_links.verify(key, repo, int(pr), sha, sig):
            raise bad_sig
        cfg = config()
        if cfg is None:
            raise _Refused(503, "Symphony is not configured", "The API has no valid WORKFLOW.md.")
        if cfg.api_access:
            token = request.headers.get(HEADER)
            if not token:
                raise _Refused(401, "Sign-in required", "Open this link through Cloudflare "
                               "Access: the request carries no Access token.")
            try:
                claims = verifier(cfg.api_access).verify(token)
            except AccessError:
                raise _Refused(401, "Sign-in not accepted", "Your Cloudflare Access sign-in "
                               "could not be verified. Sign in again, then reopen the link.") from None
            actor = person_for_email(cfg.notify_users, claims.get("email"))
            if actor is None:
                raise _Refused(403, "Not allowed", "Your e-mail is not mapped to a person in "
                               "Symphony (notify.users). Ask an admin to add it.")
        elif cfg.previews_public_links:
            # No identity: the signed link itself is the credential.
            if key is None:
                raise _Refused(503, "Preview links are off", "Public preview links need the "
                               f"link secret ({preview_links.SECRET_ENV}).")
            if not sig:
                raise bad_sig
            actor = PUBLIC_ACTOR
        else:
            raise _Refused(403, "Preview links are off", "This Symphony opens preview links "
                           "only behind Cloudflare Access (api.access) or with "
                           "previews.public_links.")
        if s.previews is None:
            raise _Refused(503, "Previews are not set up", "The API needs the GitHub App and "
                           "a Seiji server named seiji.")
        return repo, int(pr), sha, _ACTOR_CHARS.sub("-", actor)[:128]

    @router.get("/open")
    def open_preview(request: Request, repo: str = "", pr: str = "", sha: str = "",
                     sig: str = ""):
        try:
            repo, number, sha12, actor = check(request, repo, pr, sha, sig)
            try:
                state = s.previews.open_link(repo, number, sha12)
            except PreviewGone as exc:
                raise _Refused(410, "This build is gone", f"{_sentence(exc)}. Open the link of "
                               "a newer build from the pull request.") from None
            s.store.audit_event(actor=actor, action=ACTION, target=f"{repo}#{number}@{sha12}",
                                reason=state)
            if state == "ready":
                url = s.previews.link_viewer(repo, number, sha12)
                if url:
                    return RedirectResponse(url, 302, headers=_NO_STORE)
            return _page(200, f"Opening {repo}#{number} at {sha12}",
                         _STEPS["queued"], poll=True)
        except _Refused as exc:
            return _page(exc.status, exc.title, exc.text)
        except PreviewError as exc:
            return _page(502, "Could not open the preview", f"{_sentence(exc)}. Reload "
                         "the page to try again.")
        except Exception as exc:  # noqa: BLE001 — a page, not a JSON error, for a browser
            log.warning("preview_open_failed error=%s", type(exc).__name__)
            return _page(503, "Could not open the preview", "GitHub, the device lab or "
                         "Symphony's store did not answer. Reload the page to try again.")

    @router.get("/status")
    def preview_status(request: Request, repo: str = "", pr: str = "", sha: str = "",
                       sig: str = ""):
        try:
            repo, number, sha12, _ = check(request, repo, pr, sha, sig)
        except _Refused as exc:
            return JSONResponse({"state": "refused", "message": exc.text}, exc.status,
                                headers=_NO_STORE)
        progress = s.previews.progress(repo, number, sha12)
        return JSONResponse({**progress, "message": _message(progress)}, headers=_NO_STORE)

    return router
