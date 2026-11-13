"""Team configuration: which model each agent role uses, and the omp defaults everyone shares.

Administered from Tern (the Admin screen), published as numbered generations, and read by every
consumer — the daemon's runners, the merger's agents and each person's `symphony omp sync` —
at the start of each unit of work, so a publication spreads without restarts. Each consumer
reports the generation it applied, which is how the Admin screen shows that a change actually
reached everyone.

Provider credentials are not here: API keys stay in the `symphony-providers` Secret
(`providers.py`) and the host `model-provider` Secret. This document only names `provider/model`
pairs.
"""

from __future__ import annotations

import copy
import json
import re
import time

import redis

ROLES = (
    "implementer", "reviewer", "oracle", "arbiter", "developer", "reflector", "probe",
    "assistant", "researcher", "planner",
)
THINKING = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}\Z")
HISTORY = 50

# Default models, each verified to answer on Bedrock (docs/certification.md). The thinking
# levels are defaults, not certified settings. Oracle and arbiter come from other model
# families than the reviewer, so a blocking finding needs two independent opinions. The chat
# assistant (`assistant`, `researcher`, `planner`) answers people, so it favors fast models.
# Accounts without these models publish their own from the Admin screen.
DEFAULT = {
    "generation": 0,
    "models": {
        "implementer": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-opus-5-5", "thinking": "high"},
        "reviewer": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-opus-5-5", "thinking": "high"},
        "oracle": {"provider": "amazon-bedrock", "model": "global.moonshotai.kimi-k3.1", "thinking": "medium"},
        "arbiter": {"provider": "amazon-bedrock", "model": "deepseek.v3.2", "thinking": "high"},
        "developer": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-opus-5-5", "thinking": "high"},
        "reflector": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-opus-5-5", "thinking": "medium"},
        "probe": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-haiku-5-5", "thinking": "low"},
        "assistant": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-haiku-5-5", "thinking": "low"},
        "researcher": {"provider": "amazon-bedrock", "model": "global.moonshotai.kimi-k3.1", "thinking": "high"},
        "planner": {"provider": "amazon-bedrock", "model": "global.anthropic.claude-haiku-5-5", "thinking": "medium"},
    },
    # Shared omp defaults for workers and for people who run `symphony omp sync`.
    "omp": {"version": "18.20.2", "settings": {}},
}


class ConfigError(Exception):
    pass


class ConflictError(ConfigError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def validate(doc: dict) -> dict:
    models = doc.get("models")
    if not isinstance(models, dict) or set(models) != set(ROLES):
        raise ConfigError(f"models must name exactly these roles: {', '.join(ROLES)}")
    for role, ref in models.items():
        if not isinstance(ref, dict):
            raise ConfigError(f"models.{role} must be an object")
        for field in ("provider", "model"):
            if not isinstance(ref.get(field), str) or not _ID.fullmatch(ref[field]):
                raise ConfigError(f"models.{role}.{field} is not a valid id")
        if ref.get("thinking", "medium") not in THINKING:
            raise ConfigError(f"models.{role}.thinking must be one of {', '.join(THINKING)}")
    reviewer = (models["reviewer"]["provider"], models["reviewer"]["model"])
    for checker in ("oracle", "arbiter"):
        if (models[checker]["provider"], models[checker]["model"]) == reviewer:
            raise ConfigError(
                f"models.{checker} must differ from the reviewer: a finding blocks only when two "
                "independent models agree"
            )
    omp = doc.get("omp")
    if not isinstance(omp, dict) or not isinstance(omp.get("settings", {}), dict):
        raise ConfigError("omp.settings must be an object")
    return doc


def model_arg(ref: dict) -> str:
    """The `--model` value omp takes: `provider/model`."""
    return f"{ref['provider']}/{ref['model']}"


class TeamConfig:
    def __init__(self, client, namespace: str, *, now_ms=_now_ms) -> None:
        self.client = client
        self.key = f"sym:{namespace}:teamconfig"
        self.history_key = f"{self.key}:history"
        self.observed_key = f"{self.key}:observed"
        self._now = now_ms

    def get(self) -> dict:
        """The current generation. A role added after it was published gets its default, so
        a document published before the role existed still names every role."""
        raw = self.client.get(self.key)
        if not raw:
            return copy.deepcopy(DEFAULT)
        doc = json.loads(raw)
        models = doc.setdefault("models", {})
        for role in ROLES:
            if role not in models:
                models[role] = copy.deepcopy(DEFAULT["models"][role])
        return doc

    def model(self, role: str) -> dict:
        if role not in ROLES:
            raise ConfigError(f"unknown role {role!r}")
        return self.get()["models"][role]

    def publish(self, patch: dict, *, expected_generation: int, actor: str, reason: str) -> dict:
        """Apply `patch` (deep-merged into `models` and `omp`) as the next generation, only if
        the current one is still `expected_generation` (optimistic concurrency)."""
        if not reason or len(reason) > 500:
            raise ConfigError("a reason (1-500 characters) is required")
        with self.client.pipeline() as pipe:
            try:
                pipe.watch(self.key)
                current = self.get()
                if current["generation"] != expected_generation:
                    raise ConflictError(
                        f"configuration changed: generation {current['generation']} is current"
                    )
                doc = copy.deepcopy(current)
                for section in ("models", "omp"):
                    for key, value in (patch.get(section) or {}).items():
                        if isinstance(value, dict) and isinstance(doc[section].get(key), dict):
                            doc[section][key] = {**doc[section][key], **value}
                        else:
                            doc[section][key] = value
                unknown = set(patch) - {"models", "omp"}
                if unknown:
                    raise ConfigError(f"unknown sections: {', '.join(sorted(unknown))}")
                validate(doc)
                doc.update(
                    generation=current["generation"] + 1,
                    updated_at=self._now(),
                    updated_by=actor,
                    reason=reason,
                )
                pipe.multi()
                pipe.set(self.key, json.dumps(doc))
                pipe.lpush(self.history_key, json.dumps(doc))
                pipe.ltrim(self.history_key, 0, HISTORY - 1)
                pipe.execute()
            except redis.WatchError as exc:
                raise ConflictError("configuration changed concurrently; reload and retry") from exc
        return doc

    def history(self, limit: int = 20) -> list[dict]:
        return [json.loads(r) for r in self.client.lrange(self.history_key, 0, limit - 1)]

    def observe(self, consumer: str, generation: int) -> None:
        self.client.hset(
            self.observed_key, consumer, json.dumps({"generation": generation, "at": self._now()})
        )

    def observed(self) -> dict[str, dict]:
        return {k: json.loads(v) for k, v in self.client.hgetall(self.observed_key).items()}
