"""Stage 2: the merge train (T1–T7). Deterministic, no agent: strictly one step at a time
behind the `train` lease, and the only code that pushes for CI and merges.

A step takes, in order: a batch a stopped step left; the record the train had (finalizing,
then train); else the handed-off PR that shares least with what entered main since its review,
or a batch of up to `batch_max` of them when the train holds nothing. A PR alone is pre-checked
(the remote decides whether its author pushed), rebased onto main and pushed with lease, waits
for CI on that head, and is merged on exactly that head; main moving meanwhile rebases it
again, a conflict goes to stage 1 (`resolve`). A batch is chained (each member rebased onto the
one below), CI runs on the top only, and members merge in order while main and the tested tree
stay put. A red batch reruns its failed jobs once, then splits by caps until the culprit is
alone. Every effect is preceded by its intent (`pushing`, `outcome.done`, the batch record).
"""

from __future__ import annotations

import math
import re

from ..reflector import LEARNINGS_BRANCH_PREFIX
from . import comments as cm
from .ci import cancel_ci, ci_state, latest_run, restart_ci
from .context import Merger, log
from .policy import Policy
from .review import reconcile, settle_parked

RANK = {"finalizing": 0, "train": 1, "reviewed": 2, "publishing": 3, "resolve": 4}
NO_CI_MS = 30 * 60_000
CI_WAIT_S = 30
MANIFESTS = {"package.json", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "pom.xml",
             "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "mix.exs", "Package.swift"}
RETRYABLE = re.compile(r"not mergeable|base branch was modified|405", re.IGNORECASE)
# GitHub's refusal on a protected branch whose required checks haven't passed on that head.
REQUIRED = re.compile(r"required status checks?\b", re.IGNORECASE)


def local(rec: dict) -> str:
    return f"pr-{rec['pr']}-train"


def names(prs) -> str:
    return " ".join(f"#{n}" for n in prs)


class Train:
    def __init__(self, m: Merger, owner: str = "train", lease_ttl_ms: int = 60_000) -> None:
        self.m = m
        self.owner = owner
        self.ttl = lease_ttl_ms
        self.lease = None
        self.policy: Policy | None = None

    # -- the step ------------------------------------------------------------------
    def step(self) -> str | None:
        """One step; None when there was nothing to merge. Raises `TrainBusy` when another
        train loop holds the lease."""
        m = self.m
        with m.store.lease("train", self.owner, self.ttl) as lease:
            if not lease.held:
                raise TrainBusy(f"the merge train is busy: {m.store.holder('train')}")
            self.lease = lease
            self.policy = m.policy()
            m.config("train")
            reconcile(m, self.owner)
            settle_parked(m, self.policy, self.owner)
            held = m.store.batch()
            if held:
                prs = [x["pr"] for x in held["members"]]
                taken = [n for n in prs if m.store.claim(n, self.owner)]
                try:
                    if len(taken) < len(prs):
                        return f"the batch {names(prs)} waits for a run that holds one of its PRs"
                    return self.resume(held)
                finally:
                    for n in taken:
                        m.store.unclaim(n, self.owner)
            ordered = sorted((r for r in m.store.recs().values() if r["state"] not in ("publishing", "resolve")),
                             key=lambda r: (RANK[r["state"]], r["reviewed_at"]))
            queue = self.order(ordered)
            feed = {"queue": queue, "at": 0, "caps": {}, "claims": []}
            try:
                picked = self.pick(feed)
                if picked:
                    return self.start(picked, feed)
            finally:
                for n in feed["claims"]:
                    m.store.unclaim(n, self.owner)
            for r in queue:
                if not m.store.claim(r["pr"], self.owner):
                    continue
                try:
                    now = m.store.rec(r["pr"])
                    if not now or now["state"] in ("publishing", "resolve"):
                        continue
                    return self.one(now)
                finally:
                    m.store.unclaim(r["pr"], self.owner)
            return None

    def save(self, rec: dict) -> None:
        self.m.store.put_rec(rec)

    def check(self) -> None:
        self.lease.check()

    # -- the order (T2) ------------------------------------------------------------------
    def touched(self, base: str, head: str, rev: str) -> set[str]:
        """The packages a diff touches: each file's nearest directory with a package manifest
        at `rev`, else its top-level directory."""
        roots = sorted(
            {p.rsplit("/", 1)[0] if "/" in p else "" for p in self.m.git.out("ls-tree", "-r", "--name-only", rev).split("\n")
             if p.rsplit("/", 1)[-1] in MANIFESTS},
            key=len, reverse=True,
        )
        out = set()
        for f in self.m.git.changed(base, head):
            root = next((r for r in roots if r and (f == r or f.startswith(r + "/"))), None)
            out.add(root if root is not None else (f.split("/", 1)[0] if "/" in f else "."))
        return out

    def selected(self, base: str, main: str) -> set[str]:
        """What main's changes since `base` select: the policy's command, else their packages."""
        if not self.policy.overlap:
            return self.touched(base, main, main)
        wt = self.m.git.worktree("train-rank", main)
        try:
            code, output = self.m.git.sh(self.policy.overlap, cwd=wt, env={"SYMPHONY_BASE": base, "SYMPHONY_HEAD": main})
            if code != 0:
                return self.touched(base, main, main)
            return {x.strip() for x in output.split("\n") if x.strip()}
        finally:
            self.m.git.remove_worktree("train-rank")

    def order(self, queue: list[dict]) -> list[dict]:
        """Least overlap with what entered main since the fork, then fewest main commits behind,
        then smallest, then the oldest hand-off. A failure ranks FIFO, never stops."""
        at = next((i for i, r in enumerate(queue) if r["state"] == "reviewed"), -1)
        if at != 0 or len(queue) < 2:
            return queue
        try:
            git = self.m.git
            git.fetch()
            main = git.main_sha()
            plans: dict[str, set[str]] = {}
            keys: dict[int, tuple] = {}
            for r in queue:
                base = git.merge_base(main, r["head_sha"])
                if not base:
                    keys[r["pr"]] = (math.inf, math.inf, math.inf)
                    continue
                distance = git.count(f"{base}..{main}")
                if distance and base not in plans:
                    plans[base] = self.selected(base, main)
                main_side = plans.get(base, set()) if distance else set()
                overlap = len(self.touched(base, r["head_sha"], r["head_sha"]) & main_side)
                keys[r["pr"]] = (overlap, distance, git.shortstat(base, r["head_sha"]))
            ranked = sorted(queue, key=lambda r: (*keys[r["pr"]], r["reviewed_at"]))
            log.info("merger_train_order", order=[f"#{r['pr']} {keys[r['pr']]}" for r in ranked])
            return ranked
        except Exception as exc:  # noqa: BLE001
            log.warning("merger_train_order_failed", error=str(exc)[:300])
            return queue

    # -- one PR (T3) -------------------------------------------------------------------
    def one(self, rec: dict) -> str:
        if rec["state"] == "finalizing":
            return self.finalize(rec)
        return self.take(rec) or self.alone(rec)

    def take(self, rec: dict, merged_outcome=None) -> str | None:
        """The pre-check: the outcome when the PR is dropped, else None, the record taken."""
        n = rec["pr"]
        pr = self.m.gh.pr(n)
        if pr["state"] == "merged" and rec["state"] == "train":
            return self.finish(rec, merged_outcome() if merged_outcome else
                               {"end": "merged", "text": "CI passed; merged.", "done": []})
        if pr["state"] == "merged":
            # Merged outside the merge train (e.g. a human merged it on GitHub). A record
            # written before learnings PRs were excluded may still name a source ticket.
            if not (rec.get("branch") or "").startswith(LEARNINGS_BRANCH_PREFIX):
                self.m.move_ticket(rec.get("issue"), "done",
                                   f"PR #{n} was merged on GitHub outside the merge train.")
            return self.finish(rec, {"end": "merged",
                                     "text": f"#{n} was merged on GitHub outside the merge train.",
                                     "done": []})
        if pr["state"] != "open" or pr["draft"]:
            return self.finish(rec, {"end": "gone", "text": f"#{n} is {'a draft' if pr['draft'] else pr['state']}", "done": []})

        def adopt(head: str) -> None:
            if rec.get("pushing") and head == rec["pushing"]:
                rec["head_sha"] = rec["pushing"]
                rec["pushed_at"] = self.m.clock()

        adopt(pr["head_sha"])
        if pr["head_sha"] != rec["head_sha"]:
            remote = self.m.git.ls_remote(rec["branch"])
            adopt(remote)
            if remote != rec["head_sha"]:
                return self.finish(rec, {
                    "end": "gone", "done": [],
                    "text": f"its author pushed {(remote or pr['head_sha'])[:9]} after the review of "
                            f"{rec['head_sha'][:9]}; it is reviewed again"})
        rec.pop("pushing", None)
        if pr["base"] != self.m.main:
            return self.finish(rec, {"end": "gone", "text": f"#{n} was retargeted to {pr['base']}", "done": []})
        # Re-check approval: a dismissed review after hand-off sends it back.
        from .approval import check_approval

        if not check_approval(self.m, self.policy, rec):
            nxt = {**rec, "state": "reviewed"}
            for k in ("pushing", "pushed_at", "ci_since", "infra"):
                nxt.pop(k, None)
            self.save(nxt)
            log.info("merger_approval_dismissed", pr=n)
            return f"#{n} reviewed: waiting for human approval"
        rec["state"] = "train"
        self.save(rec)
        return None

    def worktree(self, rec: dict, rev: str) -> str:
        return self.m.git.worktree(local(rec), rev)

    def update(self, rec: dict, wt: str, onto: str | None = None, start: str | None = None) -> list[str]:
        """Rebases (from `start`, default the pushed head) onto `onto` (default origin/main) and
        pushes: [] once pushed, else the conflicted files, nothing pushed."""
        git = self.m.git
        git.fetch()
        git.run("switch", "-q", "-C", local(rec), start or rec["head_sha"], cwd=wt)
        files = git.rebase(wt, onto or git.origin_main)
        if files:
            return files
        self.push(rec, wt)
        return []

    def push(self, rec: dict, wt: str) -> None:
        git = self.m.git
        pushing = git.rev(local(rec), cwd=wt)
        same = pushing == rec["head_sha"]
        rec["pushing"] = pushing
        self.save(rec)
        try:
            self.m.gh.add_labels(rec["pr"], [cm.TRAIN_CI])
        except Exception:  # noqa: BLE001 — only the runner choice
            pass
        self.check()
        git.push_lease(wt, local(rec), rec["branch"], rec["head_sha"])
        rec["head_sha"] = pushing
        rec.pop("pushing", None)
        rec["pushed_at"] = self.m.clock()
        self.save(rec)
        if same:
            restart_ci(self.m.gh, pushing, self.policy.ci_workflow)
        log.info("merger_pushed", pr=rec["pr"], head=pushing[:9])

    def look(self, rec: dict) -> dict | None:
        """The PR and its CI now: the outcome when the wait ends here, else None once CI
        passed on the pushed head."""
        m = self.m
        n = rec["pr"]
        pr = m.gh.pr(n)
        if pr["state"] != "open":
            return {"state": "gone", "reason": f"#{n} is {pr['state']}"}
        if pr["base"] != m.main:
            return {"state": "gone", "reason": f"#{n} was retargeted to {pr['base']}"}
        if rec.get("pushing") and pr["head_sha"] == rec["pushing"]:
            rec["head_sha"] = rec["pushing"]
            rec["pushed_at"] = m.clock()
        rec.pop("pushing", None)
        if pr["head_sha"] != rec["head_sha"]:
            remote = m.git.ls_remote(rec["branch"])
            if remote == rec["head_sha"]:
                return {"state": "waiting"}
            return {"state": "gone", "reason": f"#{n} moved to {(remote or pr['head_sha'])[:9]} while it waited for CI"}
        state, failed = ci_state(m.gh.check_runs(rec["head_sha"]), self.policy.ci_checks)
        if state == "failed":
            return {"state": "ci-failed", "reason": f"CI failed on {rec['head_sha'][:9]}: #{n} {'; '.join(failed)}"}
        if state == "passed":
            return None
        if not rec.get("infra") and state == "none" and m.clock() - rec.get("pushed_at", m.clock()) > NO_CI_MS:
            rec["infra"] = True
            log.warning("merger_ci_stalled", pr=n, head=rec["head_sha"][:9])
        return {"state": "waiting"}

    def settle(self, rec: dict, wt: str) -> dict:
        now = self.look(rec)
        if now:
            return now
        git = self.m.git
        git.fetch()
        if git.is_ancestor(git.origin_main, rec["head_sha"]):
            mm = self.merge(rec)
            if mm.get("gone"):
                return {"state": "gone", "reason": mm["gone"]}
            if not mm.get("moved"):
                return ({"state": "merged", "main_head": mm["main_head"]} if mm["merged"]
                        else {"state": "refused", "reason": mm["refused"]})
            log.info("merger_main_moved", pr=rec["pr"])
        files = self.update(rec, wt)
        if files:
            return {"state": "conflict", "reason": "main moved into a conflict while CI ran", "files": files}
        return {"state": "waiting"}

    def merge(self, rec: dict) -> dict:
        """Merges on exactly the pushed head, once GitHub computed it mergeable and it still
        holds origin/main and targets main. A refusal GitHub contradicts (merged anyway) counts
        as a merge."""
        m, git = self.m, self.m.git
        n = rec["pr"]
        refused = ""
        for _ in range(3):
            mergeable = "UNKNOWN"
            for _poll in range(6):
                mergeable = m.gh.pr(n)["mergeable"]
                if mergeable in ("MERGEABLE", "CONFLICTING"):
                    break
                m.pause(5)
            if mergeable == "CONFLICTING":
                refused = f"it conflicts with main (mergeable: {mergeable})"
                break
            git.fetch()
            if not git.is_ancestor(git.origin_main, rec["head_sha"]):
                return {"merged": False, "main_head": "", "refused": "", "moved": True}
            base = m.gh.pr(n)["base"]
            if base != m.main:
                return {"merged": False, "main_head": "", "refused": "", "gone": f"#{n} was retargeted to {base}"}
            self.check()
            # Final approval check: a dismissed review right before merge must not go through.
            from .approval import is_approved

            if self.policy.require_approval and not is_approved(self.m, n):
                return {"merged": False, "main_head": "", "refused": "human approval was dismissed"}
            ok, message = m.gh.merge(n, rec["head_sha"], self.policy.merge_method)
            if ok:
                refused = ""
                break
            refused = message
            if not RETRYABLE.search(message):
                break
            m.pause(10)
        state = m.gh.pr(n)["state"]
        if refused and state != "merged":
            return {"merged": False, "main_head": "", "refused": refused}
        git.fetch()
        merged = state == "merged"
        return {"merged": merged, "main_head": git.main_sha()[:9] if merged else "",
                "refused": "" if merged else f"PR state after merge: #{n} {state}"}

    def alone(self, rec: dict) -> str:
        git = self.m.git
        swapped = "ci_since" in rec
        git.fetch()
        wt = self.worktree(rec, rec["head_sha"])
        if not git.is_ancestor(git.origin_main, rec["head_sha"]):
            files = self.update(rec, wt)
            if files:
                return self.to_resolve(rec, wt, files, swapped)
        elif not swapped:
            restart_ci(self.m.gh, rec["head_sha"], self.policy.ci_workflow)
        if not swapped:
            try:
                cm.labels(self.m, rec["pr"], [cm.WAIT_CI], [cm.REVIEWED])
            except Exception:  # noqa: BLE001
                pass
            rec["ci_since"] = self.m.clock()
            self.save(rec)
        while True:
            r = self.settle(rec, wt)
            self.save(rec)
            if r["state"] == "waiting":
                self.m.pause(CI_WAIT_S)
                continue
            if r["state"] == "merged":
                return self.finish(rec, {"end": "merged", "text": f"CI passed; merged, main is at {r['main_head']}.", "done": []})
            if r["state"] == "gone":
                return self.finish(rec, {"end": "gone", "text": r["reason"], "done": []})
            if r["state"] == "conflict":
                return self.to_resolve(rec, wt, r["files"], True)
            return self.finish(rec, {"end": "draft", "text": f"not merged: {r['reason']}.", "done": []})

    def to_resolve(self, rec: dict, wt: str, files: list[str], swapped: bool) -> str:
        m = self.m
        if swapped:
            cancel_ci(m.gh, rec["head_sha"], self.policy.ci_workflow)
        m.git.remove_worktree(local(rec))
        if swapped:
            try:
                cm.labels(m, rec["pr"], [cm.REVIEWED], [cm.WAIT_CI])
            except Exception:  # noqa: BLE001
                pass
        nxt = {**rec, "state": "resolve",
               "conflict": {"files": files, "main": m.git.main_sha(), "at": m.clock(), "tries": 0}}
        for k in ("pushing", "pushed_at", "ci_since", "infra"):
            nxt.pop(k, None)
        self.save(nxt)
        m.wake(f"conflict:{rec['pr']}")
        text = f"conflicts with main in {', '.join(files)}; a review worker resolves it"
        log.info("merger_to_resolve", pr=rec["pr"], files=files)
        return f"#{rec['pr']} resolve: {text}"

    def finish(self, rec: dict, outcome: dict) -> str:
        """Decides the outcome, saved before any of it is applied."""
        if outcome["end"] != "merged" and "ci_since" in rec:
            cancel_ci(self.m.gh, rec["head_sha"], self.policy.ci_workflow)
        rec.pop("pushing", None)
        done = {**rec, "state": "finalizing", "outcome": outcome}
        self.save(done)
        return self.finalize(done)

    def finalize(self, rec: dict) -> str:
        m = self.m
        o = rec["outcome"]
        if o["end"] == "draft" and not ("draft" in o["done"] and "comment" in o["done"]):
            pr = m.gh.pr(rec["pr"])
            if pr["state"] == "merged" and pr["head_sha"] == rec["head_sha"]:
                o.update(end="merged", text=f"CI passed; merged (GitHub shows it merged, though the train recorded: {o['text']})")
            elif pr["state"] != "open" or pr["head_sha"] != rec["head_sha"]:
                o.update(end="gone", text=(f"#{rec['pr']} is {pr['state']}, before its outcome was posted ({o['text']})"
                                           if pr["state"] != "open" else
                                           f"its author pushed {pr['head_sha'][:9]} before the outcome on "
                                           f"{rec['head_sha'][:9]} was posted ({o['text']})"))
            self.save(rec)
        cm.train_conclude(m, rec, self.save)
        m.git.remove_worktree(local(rec))
        if o["end"] == "merged":
            m.count("merged", rec["pr"], rec["head_sha"])
            m.git.delete_remote_branch(rec["branch"], rec["head_sha"])
            m.store.drop_baseline(rec["pr"])
        m.store.drop_rec(rec["pr"])
        log.info("merger_train_outcome", pr=rec["pr"], end=o["end"], text=o["text"][:300])
        return f"#{rec['pr']} {o['end']}: {o['text']}"

    # -- batches (T4, T5) ------------------------------------------------------------------
    def cap(self, feed: dict, n: int) -> int:
        return feed["caps"].get(n, self.policy.batch_max)

    def next_member(self, feed: dict, members: list[int]) -> dict | None:
        m = self.m
        while (len(members) < min([self.policy.batch_max, *(self.cap(feed, x) for x in members)])
               and feed["at"] < len(feed["queue"])):
            r = feed["queue"][feed["at"]]
            feed["at"] += 1
            if len(members) >= self.cap(feed, r["pr"]) or not m.store.claim(r["pr"], self.owner):
                continue
            feed["claims"].append(r["pr"])
            now = m.store.rec(r["pr"])
            if now and now["state"] == "reviewed":
                return now
            m.store.unclaim(r["pr"], self.owner)
        return None

    def pick(self, feed: dict) -> list[dict]:
        queue = feed["queue"]
        if len(queue) < 2 or queue[0]["state"] != "reviewed" or self.policy.batch_max < 2:
            return []
        feed["caps"] = self.m.store.caps()
        picked: list[dict] = []
        while (rec := self.next_member(feed, [r["pr"] for r in picked])) is not None:
            picked.append(rec)
        return picked if len(picked) > 1 else []

    def taken(self, rec: dict, outs: list[str]) -> bool:
        dropped = self.take(rec)
        if not dropped:
            return True
        outs.append(dropped)
        self.m.store.unclaim(rec["pr"], self.owner)
        return False

    def more(self, feed: dict, members: list[int], outs: list[str]) -> dict | None:
        while (rec := self.next_member(feed, members)) is not None:
            if self.taken(rec, outs):
                return rec
        return None

    def start(self, picked: list[dict], feed: dict) -> str:
        outs: list[str] = []
        recs = [r for r in picked if self.taken(r, outs)]
        while (rec := self.more(feed, [r["pr"] for r in recs], outs)) is not None:
            recs.append(rec)
        b = {"members": [{"pr": r["pr"], "reviewed": r["head_sha"]} for r in recs]}
        if len(recs) > 1:
            self.m.store.save_batch(b)
        return self.batch(b, recs, outs, feed)

    @staticmethod
    def member(b: dict, n: int) -> dict:
        return next(x for x in b["members"] if x["pr"] == n)

    def merged_outcome(self, b: dict, n: int, main_head: str) -> dict:
        others = names(x["pr"] for x in b["members"] if x["pr"] != n)
        return {"end": "merged", "done": [],
                "text": f"CI passed in a batch with {others}; merged{f', main is at {main_head}' if main_head else ''}."}

    def resume(self, b: dict) -> str:
        """A batch a stopped step left: merged members finalized, the others pre-checked again;
        then on where it stopped."""
        recs_all = self.m.store.recs()
        outs: list[str] = []
        recs: list[dict] = []
        for mem in b["members"]:
            rec = recs_all.get(mem["pr"])

            def found(mem=mem, rec=rec) -> dict:
                if not mem.get("merged"):
                    self.landed(b, mem["pr"], rec["head_sha"])
                return self.merged_outcome(b, mem["pr"], "")

            outcome = None
            if rec and rec["state"] == "finalizing":
                if rec["outcome"]["end"] == "merged" and not mem.get("merged"):
                    found()
                outcome = self.finalize(rec)
            elif mem.get("merged") and rec:
                outcome = self.finish(rec, self.merged_outcome(b, mem["pr"], ""))
            elif not mem.get("merged") and rec and rec["state"] == "train":
                outcome = self.take(rec, found)
                if not outcome:
                    recs.append(rec)
                    continue
            if outcome:
                outs.append(outcome)
            if not mem.get("merged"):
                b.setdefault("end", f"#{mem['pr']} left the batch")
        return self.batch(b, recs, outs)

    def batch(self, b: dict, recs: list[dict], outs: list[str], feed: dict | None = None) -> str:
        m, git = self.m, self.m.git

        def done() -> str:
            m.store.save_batch(None)
            return "batch: " + "; ".join(outs)

        def end(rest: list[dict], why: str) -> str:
            b["end"] = why
            m.store.save_batch(b)
            for rec in rest:
                outs.append(self.take(rec) or self.back(rec, self.member(b, rec["pr"])["reviewed"], why))
            return done()

        if b.get("end"):
            return end(recs, b["end"])
        asked = False
        while True:
            if not any(x.get("merged") for x in b["members"]):
                if len(recs) > 1 and b.get("top") != recs[-1]["head_sha"]:
                    recs = self.chain(b, recs, outs, feed)
                feed = None
                if len(recs) < 2:
                    m.store.save_batch(None)
                    if recs:
                        outs.append(self.alone(recs[0]))
                    return "batch: " + "; ".join(outs)
                top = recs[-1]
                everyone = names(r["pr"] for r in recs)
                r = self.look(top)
                self.save(top)
                if r and r["state"] == "waiting":
                    m.pause(CI_WAIT_S)
                    continue
                if r and r["state"] == "gone":
                    outs.append(self.finish(top, {"end": "gone", "text": r["reason"], "done": []}))
                    return end(recs[:-1], f"#{top['pr']} left the batch")
                if r and r["state"] == "ci-failed":
                    if "rerun" not in b:
                        run = latest_run(m.gh, top["head_sha"], self.policy.ci_workflow)
                        if run is not None:
                            # Saved before it is asked for: a stopped step follows that attempt.
                            b["rerun"] = {"id": run["id"], "attempt": run["attempt"] + 1}
                            m.store.save_batch(b)
                            m.gh.rerun_failed(run["id"])
                            asked = True
                            log.info("merger_batch_rerun", batch=everyone, reason=r["reason"][:300])
                            m.pause(CI_WAIT_S)
                            continue
                    else:
                        run = m.gh.run(b["rerun"]["id"])
                        if not asked and run["attempt"] < b["rerun"]["attempt"] and run["status"] == "completed":
                            m.gh.rerun_failed(b["rerun"]["id"])
                            asked = True
                            m.pause(CI_WAIT_S)
                            continue
                        if (run["attempt"] < b["rerun"]["attempt"] or run["status"] != "completed"
                                or run["conclusion"] == "success"):
                            m.pause(CI_WAIT_S)
                            continue
                    half = math.ceil(len(recs) / 2)
                    m.store.set_caps({x["pr"]: (half if i < half else len(recs) - half) for i, x in enumerate(recs)})
                    authors = sorted({x["author"] for x in recs})
                    m.notify("batch_split", f"CI failed twice on the batch {everyone}. The PRs go back to the merge "
                             "train in smaller batches until the failing PR is found.", link=m.pr_url(recs[0]["pr"]),
                             logins=authors, tickets=[x.get("issue") for x in recs])
                    return end(recs, f"CI failed in the batch {everyone}: {r['reason']}")
                git.fetch()
                if not git.is_ancestor(git.origin_main, recs[0]["head_sha"]):
                    log.info("merger_batch_main_moved", batch=everyone)
                    b.pop("top", None)
                    continue
            restart = False
            for i, rec in enumerate(recs):
                below = any(x.get("merged") for x in b["members"])
                git.fetch()
                if below and git.main_sha() != b.get("main"):
                    return end(recs[i:], "main moved past the merges of its batch")
                if below and not git.is_ancestor(git.origin_main, rec["head_sha"]):
                    # The members below merged as new commits: rebased onto them, only its own
                    # commits replayed, pushed only when the tree is the one CI tested.
                    tested = rec["head_sha"]
                    wt = self.worktree(rec, tested)
                    git.run("switch", "-q", "-C", local(rec), tested, cwd=wt)
                    upstream = self.member(b, rec["pr"]).get("chain_base")
                    files = git.rebase(wt, git.origin_main, upstream=upstream)
                    if files or git.tree(local(rec), cwd=wt) != git.tree(tested):
                        return end(recs[i:], f"#{rec['pr']} on the main its batch merged into is not what CI tested")
                    self.push(rec, wt)
                # Every member through merge_member: the chain cancelled the runs under the top
                # (and this one may be just pushed), and a protected main wants the required
                # checks on each merged head; it waits for them only when GitHub asks.
                mm = self.merge_member(rec)
                if mm["merged"] and below:
                    cancel_ci(m.gh, rec["head_sha"], self.policy.ci_workflow, wait=m.pause)
                self.save(rec)
                if mm.get("moved") and not below:
                    b.pop("top", None)
                    restart = True
                    break
                if mm.get("gone"):
                    outs.append(self.finish(rec, {"end": "gone", "text": mm["gone"], "done": []}))
                    return end(recs[i + 1:], mm["gone"])
                if not mm["merged"]:
                    return end(recs[i:], f"#{rec['pr']} was not merged: {mm['refused'] or 'main moved'}")
                self.landed(b, rec["pr"], rec["head_sha"])
                outs.append(self.finish(rec, self.merged_outcome(b, rec["pr"], mm["main_head"])))
            if restart:
                continue
            return done()

    def merge_member(self, rec: dict) -> dict:
        """A batch member, on the tree CI tested on the top. Without branch protection GitHub
        merges it at once; with required checks it refuses until they pass on this exact head,
        so that head's CI is restarted when the chain cancelled it, waited for (the same tree:
        it passes) and the merge tried once more."""
        mm = self.merge(rec)
        if mm["merged"] or not REQUIRED.search(mm.get("refused", "")):
            return mm
        log.info("merger_member_waits_for_required_checks", pr=rec["pr"], head=rec["head_sha"][:9])
        restart_ci(self.m.gh, rec["head_sha"], self.policy.ci_workflow)
        while True:
            r = self.look(rec)
            self.save(rec)
            if r is None:
                return self.merge(rec)
            if r["state"] == "waiting":
                self.m.pause(CI_WAIT_S)
                continue
            out = {"merged": False, "main_head": "", "refused": r["reason"]}
            return {**out, "gone": r["reason"]} if r["state"] == "gone" else out

    def chain(self, b: dict, recs: list[dict], outs: list[str], feed: dict | None) -> list[dict]:
        """Each member rebased from the head it was reviewed on onto the member below (the first
        onto origin/main) and pushed with lease; the CI on all but the top cancelled."""
        m, git = self.m, self.m.git
        b.pop("top", None)
        b.pop("rerun", None)
        m.store.save_batch(b)
        chained: list[dict] = []
        todo = list(recs)
        i = 0
        while i < len(todo):
            rec = todo[i]
            i += 1
            mem = self.member(b, rec["pr"])
            reviewed = mem["reviewed"]
            below = chained[-1] if chained else None
            wt = self.worktree(rec, reviewed)
            swapped = "ci_since" in rec
            mem["chain_base"] = below["head_sha"] if below else None
            m.store.save_batch(b)
            files = self.update(rec, wt, onto=below["head_sha"] if below else None, start=reviewed)
            if not files:
                if not swapped:
                    try:
                        cm.labels(m, rec["pr"], [cm.WAIT_CI], [cm.REVIEWED])
                    except Exception:  # noqa: BLE001
                        pass
                    rec["ci_since"] = m.clock()
                self.save(rec)
                chained.append(rec)
                continue
            on_main = git.rebase(wt, git.origin_main) if below else files
            self.unchain(rec, reviewed)
            why = f"conflicts with #{below['pr'] if below else '?'} in its batch"
            outs.append(self.to_resolve(rec, wt, on_main, swapped) if on_main else self.back(rec, reviewed, why))
            b["members"] = [x for x in b["members"] if x["pr"] != rec["pr"]]
            m.store.save_batch(b)
            m.store.unclaim(rec["pr"], self.owner)
            while feed and (newcomer := self.more(feed, [x["pr"] for x in b["members"]], outs)) is not None:
                b["members"].append({"pr": newcomer["pr"], "reviewed": newcomer["head_sha"]})
                m.store.save_batch(b)
                todo.append(newcomer)
        if chained:
            b["top"] = chained[-1]["head_sha"]
        for rec in chained[:-1]:
            if rec["head_sha"] != b.get("top"):
                cancel_ci(m.gh, rec["head_sha"], self.policy.ci_workflow, wait=m.pause)
        m.store.save_batch(b)
        return chained

    def landed(self, b: dict, n: int, head: str) -> None:
        """Member n merged: the main its merge left, which holds exactly the head's tree (its
        head held main and the members below; any merge method lands that tree), or "" when
        main holds something else (someone else merged right after): no further member merges."""
        git = self.m.git
        git.fetch()
        main = git.main_sha()
        b["main"] = main if git.tree(main) == git.tree(head) else ""
        self.member(b, n)["merged"] = True
        self.m.store.save_batch(b)

    def unchain(self, rec: dict, reviewed: str) -> None:
        """A member leaving unmerged goes back to the head it was reviewed on (its chain head
        carries the members below it), pushed with lease, intent first."""
        if rec["head_sha"] == reviewed:
            return
        rec["pushing"] = reviewed
        self.save(rec)
        self.check()
        self.m.git.push_sha(reviewed, rec["branch"], rec["head_sha"])
        cancel_ci(self.m.gh, rec["head_sha"], self.policy.ci_workflow)
        cancel_ci(self.m.gh, reviewed, self.policy.ci_workflow, wait=self.m.pause)
        rec["head_sha"] = reviewed
        rec.pop("pushing", None)
        self.save(rec)

    def back(self, rec: dict, reviewed: str, why: str) -> str:
        self.unchain(rec, reviewed)
        self.m.git.remove_worktree(local(rec))
        if "ci_since" in rec:
            try:
                cm.labels(self.m, rec["pr"], [cm.REVIEWED], [cm.WAIT_CI])
            except Exception:  # noqa: BLE001
                pass
        nxt = {**rec, "state": "reviewed"}
        for k in ("pushing", "pushed_at", "ci_since", "infra"):
            nxt.pop(k, None)
        self.save(nxt)
        log.info("merger_back_to_reviewed", pr=rec["pr"], why=why)
        return f"#{rec['pr']} reviewed: {why}"


class TrainBusy(Exception):
    pass

