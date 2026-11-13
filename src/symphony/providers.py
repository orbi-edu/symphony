"""Named-Secret provider configuration and credential-isolated OMP projections (internals §13).

Two kinds of item, both keyed by provider id and both by API key — there is no OAuth or
subscription path: a key for one of omp's built-in providers (`{builtin: true, apiKey}`,
which only replaces the credential), or a provider of the team's own (`{name, api,
baseUrl, auth, models, apiKey?}`). Workers read the Secret at every attempt, so a key saved
in the Admin screen applies to the next attempt without restarts.
"""

from __future__ import annotations

import base64
import copy
import json
import math
import os
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

_REQUEST_TIMEOUT = (3, 10)
_MAX_DOCUMENT = 512 * 1024
_MAX_PROVIDERS = 100
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
_REVISION = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_WRITE_ANNOTATION = "symphony.dev/provider-write"
_ENV_KEY = "SYMPHONY_PROVIDER_API_KEY"


class ProviderError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def _invalid() -> ProviderError:
    return ProviderError(
        "invalid_provider",
        "Provide a valid provider, supported authentication, and bounded model definitions.",
    )


def _clean(value: str) -> str:
    if not value.strip() or any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError("Invalid text")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelCost(_StrictModel):
    input: Annotated[StrictFloat | StrictInt, Field(ge=0)]
    output: Annotated[StrictFloat | StrictInt, Field(ge=0)]
    cacheRead: Annotated[StrictFloat | StrictInt, Field(ge=0)]
    cacheWrite: Annotated[StrictFloat | StrictInt, Field(ge=0)]

    @field_validator("input", "output", "cacheRead", "cacheWrite")
    @classmethod
    def finite(cls, value):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("Finite costs required")
        return value


class ModelDefinition(_StrictModel):
    id: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    name: Annotated[StrictStr, Field(min_length=1, max_length=100)] | None = None
    contextWindow: Annotated[StrictInt, Field(gt=0, le=9_007_199_254_740_991)] | None = None
    maxTokens: Annotated[StrictInt, Field(gt=0, le=9_007_199_254_740_991)] | None = None
    reasoning: StrictBool | None = None
    input: Annotated[list[Literal["text", "image"]], Field(min_length=1, max_length=2)] | None = (
        None
    )
    cost: ModelCost | None = None

    @field_validator("id", "name")
    @classmethod
    def clean_text(cls, value):
        return _clean(value) if value is not None else value

    @model_validator(mode="after")
    def consistent(self):
        if any(char.isspace() for char in self.id):
            raise ValueError("Model identity must not contain whitespace")
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Omit optional metadata rather than supplying null")
        if self.input is not None and len(set(self.input)) != len(self.input):
            raise ValueError("Duplicate input type")
        if (
            self.contextWindow is not None
            and self.maxTokens is not None
            and self.maxTokens > self.contextWindow
        ):
            raise ValueError("Output limit exceeds context window")
        return self


class BuiltinProviderKey(_StrictModel):
    """The credential for one of omp's built-in providers; models and endpoint stay omp's."""

    builtin: Literal[True]
    apiKey: Annotated[StrictStr, Field(min_length=1, max_length=8192)] | None = Field(
        default=None, repr=False
    )

    @field_validator("apiKey")
    @classmethod
    def clean_text(cls, value):
        return _clean(value) if value is not None else value

    @model_validator(mode="after")
    def consistent(self):
        if "apiKey" in self.model_fields_set and self.apiKey is None:
            raise ValueError("Omit the API key to preserve it")
        return self


class ProviderDefinition(_StrictModel):
    name: Annotated[StrictStr, Field(min_length=1, max_length=100)]
    api: Literal[
        "openai-completions",
        "openai-responses",
        "anthropic-messages",
        "google-generative-ai",
    ]
    baseUrl: Annotated[StrictStr, Field(min_length=1, max_length=2048)]
    auth: Literal["apiKey", "none"]
    models: Annotated[list[ModelDefinition], Field(min_length=1, max_length=50)]
    apiKey: Annotated[StrictStr, Field(min_length=1, max_length=8192)] | None = Field(
        default=None, repr=False
    )

    @field_validator("name", "apiKey")
    @classmethod
    def clean_text(cls, value):
        return _clean(value) if value is not None else value

    @field_validator("baseUrl")
    @classmethod
    def endpoint(cls, value):
        _clean(value)
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or any(char.isspace() for char in value)
            or "\\" in value
        ):
            raise ValueError("Absolute HTTP endpoint required")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid endpoint port")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if "apiKey" in self.model_fields_set and self.apiKey is None:
            raise ValueError("Omit the API key to preserve it")
        if self.auth == "none" and self.api not in {"openai-completions", "openai-responses"}:
            raise ValueError("This protocol requires an API key")
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("Duplicate model identity")
        return self


@dataclass(frozen=True)
class ProviderRuntime:
    provider_id: str
    models_config: dict
    environment: dict[str, str] = field(repr=False)
    # A built-in provider key rides on the host `model-provider` env; a custom provider
    # carries everything it needs.
    builtin: bool = False


class ProviderRepository:
    def __init__(self, core, namespace: str, name: str | None = None) -> None:
        self.core = core
        self.namespace = namespace
        self.name = name or os.environ.get("SYMPHONY_SECRET_PROVIDERS", "symphony-providers")

    def _load(self):
        try:
            secret = self.core.read_namespaced_secret(
                name=self.name, namespace=self.namespace, _request_timeout=_REQUEST_TIMEOUT
            )
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None, {"schemaVersion": 1, "providers": {}}
            raise ProviderError(
                "providers_unavailable", "Managed provider storage is unavailable.", 503
            ) from None
        try:
            if not isinstance(secret.metadata.resource_version, str) or not _REVISION.fullmatch(
                secret.metadata.resource_version
            ):
                raise ValueError("Invalid revision")
            encoded = (secret.data or {}).get("providers.json")
            if encoded is None:
                return secret, {"schemaVersion": 1, "providers": {}}
            if not isinstance(encoded, str) or len(encoded) > (_MAX_DOCUMENT + 2) // 3 * 4:
                raise ValueError("Invalid document size")
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > _MAX_DOCUMENT:
                raise ValueError("Invalid document size")
            document = json.loads(raw, object_pairs_hook=_unique_object)
            if (
                not isinstance(document, dict)
                or set(document) != {"schemaVersion", "providers"}
                or type(document["schemaVersion"]) is not int
                or document["schemaVersion"] != 1
                or not isinstance(document["providers"], dict)
                or len(document["providers"]) > _MAX_PROVIDERS
            ):
                raise ValueError("Invalid document")
            for provider_id, definition in document["providers"].items():
                self._validate(provider_id, definition, stored=True)
            return secret, document
        except Exception:
            raise ProviderError(
                "providers_invalid",
                "Managed provider storage is malformed; repair it on the host before use.",
                503,
            ) from None

    @staticmethod
    def _validate(provider_id, definition, *, stored=False):
        if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
            raise _invalid()
        try:
            if isinstance(definition, dict) and "builtin" in definition:
                result = BuiltinProviderKey.model_validate(definition).model_dump(exclude_unset=True)
                if stored and not result.get("apiKey"):
                    raise ValueError("A stored built-in provider item is its key")
                return result
            validated = ProviderDefinition.model_validate(definition)
            result = validated.model_dump(exclude_unset=True)
            if stored and (
                (validated.auth == "apiKey" and not validated.apiKey)
                or (validated.auth != "apiKey" and "apiKey" in result)
            ):
                raise ValueError("Inconsistent stored authentication")
            return result
        except (ValidationError, ValueError, TypeError):
            raise _invalid() from None

    @staticmethod
    def _public(secret, document):
        return {
            "revision": secret.metadata.resource_version if secret is not None else None,
            "storageReady": secret is not None,
            "items": [
                dict(
                    {
                        key: copy.deepcopy(value)
                        for key, value in definition.items()
                        if key != "apiKey"
                    },
                    id=provider_id,
                    credentialConfigured=bool(definition.get("apiKey")),
                )
                for provider_id, definition in sorted(document["providers"].items())
            ],
        }

    def read(self) -> dict:
        return self._public(*self._load())

    def save(
        self,
        provider_id,
        provider_definition,
        expected_revision,
        *,
        before_write: Callable[[], None] | None = None,
    ) -> dict:
        definition = self._validate(provider_id, provider_definition)
        secret, document = self._load()
        if secret is None:
            raise ProviderError(
                "providers_not_bootstrapped",
                "Managed provider storage is not bootstrapped. Ask the host administrator to install the named providers Secret.",
                503,
            )
        if expected_revision != secret.metadata.resource_version:
            raise ProviderError(
                "providers_conflict", "Providers changed; reload before saving again.", 412
            )
        existing = document["providers"].get(provider_id, {})
        if definition.get("builtin") or definition["auth"] == "apiKey":
            if "apiKey" not in definition:
                # Omitting the key keeps the stored one (the key is write-only).
                if not existing.get("apiKey"):
                    raise ProviderError(
                        "provider_key_required", "An API key is required for this provider."
                    )
                definition["apiKey"] = existing["apiKey"]
        else:
            definition.pop("apiKey", None)
        document["providers"][provider_id] = definition
        if len(document["providers"]) > _MAX_PROVIDERS:
            raise ProviderError(
                "providers_full", "Managed provider storage has reached its provider limit."
            )
        encoded = json.dumps(
            document, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
        if len(encoded) > _MAX_DOCUMENT:
            raise ProviderError(
                "providers_full", "Managed provider storage has reached its size limit."
            )
        replacement = copy.deepcopy(secret)
        replacement.data = dict(secret.data or {})
        replacement.data["providers.json"] = base64.b64encode(encoded).decode("ascii")
        replacement.metadata.annotations = dict(secret.metadata.annotations or {})
        write_id = uuid.uuid4().hex
        replacement.metadata.annotations[_WRITE_ANNOTATION] = write_id
        if before_write is not None:
            before_write()
        try:
            result = self.core.replace_namespaced_secret(
                name=self.name,
                namespace=self.namespace,
                body=replacement,
                _request_timeout=_REQUEST_TIMEOUT,
            )
            if not isinstance(result.metadata.resource_version, str) or not _REVISION.fullmatch(
                result.metadata.resource_version
            ):
                raise ValueError("Missing revision")
            return self._public(result, document)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 409:
                raise ProviderError(
                    "providers_conflict",
                    "Providers changed during saving; reload before saving again.",
                    412,
                ) from None
            if status in {400, 401, 403, 404, 422}:
                raise ProviderError(
                    "providers_rejected",
                    "Kubernetes rejected the provider save; stored configuration was not changed.",
                    503,
                ) from None
        # One bounded read-back can confirm an acknowledged mutation; never retry a write.
        try:
            observed, observed_document = self._load()
            if (
                observed is not None
                and (observed.metadata.annotations or {}).get(_WRITE_ANNOTATION) == write_id
                and observed_document == document
            ):
                return self._public(observed, observed_document)
        except ProviderError:
            pass
        raise ProviderError(
            "providers_outcome_unknown",
            "The save outcome could not be confirmed. Reload providers before deciding whether to save again.",
            503,
        ) from None

    def runtime(self, provider_id, model_id) -> ProviderRuntime | None:
        """What one unit of work stages for `provider_id/model_id`, read fresh each call;
        None when the team stored nothing for it (the host `model-provider` env applies)."""
        _, document = self._load()
        definition = document["providers"].get(provider_id)
        if definition is None:
            return None
        environment = {_ENV_KEY: definition["apiKey"]} if definition.get("apiKey") else {}
        if definition.get("builtin"):
            # An entry with only `apiKey` overrides a built-in provider's credential in
            # omp 18.20.2; its models, endpoint and protocol stay the engine's own.
            return ProviderRuntime(
                provider_id,
                {"providers": {provider_id: {"apiKey": _ENV_KEY}}},
                environment,
                builtin=True,
            )
        model = next((model for model in definition["models"] if model["id"] == model_id), None)
        if model is None:
            raise ProviderError(
                "provider_model_missing",
                "The workflow-selected model is not configured in its managed provider.",
                503,
            )
        provider = {key: definition[key] for key in ("api", "baseUrl", "auth")}
        provider["models"] = [copy.deepcopy(model)]
        if definition["auth"] == "apiKey":
            provider["apiKey"] = _ENV_KEY
        return ProviderRuntime(provider_id, {"providers": {provider_id: provider}}, environment)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate document field")
        result[key] = value
    return result
