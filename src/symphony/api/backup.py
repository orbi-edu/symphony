"""Backup routes (`/v1/backup`, admin only): status, export and restore of the namespace's
Redis state, backups and the archive on the configured target (`symphony.backup`).

Every route that reads or replaces the whole state is audited (`backup.*`). Exports and
archive objects are sent with `Cache-Control: no-store`.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Iterator
from typing import Annotated, BinaryIO

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from .. import backup
from ..admin_auth import Caller
from ..config import Config, ConfigHolder
from ..edge import load_config
from .auth import Authenticator
from .control import Control
from .errors import ApiError

_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_NO_STORE = {"Cache-Control": "no-store"}


def mounted_workflow() -> Config | None:
    """The WORKFLOW the API pod mounts (`edge.load_config`), read on each call."""
    holder = ConfigHolder()
    load_config(holder)
    return holder.config


def _api_error(exc: backup.BackupError) -> ApiError:
    return ApiError(exc.status_code, exc.code, exc.message)


def _file_chunks(spool: BinaryIO) -> Iterator[bytes]:
    try:
        spool.seek(0)
        while chunk := spool.read(1024 * 1024):
            yield chunk
    finally:
        spool.close()


def build_router(
    control: Control,
    auth: Authenticator,
    *,
    config: Callable[[], Config | None] = mounted_workflow,
) -> APIRouter:
    router = APIRouter(prefix="/v1/backup")
    admin = Depends(auth.dependency("admin"))
    s = control.s
    namespace = s.namespace

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except backup.BackupError as exc:
            raise _api_error(exc) from None

    def target():
        return call(backup.target, config(), namespace)

    def source_namespace(value: str | None) -> str | None:
        if value is not None and not _NAMESPACE.fullmatch(value):
            raise ApiError(422, "invalid_request", "namespace_from must be a namespace name")
        return value

    def audit_restore(caller: Caller, target_text: str, report: dict) -> None:
        control._audit(
            caller, "backup.restore", target_text,
            f"restored {report['restored']} keys of namespace {report['source_namespace']} "
            f"from the backup of {report['created_at']}",
        )

    @router.get("")
    def status(_: Caller = admin):
        return backup.status(s.client, namespace, config())

    @router.get("/export")
    def export(caller: Caller = admin):
        control._audit(caller, "backup.export", "namespace", "download of the Redis state")
        stream = backup.Export(s.client, namespace)
        filename = f"symphony-{namespace}-{backup.stamp(stream.created_ms)}.ndjson.gz"
        return StreamingResponse(
            iter(stream),
            media_type="application/gzip",
            headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.post("/restore")
    async def restore(
        request: Request,
        confirm: bool = False,
        namespace_from: Annotated[str | None, Query(max_length=63)] = None,
        caller: Caller = admin,
    ):
        """The request body is a backup file (`application/gzip`)."""
        origin = source_namespace(namespace_from)
        if not confirm:
            raise _api_error(backup.BackupError(
                "confirm_required", "A restore replaces keys. Repeat it with --confirm.", 422
            ))
        await run_in_threadpool(call, backup.check_restorable, s.client, namespace)
        with tempfile.TemporaryFile() as spool:
            async for chunk in request.stream():
                spool.write(chunk)
            report = await run_in_threadpool(
                call, backup.restore, s.client, namespace, spool,
                confirm=True, namespace_from=origin,
            )
        audit_restore(caller, "namespace", report)
        return report

    @router.post("/run")
    def run(caller: Caller = admin):
        result = call(backup.run, s.client, namespace, config())
        control._audit(
            caller, "backup.run", f"backup:{result['backup']['name']}",
            f"backup of {result['backup']['records']} keys; archived "
            f"{result['archive']['sessions']} sessions and {result['archive']['audit']} "
            "audit entries",
        )
        return result

    @router.get("/backups")
    def backups(_: Caller = admin):
        dest = target()
        return {"target": dest.describe(), "items": call(dest.list, backup.BACKUPS)}

    @router.post("/backups/{name}/restore")
    def restore_object(
        name: str,
        confirm: bool = False,
        namespace_from: Annotated[str | None, Query(max_length=63)] = None,
        caller: Caller = admin,
    ):
        origin = source_namespace(namespace_from)
        dest = target()
        report = call(
            backup.restore_from, s.client, namespace, dest, name,
            confirm=confirm, namespace_from=origin,
        )
        audit_restore(caller, f"backup:{name}", report)
        return report

    @router.get("/archive")
    def archive(_: Caller = admin):
        dest = target()
        return {"target": dest.describe(), "items": call(dest.list, backup.ARCHIVE)}

    @router.get("/archive/{name}")
    def archive_object(name: str, caller: Caller = admin):
        call(backup.check_name, backup.ARCHIVE, name)
        dest = target()
        spool = tempfile.TemporaryFile()
        try:
            call(backup.read_archive, dest, name, spool)
        except BaseException:
            spool.close()
            raise
        control._audit(caller, "backup.archive.get", f"archive:{name}", "download of an archive")
        return StreamingResponse(
            _file_chunks(spool),
            media_type="application/gzip",
            headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{name}"'},
        )

    return router
