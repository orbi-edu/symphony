"""`uv run python -m scripts.mocks.seiji --port 4780 [--public-url URL] [--allowed-host H]`.
The bearer token comes from `SEIJI_MOCK_TOKEN` (default `DEFAULT_TOKEN`), never from the
command line."""

from __future__ import annotations

import argparse
import os

from scripts.mocks.seiji.server import DEFAULT_TOKEN, serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.mocks.seiji")
    parser.add_argument("--port", type=int, default=4780)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--public-url", help="Seiji's listen.publicUrl (default: the listen origin)")
    parser.add_argument("--allowed-host", action="append", metavar="HOST",
                        help="listen.allowedHosts entry (repeat; default localhost and 127.0.0.1)")
    args = parser.parse_args()
    server = serve(args.port, os.environ.get("SEIJI_MOCK_TOKEN") or DEFAULT_TOKEN, args.host,
                   public_url=args.public_url,
                   allowed_hosts=tuple(args.allowed_host or ("localhost", "127.0.0.1")))
    print(f"seiji mock listening on http://{args.host}:{server.server_port}/mcp", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
