"""Opt-in: the mocked end-to-end certification (`scripts/e2e_mocked/run.py`).

Real omp on Bedrock against the vendor mocks: minutes and real model cost, so it only runs
with `SYMPHONY_E2E_MOCKED=1` (and `AWS_REGION` + `AWS_BEARER_TOKEN_BEDROCK`).
`SYMPHONY_E2E_SCENARIOS=1,3` narrows it. The cases: Linear with Slack (scenarios 1-8), the GitHub
tracker with Discord (1, 3, 4 and 7), the assistant: Linear with Slack (9, 10 and 12, MCP
through the gateway) and the GitHub tracker on Discord (11), and Seiji through the gateway (13).
`SYMPHONY_E2E_SCENARIOS` applies to the first two cases only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(os.environ.get("SYMPHONY_E2E_MOCKED") != "1",
                                reason="set SYMPHONY_E2E_MOCKED=1 to run the mocked certification")


@pytest.mark.parametrize("tracker, notify, mode", [("linear", "slack", "core"), ("github", "discord", "core"),
                                                   ("linear", "slack", "assistant"), ("github", "discord", "assistant"),
                                                   ("linear", "slack", "seiji")],
                         ids=["linear-slack", "github-discord", "assistant-linear-slack",
                              "assistant-github-discord", "seiji-linear-slack"])
def test_every_scenario_passes_against_the_mocks(tmp_path, tracker, notify, mode):
    out = tmp_path / "summary.json"
    argv = [sys.executable, "-m", "scripts.e2e_mocked.run", "--out", str(out), "--tracker", tracker,
            "--notify", notify]
    if mode == "assistant":
        argv.append("--assistant")
    elif mode == "seiji":
        argv += ["--scenario", "13"]
    else:
        for scenario in filter(None, os.environ.get("SYMPHONY_E2E_SCENARIOS", "").split(",")):
            argv += ["--scenario", scenario.strip()]
    done = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True, timeout=4 * 3600, check=False)
    assert out.exists(), done.stdout[-3000:] + done.stderr[-3000:]
    summary = json.loads(out.read_text())
    failed = {r["scenario"]: r["error"] or [c["assertion"] for c in r["checks"] if not c["ok"]]
              for r in summary["scenarios"] if not r["ok"]}
    assert summary["ok"] and not failed, json.dumps({"failed": failed, "error": summary.get("error"),
                                                      "tmp": summary.get("tmp")}, ensure_ascii=False)
