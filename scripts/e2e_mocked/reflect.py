"""The retrospective of one finished ticket, run on this host against the mocks.

    python -m scripts.e2e_mocked.reflect <issue_id> <identifier> <owner/repo>

The local mode runs no reflector (`reflector_skipped_local_mode`), so the mocked certification
runs the production path itself: `reflector.run_retrospective` with the App credential
(`ghapp.InstallationCredential.from_env`, scoped to the repository, as `entry/reflect.py`
does), `reflector.HttpxGitHub` and the tracker's Workpad, all through the endpoint variables.
Only the distillation differs from the Kubernetes edge: the same `omp -p` one-shot as
`entry/reflect.make_distill`, on the host's pinned omp instead of in an ephemeral pod.

Prints one JSON line: `{outcome, token}` (the retro token, without credentials).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

from symphony import ghapp, state
from symphony import reflector as reflector_mod
from symphony.config import ConfigHolder, bind_role_model
from symphony.edge import load_config, make_tracker, now_ms
from symphony.kv import RedisKV
from symphony.teamconfig import TeamConfig


def host_distill(omp: str):
    def distill(harvest, cfg):
        prompt = reflector_mod.build_distill_prompt(harvest)
        done = subprocess.run(
            [omp, "-p", "--no-title", "--auto-approve", "--provider", cfg.engine_provider,
             "--model", cfg.engine_model, "--thinking", cfg.engine_thinking or "medium", "--", prompt],
            capture_output=True, text=True, timeout=9 * 60, check=False,
        )
        if done.returncode != 0:
            raise RuntimeError(f"engine distillation failed (exit {done.returncode}): {done.stderr[-300:]}")
        source = f"PR #{harvest.pr_number}" if harvest.pr_number else harvest.issue_identifier
        return reflector_mod.parse_candidates(done.stdout, source=source)

    return distill


def main() -> int:
    issue_id, identifier, repo = sys.argv[1:4]
    holder = ConfigHolder()
    load_config(holder)
    if holder.config is None:
        print(json.dumps({"outcome": "no-config", "error": holder.last_error}))
        return 1
    kv = RedisKV()
    team_doc = TeamConfig(kv.client, os.environ["POD_NAMESPACE"]).get()
    cfg = bind_role_model(holder.config, team_doc, "reflector")
    github = reflector_mod.HttpxGitHub(
        token=ghapp.InstallationCredential.from_env(os.environ, now_ms, target_repos=(repo,)).token(),
        repo=repo,
    )
    tracker = make_tracker(cfg)
    entry = {"identifier": identifier, "target_repo": repo, "final_state": "Done", "attempt": 1}
    outcome = reflector_mod.run_retrospective(
        kv, github, cfg, issue_id, entry, f"e2e-reflect-{uuid.uuid4().hex[:8]}", now_ms(),
        distill=host_distill(os.environ.get("SYMPHONY_OMP_BIN", "omp")),
        fetch_workpad=getattr(tracker, "fetch_issue_workpad", None),
    )
    print(json.dumps({"outcome": outcome, "token": kv.get(state.retro_key(issue_id))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
