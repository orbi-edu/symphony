"""Scenario 13: an implementer uses Seiji (the device MCP server) through the gateway.

The Seiji mock (`python -m scripts.mocks.seiji`) holds a random `sj_` token. Admin stores it
as the `seiji` server's header, with `preset: seiji` for the role `implementer`. The ticket asks
the agent to acquire an iPhone, upload `build/MyApp.zip` through `$SEIJI_URL`, record a run,
export its evidence into the PR body, attach `report.md` to the ticket, and keep the lease.
Symphony's session cleanup must release it.

Every check reads the mocks' state, the store, or the agent's own environment. The pinned omp
runs behind a small wrapper (`Stack(env_probe=True)`) that records, for each engine launch,
only the SHA-256 and the prefix of `SEIJI_TOKEN`, and the value of `SEIJI_URL`. No credential
value is written or printed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import sys
import time
import zipfile

from scripts.e2e_mocked.run import AGENT_TIMEOUT_S, APP, NAMESPACE, PERSON, Scenario, until

SCENARIOS = ("13",)
SERVER = "seiji"
# Seiji's `listen.publicUrl`: another origin than the API root, so links need `rewrite_origins`.
PUBLIC_URL = "https://seiji.e2e.invalid"
BUILD_PATH, BUILD_NAME = "build/MyApp.zip", "MyApp.zip"
ENV_LOG = "agent-env.log"


def build_zip() -> bytes:
    """A small simulator-app-shaped zip: `MyApp.app/Info.plist` and a binary stub."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MyApp.app/Info.plist", '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">'
                    "<dict><key>CFBundleIdentifier</key><string>com.acme.myapp</string></dict></plist>\n")
        zf.writestr("MyApp.app/MyApp", secrets.token_bytes(4096))
    return buf.getvalue()


def omp_wrapper(real_omp: str, log_path: str) -> str:
    """A bash wrapper for omp: one line per launch with `SEIJI_TOKEN` set:
    `<sha256> <prefix before the first _> <SEIJI_URL>`. Then it runs the real omp."""
    q = lambda v: "'" + v.replace("'", "'\\''") + "'"  # noqa: E731
    return ("#!/bin/bash\n"
            'if [ -n "${SEIJI_TOKEN:-}" ]; then\n'
            '  h=$(printf %s "$SEIJI_TOKEN" | /usr/bin/shasum -a 256 | cut -d" " -f1)\n'
            f'  printf "%s %s %s\\n" "$h" "${{SEIJI_TOKEN%%_*}}" "${{SEIJI_URL:-}}" >> {q(log_path)}\n'
            "fi\n"
            f"exec {q(real_omp)} \"$@\"\n")


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


# ======================================================================================
# the mock
# ======================================================================================
def start_seiji(s) -> None:
    if getattr(s, "seiji_token", None):
        return
    from scripts.mocks.seiji.server import DEFAULT_TOKEN

    # Seiji's token shape: `sj_` and 43 characters; never the mock's default.
    s.seiji_token = "sj_" + secrets.token_urlsafe(32)[:43]
    assert s.seiji_token != DEFAULT_TOKEN
    port = s.ports["seiji"]
    s.seiji_url = f"http://127.0.0.1:{port}"
    s.spawn("mock-seiji", [sys.executable, "-m", "scripts.mocks.seiji", "--port", str(port),
                           "--public-url", PUBLIC_URL],
            env={**os.environ, "SEIJI_MOCK_TOKEN": s.seiji_token})
    until(lambda: s._healthy(f"{s.seiji_url}/healthz"), 30, "the Seiji mock")


def seiji_state(s) -> dict:
    return s.http.get(f"{s.seiji_url}/_control/state").json()


def push_build(s, data: bytes) -> str:
    """A person's commit on acme/app main with the build zip."""
    clone = s.clone_as(APP, s.person_token, PERSON)
    path = clone / BUILD_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    s.git("add", "-f", BUILD_PATH, cwd=clone)
    s.git("commit", "-q", "-m", "build: the simulator app for the device check", cwd=clone)
    s.git("push", "-q", "origin", "HEAD:main", cwd=clone)
    return s.git("rev-parse", "HEAD", cwd=clone).stdout.strip()


def env_launches(s) -> list[dict]:
    path = s.tmp / ENV_LOG
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            rows.append({"sha256": parts[0], "prefix": parts[1], "url": parts[2]})
    return rows


def held_keys(s) -> list[str]:
    return list(s.redis.scan_iter(f"sym:{NAMESPACE}:mcp:held:*"))


def viewer(s, lease_id: str):
    return s.api("POST", f"/v1/mcp/{SERVER}/viewer", {"lease_id": lease_id, "control": False})


# ======================================================================================
# the scenario
# ======================================================================================
def ticket_text(gateway_http: str) -> str:
    return (
        "Check the app on an iPhone with Seiji, the device MCP server (its tools are offered to you as "
        "`mcp__symphony_seiji_<tool>`). Do these steps in order:\n\n"
        "1. Call `acquire_device` with `platform` `ios`. Keep the `lease_id`.\n"
        f"2. Upload the build `{BUILD_PATH}` from the repository through `$SEIJI_URL`:\n"
        f"   `curl -sS -H \"Authorization: Bearer $SEIJI_TOKEN\" -H \"X-Filename: {BUILD_NAME}\" "
        f"--data-binary @{BUILD_PATH} \"$SEIJI_URL/v1/apps\"`. Keep the `app_id`.\n"
        "3. Call `run_actions` with `name` `Home screen` and `actions` `[{\"screenshot\": {\"name\": \"home\"}}]`. "
        "Keep the `run_id`.\n"
        "4. Call `export_evidence` with that `run_id`.\n"
        "5. Download the same report through `$SEIJI_URL`: `curl -sS -H \"Authorization: Bearer $SEIJI_TOKEN\" "
        "-o report.md \"$SEIJI_URL/v1/runs/<run_id>/report.md\"`. Attach `report.md` to this ticket with "
        "`symphony_attach_evidence` (title `Seiji report`, kind `report`). Do not commit `report.md`.\n"
        "6. Add `docs/device-check.md` with one line: `Checked on an iPhone with Seiji: run <run_id>, "
        "app <app_id>.` Open the pull request. Its body must contain the `export_evidence` Markdown verbatim.\n\n"
        "Do NOT call `release_device`: this ticket checks that Symphony releases the device when your "
        "session ends. Never print `$SEIJI_TOKEN`.\n\n"
        "Acceptance: the pull request exists with the report in its body, and `report.md` is attached here."
    )


def s13_seiji(run) -> None:
    s = run.s
    sc = run.results["13"] = Scenario("13", "Seiji through the gateway: lease, upload, evidence, cleanup",
                                      "W11 A, B, C, D, E")
    start_seiji(s)
    gateway = f"http://127.0.0.1:{s.ports['gateway']}"
    gateway_http = f"{gateway}/v1/mcp/{SERVER}/http"

    # -- Admin: the preset and the stored header ------------------------------------------
    r = s.api("PUT", f"/v1/admin/mcp/{SERVER}", {"preset": "seiji", "url": f"{s.seiji_url}/mcp",
                                                 "roles": ["implementer"], "enabled": True,
                                                 "rewrite_origins": [PUBLIC_URL]})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
    stored = body.get("server", body)
    sc.check("PUT /v1/admin/mcp/seiji with preset seiji → a plain definition (http, http_base, timeout, "
             "agent_env, cleanup)",
             r.status_code == 200 and stored.get("transport") == "http" and stored.get("http_base") == s.seiji_url
             and stored.get("timeout_ms") == 3900000
             and stored.get("agent_env") == {"SEIJI_TOKEN": "{token}", "SEIJI_URL": "{http_url}"}
             and stored.get("cleanup") == {"acquire_tool": "acquire_device", "id_field": "lease_id",
                                           "release_tool": "release_device", "release_arg": "lease_id"}
             and "preset" not in stored,
             {"status": r.status_code, "definition": stored})
    r = s.api("PUT", f"/v1/admin/mcp/{SERVER}/headers", {"headers": {"Authorization": f"Bearer {s.seiji_token}"}})
    sc.check("PUT .../headers → headers_set names only", r.status_code == 200
             and r.json().get("headers_set") == ["Authorization"] and s.seiji_token not in r.text,
             {"status": r.status_code, "headers_set": r.json().get("headers_set")})
    tested = s.api("POST", f"/v1/admin/mcp/{SERVER}/test").json()
    sc.check("POST .../test → ok, Seiji's tools, /v1/me {user, quota}",
             tested.get("ok") is True and "acquire_device" in (tested.get("tools") or [])
             and isinstance(tested.get("me"), dict) and {"user", "quota"} <= set(tested["me"])
             and s.seiji_token not in json.dumps(tested),
             {k: tested.get(k) for k in ("ok", "tools", "me", "protocol_version")})
    s.http.post(f"{s.seiji_url}/_control/reset")

    # -- the ticket ---------------------------------------------------------------------------
    build = build_zip()
    commit = push_build(s, build)
    sc.check(f"acme/app main carries {BUILD_PATH}", bool(commit),
             {"commit": commit[:12], "bytes": len(build), "sha256": sha(build)[:16]})
    t0 = time.monotonic()
    issue = s.create_ticket("Check the app on an iPhone", ticket_text(gateway_http), ["symphony", f"repo:{APP}"])
    run.cleanups.append(lambda: (run.cancel_open([issue["identifier"]]), [
        s.rest("PATCH", f"/repos/{APP}/pulls/{p['number']}", {"state": "closed"})
        for p in s.pulls(APP, issue["identifier"]) if p["state"] == "open"]))
    meta = s.wait_session(sc, issue)
    sid = meta["sid"]
    from symphony.mcp_servers import token_key
    from symphony.workspace import sandbox_name

    plugin = s.tmp / "root" / "credentials" / sandbox_name(issue["identifier"]) / "mcp" / "symphony" / ".mcp.json"
    staged = until(lambda: plugin.exists() and json.loads(plugin.read_text() or "{}").get("mcpServers"), 120,
                   "the staged MCP config")
    entry = staged.get(SERVER) or {}
    bearer = (entry.get("headers") or {}).get("Authorization", "").removeprefix("Bearer ")
    sc.check("the staged config points at the gateway with a session token and Seiji's timeout",
             entry.get("url") == f"{gateway}/v1/mcp/{SERVER}" and bearer.startswith("mcp_")
             and entry.get("timeout") == 3900000 and s.seiji_token not in plugin.read_text(),
             {"url": entry.get("url"), "timeout": entry.get("timeout"), "token_prefix": bearer[:4]})

    launches = until(lambda: env_launches(s), 120, "the engine launch with SEIJI_TOKEN")
    record = json.loads(s.redis.get(f"sym:{NAMESPACE}:mcp:token:{launches[0]['sha256']}") or "null")
    sc.check("the agent's env: SEIJI_TOKEN is a live session token (prefix mcp, the staged bearer, "
             "role implementer), never the stored sj_ token; SEIJI_URL is the gateway",
             all(x["prefix"] == "mcp" and x["sha256"] == sha(bearer) and x["sha256"] != sha(s.seiji_token)
                 and x["url"] == gateway_http for x in launches)
             and record is not None and record.get("role") == "implementer"
             and token_key(NAMESPACE, bearer).endswith(launches[0]["sha256"]),
             {"launches": [{"prefix": x["prefix"], "sha256": x["sha256"][:12], "SEIJI_URL": x["url"]}
                           for x in launches], "token_record": record and {k: record.get(k) for k in ("role", "session")}})

    # -- while it runs: the lease, the viewer, the end -----------------------------------------
    seen: dict = {}

    def watch():
        state = seiji_state(s)
        ready = [x for x in state["leases"] if x["status"] == "ready"]
        if ready and "viewer" not in seen:
            seen["lease"] = ready[0]["lease_id"]
            seen["lease_at"] = time.monotonic()
            r = viewer(s, seen["lease"])
            seen["viewer"] = {"status": r.status_code, "body": r.json() if r.content else None}
        if "lease" in seen:
            seen["last"] = next((x for x in state["leases"] if x["lease_id"] == seen["lease"]), None)
        m = s.api("GET", f"/v1/sessions/{sid}").json()["meta"]
        if m.get("mcp_leases"):
            seen["meta_leases"] = m["mcp_leases"]
        return m.get("state") == "ended" and m

    end = until(watch, AGENT_TIMEOUT_S, f"session {sid} to end", interval=1)
    t_end = time.monotonic()
    sc.time("ticket created → session ended", t_end - t0)
    if "lease_at" in seen:
        sc.time("ticket created → lease ready in the mock", seen["lease_at"] - t0)
    lease = seen.get("lease")
    sc.check("the agent acquired an iPhone (the mock's lease)", bool(lease), seen.get("last"))
    v = seen.get("viewer") or {}
    vb = v.get("body") or {}
    sc.check("viewer route while the lease was held → an absolute URL, view only",
             v.get("status") == 200 and str(vb.get("url", "")).startswith(("http://", "https://"))
             and "/view/open/" in vb.get("url", "") and vb.get("control") is False
             and isinstance(vb.get("expires_in"), int),
             {"status": v.get("status"), "url": str(vb.get("url", "")).split("/view/open/")[0] + "/view/open/…",
              "expires_in": vb.get("expires_in"), "control": vb.get("control"),
              "error": None if v.get("status") == 200 else vb}, fatal=False)
    sc.check("the session meta listed the held lease", any(
        (x.get("id") if isinstance(x, dict) else x) == lease for x in seen.get("meta_leases") or []),
        seen.get("meta_leases"), fatal=False)
    released = until(lambda: next((x for x in seiji_state(s)["leases"] if x["lease_id"] == lease
                                   and x["status"] == "released"), None), 120, "the session cleanup's release")
    log = s.log_text("symphony-local").splitlines()
    cleanup = [ln for ln in log if "event='mcp_cleanup'" in ln]
    finished = [ln for ln in log if "event='runner_finished'" in ln]
    stamp = lambda ln: dt.datetime.fromisoformat(re.search(r"timestamp='([^']+)'", ln).group(1)  # noqa: E731
                                                 .replace("Z", "+00:00"))
    if cleanup and finished:
        sc.time("attempt finished (runner_finished) → leases released (mcp_cleanup)",
                (stamp(cleanup[-1]) - stamp(finished[-1])).total_seconds())
    counts = dict(re.findall(r"\b(held|released|failed)=(\d+)", cleanup[-1])) if cleanup else {}
    sc.check("the agent kept the lease and the session cleanup released it: mcp_cleanup held=1 released=1 "
             "failed=0 after the attempt; the mock's lease ended `released`; no held set left in Redis",
             counts == {"held": "1", "released": "1", "failed": "0"} and released["end_reason"] == "released"
             and bool(finished) and stamp(cleanup[-1]) >= stamp(finished[-1]) and not held_keys(s),
             {"mcp_cleanup": counts, "lease": {k: released[k] for k in ("lease_id", "status", "end_reason")},
              "held_keys": held_keys(s)})
    r = viewer(s, lease)
    sc.check("viewer route after the release → 404 lease_not_held",
             r.status_code == 404 and "lease_not_held" in r.text, {"status": r.status_code, "body": r.text[:200]})

    # -- the mock's view -------------------------------------------------------------------------
    state = seiji_state(s)
    tools = [x["tool"] for x in state["requests"] if x.get("rpc_method") == "tools/call"]
    sc.check("the mock saw acquire_device, run_actions, export_evidence, and one release_device (the cleanup)",
             {"acquire_device", "run_actions", "export_evidence"} <= set(tools) and tools.count("release_device") == 1,
             {"tools_call": tools})
    sc.check("the mock saw only the stored token: every request had auth valid, no other bearer",
             all(x["auth"] == "valid" for x in state["requests"]) and state["other_bearers"] == []
             and sha(bearer) not in state["other_bearers"],
             {"requests": len(state["requests"]), "auth": sorted({x["auth"] for x in state["requests"]}),
              "other_bearers": len(state["other_bearers"])})
    apps = [a for a in state["apps"] if a["filename"] == BUILD_NAME]
    sc.check(f"the upload through $SEIJI_URL arrived intact ({len(build)} bytes, same SHA-256)",
             any(a["size"] == len(build) and a["sha256"] == sha(build) for a in apps),
             {"apps": apps, "expected": {"size": len(build), "sha256": sha(build)}})
    runs = [x for x in state["runs"] if x["lease_id"] == lease]
    run_id = runs[0]["run_id"] if runs else None
    sc.check("a recorded run on the lease", bool(run_id), state["runs"])
    report_reqs = [x for x in state["requests"] if x.get("path") == f"/v1/runs/{run_id}/report.md"]
    sc.check("report.md was downloaded through $SEIJI_URL", bool(report_reqs),
             [{k: x.get(k) for k in ("method", "path", "auth")} for x in report_reqs[:2]], fatal=False)

    # -- the PR and the ticket ---------------------------------------------------------------------
    prs = [p for p in s.pulls(APP, issue["identifier"])]
    pr = s.rest("GET", f"/repos/{APP}/pulls/{prs[0]['number']}").json() if prs else {}
    pr_body = pr.get("body") or ""
    links = [x.split(")")[0] for x in pr_body.split("(") if x.startswith("http") and "/v1/runs/" in x.split(")")[0]]
    sc.check("the PR body has the export_evidence report (heading, the run id, the evidence.zip link)",
             bool(pr) and "### ✅" in pr_body and run_id in pr_body and "evidence.zip" in pr_body,
             {"pr": pr.get("number"), "user": (pr.get("user") or {}).get("login"), "body": pr_body[:700]})
    sc.check("the links in the report point at the gateway, not at Seiji",
             bool(links) and all(x.startswith(gateway_http) for x in links)
             and PUBLIC_URL not in pr_body and s.seiji_url not in pr_body,
             {"links": links[:4]})
    lin = s.issue(issue["identifier"])
    attachments = [a for a in lin["attachments"] if "seiji" in (a.get("title") or "").lower()
                   or "report" in (a.get("title") or "").lower()]
    assets = []
    for u in s.lin("GET", "uploads")["uploads"]:
        if u.get("uploaded") and u.get("assetUrl"):
            assets.append(s.http.get(u["assetUrl"], headers={"Authorization": s.linear_key}).text)
    report = next((a for a in assets if run_id and run_id in a), None)
    sc.check("the ticket has the evidence: an attachment and the uploaded report.md with the run",
             bool(attachments) and report is not None and gateway_http in report and PUBLIC_URL not in report,
             {"attachments": [{k: a.get(k) for k in ("title", "url")} for a in attachments],
              "report": (report or "")[:300]})
    sc.check("meta.evidence[] lists the attachment", bool(end.get("evidence")), end.get("evidence"), fatal=False)
    leaked = [str(p.relative_to(s.tmp)) for p in (s.tmp / "root").rglob("*")
              if p.is_file() and p != s.tmp / "root" / "mcp.json" and p.stat().st_size < 4 << 20
              and s.seiji_token in p.read_text(errors="replace")]
    sc.check("the stored sj_ token is in no file under the root but the local mcp.json, nor in the PR or ticket",
             not leaked and s.seiji_token not in pr_body and s.seiji_token not in json.dumps(lin),
             {"files_with_the_token": leaked})


RUNS = (("13", s13_seiji),)


def execute(run, wanted: set[str]) -> None:
    import traceback

    for key, fn in RUNS:
        if key not in wanted:
            continue
        print(f"== scenario {key} ({fn.__name__})", flush=True)
        try:
            fn(run)
        except Exception as exc:  # noqa: BLE001 — one scenario's failure is its own result
            sc = run.results.setdefault(key, Scenario(key, fn.__name__, ""))
            sc.error = f"{type(exc).__name__}: {exc}"
            (run.s.tmp / "logs" / f"scenario-{key}.trace").write_text(traceback.format_exc())
            print(f"  [ERROR] {key}: {sc.error[:500]}", flush=True)
        finally:
            run.after_scenario()
