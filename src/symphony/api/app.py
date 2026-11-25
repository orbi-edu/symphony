"""The control API (`symphony-api`, port 8080): `/v1` for people and tools, `/mcp`, webhooks."""

from __future__ import annotations

import logging
import os
import time

import redis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from .. import previews
from ..admin_commands import AdminStore, CommandError
from ..admin_read import AdminProjection, LiveReader, ProjectionError
from ..admin_workflow import WorkflowError
from ..apitokens import TokenStore
from ..providers import ProviderError
from ..sessions import SessionStore
from ..teamconfig import TeamConfig
from . import admin, assistant, backup, board, mcp, merger, sessions, setup, webhooks
from . import previews as preview_routes
from .auth import Authenticator
from .catalog import Catalog
from .control import Control, MergerFleet, Services
from .errors import ApiError, error_response

log = logging.getLogger("symphony.api")

_DOMAIN_ERRORS = (CommandError, WorkflowError, ProviderError, ProjectionError)


def build_app(services: Services, *, per_minute: int | None = None) -> FastAPI:
    app = FastAPI(title="Symphony control API", docs_url=None, redoc_url=None, openapi_url=None)
    auth = Authenticator(
        services.client,
        services.namespace,
        services.tokens,
        per_minute=per_minute or int(os.environ.get("SYMPHONY_API_RATE_LIMIT", "600")),
    )
    control = Control(services)
    control.setup = setup.SetupService(control)

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    async def domain_error(_: Request, exc):
        # Workflow validation explains itself field by field.
        return error_response(
            exc.status_code, exc.code, exc.message, diagnostics=getattr(exc, "diagnostics", None)
        )

    for error_type in _DOMAIN_ERRORS:
        app.add_exception_handler(error_type, domain_error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        # Never echo the input: bodies may carry API keys or refresh tokens.
        fields = sorted({".".join(str(p) for p in err["loc"][1:]) for err in exc.errors()})
        return error_response(
            422, "invalid_request", "invalid request: " + (", ".join(fields) or "body")
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "invalid_request")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        log.exception("api_unhandled_error", exc_info=exc)
        return error_response(503, "unavailable", "a backing service is unavailable")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            services.client.ping()
        except Exception:  # noqa: BLE001 — readiness reports, never raises
            return error_response(503, "unavailable", "redis is unreachable")
        return {"status": "ready"}

    app.include_router(board.build_router(control, auth))
    app.include_router(sessions.build_router(control, auth))
    app.include_router(admin.build_router(control, auth))
    app.include_router(admin.build_mcp_router(control, auth))
    app.include_router(merger.build_router(control, auth))
    app.include_router(webhooks.build_router(control))
    app.include_router(mcp.build_router(control, auth))
    app.include_router(setup.build_router(control, auth))
    app.include_router(assistant.build_router(control, auth))
    app.include_router(backup.build_router(control, auth))
    app.include_router(preview_routes.build_router(control))
    return app


def wait_for_redis(client, *, sleep=time.sleep, max_delay_s: float = 5.0, attempts=None) -> None:
    """Block until Redis answers, backing off exponentially, so the API waits for a Redis that
    is still starting instead of crash-looping. `attempts=None` waits forever."""
    delay, tried = 0.1, 0
    while True:
        try:
            client.ping()
            return
        except (redis.ConnectionError, redis.TimeoutError, OSError) as exc:
            tried += 1
            if attempts is not None and tried >= attempts:
                raise
            log.warning("redis_not_ready retry_in=%.1fs error=%s", delay, exc)
            sleep(delay)
            delay = min(delay * 2, max_delay_s)


def build_services(
    client, aclient, namespace: str, *, kv, core=None, batch=None, secrets=None, workflow=None,
    mcp=None,
) -> Services:
    """Production wiring from one Redis (sync + asyncio clients) and the Kubernetes API.

    The local stack passes its own `secrets` (setup_secrets_local), file-backed `workflow` and
    `mcp` (`<root>/mcp.json`); otherwise they come from Kubernetes. Webhook secrets are read
    from the environment at request time, so a restart or a local secret change applies
    without rebuilding."""
    from ..admin_workflow import WorkflowRepository
    from ..providers import ProviderRepository

    tokens = TokenStore(client, namespace)
    bootstrap = os.environ.get("SYMPHONY_BOOTSTRAP_TOKEN")
    if bootstrap:
        tokens.ensure_bootstrap(bootstrap.strip())
    session_store = SessionStore(client, namespace)
    teamconfig = TeamConfig(client, namespace)
    store = AdminStore(client, namespace)
    merger_fleet = MergerFleet(client, namespace)
    if workflow is None and core is not None:
        workflow = WorkflowRepository(core, namespace)
    if secrets is None and core is not None:
        from ..setup_secrets import KubernetesSecretStore

        secrets = KubernetesSecretStore(core, namespace)
    provider_repo = ProviderRepository(core, namespace) if core is not None else None
    if mcp is None and core is not None:
        from ..mcp_servers import McpRepository

        mcp = McpRepository(core, namespace)
    return Services(
        client=client,
        aclient=aclient,
        namespace=namespace,
        sessions=session_store,
        tokens=tokens,
        teamconfig=teamconfig,
        store=store,
        kv=kv,
        reader=LiveReader(
            client, sessions=session_store, teamconfig=teamconfig, store=store, merger=merger_fleet
        ),
        projection=AdminProjection(kv, store, core, batch, namespace, workflow),
        catalog=Catalog(),
        workflow=workflow,
        providers=provider_repo,
        merger=merger_fleet,
        secrets=secrets,
        mcp=mcp,
        previews=previews.from_env(client, namespace, mcp, os.environ),
    )
