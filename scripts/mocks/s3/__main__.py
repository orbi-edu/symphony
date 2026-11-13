"""`uv run python -m scripts.mocks.s3 --port 4700 --bucket symphony-backups`."""

from __future__ import annotations

import argparse
import os

from scripts.mocks.s3.server import DEFAULT_ACCESS_KEY, DEFAULT_SECRET_KEY, serve


def main() -> None:
    parser = argparse.ArgumentParser(prog="scripts.mocks.s3")
    parser.add_argument("--port", type=int, default=4700)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--bucket", action="append", default=[], help="create a bucket (repeat)")
    args = parser.parse_args()
    # Dummy credentials only; the defaults are the example keys of the AWS SigV4 page.
    access_key = os.environ.get("S3_MOCK_ACCESS_KEY_ID", DEFAULT_ACCESS_KEY)
    secret_key = os.environ.get("S3_MOCK_SECRET_ACCESS_KEY", DEFAULT_SECRET_KEY)
    server = serve(args.port, args.host, region=args.region, buckets=tuple(args.bucket),
                   credentials={access_key: secret_key})
    print(f"s3 mock listening on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
