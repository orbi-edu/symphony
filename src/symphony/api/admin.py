"""Admin routes: team configuration, the omp catalog, provider credentials, API tokens, MCP
servers; and the operator's viewer link to a device that an MCP session holds."""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..admin_auth import Caller, token_active
from ..apitokens import TokenError
from ..mcp_servers import McpError, held_by_live_session, probe, request_viewer
from ..providers import BuiltinProviderKey, ProviderDefinition, ProviderError
from ..teamconfig import ROLES, ConfigError, ConflictError
from .auth import Authenticator
from .board import if_match
from .catalog import CatalogError
from .control import Control
from .errors import ApiError, invalid, not_found
from .sessions import not_modified


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    models: dict[str, dict[str, Any]] | None = None
    omp: dict[str, Any] | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ObservedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation: Annotated[int, Field(ge=0)]


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=64)]
    role: str
    expires_days: Annotated[int | None, Field(ge=1, le=3650)] = None


def _generation(value: str | None) -> int:
    revision = if_match(value)
    if not revision.isdigit():
        raise ApiError(422, "invalid_revision", "If-Match must be the configuration generation")
    return int(revision)


class McpHeadersBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headers: dict[str, str | None]


class McpViewerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]
    control: bool = False


def _mcp_error(exc: McpError) -> JSONResponse:
    """`{error: {code, message, field?}}`; a message never carries a stored value."""
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.field:
        detail["field"] = exc.field
        detail["message"] = f"{exc.field}: {exc.message}"
    return JSONResponse(status_code=exc.status, content={"error": detail})


def build_router(control: Control, auth: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/admin")
    viewer = Depends(auth.dependency("viewer"))
    admin = Depends(auth.dependency("admin"))
    s = control.s

    # -- team configuration ----------------------------------------------------
    @router.get("/config")
    def get_config(response: Response, _: Caller = viewer):
        document = s.teamconfig.get()
        response.headers["ETag"] = f'"{document["generation"]}"'
        return document

    @router.put("/config")
    def publish_config(
        body: ConfigBody,
        response: Response,
        caller: Caller = admin,
        if_match_header: Annotated[str | None, Header(alias="If-Match")] = None,
    ):
        expected = _generation(if_match_header)
        current = s.teamconfig.get()["generation"]
        if current != expected:
            raise ApiError(
                412, "precondition_failed", f"generation {current} is current; reload and retry"
            )
        patch = {key: value for key, value in (("models", body.models), ("omp", body.omp)) if value}
        if not patch:
            raise invalid("nothing to publish: send models and/or omp")
        try:
            document = s.teamconfig.publish(
                patch, expected_generation=expected, actor=caller.name, reason=body.reason
            )
        except ConflictError as exc:
            raise ApiError(409, "conflict", str(exc)) from None
        except ConfigError as exc:
            raise invalid(str(exc)) from None
        control._audit(
            caller, "teamconfig.publish", f"generation:{document['generation']}", body.reason
        )
        response.headers["ETag"] = f'"{document["generation"]}"'
        return document

    @router.get("/config/observed")
    def observed(_: Caller = viewer):
        return s.teamconfig.observed()

    @router.post("/config/observed", status_code=204)
    def observe(body: ObservedBody, caller: Caller = viewer):
        """`symphony omp sync` reports the generation a member applied locally."""
        s.teamconfig.observe(f"member:{caller.name}", body.generation)
        return Response(status_code=204)

    # -- catalog ---------------------------------------------------------------
    @router.get("/catalog")
    def catalog(
        request: Request,
        provider: Annotated[str | None, Query(max_length=64)] = None,
        _: Caller = viewer,
    ):
        try:
            body = s.catalog.body()
        except CatalogError as exc:
            raise ApiError(503, "catalog_unavailable", str(exc)) from None
        etag = f'"{s.catalog.version}"'
        if not_modified(request, etag):
            return Response(status_code=304, headers={"ETag": etag})
        if provider is not None:
            document = json.loads(body)
            document["providers"] = [p for p in document["providers"] if p["id"] == provider]
            if not document["providers"]:
                raise not_found("provider")
            return JSONResponse(document, headers={"ETag": etag})
        return Response(body, media_type="application/json", headers={"ETag": etag})

    # -- providers -------------------------------------------------------------
    def providers():
        if s.providers is None:
            raise ApiError(503, "providers_unavailable", "provider storage is not reachable")
        return s.providers

    @router.get("/providers")
    def list_providers(response: Response, _: Caller = admin):
        result = providers().read()
        if result["revision"] is not None:
            response.headers["ETag"] = '"' + result["revision"] + '"'
        return result

    @router.put("/providers/{provider_id}")
    def save_provider(
        provider_id: str,
        body: BuiltinProviderKey | ProviderDefinition,
        response: Response,
        caller: Caller = admin,
        if_match_header: Annotated[str | None, Header(alias="If-Match")] = None,
    ):
        """A custom provider, or `{builtin: true, apiKey?}`: only the key of a provider the
        pinned omp already knows (internals §13). The key is write-only; omitting it keeps the
        stored one."""
        repository = providers()
        revision = if_match(if_match_header)
        if isinstance(body, BuiltinProviderKey):
            try:
                known = s.catalog.provider_ids()
            except CatalogError as exc:
                raise ApiError(503, "catalog_unavailable", str(exc)) from None
            if provider_id not in known:
                raise ApiError(
                    422, "unknown_provider", "the pinned omp has no built-in provider with this id"
                )
        metadata = {
            "actor": caller.name, "provider_id": provider_id,
            "operation_id": secrets.token_hex(16),
        }
        s.store.provider_audit(**metadata, outcome="attempted")
        writing = False

        def before_write():
            nonlocal writing
            # The token may have been revoked while the Secret was being read.
            if not token_active(s.tokens, caller.token_id, "admin"):
                raise ApiError(401, "invalid_token", "the token was revoked before saving")
            writing = True

        try:
            result = repository.save(
                provider_id, body.model_dump(exclude_unset=True), revision,
                before_write=before_write,
            )
        except (ProviderError, ApiError) as exc:
            unknown = getattr(exc, "code", "") == "providers_outcome_unknown"
            s.store.provider_audit(**metadata, outcome="unknown" if unknown else "failed")
            raise
        except Exception:
            s.store.provider_audit(**metadata, outcome="unknown" if writing else "failed")
            raise ApiError(
                503,
                "providers_outcome_unknown" if writing else "providers_unavailable",
                "provider saving could not be confirmed; reload providers before saving again",
            ) from None
        s.store.provider_audit(**metadata, outcome="succeeded")
        response.headers["ETag"] = '"' + result["revision"] + '"'
        return result

    # -- tokens ----------------------------------------------------------------
    @router.get("/tokens")
    def list_tokens(_: Caller = admin):
        return {"items": s.tokens.list()}

    @router.post("/tokens", status_code=201)
    def create_token(body: TokenBody, caller: Caller = admin):
        try:
            token, record = s.tokens.create(
                body.name, body.role, actor=caller.name,
                expires_in_s=body.expires_days * 86400 if body.expires_days else None,
            )
        except TokenError as exc:
            raise invalid(str(exc)) from None
        control._audit(caller, "token.create", f"token:{record['id']}", f"{body.role} {body.name}")
        return JSONResponse({"token": token, **record}, status_code=201,
                            headers={"Cache-Control": "no-store"})

    @router.delete("/tokens/{token_id}")
    def revoke_token(token_id: str, caller: Caller = admin):
        try:
            record = s.tokens.revoke(token_id, actor=caller.name)
        except TokenError:
            raise not_found("token") from None
        control._audit(caller, "token.revoke", f"token:{token_id}", record["name"])
        return record

    # -- MCP servers -------------------------------------------------------------
    def mcp():
        if s.mcp is None:
            raise ApiError(503, "mcp_unavailable", "MCP server storage is not reachable")
        return s.mcp

    @router.get("/mcp")
    def list_mcp(_: Caller = admin):
        try:
            return {"servers": mcp().list(), "roles": list(ROLES)}
        except McpError as exc:
            return _mcp_error(exc)

    @router.put("/mcp/{name}")
    def put_mcp(name: str, body: Annotated[Any, Body()], caller: Caller = admin):
        try:
            stored = mcp().put(name, body)
        except McpError as exc:
            return _mcp_error(exc)
        control._audit(caller, "admin.mcp.put", f"mcp:{name}",
                       f"{stored['transport']} roles={','.join(stored['roles']) or '-'}")
        return stored

    @router.delete("/mcp/{name}", status_code=204)
    def delete_mcp(name: str, caller: Caller = admin):
        try:
            mcp().remove(name)
        except McpError as exc:
            return _mcp_error(exc)
        control._audit(caller, "admin.mcp.remove", f"mcp:{name}", "removed")
        return Response(status_code=204)

    @router.put("/mcp/{name}/headers")
    def put_mcp_headers(name: str, body: McpHeadersBody, caller: Caller = admin):
        try:
            names = mcp().set_headers(name, body.headers)
        except McpError as exc:
            return _mcp_error(exc)
        changed = ",".join(sorted(body.headers))
        control._audit(caller, "admin.mcp.headers", f"mcp:{name}", f"changed={changed}")
        return {"headers_set": names}

    @router.post("/mcp/{name}/test")
    async def test_mcp(name: str, caller: Caller = admin):
        repository = mcp()
        try:
            definition = await run_in_threadpool(repository.get, name)
            if definition is None:
                raise ApiError(404, "not_found", "mcp server not found")
            stored = await run_in_threadpool(repository.headers, name)
        except McpError as exc:
            return _mcp_error(exc)
        result = await probe(definition, stored)
        outcome = "ok" if result["ok"] else ("stdio" if result["ok"] is None else result["error"]["code"])
        await run_in_threadpool(control._audit, caller, "admin.mcp.test", f"mcp:{name}", outcome)
        return result

    # -- Skills ------------------------------------------------------------------
    @router.get("/skills")
    def list_skills(_: Caller = admin):
        """Configured skill sources and their current resolution status."""
        from ..admin_workflow import WorkflowError as WFErr
        from ..config import ConfigHolder
        from ..skills import resolve_sources
        holder = ConfigHolder()
        try:
            if s.workflow is not None:
                doc = s.workflow.read()
                holder.update(doc.get("text", ""))
        except (WFErr, Exception):
            pass
        cfg = holder.config
        if cfg is None or not cfg.skill_sources:
            return {"sources": []}
        import os
        import time
        sources_out = []
        for src in cfg.skill_sources:
            entry: dict = {
                "repo": src.repo,
                "path": src.path,
                "ref": src.ref,
                "roles": list(src.roles) if src.roles else None,
                "repos": list(src.repos) if src.repos else None,
                "scope": "repos" if src.repos else "global",
                "sha": None,
                "skills": [],
                "error": None,
            }
            try:
                resolved = resolve_sources(
                    [src], "implementer", None, os.environ,
                    lambda: int(time.time() * 1000),
                    cache_client=s.client, cache_ns=s.namespace,
                )
                if resolved:
                    rs = resolved[0]
                    entry["sha"] = rs.sha or None
                    entry["skills"] = [sk.name for sk in rs.skills]
                    entry["error"] = rs.error
            except Exception as exc:
                entry["error"] = str(exc)
            sources_out.append(entry)
        return {"sources": sources_out}

    return router


def build_mcp_router(control: Control, auth: Authenticator) -> APIRouter:
    """`POST /v1/mcp/{name}/viewer`: a viewer link to a device that a live Symphony session
    holds on that server (internals §18). Operators only; audited `mcp.viewer`."""
    router = APIRouter(prefix="/v1/mcp")
    operator = Depends(auth.dependency("operator"))
    s = control.s

    @router.post("/{name}/viewer")
    async def viewer(name: str, body: McpViewerBody, caller: Caller = operator):
        if s.mcp is None:
            raise ApiError(503, "mcp_unavailable", "MCP server storage is not reachable")
        try:
            definition = await run_in_threadpool(s.mcp.get, name)
            held = definition is not None and await run_in_threadpool(
                held_by_live_session, s.client, s.namespace, name, body.lease_id)
            if not held:
                raise ApiError(404, "lease_not_held", "no live Symphony session holds this lease")
            stored = await run_in_threadpool(s.mcp.headers, name)
            link = await request_viewer(definition, stored, body.lease_id, body.control)
        except McpError as exc:
            await run_in_threadpool(control._audit, caller, "mcp.viewer", f"mcp:{name}",
                                    f"lease={body.lease_id} control={body.control} {exc.code}")
            return _mcp_error(exc)
        await run_in_threadpool(control._audit, caller, "mcp.viewer", f"mcp:{name}",
                                f"lease={body.lease_id} control={body.control}")
        return JSONResponse(link, headers={"Cache-Control": "no-store"})

    return router
