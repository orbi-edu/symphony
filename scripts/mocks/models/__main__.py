"""`uv run python -m scripts.mocks.models --port 4400 --state DIR`.

Anthropic base URL: `http://HOST:PORT/anthropic`; OpenAI base URL: `http://HOST:PORT/openai/v1`.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.mocks.models.script import default_script
from scripts.mocks.models.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.mocks.models")
    parser.add_argument("--port", type=int, default=4400)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--anthropic-key", help="the API key the Anthropic routes accept")
    parser.add_argument("--openai-key", help="the API key the OpenAI routes accept")
    parser.add_argument("--codeword", help="use the default two-step script with this codeword")
    args = parser.parse_args()
    server = serve(args.port, args.state, args.host)
    keys = {
        vendor: key
        for vendor, key in (("anthropic", args.anthropic_key), ("openai", args.openai_key))
        if key
    }
    server.state.configure(
        keys=keys or None, script=default_script(args.codeword) if args.codeword else None
    )
    base = f"http://{args.host}:{server.server_port}"
    print(f"models mock listening on {base} (anthropic {base}/anthropic, openai {base}/openai/v1)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
