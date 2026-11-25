"""HTTP services — `python -m symphony.entry.serve <api|gateway>`.

Two Deployments from one image, because their audiences differ:

- api      people and their tools (CLI, Tern plugin, ACP bridge, MCP) and webhooks, behind
           bearer `sym_` tokens; reaches Redis and the Kubernetes API (workflow, providers).
- gateway  agents only: OTLP traces (`POST /v1/traces`), authenticated per attempt, and the
           MCP proxy (`/v1/mcp/<name>`), authenticated per agent session. It reads the MCP
           servers from the `symphony-mcp` Secret mounted at `SYMPHONY_MCP_DIR`. It serves
           `/healthz` and `/metrics` on `SYMPHONY_HEALTH_PORT` (8081), not on 8090.

The API serves no metrics: its only port is public.
"""

from __future__ import annotations

import os
import sys

from .. import metrics
from ..edge import API_PORT, GATEWAY_PORT, log, pod_namespace
from ..kv import RedisKV


def build(role: str):
    from ..api.app import wait_for_redis

    kv = RedisKV()
    # Redis may still be starting (same rollout, node restart): wait for it instead of crashing.
    wait_for_redis(kv.client)
    if role == "api":
        import redis.asyncio

        from ..api import build_app, build_services
        from ..kube import get_clients

        try:
            core, batch = get_clients()
        except Exception:  # noqa: BLE001 — workflow/provider/infra routes degrade without Kubernetes
            core, batch = None, None
            log.warning("api_kubernetes_client_unavailable")
        # SSE holds a blocking XREAD per viewer: an asyncio client keeps that off the threadpool,
        # and its socket timeout must outlast the 15 s block.
        aclient = redis.asyncio.Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=True,
            socket_timeout=60,
            socket_connect_timeout=5,
        )
        services = build_services(
            kv.client, aclient, pod_namespace(), kv=kv, core=core, batch=batch
        )
        if services.previews is not None:
            services.previews.start()  # releases the link previews' devices (`previews.py`)
        return build_app(services), API_PORT
    if role == "gateway":
        from ..gateway_api import build_app
        from ..mcp_servers import McpRepository

        ns = pod_namespace()
        mounted = McpRepository(None, ns, mount_dir=os.environ.get("SYMPHONY_MCP_DIR", "/etc/symphony/mcp"))
        # The health port, apart from the agent port: only a monitoring namespace may reach it.
        metrics.serve(int(os.environ.get("SYMPHONY_HEALTH_PORT", "8081")), lambda: True,
                      metrics.Scrape("gateway", lambda: metrics.gateway_families(kv.client, ns)))
        return build_app(lambda: kv, namespace=ns, mcp=mounted), GATEWAY_PORT
    raise SystemExit(f"unknown role {role!r} (want: api | gateway)")


def main() -> None:
    import uvicorn

    role = sys.argv[1] if len(sys.argv) > 1 else ""
    app, default_port = build(role)
    port = int(os.environ.get("PORT", str(default_port)))
    log.info("serving", role=role, port=port)
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=False)


if __name__ == "__main__":
    main()
