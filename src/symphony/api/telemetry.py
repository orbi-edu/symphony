"""Telemetry history routes (`symphony.rollups`; shapes in docs/api.md):

- `GET /v1/telemetry/history?from=YYYY-MM-DD&to=YYYY-MM-DD&bucket=day|week|month` (viewer) →
  `{observed_at, from, to, bucket, days, points:[{start, end, ...}], totals, by_model,
  by_repo, by_kind, activity:{rows, cols, cells, max, total}}`. One pipeline of HGETALL over
  the days that have a rollup; each answer is kept `CACHE_S`.
- `POST /v1/telemetry/backfill?force=false` (admin, audited `telemetry.backfill`) → `{days,
  kept, sessions, archive_objects, force, target}`: the session fields of past days rebuilt
  from the ended sessions in Redis and the archive on the configured backup target (none:
  Redis only).
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from .. import backup, rollups
from ..admin_auth import Caller
from ..config import Config
from .auth import Authenticator
from .backup import mounted_workflow
from .control import Control
from .errors import ApiError

CACHE_S = 60.0
_DAY = r"^\d{4}-\d{2}-\d{2}$"


def _date(value: str | None, name: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ApiError(422, "invalid_request", f"{name} must be a date (YYYY-MM-DD)") from None


def build_router(
    control: Control,
    auth: Authenticator,
    *,
    config: Callable[[], Config | None] = mounted_workflow,
    today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.UTC).date(),
    clock: Callable[[], float] = time.monotonic,
) -> APIRouter:
    router = APIRouter(prefix="/v1/telemetry")
    viewer = Depends(auth.dependency("viewer"))
    admin = Depends(auth.dependency("admin"))
    s = control.s
    cache: dict[tuple, tuple[float, dict]] = {}
    lock = threading.Lock()

    @router.get("/history")
    def history(
        start: Annotated[str | None, Query(alias="from", pattern=_DAY)] = None,
        end: Annotated[str | None, Query(alias="to", pattern=_DAY)] = None,
        bucket: Literal["day", "week", "month"] | None = None,
        _: Caller = viewer,
    ):
        first, last = _date(start, "from"), _date(end, "to") or today()
        if first is not None and first > last:
            raise ApiError(422, "invalid_request", "from must not be after to")
        key = (first, last, bucket)
        now = clock()
        with lock:
            hit = cache.get(key)
            if hit is not None and now - hit[0] < CACHE_S:
                return hit[1]
        observed = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        doc = rollups.history(s.client, first=first, last=last, bucket=bucket, observed_at=observed)
        with lock:
            for stale in [k for k, (at, _doc) in cache.items() if now - at >= CACHE_S]:
                del cache[stale]
            cache[key] = (now, doc)
        return doc

    @router.post("/backfill")
    def backfill(force: bool = False, caller: Caller = admin):
        try:
            dest = backup.target(config(), s.namespace)
        except backup.BackupError as exc:
            if exc.code != "not_configured":
                raise ApiError(exc.status_code, exc.code, exc.message) from None
            dest = None
        try:
            result = rollups.backfill(s.client, s.namespace, dest, force=force)
        except backup.BackupError as exc:
            raise ApiError(exc.status_code, exc.code, exc.message) from None
        result["target"] = dest.describe() if dest is not None else None
        control._audit(
            caller, "telemetry.backfill", "telemetry",
            f"rebuilt {result['days']} days from {result['sessions']} sessions and "
            f"{result['archive_objects']} archive objects" + (" (force)" if force else ""),
        )
        with lock:
            cache.clear()
        return result

    return router
