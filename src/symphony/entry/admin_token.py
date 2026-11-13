"""API tokens on the trusted host — `python -m symphony.entry.admin_token <command>`.

- `bootstrap`  print a fresh admin token in `sym_` format for the `SYMPHONY_BOOTSTRAP_TOKEN`
               Secret; the API installs it at start. Nothing is written anywhere.
- `create`     create a token in Redis (`REDIS_URL`) and print it once.
- `list`       list tokens (never secrets).
- `revoke ID`  revoke a token; effective on its next request.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from ..apitokens import ROLES, TokenError, TokenStore


def bootstrap_token() -> str:
    return f"sym_{secrets.token_hex(4)}_{secrets.token_urlsafe(32)}"


def _store() -> TokenStore:
    from ..edge import pod_namespace
    from ..kv import RedisKV

    return TokenStore(RedisKV().client, pod_namespace())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Symphony API tokens.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap", help="print a new admin token for SYMPHONY_BOOTSTRAP_TOKEN")
    create = commands.add_parser("create", help="create a token in Redis and print it once")
    create.add_argument("--name", required=True)
    create.add_argument("--role", required=True, choices=ROLES)
    create.add_argument("--expires-days", type=int, default=None)
    commands.add_parser("list", help="list tokens")
    revoke = commands.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("token_id")
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        print(bootstrap_token())
        return 0
    store = _store()
    try:
        if args.command == "create":
            expires = args.expires_days * 86400 if args.expires_days else None
            token, record = store.create(
                args.name, args.role, actor="admin_token", expires_in_s=expires
            )
            print(json.dumps({"token": token, **record}, indent=2))
        elif args.command == "list":
            print(json.dumps(store.list(), indent=2))
        else:
            print(json.dumps(store.revoke(args.token_id, actor="admin_token"), indent=2))
    except TokenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
