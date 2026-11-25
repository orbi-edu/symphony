# Backup, restore and archive

Symphony keeps its state in Redis. This page tells you how to back up that state, how to
restore it, and how the archive keeps ended sessions and old audit entries after Redis drops
them.

## What a backup holds

A backup holds every key of one namespace under `sym:<ns>:` and `admin:<ns>:`:

- the sessions (metadata, events, transcript, intents) and the session index;
- the API tokens (only the SHA-256 of each secret), the team configuration and its history;
- the durable commands and the audit log;
- the merger state, the MCP call counters (`sym:<ns>:mcp:calls`), the Prometheus counters
  (`sym:<ns>:metrics:counters`) and the setup records.

The `state` keys have no namespace in their name. Symphony runs one Redis per namespace
(`deploy/redis.yaml` is part of the namespaced kustomization), so they belong to the
namespace of the backup header. `backup.CORE_KEPT` and `backup.CORE_SKIPPED` classify every
family, each with its reason.

Restored, because the tracker cannot rebuild them:

| Family | Why |
|---|---|
| `hold:*` | A person's hold never expires. Without it, the daemon would take back a ticket that a person stopped |
| `generation:*` | Dispatch fencing tombstones. Without one, an old Job could address a reopened ticket |
| `metrics:<yyyymm>` | The monthly counters that feed telemetry |
| `metrics:day:<yyyymmdd>`, `metrics:days` | The daily rollups behind the telemetry history, and their index. Only their session fields can be rebuilt (`symphony telemetry backfill`); dispatches, pull requests, questions and merger results have no other source |
| `metrics:once` | The events already counted in the daily rollups. Without it, a replayed event could count twice |
| `retro:*` | Retrospective tokens. Without one, the nightly sweep repeats a retrospective and its learnings pull request |
| `lessons:usage_age` | The decay counters of learnings |
| `totals:tokens` | The token ledger totals |
| `answered:*` | Pull request comments that answered a question. Without one, the review watch acts again on a comment that already answered |
| `abandoned:*` | Canceled-ticket markers. Without one, that ticket's questions stay open until their reminder check closes them |

Not in the backup:

| Family | Why |
|---|---|
| `sym:<ns>:ratelimit:*` | API rate limit counters; they live one minute |
| `sym:<ns>:assistant:evidence:*` | Files the assistant shared that wait for an "Attach" click; they live one hour |
| `issue:*` | Ticket tracking; reconcile rebuilds it from the tracker |
| `dispatch:*`, `toolcall:*`, `subissue:*`, `telemetry:*` | Records of live runs. A restore leaves the live ones in place; old ones would be stale |
| `lock:orchestrator` | The daemon lease. A restored lease would block the live daemon |
| `orchestrator:heartbeat`, `daemon:setup`, `refresh:requested`, `ratelimits:engine`, `poll:backoff` | The daemon writes them again on its next ticks |

A backup never holds a credential. Credentials live in Kubernetes Secrets (or `secrets.env`
in local mode), never in Redis.

## The file format

A backup file is gzip of newline-delimited JSON (format 1). It is written as a stream.

| Line | Content |
|---|---|
| First | The header: `{format: 1, namespace, symphony_version, created_at, keys}`. `keys` is the number of keys that SCAN found |
| One per key | `{key, type, ttl_ms, dump}`. `dump` is the base64 of Redis `DUMP`. `ttl_ms` is the remaining time to live, or `null` for a key that never expires |
| Last | The trailer: `{sha256, records}`. `sha256` is the hex SHA-256 of every line before the trailer, the header included, newlines included |

A key that expired between SCAN and DUMP has no record, so `records` can be lower than
`keys`. The export holds only the key names in memory. It reads the values in batches of 200
keys.

A restore needs a Redis that reads the `DUMP` payloads of the Redis that made the backup: the
same Redis version or a newer one.

## Configure the target

The nightly backup and the archive need a target in `WORKFLOW.md`: an S3 bucket or a
directory. Without one, the `symphony-backup` CronJob idles (see below).

```yaml
backup:
  keep: 7                      # backups to keep (1-365)
  s3:
    endpoint: https://s3.eu-west-1.amazonaws.com
    bucket: acme-symphony-backups
    prefix: symphony/          # optional
    region: eu-west-1          # default us-east-1
retention:
  sessions_days: 7             # ended sessions in Redis (2-365)
  audit_days: 30               # finished commands and audit entries in Redis (2-3650)
```

- `backup.s3` works with any S3-compatible store that accepts path-style URLs
  (`<endpoint>/<bucket>/<key>`): AWS S3, MinIO, Ceph, Cloudflare R2. Symphony signs each
  request with AWS Signature Version 4.
- `backup.dir` is an absolute directory instead of `s3`. Use it in local mode, or in the
  cluster on a volume that you mount yourself.
- The access keys never go into `WORKFLOW.md`. Put them in the `symphony-backup` Secret:

  ```sh
  kubectl -n symphony create secret generic symphony-backup --dry-run=client -o yaml \
    --from-literal=SYMPHONY_BACKUP_ACCESS_KEY_ID=… \
    --from-literal=SYMPHONY_BACKUP_SECRET_ACCESS_KEY=… | kubectl replace -f -
  kubectl -n symphony rollout restart deploy/symphony-api
  ```

  Give the keys only `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` and `s3:DeleteObject`
  on the prefix. In local mode, set the two variables in the environment of
  `symphony.entry.local`.

Objects on the target:

| Object | Content |
|---|---|
| `<prefix><ns>/backups/<YYYYMMDDTHHMMSS.mmmZ>.ndjson.gz` | One backup. The CronJob keeps the newest `backup.keep` |
| `<prefix><ns>/archive/<YYYYMMDDTHHMMSS.mmmZ>.jsonl.gz` | One archive run (see below). The archive is never pruned |

## The nightly CronJob

`deploy/backup.yaml` runs `python -m symphony.entry.backup` at 02:15 each night. Each run:

1. archives the ended sessions and the audit entries that the archive has not stored yet;
2. writes a backup;
3. deletes the oldest backups beyond `backup.keep`.

Without a `WORKFLOW.md` or a target, the run logs `backup_not_configured` with the reason and
succeeds. `symphony backup status` shows the reason. The CronJob has a ServiceAccount with no
token and no Role: it reads Redis, the mounted `WORKFLOW.md` and the `symphony-backup` Secret.

Run it now with `symphony backup run` (the API does the same steps).

## The archive

Redis drops an ended session `retention.sessions_days` after it ends. It drops finished
commands and audit entries after `retention.audit_days`. The archive keeps them first.

Each run appends one JSONL object (gzip) with these lines:

| `kind` | Fields |
|---|---|
| `session` | `sid`, `meta`: an ended session that the archive has not stored, or that ended again |
| `event` | `sid`, `id`, `event`, `data`, `t`: one event of that session |
| `transcript` | `sid`, `index`, `entry`: one transcript entry of that session |
| `audit` | `event`: one audit entry written since the last run (and at least 5 minutes old) |

The archive stores every audit entry, so it holds the entries that Redis later drops. A run
that fails stores nothing and moves no marker: the next run repeats those entries. An entry
can thus appear twice, never zero times. The shortest retention is 2 days, so a daily run
always sees an entry before Redis drops it.

The daily rollups (`metrics:day:<yyyymmdd>`) never expire, but a day can lack sessions: one
that ended before the rollups existed, or a namespace restored from an old backup.
`symphony telemetry backfill` reads the session lines of every archive object, one at a time,
and the ended sessions still in Redis, and rebuilds the session fields of the days that have
no rollup (`--force`: every day they cover). See [api.md](api.md#telemetry-history).

```sh
symphony backup archive list
symphony backup archive get 20261116T021500.123Z.jsonl.gz | jq 'select(.kind == "session") | .meta'
```

## Download and upload a backup

```sh
symphony backup export                       # ./symphony-<ns>-<time>.ndjson.gz
symphony backup export -o /safe/place/prod.ndjson.gz
symphony backup list                         # backups on the target
```

`export` checks the whole file (format, checksum, counts) before it keeps it. The export is
audited (`backup.export`). The file holds session transcripts and the audit log: store it as
carefully as the Redis data.

## Restore runbook

A restore replaces keys. Read all the steps before you start.

1. **Find the backup.** `symphony backup list`, or a file from `symphony backup export`.
2. **Stop the work.** The restore refuses while the daemon holds its lease and is not paused.
   Pause it: `symphony pause --reason "restore from backup"`. Wait until `symphony status`
   shows that admission is paused. Or scale the daemon to zero:
   `kubectl -n symphony scale deploy/symphony-daemon --replicas=0`, and wait 60 s for its
   lease to expire. In local mode, pause: the same process serves the API and the tick.
3. **Stop the other writers.** Scale the mergers and the assistant to zero, so that they do
   not write during the restore:
   `kubectl -n symphony scale deploy/symphony-merger deploy/symphony-assistant --replicas=0`.
4. **Optional: start from an empty namespace.** The restore replaces the keys of the backup
   and leaves other keys alone. For an exact copy, delete the namespace keys first, for
   example `redis-cli --scan --pattern 'sym:<ns>:*' | xargs redis-cli del` (and the same for
   `admin:<ns>:*`, but keep `admin:<ns>:admission`).
5. **Restore.**

   ```sh
   symphony backup restore --object 20261116T021500.123Z.ndjson.gz --confirm
   symphony backup restore ./prod.ndjson.gz --confirm
   ```

   The restore first reads the whole file and checks the format, the SHA-256, the counts and
   that each key is under the namespace prefixes or in a restored `state` family. If one
   check fails, nothing is written. Then it runs `RESTORE … REPLACE` with the TTL of each key
   and prints the counts. Holds, generations and the monthly metrics come back with the rest.
6. **Another namespace.** A backup of namespace `prod` restores into `staging` only with
   `--namespace-from prod`. The keys move from `sym:prod:` to `sym:staging:` (and `admin:`).
   The `state` families keep their names: restore into the Redis of the target namespace only.
7. **Check.** `symphony status`, `symphony sessions`, `symphony audit` and
   `symphony backup status` (the `restore` row has the counts).
8. **Resume.** The restore never changes the pause flag. Run `symphony resume`, or scale the
   daemon, the mergers and the assistant back up. Reconcile rebuilds the ticket tracking from
   the tracker on its first tick.

The restore is audited (`backup.restore`) after it finishes. The API tokens come back as they
were in the backup: a token made after the backup no longer works.

## API and CLI

| CLI | API (admin) | Audit |
|---|---|---|
| `symphony backup status` | `GET /v1/backup` | — |
| `symphony backup export [-o FILE]` | `GET /v1/backup/export` | `backup.export` |
| `symphony backup restore FILE --confirm [--namespace-from NS]` | `POST /v1/backup/restore?confirm=true[&namespace_from=NS]`, body: the file | `backup.restore` |
| `symphony backup restore --object NAME --confirm` | `POST /v1/backup/backups/{name}/restore?confirm=true` | `backup.restore` |
| `symphony backup run` | `POST /v1/backup/run` | `backup.run` |
| `symphony backup list` | `GET /v1/backup/backups` | — |
| `symphony backup archive list` | `GET /v1/backup/archive` | — |
| `symphony backup archive get NAME [-o FILE]` | `GET /v1/backup/archive/{name}` | `backup.archive.get` |

[api.md](api.md#backup) has the errors. The code is in `src/symphony/backup.py`,
`src/symphony/s3.py` and `src/symphony/api/backup.py`.
