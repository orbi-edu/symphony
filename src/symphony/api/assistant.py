"""Assistant routes (admin): the active soul and the per-person memory, every call audited.

- `GET /v1/assistant/memory` → `{soul: {repo, path, sha} | null, people: [{login, count,
  updated_at}]}` (`soul` comes from the assistant's heartbeat; sha null: the file is missing).
- `GET /v1/assistant/memory/{login}` → `{login, items: [{id, text, kind, at, thread}]}`.
- `DELETE /v1/assistant/memory/{login}` → `{login, removed: N}` (wipe).
- `DELETE /v1/assistant/memory/{login}/{id}` → `{login, removed: item}`; 404 when unknown.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from ..admin_auth import Caller
from ..assistant.memory import MemoryStore
from .auth import Authenticator
from .control import Control
from .errors import not_found

Login = Annotated[str, Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")]
ItemId = Annotated[str, Path(pattern=r"^[0-9a-f]{8}$")]


def build_router(control: Control, auth: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/assistant")
    admin = Depends(auth.dependency("admin"))
    s = control.s

    def store() -> MemoryStore:
        return MemoryStore(s.client, s.namespace)

    def soul() -> dict | None:
        raw = s.client.get(f"sym:{s.namespace}:assistant:heartbeat")
        try:
            doc = json.loads(raw) if raw else {}
        except ValueError:
            return None
        return doc.get("soul") if isinstance(doc, dict) else None

    @router.get("/memory")
    def people(caller: Caller = admin):
        found = store().people()
        control._audit(caller, "assistant.memory_read", "memory", f"{len(found)} people")
        return {"soul": soul(), "people": found}

    @router.get("/memory/{login}")
    def person(login: Login, caller: Caller = admin):
        login = login.casefold()
        items = store().items(login)
        control._audit(caller, "assistant.memory_read", f"memory:{login}", f"{len(items)} items")
        return {"login": login, "items": items}

    @router.delete("/memory/{login}")
    def wipe(login: Login, caller: Caller = admin):
        login = login.casefold()
        removed = store().wipe(login)
        control._audit(caller, "assistant.memory_wipe", f"memory:{login}", f"{removed} items")
        return {"login": login, "removed": removed}

    @router.delete("/memory/{login}/{item_id}")
    def forget(login: Login, item_id: ItemId, caller: Caller = admin):
        login = login.casefold()
        gone = store().forget(login, item_id=item_id)
        if not gone:
            raise not_found(f"memory item {item_id} of {login}")
        control._audit(caller, "assistant.memory_forget", f"memory:{login}:{item_id}", "1 item")
        return {"login": login, "removed": gone[0]}

    return router
