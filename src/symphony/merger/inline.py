"""Inline review comments: after a review round posts its report, one GitHub review (`COMMENT`,
on the reviewed head) puts each blocking or confirmed finding on its diff lines.

- A finding at `path:line` or `path:start-end` gets a comment when GitHub can place it: its
  end line inside a hunk of the PR's diff, on the new side. A range that starts above that
  hunk is clamped to it. Anything else (a path alone, the PR body, a line outside the diff)
  stays in the report only.
- A confirmed finding whose `replacement` is the new text of its exact lines, at most
  `SUGGESTION_MAX` lines, carries a `suggestion` block.
- `inline` in the store keeps, per PR, each commented finding by key (path and claim) with its
  comment id: a re-review does not comment it again (also when the finding `reopens` it), and
  a finding the new round no longer reports has its thread resolved.
- The report is the record: a failed call is logged and the round goes on. The review is
  never an approval or a change request, so the approval gate never counts it.
"""

from __future__ import annotations

import hashlib
import re

from . import comments as cm
from .context import Merger, log
from .policy import Policy
from .rules import parse_diff

SUGGESTION_MAX = 30
MARK = "<!-- symphony-merger:finding:{} -->"
MARKED = re.compile(r"<!-- symphony-merger:finding:([0-9a-f]+) -->")
LOCATION = re.compile(r"^`?(?:\./)?([^\s:`]+):(\d+)(?:\s*[-–]\s*(\d+))?")


def ours(body: str) -> bool:
    """An inline comment this module wrote."""
    return bool(MARKED.search(body or ""))


def commentable(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Path → the new-side line ranges of its hunks: the lines a review comment may point at."""
    out: dict[str, list[tuple[int, int]]] = {}
    for h in parse_diff(diff).hunks:
        if h.count:
            out.setdefault(h.path, []).append((h.start, h.end))
    return out


def _location(location: str, wt: str) -> str:
    loc = (location or "").strip()
    return loc.replace(f"{wt.rstrip('/')}/", "") if wt else loc


def key_of(f: dict, wt: str) -> str:
    loc = _location(f["location"], wt)
    found = LOCATION.match(loc)
    path = found.group(1) if found else loc.split(":", 1)[0]
    claim = re.sub(r"\s+", " ", f["claim"].strip().lower())
    return hashlib.sha256(f"{path}\n{claim}".encode()).hexdigest()[:16]


def place(location: str, wt: str, hunks: dict) -> tuple[dict, bool] | None:
    """Where GitHub takes the comment (`path, line, side`, plus `start_line, start_side` for a
    range) and whether that is the finding's exact range; None: nowhere on the diff."""
    found = LOCATION.match(_location(location, wt))
    if not found:
        return None
    path, start = found.group(1), int(found.group(2))
    end = int(found.group(3) or start)
    if end < start:
        return None
    for lo, hi in hunks.get(path, ()):
        if lo <= end <= hi:
            first = max(start, lo)
            where = {"path": path, "line": end, "side": "RIGHT"}
            if first < end:
                where.update(start_line=first, start_side="RIGHT")
            return where, first == start
    return None


def worth(f: dict) -> bool:
    """Blocking, or a nit the oracle confirmed."""
    return f.get("severity") == "blocking" or f.get("verdict") in ("confirmed", "partial")


def suggestion(f: dict, exact: bool) -> str:
    text = f.get("replacement") or ""
    if not text.strip() or not exact or f.get("verdict") != "confirmed":
        return ""
    lines = text.rstrip("\n").split("\n")
    if len(lines) > SUGGESTION_MAX:
        return ""
    fence = "````" if "```" in text else "```"
    return f"{fence}suggestion\n" + "\n".join(lines) + f"\n{fence}"


def comment_body(f: dict, key: str, exact: bool, report: str, *paths: str) -> str:
    fix = (f.get("fix") or "").strip()
    parts = [
        f"**{f['id']}** ({'blocking' if f.get('severity') == 'blocking' else 'nit'}) — {f['claim'].strip()}",
        (f"Fix:\n{fix}" if "\n" in fix or fix.startswith("- ") else f"Fix: {fix}") if fix else "",
        suggestion(f, exact),
        f"[Review report]({report})",
    ]
    return cm.scrub("\n\n".join(p for p in parts if p), *paths) + "\n" + MARK.format(key)


def publish(m: Merger, policy: Policy, run, delta: dict | None, report: str) -> None:
    """The round's inline review, after its report (`report`: the comment URL). Never throws."""
    if not policy.inline_comments or not report:
        return
    try:
        _publish(m, run, delta, report)
    except Exception as exc:  # noqa: BLE001 — the report is the record
        log.warning("merger_inline_failed", pr=run.pr, error=str(exc)[:300])


def _publish(m: Merger, run, delta: dict | None, report: str) -> None:
    n = run.pr
    wt = getattr(run, "wt", "") or ""
    threads: dict[str, dict] = dict((m.store.inline(n) or {}).get("threads") or {})
    # A re-review's finding that reopens an earlier one is that one, whatever its claim says now.
    earlier = {t["id"]: k for k, t in threads.items() if delta and t.get("head") == delta["reviewed_head"]}

    def known(f: dict) -> str | None:
        k = key_of(f, wt)
        return k if k in threads else earlier.get(f.get("reopens") or "")

    reported = set()
    for f in run.findings:
        k = known(f)
        if k:
            reported.add(k)
            threads[k] = {**threads[k], "id": f["id"], "head": run.head_sha}

    gone = [k for k in threads if k not in reported]
    if gone:
        try:
            by_comment = {t["comment"]: t for t in m.gh.review_threads(n) if t.get("comment")}
            for k in gone:
                t = by_comment.get(threads[k].get("comment"))
                if t and not t["resolved"]:
                    m.gh.resolve_thread(t["id"])
                del threads[k]  # resolved now, by a person already, or its comment is gone
            log.info("merger_inline_resolved", pr=n, threads=len(gone))
        except Exception as exc:  # noqa: BLE001 — the next round tries again
            log.warning("merger_inline_resolve_failed", pr=n, error=str(exc)[:300])

    hunks = commentable(run.diff)
    comments, posted = [], []
    for f in run.findings:
        k = key_of(f, wt)
        if not worth(f) or known(f) or k in posted:
            continue
        placed = place(f["location"], wt, hunks)
        if placed is None:
            continue
        where, exact = placed
        comments.append({**where, "body": comment_body(f, k, exact, report, wt, m.git.path)})
        posted.append(k)
        threads[k] = {"comment": None, "id": f["id"], "head": run.head_sha}
    if comments:
        body = (f"**{cm.VERSION}** on `{run.head_sha[:9]}`: the findings of "
                f"[the review report]({report}) on their lines.")
        try:
            answer = m.gh.post_review(n, run.head_sha, body, comments)
        except Exception as exc:  # noqa: BLE001 — the report has every finding
            for k in posted:
                del threads[k]
            log.warning("merger_inline_post_failed", pr=n, status=getattr(exc, "status", 0),
                        error=str(exc)[:300])
        else:
            ids = {k: c["id"] for c in answer for k in MARKED.findall(c.get("body") or "")}
            for k in posted:
                threads[k]["comment"] = ids.get(k)
            log.info("merger_inline_posted", pr=n, comments=len(comments))
    if threads:
        m.store.put_inline(n, {"threads": threads})
    else:
        m.store.drop_inline(n)
