"""`uv run python -m scripts.mocks.linear --port 4100 --state DIR`."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.mocks.linear.server import serve
from scripts.mocks.linear.store import DEFAULT_API_KEY


def _team(value: str) -> tuple[str, str]:
    key, _, name = value.partition(":")
    return key.upper(), name or key.upper()


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.mocks.linear")
    parser.add_argument("--port", type=int, default=4100)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--public-url", default=None,
                        help="base of issue, asset and upload URLs (default http://HOST:PORT)")
    parser.add_argument("--team", action="append", type=_team, metavar="KEY[:NAME]",
                        help="seed team (repeatable; default ENG:Engineering)")
    parser.add_argument("--api-key", action="append", metavar="KEY",
                        help=f"accepted personal API key (repeatable; default {DEFAULT_API_KEY})")
    parser.add_argument("--webhook-url", default=None)
    parser.add_argument("--webhook-secret", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.webhook_url and not args.webhook_secret:
        parser.error("--webhook-url needs --webhook-secret")
    kwargs = {}
    if args.team:
        kwargs["teams"] = tuple(args.team)
    if args.api_key:
        kwargs["api_keys"] = tuple(args.api_key)
    server = serve(args.port, args.state, args.host, public_url=args.public_url,
                   webhook_url=args.webhook_url, webhook_secret=args.webhook_secret,
                   verbose=args.verbose, **kwargs)
    print(f"linear mock listening on http://{args.host}:{server.server_port}/graphql", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
