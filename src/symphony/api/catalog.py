"""The provider/model catalog of the pinned omp, decoded from the bundled compact `models.json`.

omp 18.20.2 ships its model database as `@oh-my-pi/pi-catalog/src/models.json`, a compact
interned encoding (`model-catalog-format.ts`): object nodes keep their non-boolean properties as
`[key id, value]` pairs and their booleans as bit masks over a shared key shape, and one-element
arrays are references to other nodes. `omp models --json` lists only providers with credentials,
so it cannot describe what an admin could configure; the bundled database can.

The image bakes that file at `SYMPHONY_OMP_CATALOG` (default
`/opt/symphony/omp-catalog/models.json`) next to the package's compiled provider rules
(`src/compat/rules.json`, installed as `rules.json`). It is decoded once per process and projected
to what the Admin screen needs, never the provider compat internals.

Symphony uses API keys only (internals §13), so the rules decide which built-in providers are
offered: `credential: "apiKey"` (an admin may store a key), `"host"` (credentials come from the
host, e.g. Vertex ADC), and OAuth/subscription-only providers are left out entirely.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..edge import OMP_VERSION

DEFAULT_PATH = "/opt/symphony/omp-catalog/models.json"
RULES_NAME = "rules.json"
# amazon-bedrock lists no env var (it resolves the AWS SDK chain) yet takes a Bedrock API key as
# its `apiKey`: verified on omp 18.20.2, the models.yml override alone authenticated a call.
_KEY_WITHOUT_ENV = frozenset({"amazon-bedrock"})
_MODEL_FIELDS = ("name", "api", "baseUrl", "contextWindow", "maxTokens", "reasoning", "input")
_COST_FIELDS = ("input", "output", "cacheRead", "cacheWrite")


class CatalogError(Exception):
    pass


def credential_kinds(rules: dict) -> dict[str, str]:
    """Provider id → `apiKey` | `host` | `oauth`, from omp's compiled rules.

    `providers[id].envVars` are the env vars omp reads, in order, for a provider's API key; the
    first one is what a stored key stands in for. A provider only works with a login when:

    - its first var is an OAuth token (`OPENAI_CODEX_OAUTH_TOKEN`, `XAI_OAUTH_TOKEN`);
    - it has no var but a `/login` flow (`auth.providers[].login`: Gemini CLI, Kimi Code…);
    - its credential is renewed by a provider hook (`auth.providers[].refresh.kind == "hook"`):
      a session exchanged at the provider, not a static key (GitHub Copilot, Cursor, Snowflake).

    A `request` refresh belongs to an optional OAuth login beside a real API key (Anthropic,
    GitLab), so those stay `apiKey`.
    """
    auth = {p["id"]: p for p in rules["auth"]["providers"]}
    kinds = {}
    for provider_id, provider in rules["providers"].items():
        if "defaultModel" not in provider:
            continue  # wire-compat only, not a provider
        env = provider.get("envVars") or []
        login = auth.get(provider_id) or {}
        hooked = (login.get("refresh") or {}).get("kind") == "hook"
        if provider_id in _KEY_WITHOUT_ENV or (env and "OAUTH" not in env[0] and not hooked):
            kinds[provider_id] = "apiKey"
        elif env or login.get("login"):
            kinds[provider_id] = "oauth"
        else:
            kinds[provider_id] = "host"
    return kinds


class _Decoder:
    def __init__(self, data: dict) -> None:
        if not isinstance(data, dict) or data.get("version") != 1:
            raise CatalogError("unsupported omp model catalog format")
        self.keys: list[str] = data["keys"]
        self.shapes: list[list[int]] = data["booleanShapes"]
        self.nodes: list[list] = data["nodes"]
        self.providers: list[list] = data["providers"]
        self._values: dict[int, Any] = {}

    def object_into(self, target: dict, node: list) -> None:
        props = node[1]
        for i in range(0, len(props), 2):
            target[self.keys[props[i]]] = self.value(props[i + 1])
        shape = self.shapes[node[2]]
        masks = node[3]
        for i, key_id in enumerate(shape):
            target[self.keys[key_id]] = bool((masks[i >> 5] >> (i % 32)) & 1)

    def value(self, encoded: Any) -> Any:
        if not isinstance(encoded, list):
            return encoded
        ref = encoded[0]
        if ref == -1:
            return None
        if ref in self._values:
            return self._values[ref]
        node = self.nodes[ref]
        if node[0] == 1:
            result: Any = [self.value(item) for item in node[1]]
        else:
            result = {}
            self.object_into(result, node)
        self._values[ref] = result
        return result

    def provider_models(self, row: list) -> list[dict]:
        constants = self.value(row[1]) or {}
        models = []
        for model_row in row[2]:
            model_id, encoded = model_row[0], model_row[1]
            if not isinstance(encoded, list) or encoded[0] < 0:
                raise CatalogError("invalid compact model row")
            node = self.nodes[encoded[0]]
            if node[0] != 0:
                raise CatalogError("invalid compact model row")
            model = {"id": model_id, **constants}
            self.object_into(model, node)
            models.append(model)
        return models


def _project(model: dict) -> dict:
    out = {"id": model["id"]}
    for field in _MODEL_FIELDS:
        out[field] = model.get(field)
    cost = model.get("cost") if isinstance(model.get("cost"), dict) else {}
    out["cost"] = {field: cost.get(field) for field in _COST_FIELDS}
    thinking = model.get("thinking")
    out["thinking"] = thinking if isinstance(thinking, dict) else None
    return out


def decode(data: dict, *, version: str, kinds: dict[str, str]) -> dict:
    """Project the compact catalog to `{source, version, providers:[{id, credential, models:[…]}]}`,
    without OAuth-only providers or ids the rules don't know (`credential_kinds`)."""
    decoder = _Decoder(data)
    providers = []
    for row in decoder.providers:
        credential = kinds.get(row[0])
        if credential not in ("apiKey", "host"):
            continue
        models = sorted(
            (_project(m) for m in decoder.provider_models(row)), key=lambda m: m["id"]
        )
        if models:
            providers.append({"id": row[0], "credential": credential, "models": models})
    providers.sort(key=lambda p: p["id"])
    return {"source": "@oh-my-pi/pi-catalog/models.json", "version": version, "providers": providers}


class Catalog:
    """Decoded once, then served from memory as pre-serialized bytes."""

    def __init__(self, path: str | os.PathLike[str] | None = None, *, version: str = OMP_VERSION):
        self.path = Path(path or os.environ.get("SYMPHONY_OMP_CATALOG", DEFAULT_PATH))
        self.version = version
        self._lock = threading.Lock()
        self._body: bytes | None = None
        self._ids: frozenset[str] | None = None

    def body(self) -> bytes:
        if self._body is None:
            with self._lock:
                if self._body is None:
                    try:
                        data = json.loads(self.path.read_bytes())
                        rules = json.loads(self.path.with_name(RULES_NAME).read_bytes())
                    except (OSError, ValueError) as exc:
                        raise CatalogError("the pinned omp model catalog is not installed") from exc
                    try:
                        document = decode(data, version=self.version, kinds=credential_kinds(rules))
                    except (KeyError, IndexError, TypeError, AttributeError) as exc:
                        raise CatalogError("the pinned omp model catalog is malformed") from exc
                    self._ids = frozenset(
                        p["id"] for p in document["providers"] if p["credential"] == "apiKey"
                    )
                    self._body = json.dumps(document, separators=(",", ":")).encode()
        return self._body

    def provider_ids(self) -> frozenset[str]:
        """Ids of the built-in providers an admin may store an API key for (internals §13)."""
        self.body()
        return self._ids
