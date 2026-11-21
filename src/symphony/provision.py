"""Repository provisioning: what each discovered repository needs before Symphony works on it.

After each discovery pass (`discovery.Refresher`), for every repository that is not archived:

- **Tracker labels** (one call for all repositories): `repo:<owner/name>`, and
  `component:<name>` for each of its components, configured or detected.
- **GitHub labels**: the merger's labels, the required labels and the learnings label, created
  in the repository when missing.
- **Merger policy**: a repository without `.symphony/merger.md` gets a pull request that adds a
  default one, from the `symphony/merger-policy` branch. A pull request that already exists on
  that branch — open, merged or closed — is never opened again: closing it is a decision.

Everything is idempotent and best effort. A repository is redone only when what it needs
changes or `RECHECK_MS` has passed; its outcome is recorded on its discovery item.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable

import httpx
import structlog

from .config import Config
from .endpoints import github_api_url
from .merger import comments as cm

log = structlog.get_logger()

PROVISIONED_PREFIX = "repos:provisioned:"
RECHECK_MS = 86_400_000
TIMEOUT_S = 10.0
POLICY_PATH = ".symphony/merger.md"
# The installation token provisioning mints: labels (issues), the policy branch and file
# (contents) and its pull request.
PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"}
POLICY_BRANCH = "symphony/merger-policy"
LABEL_COLOR = "5319e7"
MERGER_LABELS = (cm.IN_AUDIT, cm.REVIEWED, cm.WAIT_CI, cm.TRAIN_CI, cm.BYPASS,
                 cm.NEEDS_MAINTAINER, cm.REVIEW_REQUESTED, cm.WAIT_APPROVAL)

DEFAULT_POLICY = """---
# symphony-merger policy (docs/merger.md#repository-policy-symphonymergermd).
# A human approves on GitHub before the merge train merges a reviewed pull request.
require_approval: true
merge_method: squash
---

# Review rules

Rules the merger's reviewers apply to every pull request here, on top of the generic ones.
Write what a reviewer of this repository must check: build and test commands, conventions,
the paths that need extra care.
"""

POLICY_PR_BODY = (
    "Adds a default merger policy, so the merger can review this repository's pull requests "
    "once a merger runs for it. It asks for a human approval before merging.\n\n"
    "Edit the review rules before merging, or close this pull request to keep the repository "
    "without one — Symphony does not open it again."
)


def github_labels(cfg: Config) -> list[str]:
    return list(dict.fromkeys([*MERGER_LABELS, *cfg.required_labels, cfg.learnings_label]))


def tracker_labels(cfg: Config, items: list[dict]) -> list[str]:
    names: list[str] = []
    for item in items:
        if item.get("archived"):
            continue
        repo = item["name"]
        names.append(f"repo:{repo}")
        components = cfg.components.get(repo) or item.get("components") or {}
        names.extend(f"component:{c}" for c in components)
    return sorted(dict.fromkeys(names))


def run(kv, tracker, cfg: Config, items: list[dict], token: str, http: httpx.Client,
        now_ms: Callable[[], int]) -> None:
    """Provision every repository in `items`, recording each outcome on its item."""
    live = [i for i in items if not i.get("archived")]
    try:
        tracker.ensure_labels(tracker_labels(cfg, live))
    except Exception as exc:  # noqa: BLE001 — labels can be created on the next pass
        log.warning("repos_tracker_labels_failed", error=str(exc)[:300])
    labels = github_labels(cfg)
    for item in live:
        need = {"labels": labels, "policy": not item.get("merger_policy")}
        signature = hashlib.sha256(json.dumps(need, sort_keys=True).encode()).hexdigest()[:16]
        key = PROVISIONED_PREFIX + item["name"]
        done = kv.get(key) or {}
        fresh = now_ms() - done.get("at", 0) < RECHECK_MS and not done.get("failed")
        if done.get("signature") == signature and fresh:
            item["provisioning"] = done.get("outcome")
            continue
        outcome = _repository(http, token, item, labels, need["policy"])
        # A failure is retried on the next pass (every discovery), a success once a day.
        kv.put(key, {"signature": signature, "at": now_ms(), "failed": bool(outcome["error"]),
                     "outcome": outcome})
        item["provisioning"] = outcome


def _repository(http: httpx.Client, token: str, item: dict, labels: list[str],
                policy: bool) -> dict:
    api = github_api_url()
    repo = item["name"]
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    outcome: dict = {"labels_created": [], "policy_pr": None, "error": None}
    try:
        outcome["labels_created"] = _labels(http, api, headers, repo, labels)
        if policy:
            outcome["policy_pr"] = _policy_pr(http, api, headers, repo, item["default_branch"])
    except httpx.HTTPError as exc:
        outcome["error"] = _describe(exc)
        log.warning("repos_provisioning_failed", repo=repo, error=outcome["error"])
    return outcome


def _describe(exc: httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc.request.method} {exc.request.url.path} answered {exc.response.status_code}"
    return type(exc).__name__


def _labels(http: httpx.Client, api: str, headers: dict, repo: str, wanted: list[str]) -> list[str]:
    have: set[str] = set()
    url: str | None = f"{api}/repos/{repo}/labels?per_page=100"
    while url:
        response = http.get(url, headers=headers, timeout=TIMEOUT_S)
        response.raise_for_status()
        have.update(label["name"].casefold() for label in response.json())
        url = response.links.get("next", {}).get("url")
    created = []
    for name in wanted:
        if name.casefold() in have:
            continue
        response = http.post(f"{api}/repos/{repo}/labels", headers=headers, timeout=TIMEOUT_S,
                             json={"name": name, "color": LABEL_COLOR})
        if response.status_code != 422:  # 422: created meanwhile, which is what we need
            response.raise_for_status()
            created.append(name)
    return created


def _policy_pr(http: httpx.Client, api: str, headers: dict, repo: str, base: str) -> str | None:
    """The URL of the pull request that adds the policy, opened now or before."""
    owner = repo.split("/", 1)[0]
    response = http.get(f"{api}/repos/{repo}/pulls", headers=headers, timeout=TIMEOUT_S,
                        params={"head": f"{owner}:{POLICY_BRANCH}", "state": "all"})
    response.raise_for_status()
    if response.json():
        return response.json()[0].get("html_url")
    ref = http.get(f"{api}/repos/{repo}/git/ref/heads/{base}", headers=headers, timeout=TIMEOUT_S)
    ref.raise_for_status()
    made = http.post(f"{api}/repos/{repo}/git/refs", headers=headers, timeout=TIMEOUT_S,
                     json={"ref": f"refs/heads/{POLICY_BRANCH}", "sha": ref.json()["object"]["sha"]})
    if made.status_code != 422:  # 422: the branch is left from an earlier pass
        made.raise_for_status()
    put = http.put(f"{api}/repos/{repo}/contents/{POLICY_PATH}", headers=headers, timeout=TIMEOUT_S,
                   json={"message": "chore: add a default merger policy",
                         "content": base64.b64encode(DEFAULT_POLICY.encode()).decode(),
                         "branch": POLICY_BRANCH})
    if put.status_code != 422:  # 422: the file is already on the branch
        put.raise_for_status()
    pr = http.post(f"{api}/repos/{repo}/pulls", headers=headers, timeout=TIMEOUT_S,
                   json={"title": "chore: add a default merger policy", "head": POLICY_BRANCH,
                         "base": base, "body": POLICY_PR_BODY})
    pr.raise_for_status()
    return pr.json().get("html_url")
