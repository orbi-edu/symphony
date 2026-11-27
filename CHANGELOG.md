# Changelog

All notable changes to Symphony are in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Symphony
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.32] - 2026-11-27

### Added

- Tickets and merger PRs name the people responsible. `GET /v1/issues` has each ticket's
  `assignee` (`{name, login?}`, null when none), as the daemon claimed it: a person
  `notify.users` maps goes by their GitHub login; the e-mail is never shown. Each PR row of
  `GET /v1/merger` has its `author` and `reviewers` (`[{login, state}]`: requested, approved,
  changes requested or commented; bots and the author's own replies left out), and a PR parked
  for an approval has `waiting_on`. The merger records the reviewers when it already reads
  GitHub (a review starts, the approval checks, the queue); a snapshot never calls GitHub.
  Tern's Tickets view has an **Assignee** column and the session card shows it; Merger rows
  say `alice · bob ✓, carol requested` and `waiting on bob` (each reviewer still requested or
  requesting changes; `waiting on anyone with write access` when none). `symphony issues` and
  `symphony merger status` show the same
  ([api.md](docs/api.md#issues), [merger.md](docs/merger.md#api-and-store),
  [tern.md](docs/tern.md#merger)).

## [0.2.31] - 2026-11-26

### Fixed

- Builds an agent uploads to Seiji no longer fill the shared user's build quota
  (`maxBuildsPerUser`) for 72 hours. The gateway records the `app_id` of each upload a
  session makes through `$SEIJI_URL/v1/apps`, and the session's MCP cleanup (attempt, merger
  and chat assistant sessions, and the gateway's sweep after a crash) deletes it with
  `DELETE /v1/apps/{app_id}`, next to releasing its leases. A 404 or 405 (the build expired,
  or an older Seiji) is ignored. Builds of PR previews are not recorded, and servers without
  the Seiji preset record nothing ([seiji.md](docs/seiji.md#cleanup)).
- A ticket whose workspace was built for another repository set crashed every attempt with
  HTTP 403: the runner discards that workspace with its session volume, and its Role could
  not delete PersistentVolumeClaims. The `symphony-runner` Role can now.

## [0.2.30] - 2026-11-26

### Fixed

- PR previews no longer fill Seiji's build quota (`maxBuildsPerUser`, 10 by default). Every
  click, link open or restart uploaded the build again, until uploads were refused with
  HTTP 429. A sha's upload is now kept in Redis for 60 hours (under Seiji's default 72-hour
  retention) and reused; when Seiji no longer has it, the start uploads it once more.
  Symphony deletes an upload (`DELETE /v1/apps/{app_id}`) when its PR is merged or closed or
  its head moves past it and no live device runs it; a Seiji without the route answers 404,
  which is ignored. A refused upload now shows Seiji's reason, and a full quota says that
  builds are reused and expire ([seiji.md](docs/seiji.md#pr-previews)).

## [0.2.29] - 2026-11-26

### Added

- A team knowledge base for the chat assistant. `assistant.knowledge` lists repositories (or
  folders and globs in them) of the team's engineering docs, each with an optional `name` and
  a `when` line. They are always checked out and fetched every 5 minutes; each new commit
  rebuilds an in-memory index of their Markdown pages (title, first line, path). The
  assistant, the researcher and the planner get the index in their instructions with the
  commit, consult it first for questions about how the team works, cite each page as a link
  to the file at that commit, and say when the docs do not cover a question. A new
  `search_knowledge(query, base?, limit?)` tool ranks the pages' sections with BM25 (accents
  and stopwords folded away, headings boosted) and returns line ranges and blob links. A base
  whose sync fails keeps its last index and is marked as possibly stale. The workflow API
  accepts the key ([assistant.md](docs/assistant.md#team-knowledge)).
- The CLI and the Tern plugin update themselves to the release the server runs. The API
  reports it as `version` in `GET /v1/overview`. `symphony update [--check]` installs that
  release over a `uv tool` install (`uv tool install --force git+<repository>@v<version>`; the
  old CLI keeps working when it fails), then the plugin of the new release. A clone is never
  changed: the command prints what to run. After a command that talked to the API, at most every
  6 hours, an older `uv tool` install updates in the background, with one line on stderr; not
  in CI, with `SYMPHONY_AUTO_UPDATE=0`, in `symphony acp`, or for the plugin's own commands.
- `symphony version [--json]`: the CLI's version, the plugin version Tern has, the server's
  version, and how the CLI is installed.
- The Symphony panel updates an older CLI (`symphony update`) and an older plugin (`symphony tern
  install --plugin-only`) by itself, once per target, and shows the progress in its footer. For
  a CLI that runs from a clone it shows the command instead.
- Every install carries the Tern plugin of its own version (`symphony/tern_plugin` in the
  package). `symphony tern install` installs it when the CLI does not run from a clone, and
  installs nothing when Tern already has that version. `--plugin-only` installs only the
  plugin, without a login.
- Install a release with `uv tool install git+https://github.com/<owner>/symphony@v<version>`.

### Changed

- The docs, the Tern screenshots and the videos follow what shipped since 0.2.0: the README
  and `docs/features.md` list the newer features, Tern's Setup and Admin pages document Skills
  (with a new Admin › Skills screenshot), the Merger page its label filter and approval
  parking, and the videos that described the 0.2.0 behaviour are re-recorded. The Tern
  screenshot harness waits for the current Repositories heading and steps past the Skills tab.

### Fixed

- Tern's Setup › Repositories card and the Merger view's heading say that one merger serves
  every repository, each with its own train, instead of one merger per repository.
- A CLI that runs from a clone reports the clone's version. Before, an editable install kept the
  version of the day it was installed.
- The Tern plugin's `plugin.toml` carries the release version; it said `0.1.0` since the first
  release. A release now sets both, and the release workflow checks both against the tag.

## [0.2.28] - 2026-11-26

### Added

- Inline review comments. After a review round posts its report, the merger submits one
  GitHub review (`COMMENT`, on the reviewed head) that puts each blocking finding and each
  confirmed nit on its diff lines, with the fix and a link to the report. A finding whose
  `replacement` (a new optional field of the reviewer's `submit_finding`) is the new text of
  its lines, at most 30 lines, gets a `suggestion` block. Findings outside the diff or on the
  PR body stay in the report only. A re-review does not repeat a commented finding and
  resolves the thread of one it no longer reports. The review never approves or requests
  changes, so the approval gate is unchanged, and a failed call is logged without failing the
  round. `inline_comments: false` in `.symphony/merger.md` turns it off
  ([merger.md](docs/merger.md#inline-comments)).

## [0.2.27] - 2026-11-26

### Fixed

- Tern sign-in works with a CLI installed by `uv tool install` (or pipx, or Homebrew) in a
  Tern started from the Dock. Without `SYMPHONY_BIN`, the plugin looks for `symphony` on the
  daemon's PATH, then in `$UV_TOOL_BIN_DIR`, `$XDG_BIN_HOME`, `~/.local/bin`,
  `/opt/homebrew/bin` and `/usr/local/bin`. `SYMPHONY_BIN` still wins. When the CLI does not
  start, the error lists those directories, or names the `SYMPHONY_BIN` path when one is set.
- **Install in Tern** runs `symphony tern install --no-plugin`: it adds the ACP agent and the
  MCP server and does not reinstall the plugin that draws the panel. Before, it failed on an
  install without a source checkout.
- `symphony tern install` without `--plugin` on an install without a source checkout says to
  use `--plugin DIR` or `--no-plugin`, instead of naming a path inside the Python environment.
- Karpenter (EKS Auto Mode included) no longer evicts a runner, a reflector or a busy
  workspace to consolidate a node. Their pods carry `karpenter.sh/do-not-disrupt: "true"`
  next to the cluster autoscaler's `safe-to-evict: "false"`; Karpenter reads only the first.
  Before, an eviction failed the attempt ("runner died mid-attempt") and a busy workspace
  lost its uncommitted work. A released workspace drops both annotations with the same
  patch that starts its idle clock.
- A reused workspace is marked busy as soon as the runner claims it, before the ready wait
  and the checks; a workspace reattached after a lost create race is marked busy too. A
  workspace whose preparation fails is released (unless a newer attempt owns it), so it does
  not keep its node with no idle clock.
- The daemon releases the workspace of an attempt whose runner could not: one that died
  (out of memory, node lost), was stopped as stalled, or was interrupted by an operator. Its
  pod no longer pins a node until `kubernetes.sandbox.timeout_s`, including when the retries
  ran out and the ticket waits for a person.
- Karpenter no longer evicts the single-replica control-plane pods to consolidate a node.
  The merger, Redis, the daemon, the gateway and the assistant in `deploy/` carry
  `karpenter.sh/do-not-disrupt: "true"`. Before, a consolidation restarted reviews and live
  chats and stopped every component while Redis moved. The API stays movable: it keeps no
  state and its clients reconnect. No PodDisruptionBudget, which would block node drains.

### Changed

- A generation stop that cannot be confirmed says why. The error keeps its prefix, `Agent
  generation termination could not be confirmed`, and adds the cause: `timeout`, `exec
  failed or interrupted` with the exec error or the first line of partial output,
  `unreadable pid=<pid>` for a live process whose environment cannot be read,
  or `survivors count=<n>` for processes that outlived the last `KILL` (`survivors
  pgid=<pgid>` on the local stack). It names pids and counts only, never
  environment content.

## [0.2.26] - 2026-11-26

### Changed

- The merger's PVC in `deploy/merger.yaml` is 40Gi (was 20Gi). Applying it grows a bound
  claim when its StorageClass allows volume expansion (EBS gp3 grows online, with no restart).

### Fixed

- The merger no longer fills its disk. Each agent run left its directory behind
  (`sessions/<sid>`: omp's session files and the agent dir with the staged `models.yml`, MCP
  config and skills), and tool-check probes are runs too, until `git fetch` failed with "No
  space left on device" and reviews stopped. A run now removes its directory when it ends,
  however it ends (a failure before omp starts, an error while it runs). On start, each
  repository's merger removes the leftover `merger_*` directories older than one hour that no
  run of the process owns (`merger_sessions_swept`), before its first fetch. The assistant
  removes a chat session's directory when the session ends.
- A parked clean review whose CI passed, under `require_approval: true`, says "waiting for
  approval" instead of "waiting for CI", in `GET /v1/merger` (`parked[].waiting`: `ci` or
  `approval`), the Tern Merger panel and `symphony merger status`. Parked rows show the pull
  request's title (`parked[].title`) instead of the URL of the report comment; a record parked
  before this release shows `PR #n` until it settles. A review handed to the train from
  parking keeps the pull request's title, not the report's heading.
- The merger's abandon check no longer asks Linear about a ticket every 30 s for a pull
  request whose branch only looks like it names one (`alice/widget-5-app-data` read as
  `WIDGET-5`), and no longer logs `merger_abandon_check_failed` for it. Linear answers an
  unknown identifier with an "Entity not found" error, not `issue: null`; the single-ticket
  reads now take that as no ticket, and the answer is cached for a minute like any other. On a
  Linear board scoped to a team (`team_key`, no `project_slug`), only that team's key names a
  ticket in a branch, title or body.

## [0.2.25] - 2026-11-26

### Changed

- A session's chat in Tern says where the agent runs. Its first line names the pod, the
  repository and the branch, and says nothing runs on this machine. The chat tab opens in an
  empty directory named after the session (`~/.cache/symphony/remote/<repository>/<ticket>`),
  so the workdir Tern shows is no longer one of your projects.

### Fixed

- The Tern test harness starts on Tern releases without `ctl status-bar`.

## [0.2.24] - 2026-11-25

### Added

- Telemetry history over the whole period. Each UTC day gets a rollup in Redis
  (`metrics:day:<yyyymmdd>`, never expired, in the backup): dispatches, rework, runs and
  attempts, ended sessions by kind and outcome, tokens (input, output, cache read and write),
  the cost estimate in micro-USD, cost and tokens per model and per repository (50 named per
  day, the rest `other`), the hour of the week each session started, pull requests opened and
  merged with the time to merge, learning pull requests, merger results (reviewed, back to the
  author, merged, needs a maintainer), questions asked and answered per channel with the time
  to answer, attempts blocked on a person and attached session traces. Each event is counted
  where it happens, once: a replayed event (a restart, a retried retrospective, a session
  closed twice) does not count again. The monthly counters stay as they were.
- `GET /v1/telemetry/history?from=&to=&bucket=day|week|month` (viewer): the rollups as
  points (at most 120; the bucket is chosen or coarsened to fit), totals, per model, per
  repository, per kind and a weekday × hour activity grid. Cached 60 seconds.
- `symphony telemetry backfill [--force]` (`POST /v1/telemetry/backfill`, admin, audited
  `telemetry.backfill`): rebuilds the session fields of past days from the ended sessions in
  Redis and the archive objects on the backup target, streamed. Idempotent.
- Sessions keep their usage (`meta.usage`: tokens and cost of every `message_end`), for every
  kind: merger and assistant sessions too.
- Tern **Telemetry**: a History section with a period picker (all, 90 d, 30 d, 7 d; `[` `]`)
  and ten charts over the rollups: cost per bucket, tokens by type, sessions by kind,
  outcomes (bars and donut), cost by model (donut and a table with a sparkline per model),
  cost and sessions by repository (top 10), pull requests opened and merged with the mean
  time to merge, the rework rate with its period average, questions with the answer channel
  and the mean time to answer, and a weekday × hour activity heatmap. Each has a one-line
  summary and an empty state.
- Tern **Dashboard**: six tiles with 30-day sparklines (cost, sessions, PRs merged, rework
  rate, questions waiting, blocked on a person) and a 30-day cost chart, below what it showed.
- The assistant proposes tickets too. In Ask mode, a request it cannot carry out read-only (a
  change in a repository or the tracker: create a skill, fix code, update docs, open a pull
  request) no longer ends at "I can't": it investigates, writes the ticket (problem, what to
  change, acceptance criteria, how to verify, the repository) and calls `propose_tickets`. The
  proposal, its Create / Create and start / Dismiss buttons, the mapped-click gate and the
  `assistant.tickets_created` audit are the planner's. The planner keeps multi-ticket plans and
  specs.
- **Plan this**: for a request that needs a multi-step plan, the assistant answers briefly with
  a Plan this button (`offer_plan`). A click by a person mapped in `notify.users` makes the
  thread's role planner and starts a planner turn with the thread as context, like a `plan …`
  follow-up; anyone else gets a private refusal. A change of role in a thread now keeps the
  thread's sandbox.
- Files from the sandbox travel with a proposal: each ticket of `propose_tickets` accepts
  `attachments: [{path, title}]`, read at the call with `share_file`'s reader and refusals (the
  clones or `/tmp`, no `.git` or credential files, no file holding a credential, 25 MB each),
  at most 10 files and 50 MB per proposal, from a mapped asker. They wait beside the proposal
  (`sym:<ns>:assistant:proposalfile:*`, 7 days, left out of backups). Create attaches each one
  to its ticket once (a double click or a retry never attaches twice) and the body says to
  fetch them with `symphony_fetch_attachment`; Dismiss deletes them.

### Changed

- A host tool call that answers with `HostToolError` is not recorded: a refused
  `propose_tickets` no longer counts as the turn's proposal, and the agent can call it again.
- An expired proposal answers "This proposal expired. Ask the assistant again."

### Fixed

- A preview link opened after Seiji released its device for sitting idle showed "Could not
  open the preview" (HTTP 502) on every reload: Symphony still had the lease on record and
  asked Seiji for a viewer of a lease that had ended. Now the link forgets that lease and
  starts the build again (the progress page, then the viewer), and the Slack button says the
  simulator was released and starts a new one on the next click. The viewer route answers
  410 `lease_gone` for such a lease.

## [0.2.23] - 2026-11-25

### Fixed

- `api.access.team_domain`, `api.access.aud`, `previews.public_links` and
  `tracker.abandon_states` could only be set by editing WORKFLOW.md by hand: the workflow API
  (Admin › Workflow, `symphony api`) now reads and writes them. Setting both `api.access` values
  to empty turns Access verification off; half a section is refused.

## [0.2.22] - 2026-11-25

### Added

- Preview links on the pull request: when the merger finds a preview artifact for a head
  (T10), it also keeps one **Preview** comment on the PR, edited in place, with a link per
  build (newest first, the last 5): `<SYMPHONY_PUBLIC_URL>/v1/previews/open?repo=&pr=&sha=`.
  The link has no expiry: it opens that build days later, while its artifact exists, and an
  older sha opens that version. Behind Cloudflare Access no secret is needed. For
  `previews.public_links` the link must be signed: `sig`, an HMAC-SHA256 of the repository,
  PR and sha with the new optional secret `SYMPHONY_PREVIEW_LINK_SECRET` (Secret
  `symphony-api`; the merger reads only that key). With Access and the secret, links are
  signed and a present `sig` must match, but none is required.
- `GET /v1/previews/open` and `GET /v1/previews/status` on the API: browser routes, no API
  token. Behind Cloudflare Access (new `api.access: {team_domain, aud}` in `WORKFLOW.md`) the
  API verifies the `Cf-Access-Jwt-Assertion` token (RS256 against the team's cached keys,
  `aud`, `iss`, `exp`) and requires its e-mail in `notify.users`; any mapped person may open,
  not only reviewers. Without Access the route refuses, unless `previews.public_links: true`
  (a signed link required).
  The page shows the start's progress and errors, polling every 2 s, then redirects to a
  fresh Seiji control viewer; a reload mints a new one for the same device. An expired
  artifact gets a page that says so. Each open is audited as `preview.opened`.
- The answer run on a PR gets the PR's preview links in its prompt (`## Preview links`), and
  `WORKFLOW.md` tells it to reply with the link when someone asks to open or test the app in
  Seiji, instead of installing the build on a device itself.
- PR answer runs get a session trace too, titled `Session trace (answer, run N)`
  (`session-trace-answer-run-N.html`); the session report marks them `(answer, run N, …)`.
- Canceled tickets stop everything. A ticket finalized in an abandon state (new
  `tracker.abandon_states`, by default Canceled, Cancelled and Duplicate when they are
  terminal states, plus the tracker's canceled-type states such as Linear's `canceled`; Done
  never) closes its open questions: their Slack message and DM copies read ":no_entry: *Ticket
  canceled*" with the question kept and no buttons, no reminder follows, and a later answer is
  refused ("the ticket was canceled"). The merger, on its own review pass and with the
  ticket's state cached for a minute, drops the ticket's PRs from its queue, parked reviews and
  train wait list, comments once (`Closed by symphony-merger: <ticket> was moved to <state>.
  Reopen the PR to work on it again.`), closes them and releases their preview devices. A PR a
  person reopens is not closed again for the same ticket state; a ticket back in an active
  state works normally again.

### Changed

- Preview devices are one per (repository, PR, sha), at most 2 live per PR (a third build
  releases the oldest), and are recorded only in Redis: the assistant (buttons) and the API
  (links) share them, and one sweep a round runs under a Redis lock. A restarted process
  keeps sweeping the devices it finds instead of releasing them at start. A device opened
  from a link keeps its sha: a head move or an approval no longer releases it (a merge or
  close, 55 minutes and a lost lease still do). The button's reviewer rule is unchanged.
- The previews' GitHub App token also asks for `contents: read` (to resolve a link's sha).

## [0.2.21] - 2026-11-25

### Added

- `symphony_blocked(reason, needs)`: a host tool the agent calls when it cannot make progress
  without a person (`needs`: `answer`, `access`, `decision` or `other`). The run ends at the
  end of the turn with outcome `blocked`, and the ticket waits: no slot, no dispatch, no
  continuation. The daemon writes one short comment on the ticket saying what it waits for.
  The ticket gets one new run when a person acts: an answer to one of the agent's
  questions, a comment, a label or state change, any later edit, or `refresh`/`reevaluate`.
  Symphony's own comments (a question reminder, an announcement) never reopen it: the
  daemon reads the comments since its waiting comment and skips those whose id Symphony
  recorded (`sym:<ns>:comment-own:<id>`), through the new tracker read `comments_since`
  (Linear, GitHub, memory).
  The wait is kept in Redis, so a daemon restart keeps it; the API (`/v1/issues` `status`
  and `phase`, with `blocked: {needs, reason, at}`; `/v1/overview` `counts`), Tern and the
  CLI show it as `blocked_on_person`. `WORKFLOW.md` and the GitHub example tell the agent to
  call it instead of ending turns with "blocked" messages.
- `agent.max_idle_continuations` (default 3, `0` off): a safety net for agents that do not
  call the tool. That many continuation turns in a row with no new commit, no file change
  (`git rev-parse HEAD`, `git status --porcelain` and `git diff HEAD` of each repository,
  before and after the turn) and no tool call beyond reads and tracker queries end the run
  the same way, with the reason "no progress in 3 continuation turns". The count goes on
  across continuation dispatches; a turn in which a person wrote to the agent is progress.
- Each run's session trace is attached to the ticket. At the end of every implementation or
  investigation run the runner exports the omp session (`omp --export`) in the workspace,
  reads it back (at most 25 MB), replaces every match of the merger precheck's secret
  patterns and every credential value of the run (tracker key, App installation tokens,
  gateway and MCP tokens) with `[REDACTED]`, and attaches it as
  `session-trace-run-N.html`. The session report links it (`**Session trace**: <url>`,
  with the number of redactions) or says why it is missing (too large, export or upload
  failed); the run never fails over it. `agent.attach_trace: false` turns it off. Linear
  only: GitHub issues hold no files.
- The agent can read its own ticket's attachments: `symphony_ticket_attachments` lists them
  (`id`, `title`, `kind`, the URL's host, `created_at`) and `symphony_fetch_attachment(id,
  path)` downloads one into the workspace with the tracker credential on the host. Only the
  current ticket's stored files (Linear's file storage, never a link or another ticket's
  file), only inside the workspace, at most 25 MB. Offered with trackers that store files;
  the prompt in `WORKFLOW.md` names them. Before, the agent got 401 on its own earlier
  attachments.

### Changed

- The merger precheck's private-key pattern now spans the whole key block up to its `END`
  line (it still flags the header line alone), so a redaction removes the key, not only its
  first line.

### Fixed

- A ticket whose repository set changed (a `repo:` label added or removed, or a ticket that
  first ran in the default repository) no longer reuses the old workspace. The workspace pod
  and its session volume record the set they were built for; when the ticket's set differs,
  the runner deletes both and builds a new workspace, with a GitHub token minted for the new
  set and a fresh engine session (local mode: the workspace and session directories). The
  session report notes the rebuild. A set that changes while a run works ends that run at the
  next turn boundary, and the next dispatch gets the new workspace instead of continuing in
  the stale one.

## [0.2.20] - 2026-11-24

### Added

- The assistant can send a file it made in its sandbox: `share_file(path, title, comment?)`
  posts a video, screenshot, PDF, HTML page, spreadsheet or document in the thread, so the
  person gets the file instead of pasted text. It is offered with `sandbox_run`, to the same
  mapped people, and reads only files under the thread's repository clones or `/tmp`, after
  `realpath`. It refuses `.git`, the credential files (`.git-credentials`, `.netrc`, `.npmrc`,
  `.config/gh`, `.ssh`, `.aws`, `.kube`), any file holding a credential, and files over
  25 MB, with advice for a smaller video. Slack uses the external upload and needs the bot
  scope `files:write`; Discord posts a multipart message and needs **Attach Files**. Each
  share is audited as `assistant.file_shared`. With `ticket`, the thread also gets
  Attach / Dismiss buttons: a click by a mapped person attaches the file (up to 10 MB, kept
  one hour) to that ticket through the tracker, once, audited as
  `assistant.evidence_attached`. The Slack and Discord mocks serve the upload calls.

## [0.2.19] - 2026-11-24

### Added

- Notifications about a ticket now also reach its assignee as a direct message from the
  Slack or Discord bot, when `notify.users` maps them (GitHub login, or the e-mail or name
  Linear has); the channel message is unchanged. An agent's question goes by DM to the
  person who decides and to the assignee, with the same buttons: a click or a reply in the
  DM's thread answers it. `notify.direct_messages: false` keeps notifications in the
  channel only, and `notify.direct_events` limits DMs to some events; the workflow API
  reads and writes both. Slack needs only `chat:write`; Discord opens the DM with
  `POST /users/@me/channels`.

### Changed

- An agent's question (`symphony_ask`) now takes its answer where people give it. While the
  ticket has exactly one open question, a person's reply to any comment Symphony wrote on
  the ticket, a new top-level comment on the ticket, and a comment or review on the ticket's
  open pull request all answer it; before, only a reply to the question's own comment, Slack,
  Tern and the CLI did. With two or more open questions, only a reply to a question's own
  comment counts on the ticket. Symphony records the id of every comment it writes on the
  tracker (the agent's included), and those never answer, even in the question's thread;
  a person who runs Symphony with their own API key still answers from that account.
  Comments that Symphony wrote before this version are not recorded, so a reply to one of
  them does not count. Bots and the GitHub App never answer. A pull request comment that
  answers is not answered again by the review watch. The answer note says `on the pull
  request`, and the question on the ticket and in Slack says where to answer.
- An agent's question in Slack reads as a message of its own. It starts with a divider, so
  two questions in a row no longer run together. One header line follows: the ticket key and
  title, linked, and who decides. Then the question exactly as written, the options as
  buttons only (listed as text just when a button cuts one), and a one-line footer. The
  notification text is the header and the start of the question. An answered or closed
  question keeps its header. `symphony_ask` sends back to the agent a question over 1200
  characters, sub-questions run together on one line, more than 5 options or an option over
  60 characters. Its description asks for the decision first, short context and one
  decision at a time.

## [0.2.18] - 2026-11-24

### Fixed

- Two skill sources from one repository (different paths) shared one cache entry, so every
  such source delivered the skills of whichever was fetched first. The cache key now holds
  the path.
- `GET /v1/merger` never carried `label_filter`: the merger kept it in memory and the API
  reads Redis. It is now stored with the repository's merger state.

## [0.2.17] - 2026-11-24

### Fixed

- `PUT /v1/setup/skills` (`symphony setup skills`, Tern Setup) published the change but
  answered 422 `invalid_audit`, because `setup.skills` was not an audited action. Sources
  are now keyed by repository and path, so one repository can hold several (`global`,
  `artifacts`); `--remove` with `--path` removes only that source.
- `GET /v1/admin/skills` (Admin › Skills) always listed no source: it parsed a workflow
  field the read did not return. A source limited to some roles now resolves with one of
  them, so its skills show.
- The merger and the assistant staged skills only from sources that existed when they
  started: a source published later reached their agents only after a restart. Both now
  read the sources at each agent run. The assistant gets global sources and the ones scoped
  to a repository it can read, as documented, instead of every source.

## [0.2.16] - 2026-11-24

### Changed

- The merger now reviews only pull requests that carry at least one of the labels in
  `merger.label_filter.labels` (default `["symphony", "symphony-learnings"]`) or the
  `review-bypass` label. Set `merger.label_filter.enabled: false` to review every PR as
  before. The toggle takes effect without restarting the merger.

### Added

- Workflow keys `merger.label_filter.enabled` (boolean, default true),
  `merger.label_filter.labels` (list of label names) and `merger.label_filter.repos`
  (per-repository override, `owner/name` → boolean). Visible in Admin › Workflow and
  `symphony api`.
- The merger status payload (`GET /v1/merger`) includes the effective label filter per
  repository (`"label_filter": {"active": true, "labels": [...]}`); the Tern Merger tab
  shows it in a **Takes** column. A change logs `merger_label_filter_changed`.

## [0.2.15] - 2026-11-24

### Fixed

- The workflow API (Admin › Workflow, `symphony setup skills`, Tern Setup) rejected
  `skills.sources` entries as "not an array of strings", and would have failed to read a
  workflow that already had sources. Entries are now validated by the same rules as
  WORKFLOW.md and read back in the same form.

## [0.2.14] - 2026-11-24

### Added

- Skill sources (`skills.sources`): directories of skills in repositories the GitHub App
  reaches, delivered to the agents besides the skills of the repository they work in. A
  source without `repos:` is global; with `repos:` it reaches only those repositories;
  `roles:` limits it to some roles (the assistant's planner and researcher included).
  Sources resolve to a commit (cached ten minutes), are staged into each agent's user
  skills directory (the workspace pod, the merger's and the assistant's agents) and the
  session records which skills and commit it got. Admin › Skills in Tern, `GET
  /v1/admin/skills`, an optional Skills setup step (`symphony setup skills`, Tern Setup).
  `docs/skills.md`. Each skill arrives as its whole folder: supporting text files at any
  depth (templates, scripts) keep their paths and executable bit.

### Changed

- One merger serves every repository with a `.symphony/merger.md` policy on its default
  branch (from discovery), plus `kubernetes.default_repo` and `SYMPHONY_MERGER_REPO`
  (now a list). It starts and stops repositories as policies come and go; each keeps its
  own train, lease and review workers. At most `SYMPHONY_MERGER_CONCURRENCY` (default 8)
  reviews run at once across repositories. The merger Deployment requests 2 CPU / 4 GiB
  (limits 8 CPU / 16 GiB). A repository other than the default one keeps its mirror under
  `repos/<owner>__<name>/` in the merger's volume.

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
