# Observability

Symphony exposes Prometheus metrics. This page lists every metric, tells you how to scrape
them, and gives example alerts.

## Where the metrics are

The daemon, each merger, the assistant and the gateway serve two routes on their health port,
`SYMPHONY_HEALTH_PORT` (8081):

- `GET /healthz`: `200 ok` or `503 stale` (the liveness probe).
- `GET /metrics`: the metrics in the Prometheus text format 0.0.4
  (`Content-Type: text/plain; version=0.0.4; charset=utf-8`).

The API serves no metrics. Its only port (8080) is public, and `/metrics` there answers 404.
The gateway's health port is not in the `symphony-gateway` Service, so workspaces never see it.

Each component exposes only the metrics that it owns, so no series appears twice:

| Component | Metrics | Source |
|---|---|---|
| Every component | `symphony_build_info`, `symphony_metrics_up` (not the merger) | Process |
| Daemon | Tickets, attempts, retries, sessions, rate limits, tokens, cost, setup, the merger of each repository | Redis. Only the daemon that holds the lease exposes them (`symphony_daemon_leader`), so a rolling update does not count twice |
| Gateway | MCP calls, MCP cleanup, held MCP leases | Redis |
| Assistant | Threads and transports | Process state (no Redis read) |
| Merger | `symphony_build_info` only; the daemon exposes the merger of every repository | — |

## Scrape them

`deploy/monitoring/` is optional and is not part of the default `deploy/` kustomization. It
needs the Prometheus Operator (the `ServiceMonitor` CRD).

```sh
kubectl label namespace monitoring symphony.dev/monitoring=true   # the namespace of Prometheus
kubectl apply -k deploy/monitoring
```

It has three parts:

- Four headless Services (`symphony-<component>-metrics`, port `metrics` → the container port
  `health`, 8081) with the label `symphony.dev/metrics: "true"`.
- The ServiceMonitor `symphony`: every 30 s, `GET /metrics`, with the Service label
  `component` as a target label.
- The NetworkPolicy `symphony-metrics`: a namespace labeled `symphony.dev/monitoring: "true"`
  may reach port 8081 of the daemon, the mergers, the assistant and the gateway. Nothing else
  changes: the other policies in `deploy/networkpolicy.yaml` still apply.

Your Prometheus must select the ServiceMonitor (`serviceMonitorSelector` and
`serviceMonitorNamespaceSelector`). Add the labels that it expects in an overlay.

Without the Prometheus Operator, scrape the pods directly: port 8081, path `/metrics`, pods
with `app=symphony` and `component` in `daemon`, `merger`, `assistant` or `gateway`.

## Cost of a scrape

- A component caches its rendered metrics for 10 s (`metrics.CACHE_S`). A faster scrape
  interval reads Redis at most once in 10 s.
- The daemon makes one SCAN pass for the `issue:` and `hold:` keys (at most 50 pages of
  1000 keys; `symphony_metrics_truncated` is 1 when it stops early) and two pipelines. It reads
  the 500 most recently updated sessions and at most 50 repositories.
- The gateway makes two pipelines. It reads at most 500 tokens that hold MCP ids.
- The assistant reads only its process state.
- A failed read still answers: `symphony_metrics_up` is 0, and `symphony_build_info` is there.

## Label values are bounded

No label holds a ticket id, a session id, a token or a credential.

- Labels with a known set (`state`, `outcome`, `reason`, `kind`, `type`, `step`, `status`,
  `result`, `transport`) map an unknown value to `other`.
- Free labels (`provider`, `stage`, `server`, `role`) keep at most 20 values each, sorted by
  name. The other values become `other`. `model` keeps at most 8 values. `repo` lists at
  most 50 registered repositories.

## Metrics

### Every component

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `symphony_build_info` | gauge | `component`, `version`, `commit` | Always 1. The version comes from the package, the commit from `SYMPHONY_COMMIT` (`unknown` when unset). |
| `symphony_metrics_up` | gauge | — | 1 when the scrape read its source (Redis or process state), 0 when the read failed. Not on the merger, which reads nothing. |

### Daemon

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `symphony_daemon_leader` | gauge | — | 1 when this daemon holds the lease and exposes the store metrics. A standby daemon exposes 0 and no store metric. |
| `symphony_daemon_heartbeat_timestamp_seconds` | gauge | — | The last daemon heartbeat (`orchestrator:heartbeat`), as a Unix time. |
| `symphony_tickets` | gauge | `state` | Tickets that the daemon tracks (`issue:*`), by tracking state: `claimed`, `running`, `interrupting`, `retry_queued`, `blocked`, `parked`, `other`. |
| `symphony_attempts` | gauge | `state` | Attempts by state: `running` (running or interrupting), `queued` (claimed or waiting for a retry), `held` (tickets a person holds, `hold:*`). A held ticket counts only as `held`. |
| `symphony_metrics_truncated` | gauge | — | 1 when the ticket scan stopped at its page limit, so the ticket counts are partial. |
| `symphony_attempts_finished_total` | counter | `outcome` | Implementation and answer attempts that ended, by outcome: `done`, `continuation`, `failed`, `blocked`, `fenced`, `error`, `other`. Counted when the session closes. |
| `symphony_retries_total` | counter | `reason` | Redispatches of a ticket: `failure` (after a failed attempt) or `continuation` (after a clean exit). It is a field of the monthly counters (`metrics:<yyyymm>`), so it restarts at the start of each UTC month; `rate()` and `increase()` handle the reset. |
| `symphony_sessions` | gauge | `kind`, `state` | Sessions that have not ended, by kind (`implement`, `answer`, `review`, `oracle`, `arbiter`, `resolve`, `reflect`, `assist`) and state (`starting`, `running`, `waiting_input`, `idle`). Only the 500 most recently updated sessions count. |
| `symphony_rate_limit_events_total` | counter | `provider`, `stage` | Provider rate-limit observations that the runner and the merger agents recorded (internals §10): `throttled` for a retry after a 429, else the usage-limit stage. |
| `symphony_tokens_total` | counter | `model`, `type` | Tokens that implementation attempts used, by model (`provider/model`) and type (`input`, `output`, `cache_read`, `cache_write`). |
| `symphony_cost_usd_total` | counter | `model` | The estimated cost of implementation attempts in US dollars. The engine computes it from its bundled price table; it is not provider billing. |
| `symphony_daemon_setup_ready` | gauge | — | 1 when the daemon has a valid WORKFLOW and a usable tracker (`daemon:setup`), 0 when setup is incomplete. |
| `symphony_setup_step` | gauge | `step`, `status` | 1 for the current status (`done`, `todo`, `error`, `skipped`) of each setup step, 0 for the others, in the last status that the API computed (`GET /v1/setup`). |
| `symphony_setup_complete` | gauge | — | 1 when every required setup step was done in that status. |
| `symphony_setup_checked_timestamp_seconds` | gauge | — | When the API last computed the setup status, as a Unix time. |
| `symphony_merger_queue` | gauge | `repo` | Pull requests in the review queue of the merger, as its last pick saw them. |
| `symphony_merger_reviewing` | gauge | `repo` | Pull requests under review. |
| `symphony_merger_train` | gauge | `repo` | Pull requests in the merge train (`train`, `resolve` or `finalizing`). |
| `symphony_merger_parked` | gauge | `repo` | Reviews that wait for the head CI. |
| `symphony_merger_heartbeat_up` | gauge | `repo` | 1 while the merger of the repository beats (the heartbeat has a TTL of 90 s), for every registered repository. |

### Gateway

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `symphony_mcp_calls_total` | counter | `server`, `role` | MCP requests through the gateway, by server and agent role. |
| `symphony_mcp_cleanup_total` | counter | `result` | Held MCP ids that a cleanup released (`released`), or whose release failed (`failed`). The gateway sweep and every agent's end (`McpLaunch.end`) count. |
| `symphony_mcp_held_leases` | gauge | `server` | MCP ids (such as Seiji leases) that agent sessions still hold, by server. |

### Assistant

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `symphony_assistant_threads` | gauge | `state` | Assistant threads: `open` (with an omp session), `active` (a turn runs), `queued` (turns that wait for a slot). |
| `symphony_assistant_transport_connected` | gauge | `transport` | 1 while a configured chat transport (`slack`, `discord`) is connected. An unconfigured transport has no series. |

## Where the counters come from

| Counter | Writer | Store |
|---|---|---|
| `symphony_attempts_finished_total` | `SessionStore.close` for the kinds `implement` and `answer` | `sym:<ns>:metrics:counters`, field `attempts_finished\|<outcome>` |
| `symphony_rate_limit_events_total` | `usage.record_rate_limit` (runner), `merger.agents.record_rate_limit` | field `rate_limit_events\|<provider>\|<stage>` |
| `symphony_tokens_total`, `symphony_cost_usd_total` | The runner, each time it flushes usage | fields `tokens\|<model>\|<type>` and `cost\|<model>` |
| `symphony_mcp_cleanup_total` | `mcp_servers.release_held` | field `mcp_cleanup\|<result>` |
| `symphony_retries_total` | The daemon, at each redispatch | `metrics:<yyyymm>`, field `retries` |
| `symphony_mcp_calls_total` | The gateway | `sym:<ns>:mcp:calls` |

Every counter field is written with `HINCRBY` (or `HINCRBYFLOAT` for the cost). Thus
concurrent runners never lose an update.

## The history without Prometheus

Prometheus keeps what it scraped for its own retention. Symphony also keeps a daily rollup
per UTC day in Redis (`metrics:day:<yyyymmdd>`), which never expires: cost, tokens, sessions
by kind and outcome, models, repositories, pull requests, rework, merger results, questions
and the hour each session started. The Tern Telemetry and Dashboard views chart it, and
`GET /v1/telemetry/history` serves it ([api.md](api.md#telemetry-history)). It is not a
Prometheus series: alert on the counters above.

## Example alerts

These are Prometheus rules. Adjust the thresholds to your load.

```yaml
groups:
  - name: symphony
    rules:
      - alert: SymphonyDaemonDown
        # No daemon answers, none holds the lease, or the heartbeat is old.
        expr: |
          absent(up{component="daemon"} == 1)
            or max(symphony_daemon_leader) < 1
            or time() - max(symphony_daemon_heartbeat_timestamp_seconds) > 120
        for: 2m
        annotations:
          summary: The Symphony daemon is down or does not beat.

      - alert: SymphonyMergerHeartbeatMissing
        expr: symphony_merger_heartbeat_up == 0
        for: 5m
        annotations:
          summary: "The merger of {{ $labels.repo }} does not beat."

      - alert: SymphonyAttemptsFailing
        # More than half of the attempts of the last hour failed, and at least three did.
        expr: |
          sum(increase(symphony_attempts_finished_total{outcome=~"failed|error"}[1h]))
            / clamp_min(sum(increase(symphony_attempts_finished_total[1h])), 1) > 0.5
          and sum(increase(symphony_attempts_finished_total{outcome=~"failed|error"}[1h])) >= 3
        annotations:
          summary: Most Symphony attempts fail.

      - alert: SymphonyRateLimited
        expr: sum by (provider) (increase(symphony_rate_limit_events_total{stage="throttled"}[15m])) > 5
        annotations:
          summary: "The provider {{ $labels.provider }} throttles Symphony agents."

      - alert: SymphonySetupIncomplete
        expr: symphony_daemon_setup_ready == 0 or symphony_setup_complete == 0
        for: 30m
        annotations:
          summary: Symphony setup is incomplete. Run `symphony setup`.
```

`symphony_setup_complete` is the last status that the API computed. It changes when someone
opens setup (`symphony setup`, Tern › Setup, `GET /v1/setup`). `symphony_daemon_setup_ready`
is fresh: the daemon writes it at each step.

## Tests

`tests/test_metrics.py` seeds Redis and checks the values, parses the output with a parser
written from the vendored spec (`scripts/mocks/specs/prometheus-exposition-formats.md`),
bounds the labels, counts the Redis round trips of a scrape, checks the cache, checks that the
API port serves no metrics, and checks that this page lists every metric.
