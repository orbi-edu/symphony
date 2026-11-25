"""Daily rollups: the counters behind the telemetry history (`GET /v1/telemetry/history`).

One Redis hash per UTC day, `metrics:day:<yyyymmdd>` (`state.METRICS_DAY_PREFIX`), written
with HINCRBY at the moment each event happens and never expired. The monthly
`metrics:<yyyymm>` documents stay as they are; a day hash only adds detail. Every field is an
integer; money is micro-USD (`cost_micro_usd`), the engine's local estimate, not a bill.

| Field | Written by |
|---|---|
| `dispatched`, `rework` | a dispatch (`orchestrator`) |
| `runs`, `attempts` | a ticket finalized in a terminal state (`orchestrator`) |
| `prs_opened`, `prs_merged`, `time_to_merge_ms_sum`, `time_to_merge_count`, `learning_prs` | a retrospective (`reflector`) |
| `sessions:<kind>`, `outcome:<outcome>`, `hour:<0..167>`, `tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_write`, `cost_micro_usd`, `model:<id>:tokens`, `model:<id>:cost_micro_usd`, `repo:<owner/name>:sessions`, `repo:<owner/name>:cost_micro_usd` | a session that ends (`sessions.SessionStore.close`), in the day it ends |
| `merger:reviewed`, `merger:back_to_author`, `merger:merged`, `merger:needs_maintainer` | the merger (`merger.context.Merger.count`) |
| `questions:asked`, `questions:answered`, `questions:answered_via:<via>`, `questions:answer_ms_sum` | `questions.ask` and `questions.submit` |
| `blocked_on_person`, `trace_attached` | the runner |

`hour:<n>` is the hour of the week (Monday 00h UTC is 0) when the session STARTED; the
activity heatmap reads it. A day keeps at most `NAMED_LIMIT` models and repositories; the
next ones count as `model:other` / `repo:other`. `names:model` and `names:repo` count the
named ones.

Each bump carries a once-id (the session id, the question id, the dispatch): `metrics:once`
remembers it for `ONCE_KEEP_MS`, so a replayed event (a restarted process, a retried
retrospective, a session closed twice) is counted once. `metrics:days` indexes the days that
have a hash, so a read never scans the keyspace.

`backfill` rebuilds the session fields of past days from the ended sessions still in Redis
and from the archive objects (`backup.archive`); the other fields have no source but the live
bumps, so a backfill never touches them.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from . import state, usage

NAMED_LIMIT = 50
OTHER = "other"
ONCE_KEEP_MS = 35 * 24 * 3600 * 1000
MAX_POINTS = 120
BUCKETS = ("day", "week", "month")
ANCHOR = {"model": "tokens", "repo": "sessions"}  # always written with the name's first bump
TOKEN_FIELDS = ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write")
MERGER_RESULTS = ("reviewed", "back_to_author", "merged", "needs_maintainer")
VIAS = ("linear", "slack", "github", "symphony")  # `questions.VIA`
VIA_PREFIX = "questions:answered_via:"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SCALARS = (
    "dispatched", "runs", "attempts", "rework", "prs_opened", "prs_merged", "learning_prs",
    "time_to_merge_ms_sum", "time_to_merge_count", *TOKEN_FIELDS, "cost_micro_usd",
    "blocked_on_person", "trace_attached",
)
# What a backfill rebuilds: the fields a session writes when it ends.
SESSION_FIELDS = re.compile(r"(sessions|outcome|hour|model|repo|names):|tokens_|cost_micro_usd\Z")
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_NAME_CHARS = re.compile(r"[^A-Za-z0-9._/@+:-]")

_BUMP = """
if ARGV[2] ~= '' then
  if redis.call('ZADD', KEYS[3], 'NX', ARGV[3], ARGV[2]) == 0 then return 0 end
  redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', '(' .. (tonumber(ARGV[3]) - tonumber(ARGV[4])))
end
local limit = tonumber(ARGV[5])
local anchors = {model = 'tokens', repo = 'sessions'}
local admitted = {}
for i = 6, #ARGV, 2 do
  local field = ARGV[i]
  local family, name, metric = string.match(field, '^(%a+):(.+):([^:]+)$')
  if family and anchors[family] and name ~= 'other' then
    local tag = family .. ':' .. name
    if admitted[tag] == nil then
      if redis.call('HEXISTS', KEYS[1], tag .. ':' .. anchors[family]) == 1 then
        admitted[tag] = true
      elseif tonumber(redis.call('HGET', KEYS[1], 'names:' .. family) or '0') < limit then
        redis.call('HINCRBY', KEYS[1], 'names:' .. family, 1)
        admitted[tag] = true
      else
        admitted[tag] = false
      end
    end
    if not admitted[tag] then field = family .. ':other:' .. metric end
  end
  redis.call('HINCRBY', KEYS[1], field, ARGV[i + 1])
end
redis.call('ZADD', KEYS[2], ARGV[1], ARGV[1])
return 1
"""


# -- keys and names ------------------------------------------------------------------------
def day_key(day: str) -> str:
    return f"{state.METRICS_DAY_PREFIX}{day}"


def day_of(ms: int) -> str:
    """`yyyymmdd` of an epoch-ms instant, in UTC."""
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y%m%d")


def hour_of_week(ms: int) -> int:
    moment = dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
    return moment.weekday() * 24 + moment.hour


def token(value: Any, default: str = OTHER) -> str:
    """A kind, an outcome or a channel as a field part: a short lowercase word, else `other`."""
    text = str(value).strip().lower() if value is not None else ""
    if not text:
        return default
    return text if _TOKEN.fullmatch(text) else OTHER


def name(value: Any) -> str | None:
    """A model id or a repository as a field part: at most 100 safe characters."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _NAME_CHARS.sub("_", value.strip())[:100]


def micro_usd(cost: Any) -> int:
    try:
        return max(0, round(float(cost or 0) * 1_000_000))
    except (TypeError, ValueError):
        return 0


# -- writing -------------------------------------------------------------------------------
def _pairs(fields: Mapping[str, int] | Iterable[tuple[str, int]]) -> list[tuple[str, int]]:
    items = fields.items() if isinstance(fields, Mapping) else fields
    return [(str(field), int(by)) for field, by in items]


def apply(doc: dict[str, int], fields: Iterable[tuple[str, int]],
          limit: int = NAMED_LIMIT) -> dict[str, int]:
    """Add `fields` to the day document `doc` in place, exactly as the Redis script does:
    the in-memory fallback and the backfill use it, so they agree with the live bumps."""
    admitted: dict[str, bool] = {}
    for field, by in fields:
        family, _, rest = field.partition(":")
        key_name, _, metric = rest.rpartition(":")
        if family in ANCHOR and key_name and key_name != OTHER:
            tag = f"{family}:{key_name}"
            if tag not in admitted:
                if f"{tag}:{ANCHOR[family]}" in doc:
                    admitted[tag] = True
                elif doc.get(f"names:{family}", 0) < limit:
                    doc[f"names:{family}"] = doc.get(f"names:{family}", 0) + 1
                    admitted[tag] = True
                else:
                    admitted[tag] = False
            if not admitted[tag]:
                field = f"{family}:{OTHER}:{metric}"
        doc[field] = doc.get(field, 0) + by
    return doc


def bump(client, at_ms: int, fields: Mapping[str, int] | Iterable[tuple[str, int]], *,
         once: str | None = None) -> bool:
    """Add `fields` to the rollup of the day of `at_ms`, atomically. With `once`, only the
    first call with that id counts. Returns whether it counted."""
    pairs = _pairs(fields)
    if not pairs:
        return False
    day = day_of(at_ms)
    args: list[Any] = [day, once or "", at_ms, ONCE_KEEP_MS, NAMED_LIMIT]
    for field, by in pairs:
        args += [field, by]
    return bool(client.eval(_BUMP, 3, day_key(day), state.METRICS_DAYS_KEY,
                            state.METRICS_ONCE_KEY, *args))


def bump_kv(kv, at_ms: int, fields: Mapping[str, int] | Iterable[tuple[str, int]], *,
            once: str | None = None) -> bool:
    """`bump` through a `state.KV`: atomic in Redis (`RedisKV.bump_rollup`), sequential on
    an injected in-memory store."""
    atomic = getattr(kv, "bump_rollup", None)
    if atomic is not None:
        return atomic(at_ms, fields, once=once)
    pairs = _pairs(fields)
    if not pairs:
        return False
    if once:
        seen = dict(kv.get(state.METRICS_ONCE_KEY) or {})
        if once in seen:
            return False
        seen[once] = at_ms
        kv.put(state.METRICS_ONCE_KEY, seen)
    day = day_of(at_ms)
    kv.put(day_key(day), apply(dict(kv.get(day_key(day)) or {}), pairs))
    kv.put(state.METRICS_DAYS_KEY, sorted({*(kv.get(state.METRICS_DAYS_KEY) or []), day}))
    return True


# -- sessions ------------------------------------------------------------------------------
def message_usage(frame: Any) -> dict[str, int] | None:
    """The usage one engine `message_end` frame adds (`usage.usage_delta`), as rollup
    integers, or None when it carries none."""
    if not isinstance(frame, dict):
        return None
    delta = usage.usage_delta(frame)
    if not delta:
        return None
    out = {
        "input": int(delta.get("input") or 0),
        "output": int(delta.get("output") or 0),
        "cache_read": int(delta.get("cache_read") or 0),
        "cache_write": int(delta.get("cache_write") or 0),
        "cost_micro_usd": micro_usd(delta.get("cost_estimate")),
    }
    return out if any(out.values()) else None


USAGE_KEYS = ("input", "output", "cache_read", "cache_write", "cost_micro_usd")


def add_usage(total: dict[str, int], delta: Mapping[str, int] | None) -> dict[str, int]:
    for key in USAGE_KEYS:
        total[key] = total.get(key, 0) + int((delta or {}).get(key) or 0)
    return total


def session_usage(meta: Mapping, events_usage: Mapping[str, int] | None = None) -> dict[str, int]:
    """A session's usage: `meta.usage` (written when it ends), else the sum of its
    `message_end` events, raised to the run ledger's `meta.tokens` where that is higher (an
    attempt that ended before the usage was kept, its event stream trimmed)."""
    kept = meta.get("usage")
    if isinstance(kept, dict):
        return {key: int(kept.get(key) or 0) for key in USAGE_KEYS}
    out = add_usage({}, events_usage)
    tokens = meta.get("tokens") if isinstance(meta.get("tokens"), dict) else {}
    out["input"] = max(out["input"], int(tokens.get("input") or 0))
    out["output"] = max(out["output"], int(tokens.get("output") or 0))
    out["cost_micro_usd"] = max(out["cost_micro_usd"], micro_usd(tokens.get("cost")))
    return out


def session_fields(meta: Mapping, events_usage: Mapping[str, int] | None = None,
                   ) -> tuple[int, list[tuple[str, int]]] | None:
    """The day instant (when it ended) and the fields one ended session adds, or None for a
    session that has not ended."""
    ended = meta.get("ended_at") or (meta.get("updated_at") if meta.get("state") == "ended" else None)
    if not isinstance(ended, int):
        return None
    started = meta.get("created_at") if isinstance(meta.get("created_at"), int) else ended
    used = session_usage(meta, events_usage)
    tokens = used["input"] + used["output"] + used["cache_read"] + used["cache_write"]
    cost = used["cost_micro_usd"]
    fields = [
        (f"sessions:{token(meta.get('kind'), 'implement')}", 1),
        (f"outcome:{token(meta.get('outcome'), 'unknown')}", 1),
        (f"hour:{hour_of_week(started)}", 1),
    ]
    for field, key in zip(TOKEN_FIELDS, ("input", "output", "cache_read", "cache_write"),
                          strict=True):
        if used[key]:
            fields.append((field, used[key]))
    if cost:
        fields.append(("cost_micro_usd", cost))
    if model := name(meta.get("model")):
        fields += [(f"model:{model}:tokens", tokens), (f"model:{model}:cost_micro_usd", cost)]
    if repo := name(meta.get("repo")):
        fields += [(f"repo:{repo}:sessions", 1), (f"repo:{repo}:cost_micro_usd", cost)]
    return ended, fields


def record_session(client, meta: Mapping) -> bool:
    """Count an ended session in the day it ended, once per session id."""
    found = session_fields(meta)
    if found is None or not meta.get("sid"):
        return False
    at_ms, fields = found
    return bump(client, at_ms, fields, once=f"session:{meta['sid']}")


# -- backfill ------------------------------------------------------------------------------
def _redis_sessions(client, namespace: str) -> Iterator[tuple[str, dict, dict | None]]:
    from .backup import _ended_sessions

    for sid, meta in _ended_sessions(client, namespace):
        events = None
        if not isinstance(meta.get("usage"), dict):
            events = {}
            after = "-"
            key = f"sym:{namespace}:session:{sid}:events"
            while True:
                rows = client.xrange(key, min=after, count=500)
                for _id, row in rows:
                    if row.get("kind") == "message_end":
                        try:
                            add_usage(events, message_usage(json.loads(row.get("data") or "null")))
                        except ValueError:
                            pass
                if len(rows) < 500:
                    break
                after = f"({rows[-1][0]}"
        yield sid, meta, events


def _archive_sessions(lines: Iterable[bytes]) -> Iterator[tuple[str, dict, dict]]:
    """Sessions of one archive object, streamed: a `session` line, then its events."""
    current: tuple[str, dict, dict] | None = None
    for raw in lines:
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        kind = doc.get("kind")
        if kind == "session" and isinstance(doc.get("meta"), dict):
            if current is not None:
                yield current
            current = (str(doc.get("sid") or doc["meta"].get("sid") or ""), doc["meta"], {})
        elif (kind == "event" and current is not None and doc.get("sid") == current[0]
              and doc.get("event") == "message_end"):
            add_usage(current[2], message_usage(doc.get("data")))
    if current is not None:
        yield current


def _archive_objects(target) -> Iterator[Iterator[tuple[str, dict, dict]]]:
    from .backup import ARCHIVE

    for item in sorted(target.list(ARCHIVE), key=lambda i: i.get("name") or ""):
        with tempfile.TemporaryFile() as spool:
            target.get(ARCHIVE, item["name"], spool)
            spool.seek(0)
            with gzip.GzipFile(fileobj=spool, mode="rb") as lines:
                yield _archive_sessions(lines)


def backfill(client, namespace: str, target=None, *, force: bool = False,
             now_ms: int | None = None) -> dict:
    """Rebuild the session fields of the days that have no rollup yet (every day the sources
    cover, with `force`) from the archive objects on `target` (streamed, oldest first) and
    the ended sessions still in Redis. One session counts once, as its latest copy. The other
    fields of a day are left as they are, so running it twice gives the same numbers."""
    latest: dict[str, tuple[int, list[tuple[str, int]]]] = {}
    objects = 0

    def take(sid: str, meta: dict, events: dict | None) -> None:
        found = session_fields(meta, events)
        if sid and found is not None and found[0] >= latest.get(sid, (0, []))[0]:
            latest[sid] = found

    if target is not None:
        for sessions in _archive_objects(target):
            objects += 1
            for sid, meta, events in sessions:
                take(sid, meta, events)
    for sid, meta, events in _redis_sessions(client, namespace):
        take(sid, meta, events)

    days: dict[str, list[tuple[int, str, list[tuple[str, int]]]]] = {}
    for sid, (at_ms, fields) in latest.items():
        days.setdefault(day_of(at_ms), []).append((at_ms, sid, fields))
    now = int(dt.datetime.now(dt.UTC).timestamp() * 1000) if now_ms is None else now_ms
    rebuilt = kept = counted = 0
    for day, rows in sorted(days.items()):
        key = day_key(day)
        if not force and client.exists(key):
            kept += 1
            continue
        doc: dict[str, int] = {}
        for _at, _sid, fields in sorted(rows):
            apply(doc, fields)
        stale = [f for f in client.hkeys(key) if SESSION_FIELDS.match(f)]
        pipe = client.pipeline(transaction=True)
        if stale:
            pipe.hdel(key, *stale)
        pipe.hset(key, mapping=doc)
        pipe.zadd(state.METRICS_DAYS_KEY, {day: int(day)})
        pipe.zadd(state.METRICS_ONCE_KEY, {f"session:{sid}": now for _at, sid, _f in rows})
        pipe.execute()
        rebuilt += 1
        counted += len(rows)
    return {"days": rebuilt, "kept": kept, "sessions": counted, "archive_objects": objects,
            "force": force}


# -- reading -------------------------------------------------------------------------------
def _date(day: str) -> dt.date:
    return dt.date(int(day[:4]), int(day[4:6]), int(day[6:8]))


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


def _starts(first: dt.date, last: dt.date, bucket: str) -> list[dt.date]:
    out, at = [], _bucket_start(first, bucket)
    while at <= last:
        out.append(at)
        at = _next(at, bucket)
    return out


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(total: int, count: int) -> int | None:
    return round(total / count) if count else None


def _point(raw: Mapping[str, int]) -> dict:
    """The public shape of a bucket (or of the totals) from its summed fields."""
    out: dict[str, Any] = {field: int(raw.get(field, 0)) for field in SCALARS}
    out["tokens"] = sum(out[field] for field in TOKEN_FIELDS)
    out["cost_usd"] = out["cost_micro_usd"] / 1_000_000
    out["rework_rate"] = _ratio(out["rework"], out["dispatched"])
    out["mean_time_to_merge_ms"] = _mean(out["time_to_merge_ms_sum"], out["time_to_merge_count"])
    kinds = {f[9:]: v for f, v in raw.items() if f.startswith("sessions:") and v}
    out["sessions"] = sum(kinds.values())
    out["sessions_by_kind"] = dict(sorted(kinds.items()))
    out["outcomes"] = dict(sorted((f[8:], v) for f, v in raw.items()
                                  if f.startswith("outcome:") and v))
    out["merger"] = {result: int(raw.get(f"merger:{result}", 0)) for result in MERGER_RESULTS}
    out["questions_asked"] = int(raw.get("questions:asked", 0))
    out["questions_answered"] = int(raw.get("questions:answered", 0))
    via = {v: int(raw.get(f"{VIA_PREFIX}{v}", 0)) for v in VIAS}
    via.update((f[len(VIA_PREFIX):], v) for f, v in raw.items()
               if f.startswith(VIA_PREFIX) and f[len(VIA_PREFIX):] not in via and v)
    out["answered_via"] = via
    out["questions_answer_ms_sum"] = int(raw.get("questions:answer_ms_sum", 0))
    out["mean_answer_ms"] = _mean(out["questions_answer_ms_sum"], out["questions_answered"])
    return out


def _add(into: dict[str, int], raw: Mapping[str, int]) -> None:
    for field, value in raw.items():
        if not field.startswith("names:"):
            into[field] = into.get(field, 0) + value


def _named(buckets: list[dict[str, int]], family: str) -> dict[str, dict[str, list[int]]]:
    """`{name: {metric: [value per bucket]}}` of the `model:` or `repo:` fields."""
    out: dict[str, dict[str, list[int]]] = {}
    for index, raw in enumerate(buckets):
        for field, value in raw.items():
            if not field.startswith(f"{family}:"):
                continue
            key_name, _, metric = field[len(family) + 1:].rpartition(":")
            if key_name:
                series = out.setdefault(key_name, {}).setdefault(metric, [0] * len(buckets))
                series[index] += value
    return out


def read_days(client, first: str, last: str) -> dict[str, dict[str, int]]:
    """The rollups of the days in [first, last] that have one: one pipeline of HGETALL."""
    days = client.zrangebyscore(state.METRICS_DAYS_KEY, int(first), int(last))
    pipe = client.pipeline(transaction=False)
    for day in days:
        pipe.hgetall(day_key(day))
    out = {}
    for day, raw in zip(days, pipe.execute(), strict=True):
        if raw:
            out[day] = {field: int(value) for field, value in raw.items()}
    return out


def first_day(client) -> str | None:
    found = client.zrange(state.METRICS_DAYS_KEY, 0, 0)
    return found[0] if found else None


def choose_bucket(first: dt.date, last: dt.date, wanted: str | None) -> str:
    """The requested bucket (finest when none) coarsened until it gives at most MAX_POINTS."""
    for bucket in BUCKETS[BUCKETS.index(wanted) if wanted else 0:]:
        if len(_starts(first, last, bucket)) <= MAX_POINTS:
            return bucket
    return "month"


def history(client, *, first: dt.date | None, last: dt.date, bucket: str | None,
            observed_at: str) -> dict:
    """The document of `GET /v1/telemetry/history` (docs/api.md)."""
    if first is None:
        found = first_day(client)
        first = min(_date(found), last) if found else None
    if first is None:
        return _document(observed_at, None, None, bucket or "day", [], [], {})
    chosen = choose_bucket(first, last, bucket)
    starts = _starts(first, last, chosen)[-MAX_POINTS:]
    first = max(first, starts[0])
    days = read_days(client, first.strftime("%Y%m%d"), last.strftime("%Y%m%d"))
    index = {start: i for i, start in enumerate(starts)}
    buckets: list[dict[str, int]] = [{} for _ in starts]
    for day, raw in days.items():
        _add(buckets[index[_bucket_start(_date(day), chosen)]], raw)
    return _document(observed_at, first, last, chosen, starts, buckets, days)


def _document(observed_at: str, first: dt.date | None, last: dt.date | None, bucket: str,
              starts: list[dt.date], buckets: list[dict[str, int]],
              days: Mapping[str, Mapping[str, int]]) -> dict:
    totals: dict[str, int] = {}
    for raw in buckets:
        _add(totals, raw)
    points = []
    for start, raw in zip(starts, buckets, strict=True):
        end = min(_next(start, bucket) - dt.timedelta(days=1), last) if last else start
        points.append({"start": max(start, first).isoformat() if first else start.isoformat(),
                       "end": end.isoformat(), **_point(raw)})
    models = _named(buckets, "model")
    by_model = sorted((
        {"model": model, "cost_micro_usd": sum(m.get("cost_micro_usd", [])),
         "cost_usd": sum(m.get("cost_micro_usd", [])) / 1_000_000,
         "tokens": sum(m.get("tokens", [])),
         "cost_series": [v / 1_000_000 for v in m.get("cost_micro_usd", [0] * len(buckets))]}
        for model, m in models.items()), key=lambda r: (-r["cost_micro_usd"], -r["tokens"], r["model"]))
    repos = _named(buckets, "repo")
    by_repo = sorted((
        {"repo": repo, "sessions": sum(r.get("sessions", [])),
         "cost_micro_usd": sum(r.get("cost_micro_usd", [])),
         "cost_usd": sum(r.get("cost_micro_usd", [])) / 1_000_000}
        for repo, r in repos.items()), key=lambda r: (-r["cost_micro_usd"], -r["sessions"], r["repo"]))
    kinds = sorted({f[9:] for raw in buckets for f in raw if f.startswith("sessions:")})
    by_kind = sorted((
        {"kind": kind, "sessions": int(totals.get(f"sessions:{kind}", 0)),
         "series": [int(raw.get(f"sessions:{kind}", 0)) for raw in buckets]}
        for kind in kinds), key=lambda r: (-r["sessions"], r["kind"]))
    cells = [[int(totals.get(f"hour:{d * 24 + h}", 0)) for h in range(24)] for d in range(7)]
    return {
        "observed_at": observed_at,
        "from": first.isoformat() if first else None,
        "to": last.isoformat() if last else None,
        "bucket": bucket,
        "days": len(days),
        "points": points,
        "totals": _point(totals),
        "by_model": by_model,
        "by_repo": by_repo,
        "by_kind": by_kind,
        "activity": {"rows": list(WEEKDAYS), "cols": [f"{h:02d}" for h in range(24)],
                     "cells": cells, "max": max(max(row) for row in cells),
                     "total": sum(map(sum, cells))},
    }
