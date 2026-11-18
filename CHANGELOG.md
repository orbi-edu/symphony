# Changelog

All notable changes to Symphony are in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Symphony
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<<<<<<< HEAD
### Added

- Optional mirror in the `images` workflow: the version tags are copied to AWS ECR through
  OIDC when the repository secrets `MIRROR_REGISTRY`, `MIRROR_AWS_ROLE_ARN` and
  `MIRROR_AWS_REGION` are set, with the same digests. For clusters that cannot pull from
  a private GHCR.
=======
### Changed

- Assistant built-in instructions now include an untrusted-content guardrail: text from
  repositories, issues, pull requests, tickets, web pages, tool results, MCP results, and
  third-party messages is treated as information, not instructions. The soul file cannot
  override these safety rules.
- Memory hygiene: the assistant is instructed to check existing items before remembering,
  prefer `explicit` items, avoid near-duplicates, and never store sensitive personal data
  categories or third-party information beyond role.
- `MemoryStore.remember` performs deterministic dedupe: text that matches an existing item
  after normalization (casefold, collapse whitespace, strip trailing punctuation) refreshes
  that item instead of appending, and upgrades `inferred` → `explicit` without downgrading.
>>>>>>> 3b88e95 (feat(assistant): treat external content as untrusted; dedupe memory items)

## [0.2.0] - 2026-11-17

### Added

- Manual image publishing through the `images` workflow, with an explicit version checked
  against `pyproject.toml`.
- `polyglot` agent runtime: a workspace image with .NET 10, Node 22 and Python 3 for
  repositories that mix .NET, Node and Python.
- Per-repository base branch override (`kubernetes.base_branches`): when set, the merger,
  assistant, reflector and workspace treat the configured branch as the repository's main
  branch instead of the auto-detected remote default. Configurable from Setup › Repositories
  (Tern and CLI `--branch`), verified against the GitHub App, and exposed as
  `SYMPHONY_BASE_BRANCHES` to workspace hooks.
- Monorepo components and per-repository runtimes (`kubernetes.repo_runtimes`,
  `kubernetes.components`). A ticket with `component:api` dispatches into a workspace
  with the component's runtime, scoped to its path. Labels `runtime:<name>` and
  `component:<name>` on tickets. Runtime precedence: label > component > repo_runtimes >
  agent_runtime. Planner creates per-component sub-issues with `blocks` ordering.
  Setup API, CLI (`--runtime`, `--component`), and Tern updated.

### Fixed

- The assistant and the merger follow the repository's default branch (`origin/HEAD`)
  instead of assuming `main`. A repository whose default is `trunk` failed every assistant
  sync, and its pull requests were never eligible for the merger.

## [0.1.0] - 2026-11-16

The first public release. [docs/features.md](docs/features.md) lists each feature and the test
that verifies it.

### Added

#### Core

- The daemon dispatches tracker tickets with the `symphony` label: wake by webhook in under
  3 s, a safety poll, concurrency limits, retries with backoff, stall detection and a
  redispatch cooldown.
- Fencing against double dispatch: a put-if-absent token, a lease and a generation.
- One workspace pod and one session volume for each ticket, with images for `node`,
  `dotnet9` and `dotnet10`, and optional Docker (privileged, Sysbox or Kata).
- A runner Job for each attempt drives the pinned `omp` engine (18.20.2) over RPC.
- Multi-repository tickets, planning tickets with sub-issues and a blocking order, read-only
  investigations, and agent questions that never block a run.
- Live sessions: follow, steer, follow up and interrupt an agent in under 300 ms. Holds and
  local takeover with `omp --resume`.
- The reflector writes a retrospective of each finished ticket and opens a learnings pull
  request for `AGENTS.md` and skills. A person approves it.
- A token and cost ledger, native OpenTelemetry with a report on the ticket, and provider
  rate limits in the status.
- Models for each agent role from the `omp` catalog, with provider API keys only. There is
  no OAuth or subscription login.
- Kubernetes manifests (Kustomize), NetworkPolicy, Redis with AOF, a nightly sweep, and a
  local mode that runs the stack in one process.

#### Trackers

- A tracker-neutral interface used by the daemon, the runner, the reflector and the merger.
- Linear, with the `linear_graphql` host tool.
- GitHub Issues on a Projects (v2) board, with the `Status` field as the state, sub-issues,
  blocked-by links and the `github_graphql` host tool.
- A memory tracker for tests and local mode.

#### Notifications

- Slack notices (bot token or incoming webhook).
- Discord notices (bot or webhook), with mapped mentions and the 2000-character limit.

#### Merger

- `symphony-merger` reviews and merges agent pull requests with no human approval.
- Parallel review lanes, an oracle on a different model, one rejection rule, an AI arbiter
  and delta re-review.
- A merge train with batches, bisection on red CI, AI conflict resolution and crash-safe
  resume. It merges the exact head that CI tested.
- A main-branch watch that opens a ticket on the tracker. One merger for each repository.

#### Tern

- The Symphony Tern plugin: dashboard, sessions, tickets, merger, repositories,
  infrastructure, telemetry, learnings, audit, Admin and Setup views.
- Live sessions as a native chat in Tern (ACP), and a device viewer for Seiji leases.

#### CLI and MCP

- The `symphony` CLI with `--json` everywhere: login with the token in the keychain, status,
  sessions, steer, interrupt, holds, takeover, `watch`, tokens and administration.
- `symphony --version`.
- `symphony acp`, the ACP bridge for Tern.
- An MCP server at `/mcp` that gives Symphony as tools to the `omp` of each person.
- The control API (`/v1`) with API tokens and the `viewer`, `operator` and `admin` roles,
  audited durable commands, live events (SSE) and webhooks.

#### Setup

- A first-run setup: `GET /v1/setup` checks the real state. Admin-only, verified and audited
  writes of the tracker, the GitHub App, the repositories, the models, the notifications and
  the webhooks into pre-created Secrets.
- The `symphony setup` wizard and the Setup view in Tern.
- A minimal install of only Redis and the API. The other pods wait until setup is complete.

#### MCP servers

- MCP servers for each agent role, from Admin › MCP, the CLI or the API.
- The gateway proxies them: agents hold only a per-session token, and the stored headers stay
  in the gateway.
- An admin test (`initialize`, `tools/list`) that reports `unreachable`, `rejected` and
  `protocol_error`.

#### Assistant

- `symphony-assistant`, a Slack (Socket Mode) and Discord (Gateway) bot: mentions, DMs and
  threads.
- Ask, `research` and `plan` modes with read-only agents, a session for each thread and a
  concurrency cap.
- Ticket proposals that a mapped person confirms. They are idempotent and audited, with
  sub-issues and blocking relations.
- A soul in Git (`.symphony/assistant.md`, `assistant.soul`): a persona and team rules read at
  each new session, after the safety rules; its blob sha is in the heartbeat and in Tern.
- Per-person memory for mapped people (`remember`, `forget`, "I'll remember: …"), with a
  renewable TTL and credential refusal.
- Team facts by pull request: a mapped click makes Symphony open a PR on the soul file,
  audited as `assistant.team_fact_pr`.
- Memory admin: `GET/DELETE /v1/assistant/memory…`, `symphony assistant memory
  list|show|forget|wipe` and Tern Admin › Assistant, every call audited.

#### Seiji

- The Seiji preset (`--preset seiji`) gives agents an iOS simulator.
- The MCP companion HTTP API through the gateway (`http_base`), `agent_env` placeholders,
  link rewriting and session cleanup of held leases.
- `symphony mcp viewer` opens the live screen of a lease.

#### Backup and retention

- Backup and restore of a namespace's Redis state: a streamed gzip NDJSON file with a
  SHA-256 trailer. The restore checks the whole file first, refuses while the daemon runs
  unpaused, needs `--confirm`, and restores another namespace only with `--namespace-from`.
- Targets: a file through the API, S3-compatible storage (SigV4, keys in the
  `symphony-backup` Secret), or a directory in local mode.
- A nightly `symphony-backup` CronJob keeps `backup.keep` backups and archives ended sessions
  and audit entries before Redis drops them. It idles until a target is configured.
- `retention: {sessions_days, audit_days}` in `WORKFLOW.md`, with bounds.
- `symphony backup status|export|restore|run|list|archive list|get` and admin-only, audited
  `/v1/backup` routes. The runbook is in [docs/backup.md](docs/backup.md).

#### Mocks and certification

- Doc-faithful mocks of Linear, GitHub, Slack, Discord, MCP, Seiji, S3 and the model APIs in
  `scripts/mocks/`. Each mock checks every request against the vendored specification.
- A mocked end-to-end certification of 13 scenarios (`scripts/e2e_mocked`).
- Real certifications on kind (`scripts/e2e_kind.sh`), of the merger on real GitHub, and of
  the Tern plugin against a real stack.
- A kind setup certification (`scripts/e2e_kind_setup.sh`): a minimal install made complete
  by the setup API against in-cluster mocks, with NetworkPolicy enforced, RBAC, MCP through
  the gateway, the assistant on Slack Socket Mode and the Seiji lease cleanup. It found that
  setup never restarted the merger and did not restart the assistant after a tracker or App write.

#### Videos

- Twelve narrated videos with captions and a teaser GIF in `docs/media/`, listed in
  [docs/videos.md](docs/videos.md). Each video rebuilds with its `render.sh`.

#### Docs

- Install, concepts, architecture, configuration, providers, security, merger, MCP,
  assistant, Seiji, Tern, CLI, API, internals, testing, troubleshooting and glossary in
  `docs/`.
- `AGENTS.md`, `CONTRIBUTING.md`, `SECURITY.md` and `CODE_OF_CONDUCT.md`.

#### Release

- CI on each push to `main` and each pull request: ruff, pytest, `bash -n`, the Kustomize
  render and a docs link check.
- Multi-arch (`linux/amd64`, `linux/arm64`) images on GitHub Container Registry and a GitHub
  Release for each `v*` tag.
