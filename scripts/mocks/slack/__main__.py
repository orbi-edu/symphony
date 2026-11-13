"""`uv run python -m scripts.mocks.slack --port 4300 --state DIR`."""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.mocks.slack.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.mocks.slack")
    parser.add_argument("--port", type=int, default=4300)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state", type=Path, default=None)
    args = parser.parse_args()
    server = serve(args.port, args.state, args.host)
    print(f"slack mock listening on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
