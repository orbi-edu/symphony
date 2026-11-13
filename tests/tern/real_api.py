"""The real control API for the Tern plugin's real-stack tests.

    REDIS_URL=… SYMPHONY_BOOTSTRAP_TOKEN=… SYMPHONY_TERN_SECRETS=…/secrets.env \
        python tests/tern/real_api.py PORT

This is `python -m symphony.entry.serve api` with the cluster boundary swapped: there is no
Kubernetes cluster on a workstation, so
- the provider Secret (`symphony-providers`) and the workflow ConfigMap (`symphony-workflow`,
  shipped empty as a fresh install does) are in memory, behind the same `read_namespaced_*` /
  `replace_namespaced_*` calls `providers.py` and `admin_workflow.py` make in a cluster
  (resource-version CAS included);
- setup stores integration credentials in the local stack's 0600 `secrets.env`
  (`setup_secrets_local.LocalSecretStore`), as `symphony local` does;
- MCP servers live in a 0600 `mcp.json` (`McpRepository(local_path=…)`), as the local stack
  keeps them.
Everything else (routes, auth, stores, Redis, the omp catalog, the vendor checks against
`LINEAR_API_URL`, the MCP test through the gateway's proxy code) is the production code path.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import sys
import threading

import redis.asyncio
import uvicorn
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from symphony.api import build_app, build_services
from symphony.edge import pod_namespace
from symphony.kv import RedisKV
from symphony.mcp_servers import McpRepository
from symphony.setup_secrets_local import LocalSecretStore

# internals §13: a built-in key (Anthropic) and a custom provider; OpenAI has no stored item.
PROVIDERS = {
    "anthropic": {"builtin": True, "apiKey": "dummy-anthropic-key-for-tests"},
    "vllm-internal": {
        "name": "Internal vLLM", "api": "openai-completions", "baseUrl": "https://vllm.acme.internal/v1",
        # A stored apiKey provider always has its key (providers.py refuses one without).
        "auth": "apiKey", "models": [{"id": "qwen3-coder"}], "apiKey": "dummy-vllm-key-for-tests",
    },
}


class MemoryCore:
    """`CoreV1Api` for the providers Secret and the workflow ConfigMap, with Kubernetes'
    optimistic concurrency."""

    def __init__(self, namespace: str) -> None:
        body = json.dumps({"schemaVersion": 1, "providers": PROVIDERS}).encode()
        self.objects = {
            "secret": client.V1Secret(
                metadata=client.V1ObjectMeta(name="symphony-providers", namespace=namespace, resource_version="1"),
                data={"providers.json": base64.b64encode(body).decode()},
            ),
            "config_map": client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name="symphony-workflow", namespace=namespace, resource_version="1"),
                data=None,
            ),
        }
        self.lock = threading.Lock()

    def _read(self, kind: str):
        with self.lock:
            return copy.deepcopy(self.objects[kind])

    def _replace(self, kind: str, body):
        with self.lock:
            if body.metadata.resource_version != self.objects[kind].metadata.resource_version:
                raise ApiException(status=409, reason="Conflict")
            body.metadata.resource_version = str(int(body.metadata.resource_version) + 1)
            self.objects[kind] = copy.deepcopy(body)
            return copy.deepcopy(body)

    def read_namespaced_secret(self, **_kwargs):
        return self._read("secret")

    def replace_namespaced_secret(self, *, body, **_kwargs):
        return self._replace("secret", body)

    def read_namespaced_config_map(self, **_kwargs):
        return self._read("config_map")

    def replace_namespaced_config_map(self, *, body, **_kwargs):
        return self._replace("config_map", body)


def main() -> None:
    port = int(sys.argv[1])
    kv = RedisKV()
    aclient = redis.asyncio.Redis.from_url(
        os.environ["REDIS_URL"], decode_responses=True, socket_timeout=60, socket_connect_timeout=5
    )
    namespace = pod_namespace()
    services = build_services(
        kv.client, aclient, namespace, kv=kv, core=MemoryCore(namespace),
        secrets=LocalSecretStore(os.environ["SYMPHONY_TERN_SECRETS"]),
        mcp=McpRepository(None, namespace, local_path=os.environ["SYMPHONY_TERN_MCP"]),
    )
    uvicorn.run(build_app(services), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
