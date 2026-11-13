"""Scenarios 1, 3, 4 and 7 with `tracker.kind: github`: tickets are issues on a Projects (v2)
board in the GitHub mock, and the state is the board's Status field (`run.py --tracker github`).

Each function takes the `run.Run` and asserts on the GitHub mock's state, as the Linear
scenarios do on the Linear mock's."""

from __future__ import annotations

import json
import time
import traceback

from scripts.e2e_mocked.run import (
    AGENT_TIMEOUT_S,
    APP,
    EVIDENCE_BYTES,
    NAMESPACE,
    PERSON,
    READ_ONLY,
    Scenario,
    now_ms,
    until,
)

LOG_LINE = "FAILED tests/test_greet.py::test_greet - AssertionError: mocked red main"


class Board:
    """The board and its issues, read through the mock's REST API and persisted state."""

    def __init__(self, run) -> None:
        self.run, self.s = run, run.s

    def ticket(self, title: str, body: str, labels: list[str], status: str = "Todo") -> dict:
        s = self.s
        out = s.gh("POST", "issues", {"repo": APP, "user": PERSON, "title": title, "body": body,
                                      "labels": labels, "project": s.project_number, "status": status})
        number = out["issue"]["number"]
        return {"identifier": f"app-{number}", "number": number, "labels": labels, "state": status,
                "created_ms": now_ms()}

    def status(self, number: int, repo: str = APP) -> str | None:
        data = json.loads((self.s.tmp / "github" / "state.json").read_text())
        project = next(p for p in data["projects"].values()
                       if p["owner"].lower() == "acme" and p["number"] == self.s.project_number)
        repo_id = data["repos"][repo]["id"]
        item = next((i for i in project["items"] if i.get("repo_id") == repo_id and i.get("number") == number), None)
        field = next(f for f in project["fields"] if f["name"] == "Status")
        options = {o["id"]: o["name"] for o in field["options"]}
        return next((options[v] for v in (item or {}).get("values", {}).values() if v in options), None)

    def view(self, number: int, repo: str = APP) -> dict:
        s = self.s
        issue = s.rest("GET", f"/repos/{repo}/issues/{number}").json()
        comments = s.rest("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100").json()
        subs = s.rest("GET", f"/repos/{repo}/issues/{number}/sub_issues").json()
        blocked_by = s.rest("GET", f"/repos/{repo}/issues/{number}/dependencies/blocked_by").json()
        return {"number": number, "status": self.status(number, repo), "closed": issue["state"] == "closed",
                "title": issue["title"], "body": issue.get("body") or "",
                "labels": sorted(lb["name"] for lb in issue["labels"]),
                "comments": [{"user": c["user"]["login"], "body": c["body"]} for c in comments],
                "children": [c["number"] for c in subs], "blocked_by": [b["number"] for b in blocked_by]}

    def until(self, number: int, statuses: tuple[str, ...], what: str, timeout: float = AGENT_TIMEOUT_S) -> dict:
        def reached():
            v = self.view(number)
            return v if v["status"] in statuses or (v["closed"] and "closed" in statuses) else None

        return until(reached, timeout, what, interval=3)

    def close_open(self, numbers) -> None:
        for number in numbers:
            if not self.view(number)["closed"]:
                self.s.rest("PATCH", f"/repos/{APP}/issues/{number}", {"state": "closed", "state_reason": "not_planned"})

    def numbers(self) -> set[int]:
        items = self.s.rest("GET", f"/repos/{APP}/issues?state=all&per_page=100").json()
        return {i["number"] for i in items if "pull_request" not in i}


def implement_merge(run, b: Board) -> None:
    s = run.s
    sc = run.results["1"] = Scenario("1", "GitHub tracker: implement → PR → review and merge → Merging → closed",
                                     "GitHub tracker, B15, O3, X1, X3 (Discord), R1–R12, T3")
    t0, since = time.monotonic(), time.time()
    issue = b.ticket(
        "Add farewell() to app/greet.py",
        "Add a function `farewell(name: str) -> str` to `app/greet.py`, next to `greet`, and a pytest test "
        "for it in `tests/test_greet.py`.\n\nBefore you write any code, ask with `symphony_ask` which word "
        "the farewell starts with, offering exactly the options `Goodbye` and `Farewell`, and use the "
        "answer: `farewell(\"Ana\")` returns `\"<Word>, Ana!\"`.\n\n"
        "Acceptance: `python -m pytest -q` passes and the new test covers `farewell`.", ["symphony"])
    run.cleanups.append(lambda: b.close_open([issue["number"]]))
    sc.check("ticket = an issue in acme/app on the board, Status Todo, label symphony",
             b.status(issue["number"]) == "Todo", {"identifier": issue["identifier"], "status": "Todo"})
    meta = s.wait_session(sc, issue)
    sid = meta["sid"]
    hooks = [d for d in s.gh("GET", "deliveries", params={"brief": "1"})
             if d["event"] in ("issues", "projects_v2_item") and (d["status_code"] or 0) in (200, 202)]
    sc.check("issues / projects_v2_item webhooks reached /v1/webhooks/github (202)", bool(hooks),
             sorted({f"{d['event']}:{d.get('action')}" for d in hooks}))
    sc.check("dispatch < 3 s after the ticket", meta["created_at"] - issue["created_ms"] < 3000,
             {"ms": meta["created_at"] - issue["created_ms"]}, fatal=False)
    _, record = s.wait_credential(sc, issue["identifier"])
    from symphony.ghapp import WORKSPACE

    sc.check("the attempt's token: the issue's repository, exactly ghapp.WORKSPACE (no issues/projects)",
             record["repos"] == [APP] and record["permissions"] == WORKSPACE,
             {k: record[k] for k in ("repos", "permissions")})
    asked = until(lambda: s.notices("input_requested", issue["identifier"])
                  or (s.pulls(APP, issue["identifier"]) and "no-ask"), AGENT_TIMEOUT_S, "the ask (or a PR)", interval=1)
    if asked == "no-ask":
        sc.check("agent asked with symphony_ask", False, "PR opened without asking", fatal=False)
    else:
        sc.check(f"{s.notify}: input_requested", True, asked[0]["text"])
        until(lambda: s.api("GET", f"/v1/sessions/{sid}").json()["meta"].get("input_request"), 30, "the ask")
        r = s.api("POST", f"/v1/sessions/{sid}/messages", {"text": "Goodbye", "mode": "auto"})
        sc.check("answer accepted through the API", r.status_code == 202, {"status": r.status_code})
    view = b.until(issue["number"], ("In Review",), "Status In Review")
    sc.time("ticket created → Status In Review", time.monotonic() - t0)
    pr = next(p for p in s.pulls(APP, issue["identifier"]) if p["state"] == "open")
    sc.check("ready PR by the App, label symphony",
             pr["user"] == s.bot_login and not pr["draft"] and "symphony" in pr["labels"],
             {k: pr[k] for k in ("number", "title", "user", "draft", "labels", "head_ref")})
    reqs = s.git_requests(APP, since)
    sc.check("push over git smart HTTP", any(r["path"].endswith("git-receive-pack") and r["status"] == 200
                                             for r in reqs),
             sorted({f"{r['method']} {r['path'].split('?')[0]} {r['status']}" for r in reqs}))
    workpad = [c for c in view["comments"] if c["body"].lstrip().startswith("## Workpad")]
    sc.check("Workpad = one issue comment by the App", len(workpad) == 1 and workpad[0]["user"] == s.bot_login,
             {"user": workpad[0]["user"], "body": workpad[0]["body"][:200]} if workpad else view["comments"])
    diff = s.rest("GET", f"/repos/{APP}/pulls/{pr['number']}", accept="application/vnd.github.diff").text
    sc.check("the diff adds farewell() with the answered word", "def farewell" in diff and "Goodbye" in diff,
             [ln for ln in diff.splitlines() if ln.startswith("+def") or "Goodbye" in ln][:6])
    s.wait_session_end(sid)
    t1 = time.monotonic()
    s.start_merger()

    def merged():
        p = next((x for x in s.repo_state(APP)["pulls"] if x["number"] == pr["number"]), None)
        return p if p and p.get("merged") else None

    done = until(merged, AGENT_TIMEOUT_S, "the train to merge the PR", interval=3)
    sc.time("merger up → merged", time.monotonic() - t1)
    rep = [c for c in s.repo_state(APP)["comments"] if c["issue"] == pr["number"] and "**symphony-merger**" in c["body"]]
    sc.check("merger report on the PR", bool(rep), rep[0]["body"][:300] if rep else None)
    sc.check("merged by the App", done.get("merged_by") == s.bot_login, {"merged_by": done.get("merged_by")})
    view = b.until(issue["number"], ("Merging",), "Status Merging", timeout=120)
    sc.check("the merger moved the board item to Merging", view["status"] == "Merging", {"status": view["status"]})
    errors = s.notices("run_error")
    sc.check(f"no run_error on {s.notify}", not errors, [m["text"] for m in errors])
    # GitHub closes the issue when a PR that says "Closes #n" merges; the mock leaves that to the driver.
    s.rest("PATCH", f"/repos/{APP}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
    from symphony.workspace import workspace_key

    ws = s.tmp / "root" / "workspaces" / workspace_key(issue["identifier"])
    until(lambda: not ws.exists(), 60, "terminal cleanup of the workspace")
    sc.check("closed issue = terminal: the workspace is removed", not ws.exists(), {"workspace": ws.name})


def investigation(run, b: Board) -> None:
    s = run.s
    sc = run.results["3"] = Scenario("3", "GitHub tracker: investigation with a read-only token", "O6, B15")
    t0 = time.monotonic()
    before = {p["number"] for p in s.repo_state(APP)["pulls"]}
    issue = b.ticket("Which functions in app/greet.py lack a test?",
                     "Investigate which public functions in `app/greet.py` have no pytest test under `tests/`. "
                     "Answer with evidence: the commands you ran and what they printed.",
                     ["symphony", "investigation"])
    run.cleanups.append(lambda: b.close_open([issue["number"]]))
    s.wait_session(sc, issue)
    token, record = s.wait_credential(sc, issue["identifier"])
    sc.check("code token is ghapp.READ_ONLY for acme/app",
             record["permissions"] == READ_ONLY and record["repos"] == [APP],
             {k: record[k] for k in ("repos", "permissions")})
    pr_try = s.rest("POST", f"/repos/{APP}/pulls", {"title": "probe", "head": "main", "base": "main"}, token=token)
    sc.check("a PR with the attempt's token is refused (403)", pr_try.status_code == 403,
             {"status": pr_try.status_code, "message": pr_try.json().get("message")})
    view = b.until(issue["number"], ("Done", "closed"), "the investigation Done")
    sc.time("ticket created → Done", time.monotonic() - t0)
    reports = [c for c in view["comments"] if c["user"] == s.bot_login
               and not c["body"].lstrip().startswith("## Workpad") and "greet" in c["body"]]
    sc.check("report posted as an issue comment by the App", bool(reports),
             reports[0]["body"][:500] if reports else view["comments"])
    new = [p for p in s.repo_state(APP)["pulls"] if p["number"] not in before]
    sc.check("no PR", not new, [p["title"] for p in new])


def tree(run, b: Board) -> None:
    s = run.s
    sc = run.results["4"] = Scenario("4", "GitHub tracker: sub-issues, text evidence, binary refused", "O5, O2")
    t0 = time.monotonic()
    issue = b.ticket(
        "Plan the shout() follow-up",
        "This is a planning ticket: the deliverable is the plan, not code. Open no pull request.\n\n"
        "1. Run `printf 'symphony evidence: pytest passed\\n' > evidence.txt` and attach `evidence.txt` with "
        "`symphony_attach_evidence` (title `pytest evidence`, kind `log`).\n"
        "2. Run `printf '\\211PNG\\r\\n\\032\\n\\000\\000' > shot.png` and try to attach `shot.png` with "
        "`symphony_attach_evidence` (title `screenshot`, kind `screenshot`). Write the tool's answer in the "
        "Workpad; do not retry.\n"
        "3. With ONE call to `symphony_create_issues`, create two sub-issues: key `code`, title "
        "`Add shout() to app/greet.py`; and key `docs`, title `Document shout() in README.md`, "
        "`blocked_by: [\"code\"]`. Give each a one-line description with its acceptance criterion.\n"
        "4. Record the plan in the Workpad and move this ticket to Done.", ["symphony"])
    meta = s.wait_session(sc, issue)
    s.command("pause", "e2e: keep the sub-issues from being dispatched")
    run.cleanups.append(lambda: b.close_open([issue["number"], *b.view(issue["number"])["children"]]))
    view = b.until(issue["number"], ("Done", "closed"), "the planning ticket Done")
    sc.time("ticket created → Done", time.monotonic() - t0)
    children = [b.view(n) for n in view["children"]]
    sc.check("two sub-issues in acme/app, on the board in Todo (active_states[0]), label symphony",
             len(children) == 2 and all(c["status"] == "Todo" and "symphony" in c["labels"] for c in children),
             [{k: c[k] for k in ("number", "title", "status", "labels")} for c in children])
    by = {("docs" if "Document" in c["title"] else "code"): c for c in children}
    sc.check("`docs` is blocked by `code` (issue dependency)",
             len(by) == 2 and by["docs"]["blocked_by"] == [by["code"]["number"]],
             {"docs_blocked_by": by.get("docs", {}).get("blocked_by"), "code": by.get("code", {}).get("number")})
    evidence = [c for c in view["comments"] if c["body"].startswith("<!-- symphony:evidence")]
    sc.check("text evidence = one collapsible comment holding the file's text",
             len(evidence) == 1 and EVIDENCE_BYTES.decode().strip() in evidence[0]["body"]
             and "<details>" in evidence[0]["body"], evidence[0]["body"][:400] if evidence else view["comments"])
    end = s.wait_session_end(meta["sid"])
    frames = [json.loads(f["data"]) for _, f in s.redis.xrange(f"sym:{NAMESPACE}:session:{meta['sid']}:events")
              if f["kind"] == "tool_execution_end"]
    refused = [f for f in frames if f.get("toolName") == "symphony_attach_evidence" and f.get("isError")]
    sc.check("the binary file was refused by the tool", bool(refused),
             [json.dumps(r.get("result"))[:300] for r in refused[:1]])
    sc.check("meta.evidence[] lists only the text file",
             [e["title"] for e in end.get("evidence") or []] == ["pytest evidence"], end.get("evidence"))


def main_red(run, b: Board) -> None:
    s = run.s
    sc = run.results["7"] = Scenario("7", "GitHub tracker: red main → issue on the board; green → closed",
                                     "T8, X3 (Discord)")
    s.start_merger()
    s.command("pause", "e2e: the main-red ticket must not be dispatched")
    before = b.numbers()
    run.cleanups.append(lambda: (s.gh("POST", "ci", {"repo": APP, "branch": "main", "clear": True}),
                                 b.close_open(b.numbers() - before)))
    s.gh("POST", "ci", {"repo": APP, "branch": "main", "conclusion": "failure", "log": LOG_LINE})
    t0 = time.monotonic()
    s.push_main_commit("e2e: red main")
    new = until(lambda: sorted(b.numbers() - before), 300, "the main-watch issue", interval=2)
    sc.time("red run on main → issue", time.monotonic() - t0)
    t = until(lambda: (lambda v: v if v["status"] else None)(b.view(new[0])), 30, "the issue on the board")
    sc.check("issue opened on the board in Todo with symphony + ci",
             t["status"] == "Todo" and {"symphony", "ci"} <= set(t["labels"]),
             {k: t[k] for k in ("number", "title", "status", "labels")})
    sc.check("the body carries the failing job's log", LOG_LINE in t["body"], t["body"][:500])
    red = until(lambda: s.notices("main_red"), 60, "main_red notice")
    sc.check(f"{s.notify}: main_red", True, red[-1]["text"])
    s.push_main_commit("e2e: still red")
    s.gh("POST", "ci/drain", {"timeout_s": 120})
    time.sleep(35)  # one more safety poll of the watch
    sc.check("a second red run opens no second issue", len(b.numbers() - before) == 1, sorted(b.numbers() - before))
    s.gh("POST", "ci", {"repo": APP, "branch": "main", "conclusion": "success"})
    t1 = time.monotonic()
    s.push_main_commit("e2e: green main")
    closed = b.until(t["number"], ("Done", "closed"), "the issue to close", timeout=300)
    sc.time("green run on main → issue Done", time.monotonic() - t1)
    sc.check("green: comment, and Done on the board", closed["status"] == "Done" and bool(closed["comments"]),
             {"status": closed["status"], "closed": closed["closed"],
              "comment": closed["comments"][-1]["body"][:300] if closed["comments"] else None})
    green = until(lambda: s.notices("main_green"), 60, "main_green notice")
    sc.check(f"{s.notify}: main_green", True, green[-1]["text"])


SCENARIOS = (("3", investigation), ("4", tree), ("1", implement_merge), ("7", main_red))


def execute(run, wanted: set[str]) -> None:
    b = Board(run)
    for key, fn in SCENARIOS:
        if key not in wanted:
            continue
        print(f"== scenario {key} (github: {fn.__name__})", flush=True)
        try:
            fn(run, b)
        except Exception as exc:  # noqa: BLE001 — one scenario's failure is its own result
            sc = run.results.setdefault(key, Scenario(key, fn.__name__, ""))
            sc.error = f"{type(exc).__name__}: {exc}"
            (run.s.tmp / "logs" / f"scenario-{key}.trace").write_text(traceback.format_exc())
            print(f"  [ERROR] {key}: {sc.error[:500]}", flush=True)
        finally:
            run.after_scenario()
