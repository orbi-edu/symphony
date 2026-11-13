"""Nightly sweep — the CronJob's entrypoint (Modal's `nightly_sweep` cron).

Safety net for retrospectives (at-least-once) + metrics retention. On an install that is not
set up (no WORKFLOW, or no usable tracker) there is nothing to sweep: it says so once and
exits 0, so the CronJob does not read as broken while setup is still in progress.
"""

from __future__ import annotations

from .. import kube
from .. import reflector as reflector_mod
from ..config import ConfigHolder
from ..edge import TrackerNotConfigured, load_config, log, make_tracker, now_ms, pod_namespace
from ..effects import K8sEffects
from ..kv import RedisKV

NO_WORKFLOW_REASON = "WORKFLOW.md is missing, empty or invalid."


def main() -> str:
    kv = RedisKV()
    holder = ConfigHolder()
    load_config(holder)
    if holder.config is None:
        log.warning("sweep_setup_incomplete", reason=NO_WORKFLOW_REASON)
        return "setup-incomplete"
    cfg = holder.config
    try:
        tracker = make_tracker(cfg)
    except TrackerNotConfigured as exc:
        log.warning("sweep_setup_incomplete", reason=exc.reason)
        return "setup-incomplete"
    core, batch = kube.get_clients()
    fx = K8sEffects(cfg, core, batch, pod_namespace(cfg))
    reflector_mod.nightly_sweep(kv, tracker, cfg, now_ms(), fx)
    return "swept"


if __name__ == "__main__":
    main()
