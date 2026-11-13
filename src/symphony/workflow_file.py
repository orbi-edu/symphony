"""WORKFLOW.md on disk as the one ConfigMap `admin_workflow.WorkflowRepository` edits.

Local mode has no Kubernetes API, but its workflow is still published the reviewed way:
`file_workflow(path, ...)` returns a real `WorkflowRepository` whose `core` is this shim, so
validation, the allowlisted patch, the credential checks, the command replay and the
`WorkflowError`s are exactly the cluster's. The revision is the document's content hash, so
a hand edit of the file is a concurrent change (412), the same as a `kubectl edit`. The
admin-command annotations live in a JSON file beside the stack's state.

The stack reloads the file on every tick; `on_replace` lets it tick at once.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from .admin_workflow import WorkflowRepository


class ReplaceConflict(Exception):
    status = 409


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def _atomic_write(path: Path, body: str, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


class FileConfigMaps:
    """The two ConfigMap calls `WorkflowRepository` makes, over one file."""

    def __init__(
        self, path: str | os.PathLike, annotations_path: str | os.PathLike,
        *, on_replace: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.annotations_path = Path(annotations_path)
        self.on_replace = on_replace
        self._lock = threading.Lock()

    def _annotations(self) -> dict[str, str]:
        try:
            data = json.loads(self.annotations_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def read_namespaced_config_map(self, name, namespace, _request_timeout=None):
        text = self.path.read_text(encoding="utf-8")
        return SimpleNamespace(
            metadata=SimpleNamespace(resource_version=_revision(text), annotations=self._annotations()),
            data={"WORKFLOW.md": text},
        )

    def replace_namespaced_config_map(self, name, namespace, body, _request_timeout=None):
        with self._lock:
            current = self.path.read_text(encoding="utf-8")
            if body.metadata.resource_version != _revision(current):
                raise ReplaceConflict()
            mode = self.path.stat().st_mode & 0o777
            text = body.data["WORKFLOW.md"]
            _atomic_write(self.path, text, mode)
            self.annotations_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.annotations_path, json.dumps(body.metadata.annotations or {}), 0o600)
            result = copy.deepcopy(body)
            result.metadata.resource_version = _revision(text)
        if self.on_replace is not None:
            self.on_replace()
        return result


def file_workflow(
    path: str | os.PathLike, annotations_path: str | os.PathLike, namespace: str,
    *, on_replace: Callable[[], None] | None = None,
) -> WorkflowRepository:
    return WorkflowRepository(FileConfigMaps(path, annotations_path, on_replace=on_replace), namespace)
