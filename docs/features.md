# Features

Each line gives one feature and how it is verified. "Mocked" means the
[mocked certification](mocked-certification.md). "Kind" and "Merger run" mean the runs in
[certification.md](certification.md). Test files are in `tests/`.

## A. Dispatch

| Id | Feature | Verified by |
|---|---|---|
| A1 | Tracker polling by active and terminal states, with the required label `symphony` | `test_eligibility.py`, `test_linear.py`, `test_github_tracker.py`, Mocked 1 |
| A2 | Multi-repo routing by `repo:owner/name` labels, with `default_repo` | `test_repos.py`, Mocked 2 |
| A3 | Global and per-state concurrency limits | `test_dispatch.py` |
| A4 | Fencing against double dispatch: put-if-absent token, lease, generation | `test_fencing.py`, `test_dispatch.py` |
| A5 | Retries with backoff, stall detection, redispatch cooldown | `test_retry.py`, Kind (cooldown hold) |
| A6 | Reconciliation: a moved or cancelled ticket stops the attempt and cleans up | `test_reconcile.py`, Kind (terminal cleanup) |
| A7 | Strict Liquid prompt from `WORKFLOW.md`, hot reload with last-known-good | `test_prompt.py`, `test_config.py` |
| A8 | Hooks `after_create`, `before_run`, `after_run`, `before_remove` | `test_workspace.py` |

## K. Trackers

| Id | Feature | Verified by |
|---|---|---|
| K1 | Tracker-neutral interface (`trackers/base.py`) used by the daemon, the runner, the reflector and the merger | `test_linear.py`, `test_github_tracker.py`, `test_memory_tracker.py` |
| K2 | GitHub tracker: issues on a Projects (v2) board, the status option as the state | `test_github_tracker.py` (against the GitHub mock) |
| K3 | GitHub tracker: a closed issue is terminal; a move to a terminal state closes the issue (`completed` for Done, `not_planned` otherwise) and a move to another state reopens it; blocked-by dependencies hold a ticket back | `test_github_tracker.py` |
| K4 | GitHub tracker: sub-issues with blocked-by links, idempotent | `test_github_tracker.py` |
| K5 | GitHub tracker: text evidence as a collapsible comment; binary evidence refused | `test_github_tracker.py` |
| K6 | GitHub tracker: `github_graphql` agent tool, run on the host with the App token | `test_github_tracker.py` |
| K7 | GitHub tracker webhooks (`issues`, `issue_comment`, `sub_issues`, `issue_dependencies`, `projects_v2_item`) wake the daemon | `test_github_tracker.py`, `test_api_webhooks.py` |
| K8 | `tracker.kind: github` provider block checked (`owner`, `project_number`, `status_field`) | `test_github_tracker.py`, `test_admin_workflow.py` |
| K9 | Main watch ticket on any tracker (`main_watch: {ticket: true, state, labels}`) | `test_merger_units.py`, `test_github_tracker.py` |
| K10 | GitHub tracker: a pull request finds its ticket by branch, `owner/repo#n`, issue URL, or a closing keyword with a bare `#n` (`Closes #12`) in its own repository | `test_github_tracker.py` |

## B. Runner and workspace

| Id | Feature | Verified by |
|---|---|---|
| B9 | One workspace per ticket (pod and sessions volume, resumed across attempts) | `test_workspace.py`, Kind |
| B10 | Runner per attempt: turn loop, `max_turns`, timeouts, wall-clock guard | `test_runner.py`, Kind |
| B11 | omp RPC with auto-approve and a session directory (omp 18.20.2) | `test_omp_rpc.py`, `test_runner_real.py` (opt-in), Kind |
| B12 | Images per runtime (`node`, `dotnet9`, `dotnet10`) | `test_deploy.py`, Kind (image build) |
| B13 | Docker in the workspace (privileged) and `runtime_class` (sysbox, kata) | `test_deploy.py` |
| B14 | The tracker tool (`linear_graphql` or `github_graphql`) is a host tool; the tracker credential never enters the workspace | `test_hosttools.py`, `test_gateway.py`, `test_github_tracker.py` |
| B15 | GitHub App tokens per attempt, scoped to the repositories of the ticket | `test_ghapp.py`, Mocked 1 and 2 |

## C. Review watch and learning

| Id | Feature | Verified by |
|---|---|---|
| C17 | Review watch: a question on the pull request wakes an agent to answer | `test_review_watch.py`, `test_answer_run.py`, Mocked 5 |
| C18 | Reflector: retrospective → curation → learnings pull request (a person approves) | `test_reflector.py`, `test_curation.py`, Mocked 6 |
| C19 | Nightly sweep | `test_deploy.py`, Kind |
| C20 | Repository knowledge (`AGENTS.md`, `.agents/skills`) found by omp | `test_runner_real.py` (opt-in) |

## D. Usage and telemetry

| Id | Feature | Verified by |
|---|---|---|
| D21 | Token and cost ledger per attempt (local estimate) | `test_usage.py` |
| D22 | Native OTLP telemetry with rollup and a report on the ticket | `test_telemetry*.py`, `test_gateway.py`, Kind |
| D23 | Daemon heartbeat and status | `test_api_board.py`, `test_admin_read.py` |

## E. Models and providers

| Id | Feature | Verified by |
|---|---|---|
| E24 | Any provider and model from the omp catalog | `test_admin_providers.py`, `test_api_admin.py` |
| E25 | Provider API keys in a Secret (Bedrock included) | `test_admin_providers.py`, `test_deploy.py` |
| E26 | API keys for omp built-in providers from Admin, with no OAuth | `test_admin_providers.py`, `test_api_admin.py` |
| E27 | A saved key applies to the runner, the reflector and the merger on the next attempt | `test_provider_runtime.py`, `test_merger_units.py` |

## F. Operation

| Id | Feature | Verified by |
|---|---|---|
| F28 | Roles `viewer`, `operator`, `admin` on API tokens | `test_api_auth.py` |
| F29 | Durable, audited commands `pause`, `resume`, `refresh`, `reevaluate`, `interrupt` | `test_admin_commands.py`, `test_api_board.py` |
| F30 | Edit, validate and publish `WORKFLOW.md` with a reviewed diff | `test_admin_workflow.py` |
| F31 | Audit log | `test_admin_commands.py` |
| F32 | Read views in Tern (dashboard, sessions, tickets, merger, repositories, infrastructure, telemetry, learnings, audit, admin) | `tests/tern/` |

## G. Deployment

| Id | Feature | Verified by |
|---|---|---|
| G35 | Kubernetes manifests (namespace, RBAC, Redis AOF, daemon, API, gateway, CronJob, NetworkPolicy) | `test_deploy.py`, Kind |
| G36 | Declared `egress_allowlist` | `test_deploy.py` |
| G37 | Build and sync scripts | `test_deploy.py`, Kind |
| G38 | First-run setup: `GET /v1/setup` checklist from real state; admin-only, verified, audited writes of the tracker, GitHub App, repositories, notifications and webhooks; write-only Secrets with the restart of the daemon, mergers and API; local `secrets.env`; `symphony setup` wizard; idle daemon, merger and Jobs while incomplete | `test_api_setup.py`, `test_local_setup.py`, `test_cli_setup_real.py`, `test_setup_idle.py`, `test_setup_entries.py`, `test_merger_setup.py` |
| G39 | Release: CI (ruff, pytest, `bash -n`, Kustomize render, docs link check), multi-arch images on GHCR with SBOM and provenance, a GitHub Release from `CHANGELOG.md`, one version read by `symphony --version` and the MCP and ACP server information | `test_release.py`, actionlint |
| G40 | Backup and restore of the Redis state: streamed gzip NDJSON with a SHA-256 trailer, a checked restore (paused daemon, `--confirm`, namespace prefixes, `--namespace-from`), S3 (SigV4) or a directory, a nightly CronJob that keeps `backup.keep`, the archive of ended sessions and audit entries before Redis drops them, `retention:` in `WORKFLOW.md`, admin-only audited routes and `symphony backup …` ([backup.md](backup.md)) | `test_backup.py`, `test_backup_s3.py`, `test_backup_api.py`, `test_deploy.py` |

## N. People and agents

| Id | Feature | Verified by |
|---|---|---|
| N1 | API tokens: create, list, revoke, expiry | `test_api_admin.py`, `test_stores.py` |
| N2 | Live session as a native chat in Tern (ACP) | `test_acp_bridge.py`, `scripts/e2e_local.sh` |
| N3 | Steer, follow-up and interrupt in the middle of a turn | `test_runner_live.py`, `test_api_sessions.py`, Kind |
| N4 | Tern plugin with an Admin screen whose omp configuration reaches everyone | `tests/tern/` |
| N5 | MCP server for the omp of each person | `test_api_mcp.py`, `scripts/e2e_local.sh` |
| N6 | Local takeover (`symphony takeover`, `omp --resume`) and hold | `test_hold.py`, `test_api_hold.py`, `test_cli_commands.py` |
| N7 | Terminal viewer `symphony watch` | `test_cli_watch.py` |
| N8 | Cloudflare Tunnel (separate manifest in `deploy/cloudflared/`) | Manual |
| N9 | MCP servers for agents, per role, from Admin › MCP, the CLI or the API ([mcp.md](mcp.md)) | `test_mcp_servers.py`, `test_cli_commands.py`, `tests/mocks/test_mock_mcp.py` |
| N10 | Gateway MCP proxy: per-session token, stored headers added, streaming both ways, `mcp_forbidden` | `test_gateway_mcp.py`, `test_deploy.py` |
| N11 | MCP admin test (`initialize`, `tools/list`) with `unreachable`, `rejected` and `protocol_error` | `test_mcp_servers.py`, `test_gateway_mcp.py` |
| N12 | Assistant on Slack (Socket Mode) and Discord (Gateway): mentions, DMs, threads ([assistant.md](assistant.md)) | `test_slack_socket.py`, `test_discord_gateway.py`, `tests/mocks/test_mock_slack.py`, `tests/mocks/test_mock_discord.py` |
| N13 | Assistant modes (ask, `research`, `plan`), sessions per thread, concurrency cap, streamed and split answers | `test_assistant_engine.py`, `test_assistant_runtime.py` |
| N14 | Ticket proposals confirmed by a mapped person, idempotent, audited, with sub-issues and blocking relations | `test_assistant_confirm.py`, `test_assistant_trackers.py` |
| N15 | `assistant:` WORKFLOW section, idle states and heartbeat | `test_assistant_config.py`, `test_assistant_entry.py` |
| N16 | Setup step `assistant` (`symphony setup assistant`, Tern Setup) | `test_api_setup.py`, `test_cli_setup.py` |
| N17 | MCP companion HTTP API through the gateway: `http_base`, streamed uploads, binary passthrough, link rewriting (`rewrite_origins`), viewer routes never proxied ([mcp.md](mcp.md#companion-http-api)) | `test_seiji_gateway.py` |
| N18 | `agent_env` for agents (session token and gateway URLs only; literal secrets refused), in the workspace env file and in local mode | `test_mcp_companion.py` |
| N19 | Session cleanup of held MCP resources (`cleanup`): recorded by the gateway, released at session end and by the gateway sweep; `mcp_leases` and `symphony.mcp_lease` | `test_seiji_gateway.py` |
| N20 | Seiji preset (`--preset seiji`), the admin test's `/v1/me`, the attempt prompt paragraph, and the operator viewer link `symphony mcp viewer` ([seiji.md](seiji.md)) | `test_mcp_companion.py`, `test_seiji_gateway.py`, `tests/mocks/test_mock_seiji.py`, `test_seiji_real.py` (opt-in, `SYMPHONY_SEIJI_BIN`) |
| N21 | Assistant soul in Git (`assistant.soul`, `.symphony/assistant.md`): read from the mirror's `origin/main` at each new session, at most 8 KiB, front matter `name`/`language`/`tone`, after the safety rules; the blob sha in the heartbeat ([assistant.md](assistant.md#soul)) | `test_assistant_soul.py`, Mocked 14 |
| N22 | Per-person assistant memory for mapped people: `remember`/`forget`, "I'll remember: …", 50 items of 500 characters, renewable TTL, credentials refused ([assistant.md](assistant.md#memory)) | `test_assistant_memory.py`, Mocked 14 |
| N23 | Team facts by PR: `propose_team_fact`, "Open PR" by a mapped person, the App's PR under `## Team facts`, idempotent, audited `assistant.team_fact_pr` ([assistant.md](assistant.md#team-facts)) | `test_assistant_facts.py`, Mocked 14 |
| N24 | Assistant memory admin: `GET/DELETE /v1/assistant/memory…` (audited), `symphony assistant memory list\|show\|forget\|wipe`, Tern Admin › Assistant | `test_assistant_api.py`, `tests/tern/` |

## O. Agent workflow

| Id | Feature | Verified by |
|---|---|---|
| O1 | Pull requests in several repositories for one ticket | Mocked 2 |
| O2 | Proof of work: evidence attached to the ticket and listed in the merger report | `test_linear_tools.py`, `test_github_tracker.py`, Mocked 4 |
| O3 | An agent question never blocks the run (`symphony_ask` with a timeout) | `test_hosttools.py`, Mocked 1 |
| O4 | Provider rate limits in the status | `test_usage.py`, `test_runner_live.py` |
| O5 | Planning ticket → task tree with blocks; only unblocked children dispatch | `test_linear_tools.py`, `test_github_tracker.py`, Mocked 4 |
| O6 | Investigation tickets with no pull request (read-only token) | `test_ghapp.py`, `test_runner_live.py`, Mocked 3 |

## R. Merger review

Details are in [merger.md](merger.md#stage-1-review).

| Id | Feature | Verified by |
|---|---|---|
| R1 | Queue: bypass, then re-reviews, then first reviews | `test_merger_review.py` |
| R2 | Probe of models and tools, with a cache | `test_merger_review.py`, Merger run (bug 1) |
| R3 | Deterministic precheck | `test_merger_units.py` |
| R4 | Reviewers in parallel lanes | `test_merger_review.py`, Merger run |
| R5 | Oracle on a different model, one call per 4 findings | `test_merger_review.py`, Merger run |
| R6 | Typed blocking classes with agreement | `test_merger_review.py`, Merger run b |
| R7 | Typed output through host tools | `test_review_protocol.py`, Merger run (bug 2) |
| R8 | One rejection rule | `test_merger_review.py` |
| R9 | AI arbiter, then `needs-maintainer` | `test_merger_review.py` |
| R10 | Delta re-review | `test_merger_review.py` |
| R11 | CI outside the review (parked) | `test_merger_review.py` |
| R12 | Report on the pull request and labels | `test_merger_units.py`, Merger run |
| R13 | A failed run writes no pull request comment | `test_merger_review.py` |

## T. Merge train

Details are in [merger.md](merger.md#stage-2-merge-train).

| Id | Feature | Verified by |
|---|---|---|
| T1 | Serial train with a lease | `test_merger_train.py` |
| T2 | Order by least overlap | `test_merger_train.py` |
| T3 | Rebase, push with lease, merge at the exact head | `test_merger_train.py`, Merger run a |
| T4 | Batch of up to 4 with CI on the top | `test_merger_train.py`, Merger run d and f |
| T5 | Red CI: one rerun, then bisect by caps | `test_merger_train.py` |
| T6 | Conflict: developer agent and reviewer agent | `test_merger_train.py`, Merger run c |
| T7 | Crash-safe resume | `test_merger_train.py` |
| T8 | Main watch opens a ticket on the tracker | `test_merger_units.py`, `test_github_tracker.py`, Mocked 7 |

## M. Metrics

Details are in [observability.md](observability.md).

| Id | Feature | Verified by |
|---|---|---|
| M1 | Prometheus `/metrics` on the health port (8081) of the daemon, the mergers, the assistant and the gateway; never on the public API port | `test_metrics.py` |
| M2 | Tickets, attempts, retries, sessions, rate limits, tokens and cost, setup, merger per repository, MCP, assistant and build info, with bounded labels and no ticket or session ids | `test_metrics.py` |
| M3 | A scrape is cached for 10 s and costs one bounded SCAN pass and two pipelines | `test_metrics.py` |
| M4 | Optional `deploy/monitoring/`: a ServiceMonitor and a NetworkPolicy for a namespace labeled `symphony.dev/monitoring: "true"` | `test_metrics.py` |

## X. Cross-cutting

| Id | Feature | Verified by |
|---|---|---|
| X1 | Speed: webhooks and wake, intents < 300 ms, SSE < 300 ms, dashboard < 100 ms | `test_daemon_wake.py`, `test_api_sse.py`, `test_api_latency.py`, Kind |
| X2 | English user interface in Tern, the CLI and Slack | `tests/tern/`, `test_cli_*.py`, `test_notify.py` |
| X3 | Slack notices | `test_notify.py`, Mocked 1 |
| X4 | Runs on Kubernetes and on a single host (k3s or local mode) | Kind, `test_local_stack.py`, `scripts/e2e_local.sh` |
| X5 | Discord notices (webhook or bot), with mapped mentions and the 2000-character limit | `test_notify.py`, `tests/mocks/test_mock_discord.py` |
