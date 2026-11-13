"""Merger routes: what each repository's review/merge pipeline is doing, and PR actions."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field

from ..admin_auth import Caller
from .auth import Authenticator
from .control import Control


class ActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    # `owner/name`; required only when more than one repository has a merger.
    repo: Annotated[str | None, Field(min_length=3, max_length=200)] = None


def build_router(control: Control, auth: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/merger")
    viewer = Depends(auth.dependency("viewer"))
    operator = Depends(auth.dependency("operator"))

    @router.get("")
    def status(_: Caller = viewer):
        return control.merger_status()

    @router.post("/prs/{pr}/{action}", status_code=202)
    def action(
        pr: Annotated[int, Path(ge=1, le=10**9)],
        action: Literal["bypass", "requeue", "release"],
        body: ActionBody,
        caller: Caller = operator,
    ):
        return control.merger_action(caller, pr, action, body.reason, body.repo)

    return router
