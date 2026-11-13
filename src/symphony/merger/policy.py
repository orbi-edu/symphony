"""The repository's merger policy: `.symphony/merger.md`, versioned with the code.

Front matter (YAML) keys, all optional:

- `lanes`: `[{key, audits}]`, the first review's parallel reviewer lanes (default: 3)
- `blocking_classes`: classes added to the generic blocking ones
- `models`: `{reviewer|oracle|arbiter|developer: {provider, model, thinking}}`, used while no
  TeamConfig generation is published (precedence: published TeamConfig > policy > DEFAULT)
- `rule_checks`: `[{name, run}]`, shell commands run at the PR head (`SYMPHONY_BASE`,
  `SYMPHONY_HEAD` in env); a non-zero exit is a rule break
- `overlap`: a shell command printing the packages main's changes select (`SYMPHONY_BASE`,
  `SYMPHONY_HEAD`), for the train's order; default: packages touched
- `merge_method`: `rebase` | `squash` | `merge` (default `rebase`)
- `batch_max` (default 4), `fix_slots` (default 3)
- `require_human` (default false): an escalation skips the AI arbiter and asks a maintainer
- `ci`: `{checks: [names], workflow: name}`: the check runs that decide (default: every check
  run on the head) and the Actions workflow the train reruns and cancels (default: any)
- `max_file_kib` (default 1024): larger files added by a PR are a rule break
- `main_watch`: `{ticket, state, labels}`: with `ticket: true`, a red main opens a ticket on
  the WORKFLOW.md tracker, in `state` (default `Todo`) with `labels` (otherwise the watch only
  notifies)

The body holds the repository's own review rules for reviewer and oracle.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from .. import teamconfig

PATH = ".symphony/merger.md"
GENERIC_BLOCKING = ("bug", "regression", "security", "spec", "duplicate-owner")
METHODS = ("rebase", "squash", "merge")
DEFAULT_LANES = (
    {"key": "correctness", "audits": "correctness: bugs, regressions and broken callers in the changed code"},
    {"key": "security-spec", "audits": "security, and whether the PR meets every acceptance criterion of its ticket"},
    {"key": "design", "audits": "ownership (a second owner of what the code base already owns), tests, names and docs"},
)
ALL_LANE = {"key": "all", "audits": "every audit: correctness, regressions, security, the ticket's acceptance criteria, ownership, tests, names and docs"}
MODEL_ROLES = ("reviewer", "oracle", "arbiter", "developer")


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    lanes: tuple[dict, ...] = DEFAULT_LANES
    blocking_classes: tuple[str, ...] = GENERIC_BLOCKING
    models: dict = field(default_factory=dict)
    rule_checks: tuple[dict, ...] = ()
    overlap: str | None = None
    merge_method: str = "rebase"
    batch_max: int = 4
    fix_slots: int = 3
    require_human: bool = False
    ci_checks: tuple[str, ...] = ()
    ci_workflow: str | None = None
    max_file_kib: int = 1024
    main_watch: dict = field(default_factory=dict)
    rules: str = ""

    def model(self, role: str, team: dict) -> dict:
        """The model a role runs on: a published TeamConfig wins, then this policy, then the
        TeamConfig default."""
        if team.get("generation", 0) >= 1 or role not in self.models:
            return team["models"][role]
        return {**teamconfig.DEFAULT["models"][role], **self.models[role]}


def _split(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        raise PolicyError(f"{PATH}: front matter is not closed with ---")
    data = yaml.safe_load(parts[0][3:]) or {}
    if not isinstance(data, dict):
        raise PolicyError(f"{PATH}: front matter must be a mapping")
    return data, parts[1].lstrip("\n")


def _positive(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PolicyError(f"{PATH}: {key} must be a positive integer")
    return value


def parse(text: str | None) -> Policy:
    """The policy of a document; None (no file in the repository) is the default policy."""
    if text is None:
        return Policy()
    data, body = _split(text)
    lanes = data.get("lanes", list(DEFAULT_LANES))
    if not isinstance(lanes, list) or not lanes or not all(
        isinstance(x, dict) and isinstance(x.get("key"), str) and isinstance(x.get("audits"), str)
        for x in lanes
    ):
        raise PolicyError(f"{PATH}: lanes must be a non-empty list of {{key, audits}}")
    extra = data.get("blocking_classes", [])
    if not isinstance(extra, list) or not all(isinstance(c, str) and c and c != "nit" for c in extra):
        raise PolicyError(f"{PATH}: blocking_classes must be a list of class names (not nit)")
    models = data.get("models", {}) or {}
    if not isinstance(models, dict) or set(models) - set(MODEL_ROLES):
        raise PolicyError(f"{PATH}: models may name only {', '.join(MODEL_ROLES)}")
    for role, ref in models.items():
        if not isinstance(ref, dict) or not isinstance(ref.get("provider"), str) or not isinstance(
            ref.get("model"), str
        ):
            raise PolicyError(f"{PATH}: models.{role} needs provider and model")
        if ref.get("thinking", "medium") not in teamconfig.THINKING:
            raise PolicyError(f"{PATH}: models.{role}.thinking is not a thinking level")
    reviewer = models.get("reviewer")
    for checker in ("oracle", "arbiter"):
        ref = models.get(checker)
        if reviewer and ref and (ref["provider"], ref["model"]) == (reviewer["provider"], reviewer["model"]):
            raise PolicyError(f"{PATH}: models.{checker} must differ from the reviewer's model")
    checks = data.get("rule_checks", [])
    if not isinstance(checks, list) or not all(
        isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("run"), str)
        for c in checks
    ):
        raise PolicyError(f"{PATH}: rule_checks must be a list of {{name, run}}")
    overlap = data.get("overlap")
    if overlap is not None and not isinstance(overlap, str):
        raise PolicyError(f"{PATH}: overlap must be a shell command")
    method = data.get("merge_method", "rebase")
    if method not in METHODS:
        raise PolicyError(f"{PATH}: merge_method must be one of {', '.join(METHODS)}")
    require_human = data.get("require_human", False)
    if not isinstance(require_human, bool):
        raise PolicyError(f"{PATH}: require_human must be true or false")
    ci = data.get("ci", {}) or {}
    if not isinstance(ci, dict):
        raise PolicyError(f"{PATH}: ci must be a mapping")
    ci_checks = ci.get("checks", [])
    if not isinstance(ci_checks, list) or not all(isinstance(c, str) for c in ci_checks):
        raise PolicyError(f"{PATH}: ci.checks must be a list of check names")
    workflow = ci.get("workflow")
    if workflow is not None and not isinstance(workflow, str):
        raise PolicyError(f"{PATH}: ci.workflow must be a workflow name")
    watch = data.get("main_watch", {}) or {}
    if not isinstance(watch, dict):
        raise PolicyError(f"{PATH}: main_watch must be a mapping")
    unknown = sorted(set(watch) - {"ticket", "state", "labels"})
    if unknown:
        raise PolicyError(f"{PATH}: main_watch has unknown keys {', '.join(unknown)} "
                          "(keys: ticket, state, labels)")
    return Policy(
        lanes=tuple(lanes),
        blocking_classes=tuple(dict.fromkeys([*GENERIC_BLOCKING, *extra])),
        models=models,
        rule_checks=tuple(checks),
        overlap=overlap,
        merge_method=method,
        batch_max=_positive(data, "batch_max", 4),
        fix_slots=_positive(data, "fix_slots", 3),
        require_human=require_human,
        ci_checks=tuple(ci_checks),
        ci_workflow=workflow,
        max_file_kib=_positive(data, "max_file_kib", 1024),
        main_watch=watch,
        rules=body.strip(),
    )
