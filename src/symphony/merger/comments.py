"""PR comments and labels: one report per review, edited in place when CI's result comes later,
and the train's outcome comment. Each multi-step publication records the steps that took
(`done`), so a retry after a crash or a failed call repeats none."""

from __future__ import annotations

import re

from .context import Merger

VERSION = "symphony-merger"
IN_AUDIT = "in-audit"
REVIEWED = "reviewed"
WAIT_CI = "wait-ci"
TRAIN_CI = "train-ci"
BYPASS = "review-bypass"
NEEDS_MAINTAINER = "needs-maintainer"
REVIEW_REQUESTED = "review-requested"
WAIT_APPROVAL = "wait-approval"

REVIEWED_TITLE = "**Reviewed**"
NOT_MERGED_TITLE = "**Not merged**"
WAITING_TITLE = "**Waiting for CI**"
PREVIEW_TITLE = "**Preview**"
OURS = re.compile(
    r"^(\*\*(Merged|Not merged|Waiting for CI|Reviewed|Review bypassed|Rebased onto main"
    r"|Preview)\*\*\n\n)?"
    rf"\*\*{VERSION}\*\*"
)
DRAFTED = (f"This PR is back to draft with the `{REVIEW_REQUESTED}` label: address this comment, "
           "then mark it ready for review.")


def preview_comment(builds: list[dict]) -> str:
    """The PR's one Preview comment: a link per build (`preview_links`), newest first. It is
    edited in place when a new head gets a build."""
    lines = [f"- `{b['sha12']}`{' (newest)' if i == 0 else ''}: [Open in Seiji]({b['url']})"
             for i, b in enumerate(builds)]
    return "\n\n".join([
        PREVIEW_TITLE,
        f"**{VERSION}**: builds of this PR to try on a simulator in Seiji. A link opens that "
        "build, now or later, while GitHub keeps its artifact; open it again for a new "
        "viewer.",
        "\n".join(lines),
    ])


def scrub(text: str, *paths: str) -> str:
    """Repository-relative paths; any other machine-local path is dropped."""
    for p in paths:
        if p:
            text = text.replace(p.rstrip("/") + "/", "")
    return re.sub(r"/(?:home|tmp|Users|var)/[^\s`'\")\]]*", "<local path>", text)


def compose(title: str, head_sha: str, outcome: str, ci: str, body: str, drafted: bool) -> str:
    parts = [title, f"**{VERSION}** on `{head_sha[:9]}`: {outcome}", ci, body, DRAFTED if drafted else ""]
    return "\n\n".join(p for p in parts if p)


def ci_line(state: str, head_sha: str, failed: list[str] | None = None) -> str:
    h = f"`{head_sha[:9]}`"
    if state == "pending":
        return f"CI is still running on {h}: its result is added to this comment when it ends."
    if state == "passed":
        return f"CI passed on {h}."
    return f"CI failed on {h}: {'; '.join(failed or [])}"


def report_body(run, result: dict | None) -> str:
    mine = [n[len(f"#{run.pr} "):] for n in run.notes if n.startswith(f"#{run.pr} ")]
    shared = [n for n in run.notes if not n.startswith("#") and not n.startswith("not merged")]
    body = [n[6:] for n in mine if n.startswith("Body: ")]
    other = [n for n in mine if not n.startswith("Body: ")] + shared

    def finding(r: dict) -> str:
        cls = f" ({r['class']})" if r.get("class") and r["class"] != "nit" else ""
        parts = [
            f"### {r['id']}: {r['severity']}{cls}, {r['verdict']}",
            f"**Location:** {r['location']}",
            f"**Claim:** {r['claim'].strip()}",
            f"**Suggested fix:**\n{r['fix'].strip()}" if (r.get("fix") or "").strip() else "",
        ]
        return "\n\n".join(p for p in parts if p)

    def details(title: str, lines: list[str]) -> str:
        return f"<details><summary>{title}</summary>\n\n" + "\n".join(lines) + "\n\n</details>" if lines else ""

    evidence = [f"- [{e.get('title') or e['url']}]({e['url']})" + (f" ({e['kind']})" if e.get("kind") else "")
                for e in getattr(run, "evidence", []) if e.get("url")]
    parts = [
        "\n\n".join(finding(r) for r in run.findings) if run.findings else ("No reviewer findings." if result else ""),
        ("**Evidence from the ticket:**\n\n" + "\n".join(evidence)) if evidence else "",
        ("**Suggested PR body changes** (not blocking):\n\n" + "\n".join(f"- {b}" for b in body)) if body else "",
        details("Rejected findings", [f"- {x}" for x in (result or {}).get("rejected", [])]),
        details("Notes", [f"- {n.replace(chr(10), chr(10) + '  ')}" for n in other]),
    ]
    return "\n\n".join(p for p in parts if p)


def scrubbed_body(m: Merger, run, result: dict | None) -> str:
    return scrub(report_body(run, result), run.wt, m.git.path)


def labels(m: Merger, n: int, add: list[str] = (), remove: list[str] = ()) -> None:
    """Label changes; a label already in place (or gone) is not an error."""
    if add:
        m.gh.add_labels(n, list(add))
    for label in remove:
        m.gh.remove_label(n, label)


def to_draft(m: Merger, n: int) -> bool:
    try:
        m.gh.set_draft(n, True)
        m.gh.add_labels(n, [REVIEW_REQUESTED])
        return True
    except Exception:  # noqa: BLE001 — the comment then says nothing about draft
        return False


def announce(m: Merger, n: int) -> None:
    try:
        labels(m, n, [IN_AUDIT], [REVIEW_REQUESTED, WAIT_CI, REVIEWED])
    except Exception:  # noqa: BLE001 — a label never stops a review
        pass


def drop_in_audit(m: Merger, n: int) -> None:
    try:
        m.gh.remove_label(n, IN_AUDIT)
    except Exception:  # noqa: BLE001
        pass

# Slack is read by the team: one line with the PR and what happened; the report has the rest.
SAY = {
    "back_to_author": "went back to the author",
    "back_to_author_bot": "went back to the author (agent-authored; the agent picks it up in Rework, no human action needed)",
    "needs_maintainer": "needs a maintainer (the single rejection is used up and the arbiter confirmed the block)",
}


def is_bot_author(rec: dict) -> bool:
    """A GitHub App or bot authored the PR. GraphQL gives an App's login without `[bot]`, so
    the type decides; the suffix covers records written before the type was kept."""
    return rec.get("author_type") == "Bot" or (rec.get("author") or "").endswith("[bot]")


def say(m: Merger, event: str, n: int, title: str, login: str | None, link: str, why: str = "",
        *, is_bot: bool = False, ticket: str | None = None) -> None:
    reason = f": {why}" if why else ""
    text_key = f"{event}_bot" if is_bot and f"{event}_bot" in SAY else event
    m.notify(event, f"PR #{n} {title[:90]} {SAY[text_key]}{reason}",
             link=link, logins=[login] if login else [], tickets=[ticket])


def conclude(m: Merger, run, result, outcome: str, draft: bool, event: str | None, ci: str = "",
             *, is_bot: bool = False) -> tuple[str, bool]:
    """Back to the author: draft (when asked), in-audit off, the report posted, the channel
    told. Never throws: returns the comment URL ("" when posting failed) and whether it went
    back to draft."""
    drafted = draft and to_draft(m, run.pr)
    drop_in_audit(m, run.pr)
    comment = ""
    try:
        body = compose(NOT_MERGED_TITLE, run.head_sha, outcome, ci, scrubbed_body(m, run, result), drafted)
        comment = m.gh.comment(run.pr, body)
    except Exception:  # noqa: BLE001
        pass
    if event:
        m.count(event, run.pr, run.head_sha)
        say(m, event, run.pr, run.title, run.author, comment or m.pr_url(run.pr), is_bot=is_bot,
            ticket=run.issue)
    return comment, drafted


def run_failed(m: Merger, run, why: str) -> None:
    """A failed run is the machine's, not the PR's: no comment and no draft."""
    drop_in_audit(m, run.pr)
    m.notify("run_error", f"The review of PR #{run.pr} {run.title[:90]} failed (no PR comment posted): {why}",
             link=m.pr_url(run.pr), tickets=[run.issue])


def post_waiting(m: Merger, run, result, outcome: str) -> str:
    drop_in_audit(m, run.pr)
    try:
        return m.gh.comment(run.pr, compose(WAITING_TITLE, run.head_sha, outcome, ci_line("pending", run.head_sha),
                                            scrubbed_body(m, run, result), False))
    except Exception:  # noqa: BLE001
        return ""


def publish_review(m: Merger, rec: dict, save) -> None:
    """A hand-off's publication: the stored report (posted, or edited over the waiting one),
    labels swapped, the ticket moved to Merging. Throws on a failed GitHub call; the record
    keeps the steps that took."""
    pub = rec["publication"]

    def step(name: str, action) -> None:
        if name in pub["done"]:
            return
        action()
        pub["done"].append(name)
        save(rec)

    def comment() -> None:
        if pub.get("edit"):
            m.gh.edit_comment(pub["edit"], pub["report"])
            pub["url"] = pub["edit"]
        else:
            pub["url"] = m.gh.comment(rec["pr"], pub["report"])

    step("comment", comment)
    step("labels", lambda: labels(m, rec["pr"], [REVIEWED], [IN_AUDIT, NEEDS_MAINTAINER]))
    step("ticket", lambda: m.move_ticket(rec.get("issue"), "merging"))


def resolved_comment(m: Merger, rec: dict, old: str, files: list[str], note: str) -> None:
    body = [
        "**Rebased onto main**",
        f"**{VERSION}** rebased `{old[:9]}` onto main as `{rec['head_sha'][:9]}`: it conflicted with main in "
        + ", ".join(f"`{f}`" for f in files)
        + ", which an agent resolved keeping both sides, and a reviewer checked. It is back in the merge "
        "train, in its place.",
        f"Resolution: {note}" if note else "",
    ]
    m.gh.comment(rec["pr"], scrub("\n\n".join(b for b in body if b), m.git.wt_root))


def train_conclude(m: Merger, rec: dict, save) -> None:
    """The train's outcome on a PR: draft, labels, comment, the channel and the ticket, each
    once (`outcome.done`). Throws on a failed GitHub call; the outcome stays for the retry."""
    o = rec["outcome"]
    n = rec["pr"]

    def step(name: str, action) -> None:
        if name in o["done"]:
            return
        action()
        o["done"].append(name)
        save(rec)

    def draft() -> None:
        if o["end"] == "draft" and not to_draft(m, n):
            raise RuntimeError(f"#{n}: putting it back to draft failed")

    def comment() -> None:
        if o["end"] == "gone":
            return
        body = [
            "**Merged**" if o["end"] == "merged" else NOT_MERGED_TITLE,
            f"**{VERSION}** merge train on `{rec['head_sha'][:9]}`: {o['text']}",
            DRAFTED if o["end"] == "draft" else "",
        ]
        o["comment"] = m.gh.comment(n, scrub("\n\n".join(b for b in body if b), m.git.wt_root))

    def tell() -> None:
        if o["end"] == "draft":
            m.count("back_to_author", n, rec["head_sha"])
            say(m, "back_to_author", n, rec.get("title", ""), rec.get("author"),
                o.get("comment") or m.pr_url(n), "the merge train could not merge it",
                is_bot=is_bot_author(rec), ticket=rec.get("issue"))
            m.move_ticket(rec.get("issue"), "rework",
                          f"The merge train sent the PR back: {o['text']}\n\n{o.get('comment') or m.pr_url(n)}")

    step("draft", draft)
    step("labels", lambda: labels(m, n, [], [REVIEWED, WAIT_CI, TRAIN_CI]))
    step("comment", comment)
    step("notify", tell)
