"""Nightly backup — the `symphony-backup` CronJob's entrypoint.

`python -m symphony.entry.backup [--workflow PATH]`. It archives what Redis will drop
(ended sessions, audit entries), writes a backup and keeps the newest `backup.keep`
(`symphony.backup.run`). Without a WORKFLOW or a backup target it logs
`backup_not_configured` with the reason, records it for `GET /v1/backup`, and exits 0:
the CronJob idles instead of failing until someone configures it. Local mode runs the same
command against local Redis (`REDIS_URL`, `--workflow`).
"""

from __future__ import annotations

import argparse

from .. import backup
from ..config import ConfigHolder
from ..edge import load_config, log, pod_namespace
from ..kv import RedisKV


def main(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(prog="python -m symphony.entry.backup")
    parser.add_argument("--workflow", help="WORKFLOW.md (default: the mounted ConfigMap)")
    args = parser.parse_args(argv)
    holder = ConfigHolder()
    load_config(holder, args.workflow)
    try:
        result = backup.run(RedisKV().client, pod_namespace(), holder.config)
    except backup.BackupError as exc:
        if exc.code == "not_configured":
            log.warning("backup_not_configured", reason=exc.message)
            return "not-configured"
        log.error("backup_failed", code=exc.code, reason=exc.message)
        raise SystemExit(1) from None
    log.info(
        "backup_done",
        target=result["target"]["kind"],
        backup=result["backup"]["name"],
        keys=result["backup"]["records"],
        pruned=len(result["backup"]["pruned"]),
        archive=result["archive"]["name"],
        sessions=result["archive"]["sessions"],
        audit=result["archive"]["audit"],
    )
    return "done"


if __name__ == "__main__":
    main()
