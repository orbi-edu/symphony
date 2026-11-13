"""Kubernetes certification on kind: one REAL attempt (omp 18.20.2 on Amazon Bedrock) driven
end to end through the deployed stack. Run by scripts/e2e_kind.sh after the deploy:

    uv run python scripts/e2e/certify.py render --issues none|Todo|Done --out FILE
    uv run python scripts/e2e/certify.py run --context kind-symphony-e2e --url URL --token-file F

`render` writes the certification WORKFLOW.md (scripts/e2e/WORKFLOW.md.tmpl) with the
memory-tracker seed in the given state. `run` then proves, recording timings:

1. publishing the ticket (WORKFLOW.md republished with the seed in Todo) dispatches it: the
   runner Job, the workspace Pod and its session PVC appear (B9, B10);
2. the runner drives the real omp in the workspace Pod (B11): its events arrive at the host
   through the API's SSE (via the port-forward), and the transcript is readable afterwards;
3. a steer sent through `POST /v1/sessions/{sid}/messages` is applied mid-turn
   (`symphony.intent_applied`) and the agent does what it asked (b.txt);
4. the engine's native OTLP spans reach the gateway and are folded into the rollup (D22);
5. `POST /v1/commands refresh` starts a second dispatch that reuses the same Pod and session
   PVC (the first dispatch's session file and workspace files are still there);
6. the ticket goes to Done: terminal cleanup deletes the Pod and the PVC;
7. the nightly sweep CronJob runs once (`kubectl create job --from=cronjob/...`, C19).

Prints one JSON summary. No credential value is printed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "src"))

from symphony.edge import SESSION_MOUNT  # noqa: E402
from symphony.kube import WORKSPACE_CONTAINER, session_pvc_name  # noqa: E402
from symphony.workspace import sandbox_name  # noqa: E402

NAMESPACE = "symphony"
ISSUE_ID = "kind-e2e-1"
IDENTIFIER = "KIND-1"
TARGET_REPO = "e2e/kind-sandbox"
SID1 = f"{ISSUE_ID}:1"
SID2 = f"{ISSUE_ID}:2"
STEER = "also create b.txt containing the single line beta"
TASK = (
    "Do these steps in order, each as its own bash command:\n"
    "1. Create a.txt containing the single line alpha.\n"
    "2. Run `sleep 40` (it stands in for a slow build; do not skip or shorten it).\n"
    "3. Create c.txt containing the number of lines of a.txt (use wc -l).\n"
    "4. Run `ls -1` and report what it shows."
)


class Failure(RuntimeError):
    pass


def until(predicate, timeout: float, what: str, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise Failure(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


# --------------------------------------------------------------------------- render
def render(issues_state: str, provider: str, model: str) -> str:
    seed = (
        []
        if issues_state == "none"
        else [{
            "id": ISSUE_ID, "identifier": IDENTIFIER, "title": "Test files in the workspace",
            "description": TASK, "state": issues_state, "labels": [f"repo:{TARGET_REPO}"],
        }]
    )
    text = (HERE / "WORKFLOW.md.tmpl").read_text(encoding="utf-8")
    for key, value in {
        "@ISSUES@": json.dumps(seed), "@PROVIDER@": provider, "@MODEL@": model,
    }.items():
        text = text.replace(key, value)
    from symphony.config import parse_workflow

    parse_workflow(text)
    return text


# --------------------------------------------------------------------------- run
class Run:
    def __init__(self, context: str, url: str, token: str, workflow_out: Path, provider: str, model: str):
        self.context = context
        self.url = url
        self.provider, self.model = provider, model
        self.workflow_out = workflow_out
        self.http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
        self.token = token
        self.summary: dict = {"timings_s": {}, "checks": {}, "evidence": {}}
        self.events: dict[str, list[dict]] = {}
        self.connected: dict[str, float] = {}
        self.pod = sandbox_name(IDENTIFIER)
        self.pvc = session_pvc_name(self.pod)

    # -- kubectl ------------------------------------------------------------
    def kubectl(self, *args: str, check: bool = True, timeout: float = 120) -> str:
        result = subprocess.run(
            ["kubectl", "--context", self.context, "-n", NAMESPACE, *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if check and result.returncode != 0:
            raise Failure(f"kubectl {' '.join(args)}: {result.stderr.strip()[-800:]}")
        return result.stdout

    def get_json(self, kind: str, name: str):
        out = self.kubectl("get", kind, name, "-o", "json", "--ignore-not-found")
        return json.loads(out) if out.strip() else None

    def in_workspace(self, script: str) -> str:
        return self.kubectl("exec", self.pod, "-c", WORKSPACE_CONTAINER, "--", "bash", "-c", script)

    def redis(self, *args: str) -> str:
        return self.kubectl("exec", "deploy/symphony-redis", "--", "redis-cli", *args).strip()

    def publish(self, issues_state: str) -> float:
        self.workflow_out.write_text(render(issues_state, self.provider, self.model), encoding="utf-8")
        subprocess.run(
            [str(REPO / "scripts/sync_workflow.sh"), str(self.workflow_out), NAMESPACE],
            env={**_env(), "KUBE_CONTEXT": self.context}, check=True, capture_output=True,
        )
        return time.time()

    # -- API ----------------------------------------------------------------
    def meta(self, sid: str) -> dict | None:
        response = self.http.get(f"/v1/sessions/{sid}")
        return response.json()["meta"] if response.status_code == 200 else None

    def follow(self, sid: str) -> threading.Thread:
        """Tail the session's SSE from the host; every event is kept with its arrival time."""
        events = self.events.setdefault(sid, [])

        def reader():
            with httpx.Client(base_url=self.url, timeout=httpx.Timeout(10, read=None)) as client:
                with client.stream(
                    "GET", f"/v1/sessions/{sid}/events",
                    headers={"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"},
                ) as response:
                    self.connected[sid] = time.time()
                    current: dict = {}
                    for line in response.iter_lines():
                        if line.startswith("id:"):
                            current["id"] = line[3:].strip()
                        elif line.startswith("event:"):
                            current["kind"] = line[6:].strip()
                        elif line.startswith("data:"):
                            current["data"] = current.get("data", "") + line[5:].strip()
                        elif line == "" and current:
                            current["at"] = time.time()
                            events.append(current)
                            if current.get("kind") == "symphony.state" and '"ended"' in current.get("data", ""):
                                return
                            current = {}

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        return thread

    def first_event(self, sid: str, kind: str, contains: str = ""):
        for event in list(self.events.get(sid, [])):
            if event.get("kind") == kind and contains in event.get("data", ""):
                return event
        return None

    def ended(self, sid: str):
        meta = self.meta(sid)
        return meta if meta and meta.get("state") == "ended" else None

    # -- the certification ----------------------------------------------------
    def main(self) -> None:
        t = self.summary["timings_s"]
        checks = self.summary["checks"]
        evidence = self.summary["evidence"]

        # 1. dispatch: Job, Pod and PVC
        published = self.publish("Todo")
        job = until(
            lambda: self.kubectl("get", "jobs", "-l", "component=runner", "-o", "name").strip(),
            90, "the runner Job",
        )
        t["ticket_published_to_runner_job"] = round(time.time() - published, 2)
        evidence["runner_job"] = job
        pod = until(lambda: self.get_json("pod", self.pod), 120, "the workspace Pod")
        t["ticket_published_to_workspace_pod"] = round(time.time() - published, 2)
        until(lambda: (self.get_json("pod", self.pod) or {}).get("status", {}).get("phase") == "Running",
              180, "the workspace Pod to run")
        pvc = until(lambda: self.get_json("pvc", self.pvc), 60, "the session PVC")
        until(lambda: (self.get_json("pvc", self.pvc) or {}).get("status", {}).get("phase") == "Bound",
              60, "the session PVC to bind")
        pod_uid, pvc_uid = pod["metadata"]["uid"], pvc["metadata"]["uid"]
        checks["workspace_pod"] = {"name": self.pod, "image": pod["spec"]["containers"][0]["image"],
                                   "automountServiceAccountToken": pod["spec"].get("automountServiceAccountToken")}
        checks["session_pvc"] = {"name": self.pvc, "storage": pvc["spec"]["resources"]["requests"]["storage"]}

        # 2. the live session through the API
        until(lambda: self.meta(SID1), 180, "session " + SID1)
        self.follow(SID1)
        until(lambda: self.first_event(SID1, "agent_start"), 180, "agent_start over SSE")
        t["ticket_published_to_agent_start_sse"] = round(self.first_event(SID1, "agent_start")["at"] - published, 2)
        meta = self.meta(SID1)
        checks["session_meta_running"] = {k: meta.get(k) for k in ("state", "kind", "model", "pod", "identifier")}

        # 3. steer mid-turn (while the agent sleeps)
        until(lambda: self.first_event(SID1, "tool_execution_start", "sleep 40"), 240,
              "the agent to start the slow step")
        sent = time.time()
        response = self.http.post(f"/v1/sessions/{SID1}/messages", json={"text": STEER, "mode": "steer"})
        if response.status_code != 202:
            raise Failure(f"steer: {response.status_code} {response.text}")
        checks["steer_response"] = response.json()
        applied = until(lambda: self.first_event(SID1, "symphony.intent_applied", '"steer"'), 30,
                        "symphony.intent_applied (steer) over SSE")
        t["steer_post_to_intent_applied_sse"] = round(applied["at"] - sent, 3)
        checks["intent_applied"] = json.loads(applied["data"])

        # the first dispatch ends (max_turns 1); the steer's effect is in the workspace
        meta = until(lambda: self.ended(SID1), 420, "the first dispatch to end")
        t["ticket_published_to_first_dispatch_ended"] = round(time.time() - published, 2)
        checks["first_dispatch"] = {k: meta.get(k) for k in ("outcome", "turn", "tokens", "last_message")}
        files = self.in_workspace("cd /workspace && ls -1").split()
        checks["workspace_files_after_first"] = files
        b_txt = self.in_workspace("cat /workspace/b.txt 2>/dev/null || true").strip()
        checks["b_txt"] = b_txt
        if b_txt != "beta":
            raise Failure(f"the steer had no effect: /workspace holds {files}, b.txt={b_txt!r}")
        detail = self.http.get(f"/v1/sessions/{SID1}").json()
        checks["transcript_entries"] = len(detail.get("transcript") or [])
        export = self.http.get(f"/v1/sessions/{SID1}/export.jsonl")
        checks["export_jsonl_lines"] = len(export.text.splitlines())
        if not checks["transcript_entries"]:
            raise Failure("the transcript is empty after the turn")
        kinds: dict[str, int] = {}
        for event in self.events[SID1]:
            kinds[event.get("kind", "?")] = kinds.get(event.get("kind", "?"), 0) + 1
        checks["sse_event_kinds"] = kinds
        # Delivery latency: the stream id carries the append time (Redis clock, inside kind)
        # and `at` the arrival on the host, through the API's SSE and the port-forward.
        lags = sorted(
            event["at"] * 1000 - int(event["id"].split("-", 1)[0])
            for event in self.events[SID1]
            if event.get("id", "").split("-", 1)[0].isdigit()
            and int(event["id"].split("-", 1)[0]) >= self.connected[SID1] * 1000
        )
        if lags:
            checks["sse_delivery_ms"] = {
                "events": len(lags), "p50": round(lags[len(lags) // 2]),
                "p95": round(lags[int(len(lags) * 0.95) - 1]), "max": round(lags[-1]),
            }

        # 4. telemetry folded by the gateway
        def rollup():
            raw = self.redis("GET", f"telemetry:{ISSUE_ID}")
            return json.loads(raw) if raw else None

        folded = until(rollup, 60, "the telemetry rollup")
        checks["telemetry_rollup"] = {k: v for k, v in folded.items() if not isinstance(v, dict | list)}
        gateway_log = self.kubectl("logs", "deploy/symphony-gateway", "--tail", "200")
        checks["gateway_traces_posts"] = gateway_log.count("POST /v1/traces")
        if not checks["gateway_traces_posts"]:
            raise Failure("the gateway logged no POST /v1/traces")

        # 5. second dispatch on the same Pod and PVC
        session_dir = f"{SESSION_MOUNT}/{self.pod}"
        sessions_before = self.in_workspace(f"ls -1 {session_dir}").split()
        evidence["session_dir_after_first"] = sessions_before
        lines_before = self.session_lines(session_dir)
        # The clean exit queued a continuation that the anti-runaway cooldown holds (nothing
        # changed on the ticket). `refresh` is consumed by the next tick, so it is sent once
        # the daemon reports the hold: then that tick releases it.
        held = until(
            lambda: f"issue_id='{ISSUE_ID}' event='cooldown_hold'"
            in self.kubectl("logs", "deploy/symphony-daemon", "--since=2m"),
            60, "the daemon to hold the continuation (cooldown_hold)",
        )
        checks["continuation_held_by_cooldown"] = held
        refreshed = time.time()
        response = self.http.post("/v1/commands", json={"action": "refresh", "reason": "kind e2e: resume on the same workspace"})
        if response.status_code != 202:
            raise Failure(f"refresh: {response.status_code} {response.text}")
        until(lambda: self.meta(SID2), 120, "session " + SID2)
        self.follow(SID2)
        until(lambda: self.first_event(SID2, "agent_start"), 180, "agent_start of the second dispatch")
        t["refresh_to_second_agent_start_sse"] = round(self.first_event(SID2, "agent_start")["at"] - refreshed, 2)
        pod2, pvc2 = self.get_json("pod", self.pod), self.get_json("pvc", self.pvc)
        checks["second_dispatch_same_pod"] = pod2["metadata"]["uid"] == pod_uid
        checks["second_dispatch_same_pvc"] = pvc2["metadata"]["uid"] == pvc_uid
        if not checks["second_dispatch_same_pvc"]:
            raise Failure("the second dispatch got a new session PVC")
        meta2 = until(lambda: self.ended(SID2), 420, "the second dispatch to end")
        checks["second_dispatch"] = {k: meta2.get(k) for k in ("outcome", "turn", "last_message")}
        checks["second_transcript_entries"] = len(self.http.get(f"/v1/sessions/{SID2}").json().get("transcript") or [])
        sessions_after = self.in_workspace(f"ls -1 {session_dir}").split()
        evidence["session_dir_after_second"] = sessions_after
        lines_after = self.session_lines(session_dir)
        # omp resumed the first dispatch's session: the same file, grown, and no new one
        checks["session_resumed"] = {
            "same_files": sessions_after == sessions_before,
            "jsonl_lines_before": lines_before, "jsonl_lines_after": lines_after,
        }
        checks["workspace_files_after_second"] = self.in_workspace("cd /workspace && ls -1").split()
        if not set(sessions_before) <= set(sessions_after) or lines_after <= lines_before:
            raise Failure(f"the second dispatch did not continue the session: {checks['session_resumed']}")

        # 6. Done: terminal cleanup
        done = self.publish("Done")
        until(lambda: self.get_json("pod", self.pod) is None and self.get_json("pvc", self.pvc) is None,
              180, "terminal cleanup of the Pod and the PVC")
        t["done_published_to_workspace_and_pvc_deleted"] = round(time.time() - done, 2)
        checks["terminal_cleanup"] = True

        # 7. nightly sweep, once
        self.kubectl("delete", "job", "sweep-e2e", "--ignore-not-found")
        started = time.time()
        self.kubectl("create", "job", "sweep-e2e", "--from=cronjob/symphony-nightly-sweep")
        self.kubectl("wait", "--for=condition=complete", "job/sweep-e2e", "--timeout=180s", timeout=200)
        t["nightly_sweep_job"] = round(time.time() - started, 2)
        job = self.get_json("job", "sweep-e2e")
        checks["nightly_sweep"] = {"succeeded": job["status"].get("succeeded"),
                                   "log_tail": self.kubectl("logs", "job/sweep-e2e", "--tail", "5").strip().splitlines()}

    def session_lines(self, session_dir: str) -> int:
        out = self.in_workspace(f"cat {session_dir}/*.jsonl 2>/dev/null | wc -l")
        return int(out.strip() or 0)

    def diagnostics(self) -> dict:
        out = {}
        for name, args in {
            "objects": ("get", "pods,jobs,pvc", "-o", "wide"),
            "daemon": ("logs", "deploy/symphony-daemon", "--tail", "60"),
            "runner": ("logs", "-l", "component=runner", "--tail", "80", "--prefix"),
            "gateway": ("logs", "deploy/symphony-gateway", "--tail", "20"),
            "api": ("logs", "deploy/symphony-api", "--tail", "20"),
        }.items():
            try:
                out[name] = self.kubectl(*args, check=False).strip().splitlines()[-80:]
            except Exception as exc:  # noqa: BLE001 — diagnostics are best effort
                out[name] = [repr(exc)]
        return out


def _env() -> dict:
    import os

    return dict(os.environ)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render")
    r.add_argument("--issues", choices=("none", "Todo", "Done"), required=True)
    r.add_argument("--out", required=True)
    run = sub.add_parser("run")
    run.add_argument("--context", required=True)
    run.add_argument("--url", required=True)
    run.add_argument("--token-file", required=True)
    run.add_argument("--workflow-out", required=True)
    run.add_argument("--summary-out")
    for p in (r, run):
        p.add_argument("--provider", default="amazon-bedrock")
        p.add_argument("--model", default="global.anthropic.claude-opus-5-5")
    args = parser.parse_args()

    if args.command == "render":
        Path(args.out).write_text(render(args.issues, args.provider, args.model), encoding="utf-8")
        return 0

    token = Path(args.token_file).read_text().strip()
    certification = Run(args.context, args.url, token, Path(args.workflow_out), args.provider, args.model)
    code = 0
    started = time.time()
    try:
        certification.main()
        certification.summary["ok"] = True
    except Exception as exc:  # noqa: BLE001 — reported in the summary with diagnostics
        certification.summary["ok"] = False
        certification.summary["error"] = f"{type(exc).__name__}: {exc}"
        certification.summary["diagnostics"] = certification.diagnostics()
        code = 1
    certification.summary["timings_s"]["total"] = round(time.time() - started, 2)
    text = json.dumps(certification.summary, indent=2, ensure_ascii=False)
    print(text)
    if args.summary_out:
        Path(args.summary_out).write_text(text + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
