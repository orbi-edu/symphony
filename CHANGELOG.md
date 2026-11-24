# Changelog

All notable changes to Symphony are in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Symphony
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.13] - 2026-11-24

### Added

- An agent's question (`symphony_ask`) outlives the run that asked it. The daemon posts it on
  the ticket, as a comment that mentions who decides (`to`, new: a person on the ticket; the
  author by default) and numbers the options, and in Slack with one button per option. A
  reply to the comment, a button, a reply in the Slack thread, Tern or the CLI answer it; the
  first answer wins and both places say who answered. After `agent.ask_reminder_ms` (new,
  2 hours; `0` turns it off) the person is reminded once with the question again. An answer
  that comes after the agent's 15-minute wait still reaches the work: as a message to the
  run that went on without it, or, with no run, by sending a ticket in review back to Rework.
  `notify.users.slack` keys may be e-mails, so the person a ticket names can be mentioned.
- Work on a public repository never names its ticket. Discovery records each repository's
  visibility; the agent's prompt then gets a section that forbids ticket identifiers,
  tracker and company names and anything copied from the ticket, and names the branch
  `symphony/<ticket id>` (the tracker's opaque id), which the merger maps back to the
  ticket. The retrospective opens no learnings pull request on a public repository: its
  lessons come from the private ticket.
- This repository's own guard: a mandatory `public-repo-stealth` skill
  (`.omp/skills/`) and a `stealth` CI check that runs a denylist kept in the
  `STEALTH_DENYLIST` secret over each pull request's title, description, branch, commit
  messages and added lines.

### Fixed

- Failure evidence of a runner Job now says when the cluster removed its pod: a `Runner pod`
  line on the triage ticket names the deletion, the `DisruptionTarget` or `Evicted` reason
  and the node, or says that the pod is missing. A runner container that did not terminate
  shows its state (`running`, `waiting: <reason>`). The triage signature includes the
  disruption cause, so each external cause gets its own investigation, apart from crashes.
- Stopping an agent generation no longer fails when a process in the workspace exits during
  the scan. The scan read a process's state before its environment, so a process that
  exited between the two reads was a zombie with an unreadable environment, and the stop
  could not be confirmed: the runner crashed after the work, skipped the sandbox release
  and spent a retry. The scan now reads the environment first and checks the state only
  when that read fails; a live process whose environment cannot be read still fails the
  stop.
- A run that died while waiting for an answer no longer leaves its session in
  `waiting_input` for good (Tern kept showing it under "Needs input"): the daemon closes
  attempt sessions their ticket no longer runs.
- A runner whose workspace pod ended during the attempt (deadline, eviction, a failed node)
  no longer crashes at teardown with "Agent generation termination could not be confirmed".
  A pod that has ended (`Failed`, `Succeeded`) runs no process, so the stop is confirmed and
  the attempt records its real failure reason. A live pod whose exec fails, or a pod whose
  object is gone before its containers are known to have stopped, still fails closed.

## [0.2.12] - 2026-11-21

### Fixed

- Provisioning creates a prefixed label inside the label group its prefix names, so
  `repo:acme/app` joins an existing `repo` group (a workspace group keeps its children on
  the workspace). Before, every new label landed on the team, outside the group, and a
  single-select `repo` group stopped covering the new repositories. A prefix with no group
  still creates a team label.
- An empty repository (no commit yet) is no longer a provisioning error retried on every
  discovery pass: it gets its labels, and the policy pull request waits for the first push,
  checked again once a day.

## [0.2.11] - 2026-11-21

### Added

- The assistant reaches every repository the GitHub App reaches. The repositories Symphony
  works on stay checked out; the others are listed in the instructions, and the agent opens
  one with the new `open_repository` tool when the person names it. The sandbox accepts them
  too. Before, a repository no ticket had touched was "not in my workspace".

## [0.2.10] - 2026-11-21

### Added

- Repository provisioning, after each discovery and for every repository that is not
  archived. It creates the missing tracker labels (`repo:<owner/name>`,
  `component:<name>`) and the missing GitHub labels the merger and the agents use. It also
  opens one pull request that adds a default `.symphony/merger.md` where none exists, and
  never opens it again once that pull request exists, closed or not. `/v1/repos` shows the
  policy pull request and the last error.
- Tern: open a repository on the Repositories page to configure its runtime, base branch and
  components, prefilled with what discovery detected.

### Changed

- Discovered component names follow the workflow's naming rule (`data_science` becomes
  `data-science`; the path keeps the directory's spelling).

## [0.2.9] - 2026-11-21

### Added

- Repository discovery. Every 15 minutes the daemon lists the repositories the GitHub App
  reaches and detects, from each default branch, the runtime (`node`, `dotnet<N>` from
  `global.json` or the target framework, `polyglot`) and, for a monorepo, one component per
  top-level project directory. It also records the default branch, the archived flag and
  whether the repository has a merger policy. Dispatch uses a detection only where
  WORKFLOW.md configures nothing for that repository. `/v1/repos`, `symphony repos` and
  Tern's Repositories page list every reachable repository, with detected values marked.

## [0.2.8] - 2026-11-21

### Added

- Failure triage (`triage:`, off by default). A runner that dies or stalls, or a ticket
  that uses up its retry budget, opens one `investigation` ticket for `triage.repo` per
  failure signature. The ticket starts in the first active state and carries the evidence
  the cluster has (Job condition, container exit, last error, last log lines, credentials
  redacted). The agent finds the cause and specifies the fix as sub-issues. A repeat adds
  an occurrence instead; a closed investigation reopens as a new one; `triage.max_per_day`
  caps new tickets; a failing investigation is never triaged. The channel gets
  `triage_opened`, and the retry-budget report names the investigation as who acts.
- A runner Job that ended without recording an outcome is requeued on the next tick
  instead of after two stall windows (one hour with the shipped `stall_timeout_ms`).

### Fixed

- Every attempt that left an orphan process (omp's broker, a browser) ended as a runner
  crash: the workspace's PID 1 was `sleep infinity`, which never reaps, and the stop script
  could not read a zombie's environment, so it refused to confirm the engine had stopped.
  The Job failed after the work was done, and the daemon waited two stall windows before
  queuing a new attempt. Agent pods now run a reaping PID 1, and the stop script skips
  zombies.

## [0.2.7] - 2026-11-21

### Added

- Retry budget (`agent.max_failed_attempts`, default 8): after that many failed attempts in
  a row the ticket is `blocked` instead of retried forever. The ticket and the channel
  (`retries_exhausted`) get the last failure, who acts and the next step; `symphony
  reevaluate` or any later edit of the ticket queues it again with a fresh count. Each crash
  report now shows how much of the budget is used.
- Intake verdicts (`tracker.intake_states`, default `[Backlog, Triage]`): a ticket with
  Symphony's labels is told once per change whether it is queued and, if not, what is
  missing — the state, a label, the repository or an open blocker. A verdict that needs a
  person also goes to the channel (`not_queued`). The assistant's "create" reply says the
  same for the tickets it creates.
- `symphony_ask` refuses a question that asks for credentials, keys or tokens: nothing is
  stored or posted, and the agent is told to ask for the command's output instead. The
  prompt says the same.

### Changed

- The approval notice is one message per (PR, head) with separate lines for CI, the
  merger's review, who must approve and the next step; it mentions every mapped reviewer,
  says when no reviewer is requested, and reminds once after 24 hours. `notify.post` takes
  `github_logins` (a list) instead of `github_login`.
- "PR returned to its author" says no human action is needed when the agent authored it.

### Fixed

- The approval notice showed the merger's comment title (`**Waiting for CI**`) instead of
  the PR title.
- A PR merged on GitHub outside the merge train now moves its ticket to Done; before, the
  ticket stayed in Merging.
- A retrospective "learnings" PR no longer moves its source ticket: sending it back moved a
  Done ticket to Rework and re-ran the agent.

## [0.2.6] - 2026-11-20

### Added

- Merger polish rounds (`polish_rounds: N` in `.symphony/merger.md`): when an
  agent-authored PR finishes its review with no blocker but has actionable nits
  (confirmed, partial or not verified), the merger sends it back for the agent to
  fix them before going on to CI and the approval gate. The round count is separate
  from rejections, so a later real block is still a first rejection. Human-authored
  PRs are never polished. Default 0 keeps today's behaviour.
### Fixed

- Workflow API: the shipped prompt was locked as a "legacy credential-bearing field"
  because its shell snippets (`password=`, `Authorization: Bearer $TOKEN`) matched the
  credential pattern. A keyword value now counts as a credential only from 8 characters.


## [0.2.5] - 2026-11-19

### Added

- Assistant: attached images (PNG, JPEG, GIF, WebP) in Slack and Discord messages
  are downloaded and sent to the model as image blocks, up to 4 per turn and
  5 MiB each. Non-image and oversize files are still listed by name. On Slack
  the bot scope `files:read` is needed; a missing scope degrades gracefully.

## [0.2.4] - 2026-11-19

### Fixed

- Slack: the private answer to a click on a top-level channel message (such as the
  merger's "Open in simulator") went to that message's empty thread, so the clicker saw
  nothing. It now appears in the channel; a click inside a thread still answers there.

## [0.2.3] - 2026-11-19

### Added

- Merger human approval gate (`require_approval: true` in `.symphony/merger.md`):
  a reviewed pull request waits for at least one GitHub `APPROVED` review with no
  `CHANGES_REQUESTED` before the merge train takes it.  While waiting, it carries the
  label `wait-approval` and reviewers get one `needs_approval` notification per head.
  The train re-checks before merging; a dismissed approval sends the PR back.
- Merger preview announcement (`preview:` in `.symphony/merger.md`): when a workflow
  artifact named `preview.artifact` exists for a PR's current head, the merger posts one
  Slack message per (repository, PR, head sha) with a button (`preview:<owner>/<name>:<pr>:<sha12>`).
  Discord gets a plain-text fallback with no button.  Deduplication is in Redis.
- New notification events: `needs_approval` and `preview_ready`.
- `notify.post_with_button`: sends a Slack Block Kit message with one button and a
  plain-text Discord fallback.
- Seiji for the chat assistant: when a chat role (`assistant`, `researcher`,
  `planner`) has a Seiji server, the session gets a chat-specific guidance
  paragraph and the `share_device` host tool. `share_device` posts a "Control
  the device" button; clicking it sends a private one-time viewer link to the
  mapped person who asked the turn, while that session holds the lease.
- PR previews: clicking "Open in simulator" on a merger preview notification
  starts a simulator on Seiji with the PR's preview build, and sends a control
  viewer link to the clicker, a reviewer of the PR. One device per (repo, PR);
  a second click reuses the live lease. The device is released on merge,
  close, a human approval after the start, a new head, after 55 minutes, and
  when the assistant restarts. Requires a Seiji server named `seiji` and a
  GitHub App with `actions: read` and `pull_requests: read`.

## [0.2.2] - 2026-11-18

### Added

- Assistant sandbox (`assistant.sandbox`, off by default): `sandbox_run(repo, command)`
  runs commands in a workspace pod per chat thread, created on the first call. Same
  manifest and NetworkPolicy as issue workspaces, no ServiceAccount token; each repository
  is cloned with a GitHub App token scoped to it (`contents: read`) that reaches the pod on
  stdin only. Only people mapped in `notify.users` can run commands, checked at each call.
  The pod is deleted when the session ends and at assistant start; `max_lifetime_s` caps it.
  The `symphony-assistant` Role gets `pods` and `pods/exec`.
- Spec-driven planner: before proposing tickets the planner investigates (reads code and,
  with the sandbox, runs the build or tests) and writes a spec in the thread (Problem,
  Current behaviour, Proposed design, Alternatives rejected, Acceptance criteria,
  Verification plan, Risks); each ticket carries its acceptance criteria and checks.

### Fixed

- Slack answers over 4,000 characters failed with `chat.update: msg_too_long` after the
  first part was shown: the transport assumed `chat.postMessage`'s 40,000 for the in-place
  edit too. Answers now split at 4,000 characters per message.
- A failed release of a held MCP lease is now logged with its cause
  (`mcp_cleanup_release_failed` with the exception type, `mcp_cleanup_release_refused` with
  the step and HTTP status) instead of only counting `failed=1`.

## [0.2.1] - 2026-11-18

### Added

- Optional mirror in the `images` workflow: the version tags are copied to AWS ECR through
  OIDC when the repository secrets `MIRROR_REGISTRY`, `MIRROR_AWS_ROLE_ARN` and
  `MIRROR_AWS_REGION` are set, with the same digests. For clusters that cannot pull from
  a private GHCR.

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
