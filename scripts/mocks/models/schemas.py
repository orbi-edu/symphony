"""The vendored specifications and the validators built on them.

OpenAI: the OpenAPI 3.1 description from `openai/openai-openapi`, loaded verbatim. Two
OpenAPI idioms differ from plain JSON Schema and are honoured here: `oneOf` alternatives
that overlap but are told apart by a `discriminator` (an input message without `type` is
both an `EasyInputMessage` and an `Item`), so `oneOf` accepts any matching alternative; and
the OpenAPI 3.0 leftover `nullable: true`, which admits `null`.

Anthropic: no machine-readable spec is published at a stable URL, so
`anthropic_messages.schema.json` transcribes the vendored reference pages.
"""

from __future__ import annotations

import functools
import gzip
import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError, validators
from jsonschema.exceptions import best_match
from referencing import Registry
from referencing.jsonschema import DRAFT202012

SPECS = Path(__file__).resolve().parents[1] / "specs"
OPENAI_URI = "https://raw.githubusercontent.com/openai/openai-openapi/master/openapi.yaml"

_type = Draft202012Validator.VALIDATORS["type"]


def _one_of(validator, alternatives, instance, schema):
    errors = []
    for index, alternative in enumerate(alternatives):
        found = list(validator.descend(instance, alternative, schema_path=index))
        if not found:
            return
        errors.extend(found)
    yield ValidationError(f"{instance!r} is not valid under any of the given schemas", context=errors)


def _nullable_type(validator, types, instance, schema):
    if instance is None and schema.get("nullable") is True:
        return
    yield from _type(validator, types, instance, schema)


_OpenAPIValidator = validators.extend(
    Draft202012Validator, {"oneOf": _one_of, "type": _nullable_type}
)


@functools.cache
def openai_spec() -> dict:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    with gzip.open(SPECS / "openai.openapi.yaml.gz", "rb") as handle:
        return yaml.load(handle, Loader=loader)


@functools.cache
def anthropic_schema() -> dict:
    return json.loads((SPECS / "anthropic_messages.schema.json").read_text())


@functools.cache
def _openai_registry() -> Registry:
    return Registry().with_resource(OPENAI_URI, DRAFT202012.create_resource(openai_spec()))


@functools.cache
def openai_validator(pointer: str):
    """A validator for the OpenAI description's JSON pointer (`#/components/schemas/Error`)."""
    return _OpenAPIValidator({"$ref": f"{OPENAI_URI}{pointer}"}, registry=_openai_registry())


def openai_component(name: str):
    return openai_validator(f"#/components/schemas/{name}")


def openai_request_validator(path: str, method: str):
    """The validator for the JSON request body the operation itself declares."""
    body = openai_spec()["paths"][path][method]["requestBody"]["content"]["application/json"]
    return openai_validator(body["schema"]["$ref"])


def openai_request_properties(path: str, method: str) -> frozenset[str]:
    """Every top-level body field the operation's schema declares, through `$ref` and
    composition. The description leaves request objects open (no `additionalProperties:
    false`), while the API answers any other field with `unknown_parameter`."""
    body = openai_spec()["paths"][path][method]["requestBody"]["content"]["application/json"]
    return _declared(body["schema"]["$ref"])


@functools.cache
def _declared(pointer: str) -> frozenset[str]:
    node = openai_spec()
    for part in pointer.removeprefix("#/").split("/"):
        node = node[int(part)] if isinstance(node, list) else node[part]
    names = set(node.get("properties", {}))
    if "$ref" in node:
        names |= _declared(node["$ref"])
    for keyword in ("allOf", "anyOf", "oneOf"):
        for index in range(len(node.get(keyword, []))):
            names |= _declared(f"{pointer}/{keyword}/{index}")
    return frozenset(names)


@functools.cache
def anthropic_validator(definition: str, *, beta: bool = False, sampling: bool = False):
    """A validator for `#/$defs/<definition>`; for the request body, `beta` admits the
    beta-only top-level fields and `sampling` the ones API versions before 2026-10-21 keep."""
    definitions = anthropic_schema()["$defs"]
    if definition != "request":
        return Draft202012Validator({"$ref": f"#/$defs/{definition}", "$defs": definitions})
    request = json.loads(json.dumps(definitions["request"]))
    for flag, extra in ((beta, "beta_properties"), (sampling, "sampling_properties")):
        if flag:
            request["properties"].update(
                {k: v for k, v in definitions[extra].items() if k != "description"}
            )
    return Draft202012Validator({**request, "$defs": definitions})


def first_error(validator, instance) -> ValidationError | None:
    return best_match(validator.iter_errors(instance))


def dotted(error: ValidationError) -> str:
    return ".".join(str(part) for part in error.absolute_path)
