"""`uv run python -m scripts.mocks.discord --port 4500 --state DIR`."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.mocks.discord.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.mocks.discord")
    parser.add_argument("--port", type=int, default=4500)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state", type=Path, default=None)
    args = parser.parse_args()
    server = serve(args.port, args.state, args.host)
    print(f"discord mock listening on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
