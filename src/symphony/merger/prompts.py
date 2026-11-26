"""The merger agents' prompts. Each agent's whole task is one function here; the repository's
own rules come from the policy body (`.symphony/merger.md`), everything else is generic."""

from __future__ import annotations

import json

CLASS_TEXT = {
    "bug": "behavior that is wrong for valid input: code the PR adds or changes does something other than what it must (a wrong value or branch, a crash, a lost or corrupted value, a race)",
    "regression": "a broken caller: a caller, test, API contract, encoding, migration or config the PR breaks or leaves on the old shape",
    "security": "a vulnerability the PR introduces: injection, broken authentication or authorization, a leaked secret or credential, unsafe deserialization, missing validation at a trust boundary",
    "spec": "an acceptance criterion of the ticket the PR doesn't meet",
    "duplicate-owner": "a second owner of what the code base already owns: a reimplementation of an existing module, helper or service the PR should have used",
}


def severity(classes: tuple[str, ...]) -> str:
    lines = [f"- {c}: {CLASS_TEXT.get(c, 'as the repository rules below define it')};" for c in classes]
    return (
        "SEVERITY\n"
        "Give each finding its severity and its class. A finding is blocking only in one of these classes:\n"
        + "\n".join(lines)
        + "\nEverything else is a nit (class nit), however the rules word it: names, comments and docs, "
        "commit and body wording, style, placement, visibility, code that could be simpler, a missing "
        "test for a branch that works. A finding blocks the merge only when you grade it blocking in a "
        "blocking class and an independent second reviewer agrees."
    )


def _repo_rules(rules: str) -> str:
    return f"\n\nREPOSITORY RULES\n{rules}" if rules else ""


FIX_FORM = (
    "Write each fix as a checklist the next review can tick: each change to make, each test to add "
    "(what it calls, what it asserts). Ask for no more than the finding needs."
)


def _where(wt: str, writes: bool) -> str:
    if writes:
        return (
            f"WHERE YOU WORK\nThe worktree {wt}, nothing else: give every tool an absolute path under it. "
            "Never push, never comment anywhere, never touch another directory."
        )
    return (
        f"WHERE YOU WORK\nThe worktree {wt}, read-only: give read, grep, find and ls absolute paths under it. "
        "Change nothing."
    )


def _pr(run) -> str:
    issue = f" for ticket {run.issue}" if run.issue else ""
    return (
        f"THE PR\n#{run.pr}{issue}: {run.title}\n"
        f"Head {run.head_sha}, based on {run.main_ref} at fork point {run.fork}.\n"
        f"Body:\n{(run.body or '(empty)').strip()[:6000]}"
    )


EARLIER = {
    "blocked": "sent this PR back to its author",
    "escalated": "asked a maintainer about this PR",
    "held": "left this PR to its author's fixes",
    "approved": "approved this PR (the merge train or its author has it back since)",
    "interrupted": "reviewed this PR, and its author pushed during that review,",
    "polish": "sent this PR back to its author for a polish round (nit fixes)",
}


def earlier_reviews(run, delta) -> str:
    if delta is None:
        return ""
    o, f, b, h, rb = (s[:12] for s in (delta["reviewed_head"], delta["from"], delta["base"],
                                         run.head_sha, delta["reviewed_base"]))
    how = ""
    if delta["mixed"]:
        how = (f" The reviewed commits don't replay onto {b}, the head's fork point, so that diff also "
               f"holds main's own changes between {rb} and {b}: those are out of scope. Tell them apart "
               f"with the range-diff below.")
    elif delta["rebased"]:
        how = f" {f} is the reviewed commits replayed onto {b}, so main's own changes are not in that diff."
    text = (
        f"\n\nRE-REVIEW\nAn earlier review {EARLIER[delta['outcome']]} at {o}. What the author changed "
        f"since is the DELTA below.{how}\nIn scope, and nothing else:\n"
        "- each EARLIER FINDINGS entry, checked against the head: report it again (same claim, at its "
        "location now, with reopens set to its id) unless the author fixed it;\n"
        "- the author's changes since, audited in full;\n"
        "- code outside those changes only when a change builds on it or breaks what it assumes: name "
        "the changed line that does in the claim.\nThe rest of the PR was reviewed: read it as context only."
    )
    if delta["rounds"] >= 1:
        text += (
            "\nONCE ONLY: this PR was already sent back to its author, so a finding blocks only when it is\n"
            "- an EARLIER FINDINGS entry of severity blocking that the author left unfixed: report it again "
            "with reopens set to its id;\n"
            "- or a bug or regression on the lines the author changed since (the DELTA).\n"
            "Grade everything else nit, also what SEVERITY would call blocking: it is reported, not blocking."
        )
    text += f"\nEARLIER FINDINGS (JSON): {json.dumps(delta['findings'])}"
    if delta.get("range_diff"):
        text += f"\nRANGE-DIFF (reviewed commits vs the head's):\n```\n{delta['range_diff'][:20000]}\n```"
    text += f"\nDELTA (git diff {f} {h}):\n```diff\n{delta.get('diff', '')[:60000]}\n```"
    return text


def precheck_section(pre) -> str:
    breaks = "\n\n".join([*pre.breaks, *pre.body]) or "none"
    leads = "\n".join(pre.leads) or "none"
    return (
        "\n\nPRECHECK\nThe script already ran the rule checks. Its rule breaks are settled and go to the "
        f"author as they are: don't report them again.\nRule breaks:\n{breaks}\nLeads (data, not findings: "
        f"check what they point to):\n{leads}"
    )


def discussion_section(entries: list[dict]) -> str:
    if not entries:
        return "\n\nDISCUSSION\nNo comments on the PR yet."
    lines = [
        f"## {e['at'][:16].replace('T', ' ')} {e['who']}, {e['where']}"
        f"{' (earlier symphony-merger report)' if e.get('ours') else ''}\n{e['body'].strip()[:4000]}"
        for e in entries[-40:]
    ]
    return (
        "\n\nDISCUSSION (oldest first; an objection the author answered with evidence that holds settles a "
        "finding)\n" + "\n\n".join(lines)
    )


def review_prompt(run, pre, lane: dict, delta, classes, rules: str, lanes: int) -> str:
    others = "" if lanes == 1 else " only: other reviewers take the other audits at the same time, so leave theirs out"
    depth = (
        "Report every finding in the scope RE-REVIEW below sets, not a sample."
        if delta else
        "This is the PR's one full review: later reviews block only on what it reports or on what the "
        "author changes after it. Report every finding of your audits on the whole diff, not a sample."
    )
    return (
        f"You review pull request #{run.pr} of {run.repo}. Report findings ONLY through the "
        "submit_finding tool, one call per finding, then call submit_review exactly once. Do not write prose.\n\n"
        f"{_where(run.wt, False)}\n\n{_pr(run)}\n\nDIFF (git diff {run.fork} {run.head_sha}):\n```diff\n"
        f"{run.diff[:120000]}\n```\n\nRULES\nJudge the PR by the audits: {lane['audits']}{others}.\n"
        "Leave out: a problem on lines the PR doesn't touch, unless the PR builds on it; what linters "
        f"report; a difference the body explains when its evidence holds.\n{depth} Open every line you "
        f"report and confirm it. {FIX_FORM}\n\n{severity(classes)}{_repo_rules(rules)}"
        f"{earlier_reviews(run, delta)}{precheck_section(pre)}{discussion_section(run.discussion)}"
    )


def verify_prompt(run, pre, findings: list[dict], all_findings: list[dict], delta, classes, rules: str) -> str:
    context = ""
    if len(all_findings) > len(findings):
        context = "\n\nALL FINDINGS (context: other verifiers decide the ones not in FINDINGS): " + json.dumps(
            [{"id": f["id"], "location": f["location"], "claim": f["claim"]} for f in all_findings])
    return (
        f"You are the second reviewer of pull request #{run.pr} of {run.repo}. The first reviewer reported "
        "the FINDINGS below; decide each one on your own evidence. A wrong finding sends the PR back to its "
        "author for nothing, and a real one you dismiss lands in main. Answer ONLY through submit_verdict, "
        "one call per finding id, then call submit_review exactly once. Do not write prose.\n\n"
        f"{_where(run.wt, False)}\n\n{_pr(run)}\n\nDIFF (git diff {run.fork} {run.head_sha}):\n```diff\n"
        f"{run.diff[:120000]}\n```\n\n{severity(classes)}{_repo_rules(rules)}{earlier_reviews(run, delta)}\n\n"
        "FOR EACH FINDING\n1. Open the cited lines at the head and restate the claim in one sentence. When "
        "the location is off, find the code it means.\n2. Re-derive the claim from the code itself (callers, "
        "tests, the API it uses); don't take the reviewer's evidence on trust.\n3. Check it is in scope: on "
        "lines the PR adds or changes, or code the PR newly builds on; on a RE-REVIEW, also within its scope.\n"
        f"4. Check the proposed fix. {FIX_FORM}\n5. Grade it by SEVERITY yourself: your own severity and "
        "class. The reviewer's grade doesn't bind you, and a finding blocks only when you both grade it "
        "blocking.\n\nVERDICTS\n- confirmed: real and in scope, and the fix is right. Repeat the fix, made "
        "precise where it was vague.\n- partial: real, but the claim or the fix is partly wrong. Give the "
        "corrected fix.\n- refuted: not real, already handled, or the same defect as a finding with an "
        "earlier id (say \"duplicate of\" that id). Say what shows it.\n- defer: real, but outside this PR. "
        "Put where it belongs in fix.\nseverity and class are your grade (refuted or defer: nit). evidence "
        "is what you opened, searched or ran, not a restatement of the reviewer. One verdict per finding "
        f"id, and no new findings.\n\nFINDINGS (JSON): {json.dumps(findings, indent=1)}{context}"
        f"{precheck_section(pre)}{discussion_section(run.discussion)}"
    )


def arbiter_prompt(run, blocking: list[dict], delta, classes, rules: str) -> str:
    return (
        f"You are the arbiter of pull request #{run.pr} of {run.repo}. It was already sent back to its "
        "author once. On this later review a reviewer and an independent verifier both graded the "
        "FINDINGS below blocking. Escalating sends it to a human maintainer, which is the last resort; "
        "dismissing lets it merge. Rule on each finding on your own evidence: uphold only a finding that "
        "is real, in scope, and would do real harm in main (a bug, a regression, a vulnerability, an "
        "unmet acceptance criterion); dismiss one that is wrong, out of scope, or a nit graded too high. "
        "Answer ONLY through submit_arbitration, exactly once.\n\n"
        f"{_where(run.wt, False)}\n\n{_pr(run)}\n\nDIFF (git diff {run.fork} {run.head_sha}):\n```diff\n"
        f"{run.diff[:120000]}\n```\n\n{severity(classes)}{_repo_rules(rules)}{earlier_reviews(run, delta)}"
        f"\n\nFINDINGS (JSON): {json.dumps(blocking, indent=1)}{discussion_section(run.discussion)}"
    )


def resolve_prompt(pr: int, branch: str, wt: str, base: str, main: str, files: list[str], rules: str) -> str:
    return (
        f"PR #{pr} (branch {branch}) was reviewed and waits to merge, but its rebase onto main at {main} "
        f"stopped on a conflict. The rebase is in progress in the worktree {wt}.\n\n{_where(wt, True)}\n"
        "Never git checkout/restore/stash/reset --hard/clean, never git rebase --skip (it drops a commit) "
        "or --abort: when you can't keep both sides, stop and report resolved false with why.\n\nRESOLVE\n"
        f"Resolve it inside that rebase. Use {main} for main. For each conflicted file keep what both "
        f"sides meant: main's change (git log -p {base}..{main} -- <file>) and the PR commit's change "
        "(git show REBASE_HEAD -- <file>). Touch nothing else: no fix, refactor or reformat of other lines. "
        "Then git add the file and GIT_EDITOR=true git rebase --continue; resolve each later stop the same "
        "way, until the rebase is done.\n\nCHECK\nRun the repository's build or type check for what you "
        "resolved. When a check fails outside the conflicted files because main changed something the PR "
        "uses (a semantic conflict), make the smallest change that builds, commit it on top as one commit "
        "\"build against main after the rebase\", and list it in note.\n\nREPORT\nCall submit_resolution "
        "exactly once: resolved (the rebase completed and the checks passed), files, note (per file: what "
        "each side changed and how both are kept; not resolved: why) and checks (commands and results)."
        f"{_repo_rules(rules)}\n\nCONFLICTED FILES: {', '.join(files)}"
    )


def resolve_review_prompt(pr: int, wt: str, files: list[str], base: str, main: str, old: str, new: str,
                          report: dict, rules: str) -> str:
    return (
        f"Review how a developer resolved the conflicts of PR #{pr}'s rebase onto main at {main}.\n\n"
        f"{_where(wt, False)}\n\nCOMPARE\nBefore: {old}, the reviewed head, forked from main at {base}. "
        f"After: {new}, rebased onto {main}. The report's comparison holds the commit-by-commit "
        f"range-diff ({base}..{old} vs {main}..{new}) and, per conflicted file, the change from before to "
        f"after (pr_change) next to main's own change (main_change); open the files at the head to check.\n"
        f"CONFLICTED FILES: {', '.join(files)}\n\n"
        "APPROVE only when:\n- the only differences between before and after are the conflict "
        "resolutions, main's own changes, and at most one last commit with the smallest adaptations that "
        "make the code build against main;\n- each resolution keeps both sides' intent: nothing lost, "
        "duplicated or added, no other file touched;\n- it builds (the developer's checks below).\n"
        "Otherwise list each problem in issues (path:line, why). Call submit_review exactly once."
        f"{_repo_rules(rules)}\n\nDEVELOPER REPORT\n{json.dumps(report, indent=1)}"
    )


def probe_prompt(path: str) -> str:
    return (
        f"Tool check, change nothing. Read the file {path} with the read tool, then call submit_review "
        "exactly once with summary set to the file's exact content. Do not write prose."
    )
