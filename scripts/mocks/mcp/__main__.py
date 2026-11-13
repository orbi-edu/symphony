"""`uv run python -m scripts.mocks.mcp --port 4600`. The bearer token comes from
`MCP_MOCK_TOKEN` (default `mock-mcp-token`), never from the command line."""

from __future__ import annotations

import argparse
import os

from scripts.mocks.mcp.server import DEFAULT_TOKEN, serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.mocks.mcp")
    parser.add_argument("--port", type=int, default=4600)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = serve(args.port, os.environ.get("MCP_MOCK_TOKEN") or DEFAULT_TOKEN, args.host)
    print(f"mcp mock listening on http://{args.host}:{server.server_port}/mcp", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
