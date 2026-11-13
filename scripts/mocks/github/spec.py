"""GitHub's published descriptions, loaded once: the REST OpenAPI (`api.github.com.json`) and the
GraphQL schema (`schema.docs.graphql`), both vendored under `scripts/mocks/specs/`.

The REST description is OpenAPI 3.0, whose `nullable: true` is not JSON Schema; it is rewritten
to `type: [..., "null"]` / `anyOf` once at load, and every schema is then checked with
jsonschema (Draft 4, the dialect OpenAPI 3.0 schemas are based on) through JSON-pointer refs into
the whole document, so request and response checks use GitHub's own definitions.
"""

from __future__ import annotations

import copy
import gzip
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from jsonschema import Draft4Validator
from referencing import Registry
from referencing.jsonschema import DRAFT4

SPECS = Path(__file__).resolve().parent.parent / "specs"
REST_FILE = SPECS / "github-api.github.com.json.gz"
GRAPHQL_FILE = SPECS / "github-schema.docs.graphql"
ROOT = "urn:github-rest"
DOCS = "https://docs.github.com/rest"
# Placeholder for required fields the mock has no data for; a fixed instant keeps it deterministic.
EPOCH = "2026-01-01T00:00:00Z"
# Parameters whose value is a path that may contain slashes (`contents/{path}`, `git/ref/{ref}`).
GREEDY = {"path", "ref", "basehead"}


class SpecError(Exception):
    """A request the description does not allow; carries GitHub's error response."""

    def __init__(self, status: int, message: str, documentation_url: str = DOCS, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.body = {"message": message, "documentation_url": documentation_url,
                     "status": str(status), **extra}


def pointer(*segments: str) -> str:
    return "/" + "/".join(s.replace("~", "~0").replace("/", "~1") for s in segments)


def _prepare(node):
    """OpenAPI 3.0 → JSON Schema: `nullable` becomes a null type, and every local `$ref` names
    the whole document, so any subschema validates on its own against the registry."""
    if isinstance(node, list):
        return [_prepare(x) for x in node]
    if not isinstance(node, dict):
        return node
    for key, value in list(node.items()):
        if key == "$ref" and isinstance(value, str) and value.startswith("#"):
            node[key] = ROOT + value
        else:
            node[key] = _prepare(value)
    if node.get("nullable") is True:
        del node["nullable"]
        kind = node.get("type")
        if isinstance(kind, str):
            node["type"] = [kind, "null"]
            if "enum" in node and None not in node["enum"]:
                node["enum"] = [*node["enum"], None]
            return node
        return {"anyOf": [node, {"type": "null"}]}
    return node


def _ref_ptr(ref: str) -> str:
    return ref.split("#", 1)[1]


@dataclass
class Operation:
    method: str
    template: str
    op: dict
    pointer: str
    params: dict = field(default_factory=dict)

    @property
    def docs(self) -> str:
        return (self.op.get("externalDocs") or {}).get("url") or DOCS


class RestSpec:
    def __init__(self, path: Path = REST_FILE) -> None:
        with gzip.open(path) as fh:
            self.doc = _prepare(json.load(fh))
        self.registry = Registry().with_resource(ROOT, DRAFT4.create_resource(self.doc))
        self._validators: dict[str, Draft4Validator] = {}
        self._lock = threading.Lock()
        self._routes: list[tuple[int, re.Pattern, list[str], str]] = []
        for template in self.doc["paths"]:
            names: list[str] = []
            parts = []
            for segment in template.strip("/").split("/"):
                m = re.fullmatch(r"\{([^}]+)\}", segment)
                if m:
                    names.append(m.group(1))
                    parts.append("(.+)" if m.group(1) in GREEDY else "([^/]+)")
                else:
                    parts.append(re.escape(segment))
            literal = sum(len(s) for s in template.split("/") if not s.startswith("{"))
            self._routes.append((literal, re.compile("/" + "/".join(parts)), names, template))
        self._routes.sort(key=lambda r: -r[0])

    # -- lookup -------------------------------------------------------------------
    def deref(self, node: dict) -> dict:
        while isinstance(node, dict) and "$ref" in node:
            node = self.at(_ref_ptr(node["$ref"]))
        return node

    def at(self, ptr: str):
        node = self.doc
        for seg in ptr.strip("/").split("/"):
            seg = seg.replace("~1", "/").replace("~0", "~")
            node = node[int(seg)] if isinstance(node, list) else node[seg]
        return node

    def validator(self, ptr: str) -> Draft4Validator:
        with self._lock:
            v = self._validators.get(ptr)
            if v is None:
                v = Draft4Validator({"$ref": f"{ROOT}#{ptr}"}, registry=self.registry)
                self._validators[ptr] = v
            return v

    def errors(self, ptr: str, value) -> list[str]:
        out = []
        for err in self.validator(ptr).iter_errors(value):
            where = "/".join(str(p) for p in err.absolute_path)
            out.append(f"{where or '(root)'}: {err.message}"[:500])
        return out

    def match(self, method: str, raw_path: str) -> Operation:
        """The operation for `method raw_path`, with its path parameters decoded and checked.
        A path the description does not have, or a method it does not list, is GitHub's 404."""
        for _, rx, names, template in self._routes:
            m = rx.fullmatch(raw_path)
            if not m:
                continue
            item = self.doc["paths"][template]
            op = item.get(method.lower())
            if op is None:
                continue
            values = {n: unquote(v) for n, v in zip(names, m.groups(), strict=True)}
            if any("/" in v for n, v in values.items() if n not in GREEDY):
                continue
            operation = Operation(method.upper(), template, op, pointer("paths", template, method.lower()))
            operation.params = self._check_params(operation, item, "path", values)
            return operation
        raise SpecError(404, "Not Found")

    def _parameters(self, operation: Operation, item: dict) -> list[dict]:
        params = [self.deref(p) for p in (item.get("parameters") or []) + (operation.op.get("parameters") or [])]
        return params

    def _check_params(self, operation: Operation, item: dict, where: str, values: dict) -> dict:
        out = {}
        for param in self._parameters(operation, item):
            if param.get("in") != where:
                continue
            name = param["name"]
            if name not in values:
                if param.get("required"):
                    raise SpecError(422, f"Invalid request.\n\n\"{name}\" wasn't supplied.", operation.docs)
                continue
            out[name] = self._coerce(operation, param, values[name])
        # Query parameters the description does not define are ignored, as GitHub does.
        return out

    def _coerce(self, operation: Operation, param: dict, raw):
        schema = self.deref(param.get("schema") or {})
        kind = schema.get("type")
        kinds = kind if isinstance(kind, list) else [kind]
        value = raw
        try:
            if "integer" in kinds:
                value = int(raw)
            elif "number" in kinds:
                value = float(raw)
            elif "boolean" in kinds:
                if raw not in ("true", "false"):
                    raise ValueError(raw)
                value = raw == "true"
            elif "array" in kinds:
                value = raw.split(",") if isinstance(raw, str) else raw
        except ValueError:
            raise SpecError(422, f"Invalid request.\n\n{param['name']}: {raw!r} is not of type {kind}.",
                            operation.docs) from None
        sub = Draft4Validator(schema, registry=self.registry)
        problems = [e.message for e in sub.iter_errors(value)]
        if problems:
            raise SpecError(422, f"Invalid request.\n\n{param['name']}: {problems[0]}", operation.docs)
        return value

    def query(self, operation: Operation, query: dict[str, str]) -> dict:
        item = self.doc["paths"][operation.template]
        return self._check_params(operation, item, "query", query)

    def body(self, operation: Operation, raw: bytes, content_type: str):
        """The decoded JSON body, checked against the operation's request schema."""
        rb = self.deref(operation.op.get("requestBody") or {})
        if not rb:
            return None
        content = rb.get("content") or {}
        if not raw.strip():
            if rb.get("required"):
                raise SpecError(400, "Problems parsing JSON", operation.docs)
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            raise SpecError(400, "Problems parsing JSON", operation.docs) from None
        media = "application/json" if "application/json" in content else next(iter(content), None)
        if media is None:
            return value
        ptr = operation.pointer + pointer("requestBody", "content", media, "schema")
        if "$ref" in (operation.op.get("requestBody") or {}):
            ptr = _ref_ptr(operation.op["requestBody"]["$ref"]) + pointer("content", media, "schema")
        problems = self.errors(ptr, value)
        if problems:
            raise SpecError(422, "Invalid request.\n\n" + "\n".join(problems[:5]), operation.docs)
        return value

    # -- responses ------------------------------------------------------------------
    def response_pointer(self, operation: Operation, status: int, media: str = "application/json") -> str | None:
        responses = operation.op.get("responses") or {}
        key = str(status) if str(status) in responses else None
        if key is None:
            return None
        resp_ptr = operation.pointer + pointer("responses", key)
        resp = responses[key]
        if "$ref" in resp:
            resp_ptr = _ref_ptr(resp["$ref"])
            resp = self.at(resp_ptr)
        content = resp.get("content") or {}
        if media not in content:
            return None
        if "schema" not in content[media]:
            return None
        return resp_ptr + pointer("content", media, "schema")

    def webhook_pointer(self, name: str) -> str | None:
        hooks = self.doc.get("x-webhooks") or {}
        if name not in hooks:
            return None
        return pointer("x-webhooks", name, "post", "requestBody", "content", "application/json", "schema")

    def conform(self, ptr: str | None, value, base: str = ""):
        """`value` with every REQUIRED field the mock does not model filled from the schema
        (null where nullable, the first enum value, an empty string/zero/false/list otherwise),
        so responses carry the documented shape. Fields the mock does set are kept as they are."""
        if ptr is None:
            return value
        return self._conform(self.at(ptr), value, base, 0)

    def _nullable_schema(self, schema: dict) -> bool:
        kind = schema.get("type")
        if isinstance(kind, list) and "null" in kind:
            return True
        for branch in schema.get("anyOf") or schema.get("oneOf") or []:
            b = self.deref(branch)
            if b.get("type") == "null" or (isinstance(b.get("type"), list) and "null" in b["type"]):
                return True
        return False

    def _object_branches(self, schema: dict) -> list[dict]:
        """The non-null branches of an anyOf/oneOf, nested ones flattened."""
        out = []
        for branch in schema.get("anyOf") or schema.get("oneOf") or []:
            b = self.deref(branch)
            if b.get("type") == "null":
                continue
            if ("anyOf" in b or "oneOf" in b) and not self._is_object(b):
                out.extend(self._object_branches(b))
            else:
                out.append(b)
        return out

    def _conform(self, schema, value, base: str, depth: int):
        schema = self.deref(schema)
        if value is None or depth > 40 or not isinstance(schema, dict):
            return value
        if "allOf" in schema:
            for sub in schema["allOf"]:
                value = self._conform(sub, value, base, depth + 1)
        if "anyOf" in schema or "oneOf" in schema:
            branches = self._object_branches(schema)
            if isinstance(value, dict):
                candidates = [b for b in branches if self._is_object(b)]
                for b in candidates:
                    trial = self._conform(b, copy.deepcopy(value), base, depth + 1)
                    if not list(Draft4Validator(b, registry=self.registry).iter_errors(trial))[:1]:
                        return trial
                if candidates:
                    return self._conform(candidates[0], value, base, depth + 1)
            elif isinstance(value, list):
                for b in branches:
                    if b.get("type") == "array" or (isinstance(b.get("type"), list) and "array" in b["type"]):
                        return self._conform(b, value, base, depth + 1)
            return value
        if isinstance(value, dict) and self._is_object(schema):
            props = schema.get("properties") or {}
            for key in schema.get("required") or []:
                if key not in value:
                    value[key] = self._default(props.get(key, {}), base, depth + 1)
            for key, sub in props.items():
                if key in value and value[key] is not None:
                    value[key] = self._conform(sub, value[key], base, depth + 1)
            return value
        if isinstance(value, list) and "items" in schema:
            return [self._conform(schema["items"], v, base, depth + 1) for v in value]
        return value

    def _is_object(self, schema: dict) -> bool:
        kind = schema.get("type")
        return kind == "object" or (isinstance(kind, list) and "object" in kind) or "properties" in schema \
            or "allOf" in schema

    def _default(self, schema, base: str, depth: int):
        schema = self.deref(schema)
        if not isinstance(schema, dict):
            return None
        if self._nullable_schema(schema):
            return None
        if "enum" in schema:
            return schema["enum"][0]
        if "allOf" in schema or self._is_object(schema):
            return self._conform(schema, {}, base, depth)
        if "anyOf" in schema or "oneOf" in schema:
            branches = self._object_branches(schema)
            return self._default(branches[0], base, depth) if branches else None
        kind = schema.get("type")
        kind = next((k for k in kind if k != "null"), None) if isinstance(kind, list) else kind
        if kind == "string":
            fmt = schema.get("format")
            if fmt == "date-time":
                return EPOCH
            if fmt == "uri":
                return f"{base}/"
            return ""
        if kind in ("integer", "number"):
            return 0
        if kind == "boolean":
            return False
        if kind == "array":
            return []
        return None


_rest: RestSpec | None = None
_rest_lock = threading.Lock()


def rest() -> RestSpec:
    global _rest
    with _rest_lock:
        if _rest is None:
            _rest = RestSpec()
        return _rest


_graphql = None
_graphql_ready = threading.Event()
_graphql_lock = threading.Lock()


def graphql_schema():
    """The GitHub GraphQL schema (graphql-core). Built once; ~0.5 s, so the server starts the
    build in the background and the first GraphQL request waits for it."""
    global _graphql
    with _graphql_lock:
        if _graphql is None:
            from graphql import build_schema

            # GitHub's published schema trips one rule newer graphql-core enforces (a deprecated
            # field implementing a non-deprecated interface field); it is GitHub's schema as served,
            # so it is taken as valid. Documents are still validated against it on every request.
            _graphql = build_schema(GRAPHQL_FILE.read_text(), assume_valid=True)
            _graphql_ready.set()
        return _graphql
