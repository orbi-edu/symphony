"""Mock GitHub: REST, GraphQL, git smart HTTP, Actions CI and signed webhooks.

    uv run python -m scripts.mocks.github --port 4200 --state /tmp/gh-mock \\
        [--https-port 4201] [--webhook-url http://127.0.0.1:8000/v1/webhooks/github --webhook-secret S]

Point Symphony at it (internals §14):

    GITHUB_API_URL=http://127.0.0.1:4200  GITHUB_GRAPHQL_URL=http://127.0.0.1:4200/graphql
    GITHUB_SERVER_URL=http://127.0.0.1:4200
    GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY / GITHUB_APP_INSTALLATION_ID: from `GET /_mock/app` (`env`)

Every request is checked against GitHub's own description: a path or method it lacks is 404
`Not Found`, a bad parameter or body is 422 `Invalid request` (both with `documentation_url`),
GraphQL is validated and executed on the real schema. App JWTs (RS256, `iss` = App id, `exp`
≤ 10 min after `iat`) mint installation tokens scoped to the requested repositories and
permissions; every REST, GraphQL and git call enforces them (403 `Resource not accessible by
integration`, git push 403 without `contents: write`).

Git: `git clone http://127.0.0.1:4200/<owner>/<repo>.git` with Basic auth `x-access-token:<token>`
(Symphony's credential helper does exactly that).

HTTPS (`--https-port`): a private CA is written to `<state>/ca.pem`. `git -c http.sslCAInfo=<state>/ca.pem
clone https://localhost:4201/<owner>/<repo>.git`; Python/httpx `SSL_CERT_FILE=<state>/ca.pem`; `gh`
treats any host but github.com as GitHub Enterprise (`/api/v3`, `/api/graphql`, both served):
`GH_HOST=localhost:4201 GH_ENTERPRISE_TOKEN=<token> SSL_CERT_FILE=<state>/ca.pem gh pr list -R owner/repo`
(on macOS Go ignores SSL_CERT_FILE: add the CA to the login keychain instead).

Control API (JSON):
    GET  /_mock/health | /_mock/app | /_mock/state[?repo=o/r] | /_mock/deliveries[?event=&since=&brief=1]
         /_mock/requests | /_mock/violations
    POST /_mock/app {public_key | private_key}             use the operator's App key
    POST /_mock/repos {owner, name, seed?, default_branch?, ci?, labels?}
    POST /_mock/installations {account, repositories?: [names] | "all", permissions?}
    POST /_mock/users {login, name?}                       → a person and their token
    POST /_mock/comment {repo, number, user, body, kind?: issue|review_comment|review, path?, line?,
                         in_reply_to?, event?}
    POST /_mock/ci {repo, sha|pr|branch|default, conclusion?, jobs?, log?, delay_s?, hold?, interrupted?,
                    clear?, rerun?}
    POST /_mock/ci/config {repo, workflow?, jobs?, mode?: auto|success|failure|off, command?, delay_s?}
    POST /_mock/ci/drain {timeout_s?}                      wait until no run is queued or running
    POST /_mock/protection {repo, branch, required_checks: [names] | null}
    POST /_mock/webhook {url, secret}
    POST /_mock/reset
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from .server import MockServer


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.mocks.github", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4200)
    p.add_argument("--https-port", type=int, default=None, help="also serve HTTPS (private CA in the state dir)")
    p.add_argument("--state", required=True, help="state directory (bare repos, state.json, CA)")
    p.add_argument("--public-url", default=None, help="URL in links and webhooks (default http://HOST:PORT)")
    p.add_argument("--webhook-url", default=None)
    p.add_argument("--webhook-secret", default=None)
    p.add_argument("--app-id", type=int, default=1)
    p.add_argument("--app-slug", default="symphony")
    p.add_argument("--app-key", default=None, help="PEM file: the App's public key (or private key) to verify JWTs")
    p.add_argument("--tls-host", action="append", default=[], help="extra name in the HTTPS certificate")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    server = MockServer(args.state, host=args.host, port=args.port, https_port=args.https_port,
                        public_url=args.public_url, webhook_url=args.webhook_url, webhook_secret=args.webhook_secret,
                        app_id=args.app_id, app_slug=args.app_slug, tls_hosts=args.tls_host, verbose=args.verbose)
    if args.app_key:
        with open(args.app_key) as fh:
            pem = fh.read()
        if "PRIVATE KEY" in pem:
            server.mock.store.register_app_key(private_key=pem)
        else:
            server.mock.store.register_app_key(public_key=pem)
    server.start()
    extra = f", {server.https_url} (CA {server.ca_path})" if server.https_url else ""
    print(f"mock-github listening on {server.url}{extra}; state {server.state_dir}", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
