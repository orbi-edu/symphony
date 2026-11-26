"""Board routes: me, overview, issues, commands, audit, workflow and the read projections.

Response shapes (timestamps: `*_at`/`*At` are ISO-8601 UTC strings ending in `Z`, except the
token and session documents, which keep Redis epoch milliseconds):

- `GET /v1/overview` (ETag, 304) → `{version, daemon:{alive, paused, heartbeat_at}, counts:{running,
  waiting_input, queued, retrying, in_review, merging, held, blocked_on_person}, tokens_today,
  cost_today,
  merger:{reviewing, train, parked} (summed over every repository's merger), config_generation,
  rate_limits:[{model ("provider/model"),
  stage, remaining|null, limit|null, reset_at|null, retry_after_ms|null, observed_at, source
  ("usage_limit"|"retry")}], held:[{issue_id, identifier|null, by, reason, at}]}`;
  `rate_limits` holds only observations from the last hour; held tickets are counted only
  under `held` (also those Symphony never tracked).
  `version` is the Symphony release the API runs: the CLI and the Tern plugin update themselves
  to it (docs/cli.md).
- `GET /v1/issues` → `{items:[{issue_id, identifier, title, repo, status, phase ("held" when a
  person holds it), held:{by, reason, at}|null, blocked:{needs, reason, at}|null,
  tracker_state, attempt, seq, session_id, session_state, input_request, model, tokens, cost,
  pr_url, reason, updated_at, evidence_count}]}`; `status` and `phase` are `blocked_on_person`
  while the agent waits for a person (`symphony_blocked`), and `blocked` says for what
  (`needs` ∈ `answer`|`access`|`decision`|`other`); `reason` is why the issue cannot be
  dispatched, a code or null:
  `no_repo_designation`|`malformed_repo_designation`|`mixed_owner_repo_designation`|
  `repo_not_authorized`.
- `POST /v1/commands {action, target?, reason (8-1000)}` (operator, `Idempotency-Key` optional)
  → 202 command. `reevaluate` targets an identifier/issue id or `{issueId, dispatchSeq}`;
  `hold`/`release` target an identifier/issue id or `{issueId}` (any id, tracked or not);
  holding a held ticket is 409 `already_held`, releasing one that is not held 409 `not_held`.
- `GET /v1/audit?cursor=&limit=1..200` (default 50) → `{items:[{id, time, actor, action,
  target, result, reason, commandId|null}], nextCursor|null}`, newest first; pass
  `nextCursor` back as `cursor` for the next page.
- `GET /v1/merger` (viewer) → `{repos:[{repo ("owner/name"), alive, reviewing, parked, train,
  batch, queue}]}` sorted by repo: one merger Deployment per repository, each entry its
  `MergerStore.snapshot()`; `alive` is that merger's heartbeat. `POST /v1/merger/prs/{n}/
  {bypass|requeue|release} {reason (1-1000), repo?}` (operator) → 202 `{id, pr, action, actor,
  reason, at, repo}`; `repo` may be omitted only when one repository is registered (else 422
  `repo_required`), and must be registered (404 `unknown_repo`).
- The projections below carry no user-facing prose: every human-visible field is a stable
  code from the sets listed here, for clients to label. Each carries
  `observedAt` and `sources:{<name>:{status: "fresh"|"stale"|"unavailable", observedAt|null,
  note}}`, where `note` is English operator detail for logs, never for display. An
  "unavailable" source means "unknown", not "none".
- `GET /v1/repos` → `{observedAt, items:[{name ("owner/repo"), runtime|null, routes, issues|null,
  base_branch|null, configured_runtime|null, detected_runtime|null, components,
  components_source ("configured"|"detected"|null), default_branch|null, archived|null,
  merger_policy|null, policy_pr|null, provisioning_error|null}], sources:{state, workflow,
  learning, github}, limits:{runs, resources,
  truncated}}`; `routes` ⊆ `issue` (a tracked issue routes to it) | `retrospective` (a retained
  retrospective) | `default` (the workflow `default_repo`) | `installation` (the GitHub App
  reaches it, from the daemon's discovery) | `workflow` (WORKFLOW.md configures its base
  branch, runtime or components); `runtime` is what tracked runs used; `issues` counts
  tracked runs only.
- `GET /v1/infra` → `{observedAt, paused, heartbeatAt, services:[{name, role, status, detail}],
  resources:[{name, kind, status, component, detail, activePods|null, createdAt}],
  sources:{state, kubernetes}, limits:{resources, truncated}}`.
  services: `name` is `redis`, `daemon` or a pod name; `role` ∈ `redis`|`daemon`|`api`|
  `gateway`|`merger`|`runner`|`workspace`|`sweep`|`other`; `status` ∈ `healthy`|`degraded`|
  `unknown`; `detail` ∈ `ok`|`stale`|`unreadable` (redis, daemon heartbeat) or the pod code
  below. resources: `kind` ∈ `Pod`|`Job`|`PersistentVolumeClaim`; `status` is the raw
  Kubernetes phase; `detail` is a code — Pod `ready`|`not_ready`|`pending`|`failed`|
  `succeeded`|`unknown`, Job `active`|`complete`|`failed`|`unknown`, PVC `bound`|`pending`|
  `lost`|`unknown`; `component` is the pod's `component` label.
- `GET /v1/telemetry?limit=1..500` (default 200) → `{observedAt, totals:{input, output,
  cacheRead, cacheWrite, total, costEstimate}|null, models:[{model, requests, input, output}],
  monthly:[{month ("YYYY-MM"), dispatched, rework, prsOpened, prsMerged, learningPrs}],
  sessions:[{sid, identifier, kind, state, model, turn, tokens:{input, output, total, cost},
  updatedAt}], sources:{telemetry, metrics}, limits:{truncated, sessions}}`.
- `GET /v1/learning` → `{observedAt, items:[{id, identifier, repo, status, provenance, outcome,
  prUrl|null, updatedAt}], sources:{learning}, limits:{truncated}}`; `status` ∈ `pending`|
  `completed`|`failed`; `provenance` ∈ `retrospective`|`retained`; `outcome` ∈
  `nothing_to_learn`|`pr_opened`|`completed`|`failed`|`pending`.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__, wake
from ..admin_auth import Caller
from ..admin_commands import CommandError, public_command
from .auth import Authenticator
from .control import Control
from .errors import ApiError
from .sessions import json_with_etag

_REVISION = re.compile(r'"?([A-Za-z0-9_.:-]{1,128})"?\Z')


class CommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["pause", "resume", "refresh", "reevaluate", "hold", "release"]
    target: str | dict[str, Any] | None = None
    reason: Annotated[str, Field(min_length=8, max_length=1000)]


class WorkflowValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: Annotated[dict[str, Any], Field(min_length=1, max_length=61)]


class WorkflowPublishBody(WorkflowValidateBody):
    reason: Annotated[str, Field(min_length=8, max_length=1000)]
    documentHash: Annotated[str | None, Field(pattern=r"^[0-9a-f]{64}$")] = None


def if_match(value: str | None) -> str:
    """The resource revision named by `If-Match` (quoted or bare)."""
    if value is None:
        raise ApiError(428, "precondition_required", "an If-Match header is required")
    match = _REVISION.fullmatch(value.strip())
    if match is None:
        raise ApiError(422, "invalid_revision", "If-Match must name one resource revision")
    return match.group(1)


def build_router(control: Control, auth: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1")
    viewer = Depends(auth.dependency("viewer"))
    operator = Depends(auth.dependency("operator"))
    admin = Depends(auth.dependency("admin"))
    services = control.s

    @router.get("/me")
    def me(caller: Caller = viewer):
        return caller.public()

    @router.get("/overview")
    def overview(request: Request, _: Caller = viewer):
        return json_with_etag(request, {"version": __version__, **control.overview()})

    @router.get("/issues")
    def issues(_: Caller = viewer):
        return {"items": control.issues()}

    @router.post("/commands", status_code=202)
    def command(
        body: CommandBody,
        caller: Caller = operator,
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        return control.command(caller, body.action, body.reason, body.target, idempotency_key)

    @router.get("/commands/{command_id}")
    def get_command(command_id: str, _: Caller = viewer):
        return control.get_command(command_id)

    @router.get("/audit")
    def audit(
        cursor: Annotated[str | None, Query(max_length=256)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        _: Caller = viewer,
    ):
        return services.store.audit(cursor=cursor, limit=limit)

    def workflow():
        if services.workflow is None:
            raise ApiError(503, "workflow_unavailable", "the workflow document is not reachable")
        return services.workflow

    @router.get("/workflow")
    def get_workflow(response: Response, _: Caller = viewer):
        result = workflow().read()
        result["observed"] = services.store.workflow_observations()
        response.headers["ETag"] = '"' + result["revision"] + '"'
        return result

    @router.post("/workflow/validate")
    def validate_workflow(
        body: WorkflowValidateBody,
        _: Caller = admin,
        if_match_header: Annotated[str | None, Header(alias="If-Match")] = None,
    ):
        return workflow().validate(if_match(if_match_header), body.patch)

    @router.put("/workflow", status_code=202)
    def publish_workflow(
        body: WorkflowPublishBody,
        caller: Caller = admin,
        if_match_header: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ):
        """Validated here, published by the daemon (it holds the lease) as a durable command."""
        revision = if_match(if_match_header)
        review = workflow().validate(revision, body.patch)
        if body.documentHash is not None and review["documentHash"] != body.documentHash:
            raise ApiError(
                409, "review_mismatch", "the document does not match the reviewed changes"
            )
        payload = {
            "baseRevision": revision, "patch": body.patch, "documentHash": review["documentHash"],
        }
        key = idempotency_key or review["documentHash"][:32] + revision[-32:]
        try:
            record = services.store.accept(
                actor=caller.name, role=caller.role, credential_fingerprint=caller.token_id,
                action="workflow", target=None, reason=body.reason, idempotency_key=key,
                payload=payload,
            )
        except CommandError as exc:
            raise ApiError(exc.status_code, exc.code, exc.message) from None
        wake.wake(services.client, services.namespace, wake.DAEMON, "command:workflow")
        return public_command(record)

    def projection():
        if services.projection is None:
            raise ApiError(503, "projection_unavailable", "the read projection is not wired")
        return services.projection

    @router.get("/repos")
    def repos(_: Caller = viewer):
        return projection().repos()

    @router.get("/infra")
    def infra(_: Caller = viewer):
        return projection().infra()

    @router.get("/telemetry")
    def telemetry(limit: Annotated[int, Query(ge=1, le=500)] = 200, _: Caller = viewer):
        return projection().telemetry(services.sessions, limit=limit)

    @router.get("/learning")
    def learning(_: Caller = viewer):
        return projection().learning()

    return router
