"""Stage 1: the review of one PR, its queue, and the hand-off to the merge train (R1–R13).

A review runs the precheck, reviewer lanes (one reviewer on a re-review) and one oracle per four
findings, all typed through host tools. A finding blocks only when the reviewer and the oracle
both grade it blocking in a blocking class; on a PR already sent back once, only within the
once-only scope, and then an AI arbiter decides between releasing it and asking a maintainer.
CI never holds the review: red CI is a blocker in the same report; still running, the report
goes out now and the review is parked until CI settles it.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..teamconfig import model_arg
from . import agents as ag
from . import comments as cm
from . import prompts
from .ci import cancel_ci, ci_state, revive_ci
from .context import Merger, log
from .policy import ALL_LANE, Policy
from .rules import near_hunks, parse_diff, rule_checks
from .store import INSTANCE

HOUR_MS = 3600_000
BUSY_RETRY_MS = 10 * 60_000
PROBE_TTL_MS = 10 * 60_000
VERIFY_CHUNK = 4
ROLE_KIND = {"reviewer": "review", "oracle": "oracle", "arbiter": "arbiter", "developer": "resolve"}
REVIEW_TOOLS = ("read", "grep")


class ReviewError(Exception):
    pass


class ProbeFailed(Exception):
    """A role's model or tools are down: the worker backs off instead of failing PR after PR."""


@dataclass
class Run:
    pr: int
    repo: str
    title: str
    body: str
    branch: str
    head_sha: str
    author: str
    ready_at: str
    issue: str | None
    started_at: int
    author_type: str = "User"
    fork: str = ""
    main_ref: str = "origin/main"
    diff: str = ""
    discussion: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    evidence: list = field(default_factory=list)


def wt_name(n: int) -> str:
    return f"pr-{n}-review"


# -- agents ---------------------------------------------------------------------------
def run_agent(m: Merger, policy: Policy, team: dict, role: str, run: Run | None, pr: int, prompt: str,
              cwd: str, host_tools: list, tools: tuple[str, ...] = REVIEW_TOOLS, model: dict | None = None):
    sid = m.store.next_session_id(pr, role)
    meta = {
        "identifier": (run.issue if run and run.issue else f"PR #{pr}") if pr else "probe",
        "title": f"{role} · #{pr} {run.title}" if run else f"{role} · #{pr}",
        "repo": m.repo,
        "branch": run.branch if run else "",
        "url": m.pr_url(pr) if pr else "",
        "issue_id": None,
        "seq": None,
    }
    if pr:
        m.store.add_session(pr, sid)
    spec = ag.AgentSpec(sid=sid, kind=ROLE_KIND[role], role=role, model=model or policy.model(role, team),
                        prompt=prompt, cwd=cwd, tools=tools, host_tools=host_tools, meta=meta)
    return m.agents.run(spec)


def probe(m: Merger, policy: Policy, team: dict, roles: list[str]) -> None:
    """Each role's model with the role's tools, on a nonce only a working read tool returns;
    cached 10 minutes per role and model."""
    probe_ref = team["models"]["probe"]
    for role in roles:
        model = {**policy.model(role, team), "thinking": probe_ref.get("thinking", "low")}
        cached = m.store.probe(role)
        if (cached and cached.get("ok") and cached.get("model") == model_arg(model)
                and m.clock() - cached.get("at", 0) < PROBE_TTL_MS):
            continue
        nonce = secrets.token_hex(8)
        # One directory per probe: workers probe the same role at once, each on its own nonce.
        directory = os.path.join(m.git.root, "probe", f"{role}-{nonce}")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "PROBE.txt")
        with open(path, "w") as fh:
            fh.write(nonce)
        tools = ag.EDIT_TOOLS if role == "developer" else REVIEW_TOOLS
        try:
            res = run_agent(m, policy, team, role, None, 0, prompts.probe_prompt(path), directory,
                            ag.probe_tools(), tools, model)
            got = (res.last("submit_review") or {}).get("summary", "").strip()
            ok = res.status == "completed" and got == nonce
            error = "" if ok else (res.error or f"the read returned {got[:80]!r}, not the file's content")
        except ag.AgentFailed as exc:
            ok, error = False, str(exc)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        m.store.put_probe(role, {"ok": ok, "error": error, "at": m.clock(), "model": model_arg(model)})
        if not ok:
            raise ProbeFailed(f"{role} ({model_arg(model)}): {error}")


# -- baselines and deltas ----------------------------------------------------------------
def prior_rounds(m: Merger, n: int) -> int:
    b = m.store.baseline(n)
    return 0 if b is None else int(b.get("rounds", 1))


def save_baseline(m: Merger, n: int, head: str, findings: list, rounds: int, outcome: str) -> None:
    m.store.put_baseline(n, {
        "pr": n, "head": head, "base": m.git.merge_base(m.git.origin_main, head), "rounds": rounds,
        "outcome": outcome,
        "findings": [f for f in findings if f.get("verdict") not in ("refuted", "defer")],
    })


def plan_delta(m: Merger, n: int, head: str) -> dict | None:
    """What a re-review compares against (state.ts planDelta): `from` is a commit whose tree is
    the reviewed PR, so `git diff from head` shows only the author's changes; `mixed` when the
    reviewed commits don't replay onto the new fork point. None: a full review."""
    b = m.store.baseline(n)
    if b is None:
        return None
    git = m.git
    if not (git.exists(b["head"]) and b.get("base") and git.exists(b["base"])):
        log.info("merger_full_review", pr=n, why="the reviewed head is gone")
        return None
    base = git.merge_base(git.origin_main, head)
    if not base:
        return None
    delta = {"reviewed_head": b["head"], "reviewed_base": b["base"], "base": base,
             "findings": b["findings"], "rounds": int(b.get("rounds", 1)), "outcome": b.get("outcome", "blocked")}
    if base == b["base"]:
        delta.update(**{"from": b["head"], "rebased": False, "mixed": False})
    else:
        mixed = any(git.merges(r) for r in (f"{b['base']}..{b['head']}", f"{base}..{head}"))
        replayed = "" if mixed else git.replay(base, f"{b['base']}..{b['head']}")
        delta.update(**{"from": replayed or b["head"], "rebased": True, "mixed": not replayed})
        proc = git.run("range-diff", f"{b['base']}..{b['head']}", f"{base}..{head}", check=False)
        delta["range_diff"] = proc.stdout
    delta["diff"] = git.run("diff", "--no-color", delta["from"], head, check=False).stdout
    return delta


# -- review and verification ---------------------------------------------------------------
def letters(k: int) -> str:
    return chr(65 + k) if k < 26 else letters(k // 26 - 1) + letters(k % 26)


def merge_reviews(parts: list[dict]) -> dict:
    """The lanes' reviews as one: re-lettered A, B, … in lane order, a finding another lane
    already reported at the same location with the same claim dropped."""
    seen, findings = set(), []
    for f in (f for p in parts for f in p["findings"]):
        key = (f["location"].strip(), re.sub(r"\s+", " ", f["claim"].strip().lower()))
        if key in seen:
            continue
        seen.add(key)
        findings.append({**f, "id": letters(len(findings))})
    return {"strengths": [s for p in parts for s in p["strengths"]], "findings": findings,
            "verified": "\n".join(p["verified"] for p in parts if p["verified"])}


def graded_blocking(g: dict, classes: tuple[str, ...]) -> bool:
    return g.get("severity") == "blocking" and g.get("class") in classes


def on_body(location: str) -> bool:
    loc = (location or "").strip()
    return not re.match(r"^[\w./-]+\.\w+(:\d+)?", loc) and bool(re.search(r"\b(body|title)\b", loc, re.I))


def once_only(m: Merger, run: Run, delta: dict | None):
    if not delta or delta["rounds"] < 1:
        return lambda f, x: True
    since = parse_diff(m.git.diff(delta["from"], run.head_sha, exact=True)).hunks
    own = parse_diff(m.git.diff(m.git.origin_main, run.head_sha)).hunks
    earlier = {f["id"] for f in delta["findings"]
               if f.get("severity") == "blocking" and f.get("verdict") in ("confirmed", "partial")}

    def scoped(f: dict, x: dict) -> bool:
        reopens = x.get("reopens") or (f.get("reopens") if x["verdict"] == "confirmed" else None)
        if reopens and reopens in earlier:
            return True
        loc = f["location"].strip().replace(f"{run.wt}/", "")
        return x["class"] in ("bug", "regression") and near_hunks(loc, since) and near_hunks(loc, own)

    return scoped


def review_and_verify(m: Merger, policy: Policy, team: dict, run: Run, delta: dict | None) -> dict:
    n = run.pr
    classes = policy.blocking_classes
    pr_doc = {"body": run.body}
    pre = rule_checks(m.git, policy, run.wt, m.git.origin_main, run.head_sha, pr_doc)
    m.store.set_reviewing(n, phase="review")
    lanes = [ALL_LANE] if delta else list(policy.lanes)

    def review(lane: dict) -> dict:
        res = run_agent(m, policy, team, "reviewer", run, n,
                        prompts.review_prompt(run, pre, lane, delta, classes, policy.rules, len(lanes)),
                        run.wt, ag.reviewer_tools(classes, bool(delta)))
        if res.status != "completed":
            raise ReviewError(f"reviewer {lane['key']} failed: {res.error}")
        done = res.last("submit_review") or {}
        return {"findings": res.calls["submit_finding"], "strengths": done.get("strengths", []),
                "verified": done.get("verified", "")}

    with ThreadPoolExecutor(max_workers=len(lanes)) as pool:
        parts = list(pool.map(review, lanes))
    merged = merge_reviews(parts)
    findings = merged["findings"]
    m.store.set_reviewing(n, phase="verify")

    def verify(subset: list[dict]) -> list[dict]:
        chunks = [subset[i:i + VERIFY_CHUNK] for i in range(0, len(subset), VERIFY_CHUNK)]

        def one(chunk: list[dict]) -> list[dict]:
            ids = [f["id"] for f in chunk]
            try:
                res = run_agent(m, policy, team, "oracle", run, n,
                                prompts.verify_prompt(run, pre, chunk, findings, delta, classes, policy.rules),
                                run.wt, ag.oracle_tools(classes, ids, bool(delta)))
            except ag.AgentFailed as exc:
                log.warning("merger_oracle_failed", pr=n, ids=ids, error=str(exc)[:300])
                return []
            if res.status == "aborted":
                raise ReviewError(f"an oracle was interrupted: {res.error}")
            # A verdict counts only for a finding of its oracle's chunk; the last one per id wins.
            by_id = {v["id"]: v for v in res.calls["submit_verdict"] if v["id"] in ids}
            return list(by_id.values())

        if not chunks:
            return []
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            return [v for vs in pool.map(one, chunks) for v in vs]

    verdicts = verify(findings)

    def unsettled() -> list[dict]:
        have = {v["id"] for v in verdicts}
        return [f for f in findings if graded_blocking(f, classes) and not on_body(f["location"]) and f["id"] not in have]

    if unsettled():
        verdicts.extend(verify(unsettled()))
    if unsettled():
        run.notes.append(f"#{n} {', '.join(f['id'] for f in unsettled())}: no oracle verdict twice; "
                         "reported as not verified, not blocking.")

    by_id = {f["id"]: f for f in findings}

    def real(x: dict) -> bool:
        return x["verdict"] in ("confirmed", "partial")

    scoped = once_only(m, run, delta)

    def both(x: dict) -> bool:
        f = by_id.get(x["id"])
        return real(x) and f is not None and graded_blocking(f, classes) and graded_blocking(x, classes)

    narrowed = [x["id"] for x in verdicts if both(x) and not scoped(by_id[x["id"]], x)]
    if narrowed:
        run.notes.append(f"#{n} Re-review: {', '.join(narrowed)} reported, not blocking: the PR was already "
                         "sent back once, and they repeat no earlier blocking finding and are no bug or "
                         "regression on the lines changed since.")
    blocking = []
    for x in verdicts:
        if both(x) and scoped(by_id[x["id"]], x):
            f = by_id[x["id"]]
            if on_body(f["location"]):
                run.notes.append(f"#{n} Body: {x['id']} ({f['location']}): {f['claim']} Suggested: {x['fix']}")
            else:
                blocking.append(x["id"])
    line = ""
    if blocking:
        line = f"#{n} {', '.join(blocking)}: confirmed, for its author to fix"
        run.blockers.append(line)
    for b in pre.body:
        run.notes.append(f"#{n} Body: {b}")
    for x in verdicts:
        if x["verdict"] == "defer":
            run.notes.append(f"#{n} {x['id']} deferred: {x['fix']}")
    for b in pre.breaks:
        run.notes.append(f"#{n} {b}")
        run.blockers.append(f"#{n} rule break: {b.split(chr(10))[0].rstrip(':')}")
    vmap = {x["id"]: x for x in verdicts}
    rows = []
    for f in findings:
        x = vmap.get(f["id"])
        # The grade that decided: the oracle's on a real finding (blocking only when both agreed
        # in scope); a refuted, deferred or unverified finding is reported as a nit.
        verified = bool(x and real(x))
        rows.append({"id": f["id"], "severity": "blocking" if f["id"] in blocking else "nit",
                     "class": x["class"] if x else f["class"], "location": f["location"],
                     "verdict": x["verdict"] if x else "not verified", "claim": f["claim"],
                     "fix": x["fix"] if verified else ""})
    run.findings = rows
    return {"pr": n, "findings": len(findings), "confirmed": blocking, "blocker": line,
            "rejected": [f"{x['id']} {x['verdict']}: {x['evidence']}" for x in verdicts if not real(x)]}


def arbitrate(m: Merger, policy: Policy, team: dict, run: Run, delta: dict | None, ids: list[str]) -> dict | None:
    """R9: a blocking finding after the PR was sent back once goes to a third model. Returns
    `{upheld: [ids], rulings: […], summary}`, or None when the arbiter failed (a maintainer then
    decides)."""
    blocking = [f for f in run.findings if f["id"] in ids]
    m.store.set_reviewing(run.pr, phase="arbitrate")
    try:
        res = run_agent(m, policy, team, "arbiter", run, run.pr,
                        prompts.arbiter_prompt(run, blocking, delta, policy.blocking_classes, policy.rules),
                        run.wt, ag.arbiter_tools(ids))
    except ag.AgentFailed as exc:
        log.warning("merger_arbiter_failed", pr=run.pr, error=str(exc)[:300])
        return None
    out = res.last("submit_arbitration")
    if res.status != "completed" or out is None:
        return None
    return {"upheld": [r["id"] for r in out["rulings"] if r["ruling"] == "uphold"],
            "rulings": out["rulings"], "summary": out["summary"]}


# -- CI, parking, hand-off -------------------------------------------------------------------
def ci_now(m: Merger, policy: Policy, n: int, head_sha: str, branch: str, revive: bool = False) -> dict:
    """One look at CI on the head: passed, failed, pending, or moved (the author pushed; the
    remote decides, as GitHub's API can lag a push)."""
    pr = m.gh.pr(n)
    at = {"open": pr["state"] == "open", "draft": pr["draft"]}
    if pr["head_sha"] != head_sha:
        remote = m.git.ls_remote(branch)
        return {"state": "pending" if remote == head_sha else "moved", "failed": [], **at}
    state, failed = ci_state(m.gh.check_runs(head_sha), policy.ci_checks)
    if state in ("passed", "failed"):
        return {"state": state, "failed": failed, **at}
    if state == "none" and revive:
        revive_ci(m.gh, head_sha, policy.ci_workflow)
    return {"state": "pending", "failed": [], **at}


def parked_of(m: Merger, run: Run) -> dict:
    return {"pr": run.pr, "branch": run.branch, "author": run.author, "title": run.title,
            "issue": run.issue, "head_sha": run.head_sha, "ready_at": run.ready_at,
            "started_at": run.started_at, "parked_at": m.clock(), "findings": run.findings,
            "rounds": prior_rounds(m, run.pr)}


def unpark(m: Merger, rec: dict, why: str) -> None:
    """A parked review no longer waits; a clean one stands for its head as the next baseline."""
    if rec["kind"] == "clean":
        save_baseline(m, rec["pr"], rec["head_sha"], rec["findings"], rec["rounds"], "interrupted")
    m.store.unpark(rec["pr"])
    log.info("merger_unparked", pr=rec["pr"], why=why)


def hand_off_text(head_sha: str) -> str:
    return (f"reviewed, waiting to merge: the merge train rebases `{head_sha[:9]}` onto main and merges "
            "it once CI passes, else it goes back to draft.")


def hand_off(m: Merger, policy: Policy, run, report: str, edit: str | None = None) -> None:
    """A positive review to the train: the record first (publishing, with the rendered report),
    then the publication, then the record is marked reviewed. A crash leaves a publishing
    record that `reconcile` finishes."""
    n = run["pr"] if isinstance(run, dict) else run.pr
    get = (lambda k: run[k]) if isinstance(run, dict) else (lambda k: getattr(run, k))
    rec = {
        "pr": n, "branch": get("branch"), "author": get("author"), "title": get("title"),
        "issue": get("issue"), "head_sha": get("head_sha"), "reviewed_at": m.clock(),
        "ready_at": get("ready_at"), "started_at": get("started_at"), "policy": cm.VERSION,
        "state": "publishing", "publication": {"report": report, "done": [], **({"edit": edit} if edit else {})},
    }
    m.store.drop_cap(n)
    save_baseline(m, n, rec["head_sha"], get("findings"), prior_rounds(m, n), "approved")
    m.store.put_rec(rec)
    cancel_ci(m.gh, rec["head_sha"], policy.ci_workflow)
    try:
        cm.publish_review(m, rec, m.store.put_rec)
        m.git.remove_worktree(wt_name(n))
        m.store.drop_run(n)
    except Exception as exc:  # noqa: BLE001 — reconcile finishes it
        log.warning("merger_handoff_failed", pr=n, error=str(exc)[:300])
        return
    m.store.put_rec({**rec, "state": "reviewed"})
    m.wake(f"handoff:{n}")


def bypass_hand_off(m: Merger, policy: Policy, p: dict) -> None:
    n = p["number"]
    now = m.clock()
    report = (f"**Review bypassed**\n\n**{cm.VERSION}** on `{p['head_sha'][:9]}`: the `{cm.BYPASS}` label hands "
              "it to the merge train without a review. The train merges it once CI passes on main.")
    rec = {"pr": n, "branch": p["head_ref"], "author": p["author"], "title": p["title"],
           "issue": m.ticket_of(p), "head_sha": p["head_sha"], "reviewed_at": now,
           "ready_at": p["ready_at"], "started_at": now, "policy": "bypass", "state": "publishing",
           "publication": {"report": report, "done": []}}
    m.store.drop_cap(n)
    m.store.drop_baseline(n)
    m.store.put_rec(rec)
    cancel_ci(m.gh, rec["head_sha"], policy.ci_workflow)
    cm.publish_review(m, rec, m.store.put_rec)
    m.store.put_rec({**rec, "state": "reviewed"})
    m.wake(f"handoff:{n}")


def reconcile(m: Merger, owner: str) -> None:
    """A publishing record whose run is gone stopped mid-publication: GitHub still on that head
    finishes it, any other head (or a closed PR) drops it, for stage 1 to review again."""
    for n, r in m.store.recs().items():
        if r["state"] != "publishing" or not m.store.claim(n, owner):
            continue
        try:
            now = m.store.rec(n)
            if not now or now["state"] != "publishing":
                continue
            pr = m.gh.pr(n)
            same = pr["state"] == "open" and pr["head_sha"] == now["head_sha"]
            if same:
                cm.publish_review(m, now, m.store.put_rec)
            m.git.remove_worktree(wt_name(n))
            m.store.drop_run(n)
            if same:
                m.store.put_rec({**now, "state": "reviewed"})
                m.wake(f"handoff:{n}")
            else:
                m.store.drop_rec(n)
            log.info("merger_reconciled", pr=n, handed=same)
        except Exception as exc:  # noqa: BLE001 — retried next pass
            log.warning("merger_reconcile_failed", pr=n, error=str(exc)[:300])
        finally:
            m.store.unclaim(n, owner)


def settle_parked(m: Merger, policy: Policy, owner: str) -> None:
    """Each parked review whose head CI ended: green hands a clean one to the train (its comment
    becomes the Reviewed report), red sends it back; a report only gets the CI line."""
    for n in list(m.store.all_parked()):
        if not m.store.claim(n, owner):
            continue
        try:
            rec = m.store.parked(n)
            if not rec:
                continue
            handed = m.store.rec(n)
            if handed and handed["head_sha"] == rec["head_sha"]:
                m.store.unpark(n)
                continue
            ci = ci_now(m, policy, n, rec["head_sha"], rec["branch"], revive=True)
            if not ci["open"]:
                unpark(m, rec, "the PR is closed")
            elif ci["state"] == "moved":
                unpark(m, rec, "its author pushed")
            elif rec["kind"] == "clean" and ci["draft"]:
                unpark(m, rec, "its author put it back to draft")
            elif ci["state"] == "pending":
                continue
            elif rec["kind"] == "report":
                if rec.get("comment"):
                    m.gh.edit_comment(rec["comment"], cm.compose(
                        rec["title"], rec["head_sha"], rec["outcome"],
                        cm.ci_line(ci["state"], rec["head_sha"], ci["failed"]), rec["body"], rec["drafted"]))
                m.store.unpark(n)
            elif ci["state"] == "passed":
                from .approval import check_approval

                if not check_approval(m, policy, rec):
                    continue  # still parked, waiting for approval
                report = cm.compose(cm.REVIEWED_TITLE, rec["head_sha"], hand_off_text(rec["head_sha"]),
                                    cm.ci_line("passed", rec["head_sha"]), rec["body"], False)
                hand_off(m, policy, rec, report, rec.get("comment") or None)
                m.store.unpark(n)
            else:
                ci_sent_back(m, rec, ci["failed"])
        except Exception as exc:  # noqa: BLE001
            log.warning("merger_settle_failed", pr=n, error=str(exc)[:300])
        finally:
            m.store.unclaim(n, owner)


def ci_sent_back(m: Merger, rec: dict, failed: list[str]) -> None:
    n = rec["pr"]
    blocker = f"#{n} CI failed on {rec['head_sha'][:9]}: {'; '.join(failed)}"
    first = rec["rounds"] < 1
    save_baseline(m, n, rec["head_sha"], rec["findings"], rec["rounds"] + 1, "blocked" if first else "held")
    drafted = first and cm.to_draft(m, n)
    outcome = (f"not merged: {blocker}." if first else
               f"not merged: {blocker}. It was sent back once already, so it stays ready: push the fix, and "
               "the next review covers only what changed.")
    if rec.get("comment"):
        m.gh.edit_comment(rec["comment"], cm.compose(cm.NOT_MERGED_TITLE, rec["head_sha"], outcome,
                                                     cm.ci_line("failed", rec["head_sha"], failed), rec["body"], drafted))
    m.store.unpark(n)
    cm.say(m, "back_to_author", n, rec.get("title", ""), rec["author"], rec.get("comment") or m.pr_url(n),
           "CI failed")
    m.move_ticket(rec.get("issue"), "rework", f"The merger sent the PR back: {outcome}\n\n{rec.get('comment') or m.pr_url(n)}")


# -- one review -----------------------------------------------------------------------------
def ticket_evidence(m: Merger, identifier: str | None) -> list[dict]:
    """The implementer's proof of work for the report: its sessions' `meta.evidence` and the
    ticket's attachments on the tracker, each URL once."""
    if not identifier:
        return []
    items = [e for s in m.agents.sessions.list(limit=200)
             if s.get("identifier") == identifier and s.get("kind") == "implement"
             for e in (s.get("evidence") or [])]
    if m.tracker is not None:
        try:
            items.extend(m.tracker.attachments(identifier))
        except Exception as exc:  # noqa: BLE001 — the report goes out without them
            log.warning("merger_evidence_failed", issue=identifier, error=str(exc)[:300])
    seen, out = set(), []
    for e in items:
        if isinstance(e, dict) and e.get("url") and e["url"] not in seen:
            seen.add(e["url"])
            out.append({"title": e.get("title") or e["url"], "url": e["url"], "kind": e.get("kind") or ""})
    return out


def find_pr(m: Merger, n: int) -> dict:
    p = m.gh.pr(n)
    if p["state"] != "open":
        raise ReviewError(f"PR #{n} is {p['state']}")
    if p["draft"]:
        raise ReviewError(f"PR #{n} is a draft")
    if p["base"] != m.main:
        raise ReviewError(f"PR #{n} is based on {p['base']}, not {m.main}")
    return p


def review_pr(m: Merger, policy: Policy, team: dict, n: int, worker: str) -> tuple[str, str, str, str]:
    """Reviews PR n (claimed by the caller). Returns (stop, head, ready_at, reason)."""
    m.git.fetch()
    p = find_pr(m, n)
    prior = m.store.rec(n)
    if prior and prior["state"] in ("train", "resolve", "finalizing"):
        raise ReviewError(f"#{n} is in the merge train")
    if prior and prior["state"] in ("reviewed", "publishing") and prior["head_sha"] == p["head_sha"]:
        raise ReviewError(f"#{n} is already handed to the merge train on {p['head_sha'][:9]}")
    parked = m.store.parked(n)
    if parked and parked["head_sha"] == p["head_sha"]:
        raise ReviewError(f"#{n} is reviewed on {p['head_sha'][:9]} and waits for CI")
    if parked:
        unpark(m, parked, "its author pushed")
    saved = m.store.saved_run(n)
    if saved and saved["head"] != p["head_sha"]:
        m.store.drop_run(n)
        saved = None
    delta = None if saved else plan_delta(m, n, p["head_sha"])
    run = Run(pr=n, repo=m.repo, title=p["title"], body=p["body"], branch=p["head_ref"], head_sha=p["head_sha"],
              author=p["author"], ready_at=p["ready_at"], issue=m.ticket_of(p), started_at=m.clock(),
              author_type=p.get("author_type", "User"))
    m.store.set_reviewing(n, head_sha=run.head_sha, worker=worker, phase="pin", started_at=run.started_at)
    try:
        run.wt = m.git.worktree(wt_name(n), run.head_sha)
        run.fork = m.git.merge_base(m.git.origin_main, run.head_sha)
        run.diff = m.git.run("diff", "--no-color", run.fork, run.head_sha).stdout
        run.discussion = [{**e, "ours": bool(cm.OURS.match(e["body"]))} for e in m.gh.discussion(n)]
        run.evidence = ticket_evidence(m, run.issue)
        cm.announce(m, n)
        if delta:
            how = (f", compared with that head itself, as its commits don't replay onto main at {delta['base'][:9]}"
                   if delta["mixed"] else
                   f", its reviewed commits replayed onto main at {delta['base'][:9]}" if delta["rebased"] else "")
            run.notes.append(f"#{n} Re-review: the earlier findings, and what the author changed since "
                             f"{delta['reviewed_head'][:9]}{how}.")
        if saved:
            run.notes, run.blockers, run.findings = saved["notes"], saved["blockers"], saved["findings"]
            result = saved["result"]
        else:
            result = review_and_verify(m, policy, team, run, delta)
            m.store.save_run(n, {"head": run.head_sha, "result": result, "notes": run.notes,
                                 "blockers": run.blockers, "findings": run.findings})
        return _outcome(m, policy, team, run, result, delta)
    except Exception as exc:
        m.store.drop_run(n)
        cm.run_failed(m, run, f"run failed: {str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__}")
        raise
    finally:
        m.git.remove_worktree(wt_name(n))
        m.store.clear_reviewing(n)


def _finish(m: Merger, n: int) -> None:
    rec = m.store.rec(n)
    if rec and rec["state"] == "reviewed":
        m.store.drop_rec(n)
    m.store.drop_run(n)


def _is_bot_author(run: Run) -> bool:
    """A GitHub App or bot: __typename == Bot, or the login ends with [bot]."""
    return run.author_type == "Bot" or run.author.endswith("[bot]")


def _actionable_nits(findings: list) -> list[dict]:
    """Findings worth fixing: confirmed, partial or not verified (unrefuted)."""
    return [f for f in findings
            if f.get("verdict") in ("confirmed", "partial", "not verified")]


def _polish_comment(nits: list[dict]) -> str:
    """The list of actionable nits for the PR comment and the ticket."""
    lines = ["The review found nits that an automated author can fix:\n"]
    for f in nits:
        fix = f" Fix: {f['fix']}" if f.get("fix") else ""
        lines.append(f"- **{f['id']}** `{f['location']}`: {f['claim']}{fix}")
    lines.append("\nFix the ones that hold; for the ones that do not, reply on the PR why; "
                 "then mark the PR ready.")
    return "\n".join(lines)


def _outcome(m: Merger, policy: Policy, team: dict, run: Run, result: dict, delta) -> tuple[str, str, str, str]:
    n = run.pr

    def give_back(stop: str, text: str, draft: bool, event: str | None, ci_pending: bool) -> tuple:
        _finish(m, n)
        comment, drafted = cm.conclude(m, run, result, text, draft, event,
                                       cm.ci_line("pending", run.head_sha) if ci_pending else "")
        if ci_pending:
            m.store.park({**parked_of(m, run), "kind": "report", "title": cm.NOT_MERGED_TITLE, "outcome": text,
                          "body": cm.scrubbed_body(m, run, result), "drafted": drafted, "comment": comment})
        if stop in ("blocked", "held"):
            m.move_ticket(run.issue, "rework",
                          f"The merger sent the PR back: {text}\n\n{comment or m.pr_url(n)}")
        elif stop == "polish":
            m.move_ticket(run.issue, "rework",
                          f"The merger sent the PR back for polish.\n\n"
                          f"{_polish_comment(_actionable_nits(run.findings))}\n\n"
                          f"{comment or m.pr_url(n)}")
        return stop, run.head_sha, run.ready_at, text[:500]

    remote = m.git.ls_remote(run.branch)
    if remote != run.head_sha:
        save_baseline(m, n, run.head_sha, run.findings, prior_rounds(m, n), "interrupted")
        text = (f"stopped: its author pushed {remote[:9] or '(branch gone)'} during the review of "
                f"{run.head_sha[:9]}; the next review covers what the push changed since this one.")
        run.notes.append(text)
        return give_back("gone", text, False, None, False)

    m.store.set_reviewing(n, phase="ci")
    ci = ci_now(m, policy, n, run.head_sha, run.branch)
    if ci["state"] == "moved":
        save_baseline(m, n, run.head_sha, run.findings, prior_rounds(m, n), "interrupted")
        return give_back("gone", "stopped: the head moved", False, None, False)
    if ci["state"] == "failed":
        run.blockers.append(f"#{n} CI failed on {run.head_sha[:9]}: {'; '.join(ci['failed'])}")
    ci_pending = ci["state"] == "pending"

    stop = None
    if run.blockers:
        rounds = prior_rounds(m, n)
        judged = bool(result["confirmed"])
        stop = "blocked" if rounds < 1 else ("escalated" if judged else "held")
        if stop == "escalated" and not policy.require_human:
            ruling = arbitrate(m, policy, team, run, delta, result["confirmed"])
            if ruling is None:
                run.notes.append(f"#{n} Arbiter: no ruling (the agent failed); a maintainer decides.")
            elif ruling["upheld"]:
                run.notes.append(f"#{n} Arbiter upheld {', '.join(ruling['upheld'])}: {ruling['summary']}")
            else:
                for f in run.findings:
                    if f["id"] in result["confirmed"]:
                        f["severity"] = "nit"
                        f["verdict"] = f"{f['verdict']}, dismissed by the arbiter"
                for r in ruling["rulings"]:
                    run.notes.append(f"#{n} Arbiter dismissed {r['id']}: {r['reason']}")
                run.blockers = [b for b in run.blockers if b != result["blocker"]]
                result = {**result, "confirmed": [], "blocker": ""}
                stop = "held" if run.blockers else None
        if stop:
            save_baseline(m, n, run.head_sha, run.findings, rounds + 1, stop)
            run.notes.append("not merged:" + "".join(f"\n- {b}" for b in run.blockers))
            blockers = "; ".join(run.blockers)
            if stop == "blocked":
                return give_back("blocked", f"not merged: {blockers}.", True, "back_to_author", ci_pending)
            if stop == "escalated":
                try:
                    m.gh.add_labels(n, [cm.NEEDS_MAINTAINER])
                except Exception:  # noqa: BLE001 — the comment and the channel still say it
                    pass
                return give_back(
                    "escalated",
                    f"needs a maintainer: {blockers}. It was sent back once already, so it stays ready with the "
                    f"`{cm.NEEDS_MAINTAINER}` label instead of going back to draft, and a maintainer decides: "
                    f"answer the findings here and remove the label to have this head reviewed again, or add "
                    f"`{cm.BYPASS}`. A push is reviewed again from this review.",
                    False, "needs_maintainer", ci_pending)
            return give_back(
                "held",
                f"not merged: {blockers}. It was sent back once already, so it stays ready: push the fix, and "
                "the next review covers only what changed.", False, "back_to_author", ci_pending)

    # -- polish round: an agent-authored PR with actionable nits is sent back to its author
    # before going on to CI / approval / hand-off, so the agent can fix them autonomously.
    if (policy.polish_rounds > 0
            and _is_bot_author(run)
            and run.issue
            and m.store.polish_rounds(n) < policy.polish_rounds):
        nits = _actionable_nits(run.findings)
        if nits:
            m.store.incr_polish(n)
            save_baseline(m, n, run.head_sha, run.findings, prior_rounds(m, n), "polish")
            # The list goes in the report itself: the author's Rework prompt reads the latest
            # report, and one comment is all the PR needs.
            return give_back("polish", f"polish round: {len(nits)} actionable nit(s) for its author to fix.\n\n"
                             f"{_polish_comment(nits)}", True, "back_to_author", ci_pending)

    if ci_pending:
        outcome = (f"reviewed, nothing blocking: once CI passes on `{run.head_sha[:9]}` the merge train takes "
                   "it; a red CI sends it back.")
        _finish(m, n)
        comment = cm.post_waiting(m, run, result, outcome)
        m.store.park({**parked_of(m, run), "kind": "clean", "title": cm.WAITING_TITLE, "outcome": outcome,
                      "body": cm.scrubbed_body(m, run, result), "drafted": False, "comment": comment})
        return "waiting", run.head_sha, run.ready_at, ""

    # Approval gate: the review passed and CI is green, but the policy requires a human
    # approval on GitHub.  Park the PR until it is approved.
    from .approval import check_approval

    if not check_approval(m, policy, {"pr": n, "head_sha": run.head_sha, "author": run.author,
                                      "title": run.title, "branch": run.branch}):
        outcome = (f"reviewed, nothing blocking: waiting for a human approval on `{run.head_sha[:9]}`. "
                   "Once approved, the merge train takes it.")
        _finish(m, n)
        comment = cm.post_waiting(m, run, result, outcome)
        m.store.park({**parked_of(m, run), "kind": "clean", "title": cm.WAITING_TITLE, "outcome": outcome,
                      "body": cm.scrubbed_body(m, run, result), "drafted": False, "comment": comment})
        return "waiting", run.head_sha, run.ready_at, ""

    report = cm.scrub(cm.compose(cm.REVIEWED_TITLE, run.head_sha, hand_off_text(run.head_sha), "",
                                 cm.report_body(run, result), False), run.wt, m.git.path)
    hand_off(m, policy, run, report)
    return "reviewed", run.head_sha, run.ready_at, ""


# -- the queue --------------------------------------------------------------------------------
def retry(s: dict, p: dict, main: str, now: int) -> bool:
    if s["head"] != p["head_sha"]:
        return True
    if p["ready_at"] > (s.get("ready_at") or ""):
        return True
    if s["stop"] == "busy":
        return now - s["at"] > BUSY_RETRY_MS
    if s["stop"] == "error":
        return s["main"] != main or now - s["at"] > HOUR_MS
    if s["stop"] == "escalated":
        return cm.NEEDS_MAINTAINER not in p["labels"]
    return False


def eligible(m: Merger, p: dict, skips: dict, recs: dict, parked: dict, main: str) -> bool:
    if p["draft"] or p["base"] != m.main:
        return False
    r = recs.get(p["number"])
    if r and (r["state"] != "reviewed" or r["head_sha"] == p["head_sha"]):
        return False
    if cm.BYPASS in p["labels"]:
        return True
    if (parked.get(p["number"]) or {}).get("head_sha") == p["head_sha"]:
        return False
    s = skips.get(p["number"])
    return not s or retry(s, p, main, m.clock())


def pick(m: Merger, policy: Policy, owner: str) -> dict | None:
    """R1: bypassed PRs first, then re-reviews, then first reviews, each oldest ready first,
    among open, non-draft PRs based on main whose head has CI, not reviewed on that head, not in
    the train. Returns the PR claimed for `owner`, or None."""
    m.git.fetch()
    main = m.git.main_sha()
    prs = [p for p in m.gh.list_open() if p["base"] == m.main]
    open_ = {p["number"] for p in prs}
    skips = m.store.skips()
    for k in set(skips) - open_:
        m.store.drop_skip(k)
    recs, parked = m.store.recs(), m.store.all_parked()
    rereview = m.store.baseline_prs()
    candidates = sorted(
        (p for p in prs if eligible(m, p, skips, recs, parked, main)),
        key=lambda p: (cm.BYPASS not in p["labels"], p["number"] not in rereview, p["ready_at"], p["number"]),
    )
    m.store.set_queue([
        {"pr": p["number"], "title": p["title"], "author": p["author"], "ready_at": p["ready_at"],
         "kind": "bypass" if cm.BYPASS in p["labels"] else "rereview" if p["number"] in rereview else "first"}
        for p in candidates
    ])
    for p in candidates:
        bypassed = cm.BYPASS in p["labels"]
        if not bypassed:
            checks = m.gh.check_runs(p["head_sha"])
            if not checks:
                continue
            if ci_state(checks, policy.ci_checks)[0] == "none":
                revive_ci(m.gh, p["head_sha"], policy.ci_workflow)
        if not m.store.claim(p["number"], owner):
            continue
        try:
            now = m.gh.pr(p["number"])
            if now["state"] == "open" and eligible(m, now, m.store.skips(), m.store.recs(), m.store.all_parked(), main):
                return {**now, "main": main}
        except Exception:
            m.store.unclaim(p["number"], owner)
            raise
        m.store.unclaim(p["number"], owner)
    return None


def apply_actions(m: Merger, owner: str) -> None:
    """Operator actions from the API, applied once each, by one worker at a time."""
    with m.store.lease("actions", owner, 60_000) as lease:
        if not lease.held:
            return
        for a in m.store.pending_actions():
            n, action = a["pr"], a["action"]
            try:
                if action == "bypass":
                    m.gh.add_labels(n, [cm.BYPASS])
                    m.store.drop_skip(n)
                elif action == "requeue":
                    m.store.drop_skip(n)
                    m.gh.remove_label(n, cm.NEEDS_MAINTAINER)
                elif action == "release":
                    m.store.force_release(f"pr:{n}")
                    m.store.clear_reviewing(n)
                    m.store.drop_skip(n)
                log.info("merger_action", pr=n, action=action, actor=a["actor"], reason=a["reason"])
            except Exception as exc:  # noqa: BLE001 — logged; the action is not retried forever
                log.warning("merger_action_failed", pr=n, action=action, error=str(exc)[:300])
            m.store.ack_action(a["id"])


class ReviewWorker:
    """One review worker: actions, interrupted hand-offs, parked CI, conflicts, then one PR."""

    def __init__(self, m: Merger, worker_id: str, instance: str = INSTANCE) -> None:
        self.m = m
        self.id = worker_id
        self.owner = f"review:{instance}:{worker_id}"

    def step(self) -> str | None:
        """One unit of work; None when there was nothing to do."""
        from .resolve import resolve_next

        m = self.m
        policy = m.policy()
        apply_actions(m, self.owner)
        reconcile(m, self.owner)
        settle_parked(m, policy, self.owner)
        # Preview announcements: scan during the normal review pass.
        from .approval import check_previews_for_open_prs

        check_previews_for_open_prs(m, policy)
        team = m.config("review")
        resolved = resolve_next(m, policy, team, self.owner)
        if resolved:
            return resolved
        p = pick(m, policy, self.owner)
        if p is None:
            return None
        n = p["number"]
        try:
            if cm.BYPASS in p["labels"]:
                bypass_hand_off(m, policy, p)
                m.store.drop_skip(n)
                return f"#{n} bypassed"
            probe(m, policy, team, ["reviewer", "oracle"] + ([] if policy.require_human else ["arbiter"]))
            stop, head, ready, reason = "error", p["head_sha"], p["ready_at"], ""
            try:
                stop, head, ready, reason = review_pr(m, policy, team, n, self.id)
            except Exception as exc:  # noqa: BLE001 — skipped as an error, retried later
                reason = str(exc)[:1000]
                log.warning("merger_review_failed", pr=n, error=reason)
            if stop in ("reviewed", "gone", "waiting"):
                m.store.drop_skip(n)
            else:
                m.store.put_skip(n, {"head": head, "main": p["main"], "stop": stop, "reason": reason,
                                     "at": m.clock(), "ready_at": ready})
            return f"#{n} {stop}"
        finally:
            m.store.unclaim(n, self.owner)
