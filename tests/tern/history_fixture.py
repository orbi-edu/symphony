"""Believable `GET /v1/telemetry/history` documents (docs/api.md) for the Tern plugin's tests.

Daily rollups come from a seeded generator, so a day always has the same numbers; `history_doc`
buckets them like the API: every bucket of the range present and zero-filled, weeks from Monday,
at most 120 points.
"""

from __future__ import annotations

import datetime as dt
import random

FIRST_DAY_AGO = 75  # the fake team's first rollup
KINDS = ("implement", "review", "answer", "oracle", "reflect", "assist")
OUTCOMES = ("done", "continuation", "failed", "blocked", "fenced", "error")
CHANNELS = ("linear", "slack", "github", "symphony")
MODELS = ("anthropic/claude-sonnet-5", "openai/gpt-5.5", "anthropic/claude-haiku-5")
REPOS = ("acme/app", "acme/web", "acme/billing", "acme/gateway", "acme/infra")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SUMMED = (
    "dispatched", "runs", "attempts", "rework", "prs_opened", "prs_merged", "learning_prs",
    "time_to_merge_ms_sum", "time_to_merge_count", "tokens_input", "tokens_output",
    "tokens_cache_read", "tokens_cache_write", "tokens", "cost_micro_usd", "sessions",
    "questions_asked", "questions_answered", "questions_answer_ms_sum", "blocked_on_person",
    "trace_attached",
)
MAPS = ("sessions_by_kind", "outcomes", "merger", "answered_via")


def _day(day: dt.date) -> dict:
    """One day's rollup, plus its per-model and per-repo cost and its session start hours."""
    r = random.Random(day.toordinal())
    weekend = day.weekday() >= 5
    sessions = r.randint(0, 3) if weekend else r.randint(4, 14)
    kinds = {k: 0 for k in KINDS}
    for _ in range(sessions):
        kinds[r.choices(KINDS, weights=(6, 3, 2, 1, 1, 1))[0]] += 1
    outcomes = {k: 0 for k in OUTCOMES}
    for _ in range(sessions):
        outcomes[r.choices(OUTCOMES, weights=(12, 3, 1, 1, 1, 1))[0]] += 1
    dispatched = kinds["implement"]
    rework = sum(1 for _ in range(dispatched) if r.random() < 0.15)
    merged = r.randint(0, dispatched)
    asked = r.randint(0, 3)
    answered = min(asked, r.randint(0, 3))
    via = {k: 0 for k in CHANNELS}
    for _ in range(answered):
        via[r.choices(CHANNELS, weights=(5, 3, 1, 1))[0]] += 1
    tin, tout = sessions * r.randint(40_000, 90_000), sessions * r.randint(6_000, 14_000)
    tread, twrite = sessions * r.randint(80_000, 200_000), sessions * r.randint(5_000, 20_000)
    cost = sessions * r.randint(150_000, 600_000)  # micro USD
    models = {}
    left = cost
    for m, share in zip(MODELS, (0.7, 0.22), strict=False):
        models[m] = int(cost * share)
        left -= models[m]
    models[MODELS[-1]] = left
    repos = {}
    for _ in range(sessions):
        repo = r.choices(REPOS, weights=(5, 4, 2, 2, 1))[0]
        repos.setdefault(repo, [0, 0])
        repos[repo][0] += 1
    for entry in repos.values():
        entry[1] = cost * entry[0] // max(sessions, 1)
    hours = [r.choice((9, 10, 11, 14, 15, 16, 17, 20)) for _ in range(sessions)]
    ttm_count = merged
    point = {
        "dispatched": dispatched, "runs": sessions, "attempts": dispatched + rework, "rework": rework,
        "prs_opened": dispatched, "prs_merged": merged, "learning_prs": int(r.random() < 0.1),
        "time_to_merge_ms_sum": ttm_count * r.randint(1, 9) * 3_600_000, "time_to_merge_count": ttm_count,
        "tokens_input": tin, "tokens_output": tout, "tokens_cache_read": tread, "tokens_cache_write": twrite,
        "tokens": tin + tout + tread + twrite, "cost_micro_usd": cost, "sessions": sessions,
        "sessions_by_kind": kinds, "outcomes": outcomes,
        "merger": {"reviewed": merged, "back_to_author": rework, "merged": merged, "needs_maintainer": 0},
        "questions_asked": asked, "questions_answered": answered, "answered_via": via,
        "questions_answer_ms_sum": answered * r.randint(10, 240) * 60_000,
        "blocked_on_person": int(r.random() < 0.2), "trace_attached": sessions // 2,
    }
    return {"point": point, "models": models, "repos": repos, "hours": hours, "weekday": day.weekday()}


def _zero() -> dict:
    p = {k: 0 for k in SUMMED}
    p.update({"sessions_by_kind": {}, "outcomes": {}, "answered_via": {},
              "merger": {"reviewed": 0, "back_to_author": 0, "merged": 0, "needs_maintainer": 0}})
    return p


def _add(into: dict, p: dict) -> None:
    for k in SUMMED:
        into[k] += p[k]
    for k in MAPS:
        for sub, n in p[k].items():
            if n:
                into[k][sub] = into[k].get(sub, 0) + n


def _derive(p: dict) -> dict:
    """The derived keys; a null rate or mean is left out, as Tern's JSON decoder drops nulls."""
    p["cost_usd"] = p["cost_micro_usd"] / 1e6
    if p["dispatched"]:
        p["rework_rate"] = p["rework"] / p["dispatched"]
    if p["time_to_merge_count"]:
        p["mean_time_to_merge_ms"] = p["time_to_merge_ms_sum"] // p["time_to_merge_count"]
    if p["questions_answered"]:
        p["mean_answer_ms"] = p["questions_answer_ms_sum"] // p["questions_answered"]
    return p


def _bucket_start(day: dt.date, bucket: str) -> dt.date:
    if bucket == "week":
        return day - dt.timedelta(days=day.weekday())
    if bucket == "month":
        return day.replace(day=1)
    return day


def _next(start: dt.date, bucket: str) -> dt.date:
    if bucket == "week":
        return start + dt.timedelta(days=7)
    if bucket == "month":
        return (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return start + dt.timedelta(days=1)


def history_doc(today: dt.date, frm: dt.date | None = None, bucket: str | None = None) -> dict:
    """The history from `frm` (default: the first rollup) to `today`."""
    first = today - dt.timedelta(days=FIRST_DAY_AGO)
    start = frm or first
    order = ("day", "week", "month")
    bucket = bucket or next((b for b in order if _count(start, today, b) <= 120), "month")
    while bucket != "month" and _count(start, today, bucket) > 120:
        bucket = order[order.index(bucket) + 1]
    points, models, repos, kinds = [], {}, {}, {}
    cells = [[0] * 24 for _ in WEEKDAYS]
    totals = _zero()
    b = _bucket_start(start, bucket)
    while b <= today:
        end = min(_next(b, bucket) - dt.timedelta(days=1), today)
        p = _zero()
        for i in range((end - b).days + 1):
            day = b + dt.timedelta(days=i)
            if day < start or day < first:
                continue
            d = _day(day)
            _add(p, d["point"])
            for m, c in d["models"].items():
                models.setdefault(m, {})[len(points)] = models.get(m, {}).get(len(points), 0) + c
            for repo, (n, c) in d["repos"].items():
                acc = repos.setdefault(repo, [0, 0])
                acc[0] += n
                acc[1] += c
            for hour in d["hours"]:
                cells[d["weekday"]][hour] += 1
        _add(totals, p)
        for k, n in p["sessions_by_kind"].items():
            kinds.setdefault(k, {})[len(points)] = n
        points.append({"start": b.isoformat(), "end": end.isoformat(), **_derive(p)})
        b = _next(b, bucket)
    n = len(points)
    by_model = sorted(
        ({"model": m, "cost_micro_usd": sum(per.values()), "cost_usd": sum(per.values()) / 1e6,
          "tokens": sum(per.values()) * 3, "cost_series": [per.get(i, 0) / 1e6 for i in range(n)]}
         for m, per in models.items()),
        key=lambda x: -x["cost_micro_usd"],
    )
    by_repo = sorted(
        ({"repo": repo, "sessions": s, "cost_micro_usd": c, "cost_usd": c / 1e6} for repo, (s, c) in repos.items()),
        key=lambda x: (-x["cost_micro_usd"], -x["sessions"]),
    )
    by_kind = sorted(
        ({"kind": k, "sessions": sum(per.values()), "series": [per.get(i, 0) for i in range(n)]}
         for k, per in kinds.items()),
        key=lambda x: -x["sessions"],
    )
    total = sum(map(sum, cells))
    return {
        "observed_at": f"{today.isoformat()}T12:00:00.000Z",
        "from": start.isoformat(), "to": today.isoformat(), "bucket": bucket,
        "days": (today - max(start, first)).days + 1,
        "points": points, "totals": _derive(totals), "by_model": by_model, "by_repo": by_repo,
        "by_kind": by_kind,
        "activity": {"rows": list(WEEKDAYS), "cols": [f"{h:02d}" for h in range(24)], "cells": cells,
                     "max": max(map(max, cells)), "total": total},
    }


def _count(start: dt.date, end: dt.date, bucket: str) -> int:
    n, b = 0, _bucket_start(start, bucket)
    while b <= end:
        n += 1
        b = _next(b, bucket)
    return n


def empty_history_doc(today: dt.date) -> dict:
    """A backend with no rollup yet and no `from`: no range, no points."""
    return {
        "observed_at": f"{today.isoformat()}T12:00:00.000Z", "days": 0, "bucket": "day", "points": [],
        "totals": _derive(_zero()), "by_model": [], "by_repo": [], "by_kind": [],
        "activity": {"rows": list(WEEKDAYS), "cols": [f"{h:02d}" for h in range(24)],
                     "cells": [[0] * 24 for _ in WEEKDAYS], "max": 0, "total": 0},
    }
