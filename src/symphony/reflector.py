"""Reflector: harvest → distillation → mechanical curation → learnings PR (spec US4).

Curation is pure, deterministic code (FR-029); distillation (LLM) is injectable.
Human gate by construction: this module ONLY opens pull requests — it never merges
(constitution V). Exactly-one retro per issue: `retro:<id>` token owned by
call_id (at-most-once) + nightly sweep (at-least-once).
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from . import state
from .config import Config
from .endpoints import github_api_url
from .trackers.base import GitHubLike, TrackerAdapter, TrackerUnavailable

DECAY_AFTER_INCORPORATIONS = 10
ORPHAN_RETRO_AGE_MS = 60 * 60 * 1000
SWEEP_WINDOW_DAYS = 7
AGENTS_MD_BYTE_BUDGET = 32 * 1024


@dataclass(frozen=True)
class Lesson:
    title: str
    description: str
    content: str
    globs: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    source: str = ""
    kind: str = "lesson"  # "lesson" | "skill-proposal"

    @property
    def slug(self) -> str:
        text = unicodedata.normalize("NFKD", self.title).encode("ascii", "ignore").decode()
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass
class Harvest:
    issue_identifier: str
    final_state: str
    pr_number: int | None = None
    diff: str = ""
    review_threads: list[dict] = field(default_factory=list)
    ci_results: list[dict] = field(default_factory=list)
    workpad: str = ""
    attempts: int = 1
    tokens: dict = field(default_factory=dict)


@dataclass
class CurationResult:
    additions: list[Lesson] = field(default_factory=list)
    replacements: list[tuple[str, Lesson]] = field(default_factory=list)  # (old slug, new lesson)
    removals: list[str] = field(default_factory=list)  # decayed slugs
    skipped: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.additions or self.replacements or self.removals)


# --------------------------------------------------------------------------
# mechanical curation (pure, deterministic — FR-029)
# --------------------------------------------------------------------------
def curate(
    candidates: list[Lesson],
    existing: list[Lesson],
    budget_lines: int,
    usage: dict[str, int],
    incorporations_since_use: dict[str, int] | None = None,
    decay_after: int = DECAY_AFTER_INCORPORATIONS,
) -> CurationResult:
    result = CurationResult()
    existing_by_slug = {lesson.slug: lesson for lesson in existing}

    # decay first: a lesson with no recorded use after N incorporations is dropped (frees budget)
    for slug, since in (incorporations_since_use or {}).items():
        if slug in existing_by_slug and usage.get(slug, 0) == 0 and since >= decay_after:
            result.removals.append(slug)

    surviving = {s: le for s, le in existing_by_slug.items() if s not in result.removals}

    for cand in candidates:
        # Every field is load-bearing: the description is the skill front matter's
        # `description`, which the engine requires — a lesson without one would be merged
        # and then silently dropped when loaded.
        if not cand.title.strip() or not cand.description.strip() or not cand.content.strip():
            result.skipped.append(cand.slug or "<untitled>")
            continue
        current = surviving.get(cand.slug)
        if current is not None:
            if current.content.strip() == cand.content.strip():
                result.skipped.append(cand.slug)  # dedupe: identical lesson already exists
            else:
                # contradiction/update ⇒ the new one REPLACES the old — they never coexist
                result.replacements.append((cand.slug, cand))
                surviving[cand.slug] = cand
            continue
        result.additions.append(cand)
        surviving[cand.slug] = cand

    # root AGENTS.md budget: ~3 lines per lesson; overflow is trimmed from the end
    lesson_lines = 3
    projected = (len(surviving) - len([a for a in result.additions])) * lesson_lines
    kept: list[Lesson] = []
    for cand in result.additions:
        if projected + (len(kept) + 1) * lesson_lines > budget_lines:
            result.skipped.append(cand.slug)
            continue
        kept.append(cand)
    result.additions = kept
    return result


def _flatten(text: str) -> str:
    """A lesson occupies exactly one line in AGENTS.md — collapses line breaks/whitespace
    and strips HTML comment markers, guaranteeing a faithful round-trip with the parser
    (without this, multi-line content would be truncated on the next curation = clobber)."""
    return " ".join(text.replace("<!--", "(").replace("-->", ")").split())


def render_agents_md(existing: list[Lesson], curation: CurationResult) -> str:
    removed = set(curation.removals) | {slug for slug, _ in curation.replacements}
    lessons = [le for le in existing if le.slug not in removed]
    lessons += [le for _, le in curation.replacements]
    lessons += list(curation.additions)
    lines = ["# AGENTS.md", "", "Always-active conventions and lessons (curated via symphony-learnings).", ""]
    for le in sorted(lessons, key=lambda x: x.slug):
        lines.append(f"## {_flatten(le.title)}")
        lines.append(f"- {_flatten(le.description)}")
        lines.append(f"- {_flatten(le.content)}  <!-- source: {_flatten(le.source) or 'n/a'} -->")
        lines.append("")
    text = "\n".join(lines)
    if len(text.encode()) > AGENTS_MD_BYTE_BUDGET:
        raise ValueError("combined AGENTS.md exceeds 32 KiB — curation should have trimmed it")
    return text


def parse_agents_md(text: str | None) -> list[Lesson]:
    """Inverse of render_agents_md — loads the REAL curation base from the target
    repo's AGENTS.md. Without this, every learnings PR would clobber the lessons
    already approved (curation would become dead code)."""
    if not text:
        return []
    lessons: list[Lesson] = []
    title = description = content = source = None
    for line in text.splitlines():
        if line.startswith("## "):
            if title and content:
                lessons.append(
                    Lesson(title=title, description=description or "", content=content, source=source or "")
                )
            title, description, content, source = line[3:].strip(), None, None, None
        elif line.startswith("- ") and title:
            body = line[2:].strip()
            match = re.search(r"<!--\s*source:\s*(.*?)\s*-->\s*$", body)  # anchored at the END
            if match:
                source = match.group(1)
                body = body[: match.start()].strip()
            if description is None:
                description = body
            else:
                content = body
    if title and content:
        lessons.append(Lesson(title=title, description=description or "", content=content, source=source or ""))
    return lessons


def detect_lesson_usage(harvest: Harvest, existing: list[Lesson]) -> set[str]:
    """FR-032: 'lesson usage is recorded by the retrospective' — a lesson counts
    as used when its title appears in the work (Workpad or diff)."""
    corpus = f"{harvest.workpad}\n{harvest.diff}".casefold()
    return {le.slug for le in existing if le.title and le.title.casefold() in corpus}


def render_skill(lesson: Lesson) -> tuple[str, str]:
    # Codex discovers repo skills under .agents/skills/ (official docs) — NOT .codex/skills/
    path = f".agents/skills/{lesson.slug}/SKILL.md"
    body = (
        f"---\nname: {lesson.slug}\ndescription: {lesson.description}\n---\n\n"
        f"# {lesson.title}\n\n## Steps\n\n{lesson.content}\n\n*Source: {lesson.source or 'n/a'}*\n"
    )
    return path, body


# --------------------------------------------------------------------------
# distillation (injectable; the real one is a one-shot engine run, entry/reflect.py)
# --------------------------------------------------------------------------
DISTILL_INSTRUCTIONS = (
    "You are the Reflector of a continual-learning pipeline. From the outcome of an "
    "issue (diff, review threads with what was accepted/contested, CI, Workpad, "
    "number of attempts), distill AT MOST 3 GENERALIZABLE lesson candidates in the "
    "schema: title; 1-line description; content (pitfall/strategy + next "
    "action); scope (globs). NEVER a lesson about a specific line of this PR. If the "
    "pattern is procedural and recurring, propose a skill (kind=skill-proposal). "
    "PREFER answering 'nothing to learn' over producing a weak lesson — write only what "
    "was confirmed by the outcome. Respond with a JSON array of objects "
    "{title, description, content, globs, kind} or [] if there is nothing."
)


def build_distill_prompt(harvest: Harvest) -> str:
    threads = "\n".join(
        f"- [{t.get('severity', '?')}] {t.get('author', '?')}: {t.get('body', '')[:300]}"
        f" (resolved={t.get('resolved')}, contested={t.get('contested', False)})"
        for t in harvest.review_threads
    )
    return (
        f"{DISTILL_INSTRUCTIONS}\n\n## Issue {harvest.issue_identifier} — outcome: "
        f"{harvest.final_state} ({harvest.attempts} attempt(s))\n\n## Review threads\n"
        f"{threads or '(none)'}\n\n## CI\n{harvest.ci_results}\n\n## Workpad\n"
        f"{harvest.workpad[:2000]}\n\n## Final diff (truncated)\n{harvest.diff[:4000]}"
    )


def parse_candidates(text: str, source: str) -> list[Lesson]:
    import json

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out: list[Lesson] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        out.append(
            Lesson(
                title=" ".join(str(item.get("title", "")).split()),
                description=" ".join(str(item.get("description", "")).split()),
                content=" ".join(str(item.get("content", "")).split()),
                globs=tuple(item.get("globs") or ()),
                kind="skill-proposal" if item.get("kind") == "skill-proposal" else "lesson",
                source=source,
            )
        )
    return out


# The real distillation (one-shot `codex exec` in an ephemeral sandbox) lives at the
# Kubernetes edge: entry/reflect.py::k8s_engine_distill — this module stays pure and receives an
# injected `distill`.


# --------------------------------------------------------------------------
# retrospective with ownership (exactly-one) and nightly sweep
# --------------------------------------------------------------------------
def _adopt_retro_token(kv: state.KV, issue_id: str, call_id: str, now_ms: int) -> bool:
    """At-most-once without CAS: acquires or re-adopts (same call_id) the token; a
    foreign, live call_id ⇒ backs off with no side effects (constitution IV)."""
    if state.acquire_retro_token(kv, issue_id, now_ms):
        token = kv.get(state.retro_key(issue_id))
        token.update(reflector_call_id=call_id)
        kv.put(state.retro_key(issue_id), token)
        return True
    token = kv.get(state.retro_key(issue_id)) or {}
    if token.get("status") == "done":
        return False
    holder = token.get("reflector_call_id")
    if holder in (None, call_id):
        token.update(reflector_call_id=call_id, at=now_ms)
        kv.put(state.retro_key(issue_id), token)
        return True
    return False


def _parse_iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(_dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def run_retrospective(
    kv: state.KV,
    github: GitHubLike,
    cfg: Config,
    issue_id: str,
    entry: dict,
    call_id: str,
    now_ms: int,
    distill: Callable[[Harvest, Config], list[Lesson]],
    existing_lessons: list[Lesson] | None = None,
    usage: dict[str, int] | None = None,
    fetch_workpad: Callable[[str], str] | None = None,
) -> str:
    if not cfg.learning_enabled:
        return "learning-disabled"
    if not _adopt_retro_token(kv, issue_id, call_id, now_ms):
        return "skipped"

    identifier = entry.get("identifier") or issue_id
    # Terminal cleanup removes issue:* before this Job finishes. Retain only the
    # provenance needed to interpret the outcome, never the entry's credentials,
    # failure text, workpad, trace data, or session paths.
    token = kv.get(state.retro_key(issue_id)) or {}
    token.update(
        identifier=identifier,
        target_repo=entry.get("target_repo"),
        provenance="retrospective",
        dispatch_seq=entry.get("dispatch_seq"),
    )
    kv.put(state.retro_key(issue_id), token)
    yyyymm = _dt.datetime.fromtimestamp(now_ms / 1000, tz=_dt.UTC).strftime("%Y%m")

    if existing_lessons is None:
        existing_lessons = parse_agents_md(github.get_file("AGENTS.md"))

    pr = github.get_pr_for_issue(identifier)
    if pr is not None:
        token["source_pr"] = pr.get("html_url")
        kv.put(state.retro_key(issue_id), token)
    harvest = Harvest(
        issue_identifier=identifier,
        final_state=entry.get("final_state", "terminal"),
        attempts=entry.get("attempt", 1),
        tokens=entry.get("tokens") or {},
        workpad=fetch_workpad(identifier) if fetch_workpad else "",
    )
    if pr is not None:
        harvest.pr_number = pr.get("number")
        harvest.diff = github.get_pr_diff(harvest.pr_number)
        harvest.review_threads = github.get_review_threads(harvest.pr_number)
        harvest.ci_results = github.get_ci_results(harvest.pr_number)

    # distillation comes BEFORE any metric write: if it fails (infra), the token
    # stays "started", the sweep retries, and NOTHING gets inflated by the retry
    candidates = distill(harvest, cfg)

    if pr is not None:
        state.bump_metric(kv, yyyymm, "prs_opened")
        merged_at = _parse_iso_ms(pr.get("merged_at") or (pr.get("pull_request") or {}).get("merged_at"))
        created_at = _parse_iso_ms(pr.get("created_at"))
        if merged_at:
            state.bump_metric(kv, yyyymm, "prs_merged")
            if created_at:
                state.bump_metric(kv, yyyymm, "time_to_merge_ms_sum", by=merged_at - created_at)
        for thread in harvest.review_threads:
            state.bump_metric_category(
                kv, yyyymm, "review_comments", str(thread.get("category") or thread.get("severity") or "other")
            )

    # lesson usage: recorded BY the retrospective (FR-032) and input to decay (FR-029)
    used = detect_lesson_usage(harvest, existing_lessons)
    for slug in used:
        state.bump_metric_category(kv, yyyymm, "lesson_uses", slug)
    ages: dict[str, int] = dict(kv.get(state.LESSONS_AGE_KEY) or {})
    for slug in used:
        ages[slug] = 0

    curation = curate(
        candidates,
        existing_lessons,
        cfg.agents_md_budget_lines,
        usage if usage is not None else dict.fromkeys(used, 1),
        incorporations_since_use=ages,
        decay_after=cfg.decay_after_incorporations,
    )

    # lesson "age" counts INCORPORATIONS (FR-029): it only advances when this retro
    # actually proposes a change to the base — "nothing to learn" retros age nothing
    if not curation.empty:
        for lesson in existing_lessons:
            if lesson.slug not in used:
                ages[lesson.slug] = ages.get(lesson.slug, 0) + 1
    kv.put(state.LESSONS_AGE_KEY, ages)

    token = kv.get(state.retro_key(issue_id)) or {}
    state.touch_metrics(kv, yyyymm)
    if curation.empty:
        token.update(status="done", outcome="nothing-to-learn", learnings_pr=None, at=now_ms)
        kv.put(state.retro_key(issue_id), token)
        return "nothing-to-learn"

    files: dict[str, str] = {"AGENTS.md": render_agents_md(existing_lessons, curation)}
    for lesson in [*curation.additions, *[le for _, le in curation.replacements]]:
        if lesson.kind == "skill-proposal":
            path, body = render_skill(lesson)
            files[path] = body

    pr_url = github.open_pr(
        branch=f"symphony/learnings/{identifier}",
        title=f"learnings: {identifier}",
        body=(
            f"Lessons distilled from the retrospective of {identifier} "
            f"(+{len(curation.additions)} new, {len(curation.replacements)} replacements, "
            f"{len(curation.removals)} decayed). Human gate required — never auto-merge."
        ),
        files=files,
        labels=[cfg.learnings_label],
    )
    token.update(status="done", outcome="learnings-pr-opened", learnings_pr=pr_url, at=now_ms)
    kv.put(state.retro_key(issue_id), token)
    state.bump_metric(kv, yyyymm, "learnings_prs")
    return "learnings-pr-opened"


def nightly_sweep(kv: state.KV, tracker: TrackerAdapter, cfg: Config, now_ms: int, fx) -> dict:
    """At-least-once: sweeps terminals within the window (7d, matched to the Dict TTL),
    fires missed retros, reopens orphaned 'started' ones, and touches metrics retention."""
    yyyymm = _dt.datetime.fromtimestamp(now_ms / 1000, tz=_dt.UTC).strftime("%Y%m")
    state.touch_metrics(kv, yyyymm)
    if not cfg.learning_enabled:
        return {"spawned": 0}
    try:
        terminals = tracker.fetch_issues_by_states(list(cfg.effective_reflect_on))
    except TrackerUnavailable:
        fx.emit("nightly_sweep_skipped", reason="tracker_unavailable")
        return {"spawned": 0}
    spawned = 0
    cutoff_iso = _dt.datetime.fromtimestamp(now_ms / 1000 - SWEEP_WINDOW_DAYS * 86400, tz=_dt.UTC).isoformat()
    for issue in terminals:
        if issue.updated_at and issue.updated_at < cutoff_iso:
            continue  # outside the sweep window — durable evidence lives in the PR/label
        token = kv.get(state.retro_key(issue.id))
        if token is None:
            if state.acquire_retro_token(kv, issue.id, now_ms):
                fx.spawn_reflector(issue.id, {"identifier": issue.identifier, "final_state": issue.state})
                spawned += 1
        elif (
            token.get("status") == "started"
            and now_ms - token.get("at", now_ms) > ORPHAN_RETRO_AGE_MS
            and not (token.get("reflector_call_id") and fx.is_call_running(token["reflector_call_id"]))
        ):
            token.update(reflector_call_id=None, at=now_ms)  # orphan: release for re-adoption
            kv.put(state.retro_key(issue.id), token)
            fx.spawn_reflector(issue.id, {"identifier": issue.identifier, "final_state": issue.state})
            spawned += 1
    fx.emit("nightly_sweep_done", spawned=spawned)
    return {"spawned": spawned}


class HttpxGitHub:  # pragma: no cover — thin network edge (validated in the smoke test)
    """Minimal GitHubLike over the REST API (reads + opening the learnings PR)."""

    def __init__(self, token: str, repo: str, *, base_branch: str | None = None) -> None:
        import httpx

        self.repo = repo
        self._base_branch = base_branch
        self._client = httpx.Client(
            base_url=github_api_url(),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=20.0,
        )

    def get_file(self, path: str) -> str | None:
        import base64

        resp = self._client.get(f"/repos/{self.repo}/contents/{path}")
        if resp.status_code != 200:
            return None
        payload = resp.json()
        if payload.get("encoding") == "base64":
            return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        return payload.get("content")

    def get_pr_for_issue(self, identifier: str) -> dict | None:
        resp = self._client.get("/search/issues", params={"q": f"repo:{self.repo} is:pr {identifier}"})
        items = resp.json().get("items", []) if resp.status_code == 200 else []
        if not items:
            return None
        # full PR detail (merged_at/created_at for merge metrics — FR-032)
        number = items[0].get("number")
        detail = self._client.get(f"/repos/{self.repo}/pulls/{number}")
        return detail.json() if detail.status_code == 200 else items[0]

    def get_pr_diff(self, pr_number: int) -> str:
        resp = self._client.get(
            f"/repos/{self.repo}/pulls/{pr_number}", headers={"Accept": "application/vnd.github.diff"}
        )
        return resp.text[:100_000] if resp.status_code == 200 else ""

    _SEVERITY_RE = re.compile(r"\b(critical|major|minor|trivial|nitpick)\b", re.IGNORECASE)

    def get_review_threads(self, pr_number: int) -> list[dict]:
        resp = self._client.get(f"/repos/{self.repo}/pulls/{pr_number}/comments", params={"per_page": 100})
        if resp.status_code != 200:
            return []
        comments = resp.json()
        replies_by_parent: dict[int, int] = {}
        for c in comments:
            parent = c.get("in_reply_to_id")
            if parent:
                replies_by_parent[parent] = replies_by_parent.get(parent, 0) + 1
        threads = []
        for c in comments:
            if c.get("in_reply_to_id"):
                continue  # replies count as a contestation signal, not their own thread
            body = c.get("body", "")
            sev = self._SEVERITY_RE.search(body)
            threads.append(
                {
                    "author": (c.get("user") or {}).get("login"),
                    "body": body,
                    "severity": sev.group(1).lower() if sev else "unknown",
                    "resolved": None,  # real resolution requires GraphQL — don't claim what we don't know
                    "contested": replies_by_parent.get(c.get("id"), 0) > 0,
                }
            )
        return threads

    def get_ci_results(self, pr_number: int) -> list[dict]:
        detail = self._client.get(f"/repos/{self.repo}/pulls/{pr_number}")
        if detail.status_code != 200:
            return []
        sha = (detail.json().get("head") or {}).get("sha")
        if not sha:
            return []
        runs = self._client.get(f"/repos/{self.repo}/commits/{sha}/check-runs", params={"per_page": 50})
        if runs.status_code != 200:
            return []
        return [
            {"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion")}
            for r in runs.json().get("check_runs", [])
        ]

    def open_pr(self, branch: str, title: str, body: str, files: dict[str, str], labels: list[str]) -> str:
        # creates blobs/tree/commit/branch via the contents API (small files, v1)
        base = self._base_branch or self._client.get(f"/repos/{self.repo}").json().get("default_branch", "main")
        ref = self._client.get(f"/repos/{self.repo}/git/ref/heads/{base}").json()
        sha = ref.get("object", {}).get("sha")
        self._client.post(f"/repos/{self.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
        for path, content in files.items():
            import base64

            existing = self._client.get(f"/repos/{self.repo}/contents/{path}", params={"ref": branch})
            payload = {
                "message": f"{title}: {path}",
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch,
            }
            if existing.status_code == 200:
                payload["sha"] = existing.json().get("sha")
            self._client.put(f"/repos/{self.repo}/contents/{path}", json=payload)
        pr = self._client.post(
            f"/repos/{self.repo}/pulls", json={"title": title, "head": branch, "base": base, "body": body}
        ).json()
        if labels and pr.get("number"):
            self._client.post(f"/repos/{self.repo}/issues/{pr['number']}/labels", json={"labels": labels})
        return pr.get("html_url", "")
