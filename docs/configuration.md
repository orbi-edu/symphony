# Configuration

Symphony has three kinds of configuration:

| Kind | Where | Who changes it |
|---|---|---|
| Policy and agent prompt | `WORKFLOW.md` (ConfigMap `symphony-workflow`, or a file in local mode) | admin |
| Agent role models | Team configuration (Redis), published from Tern or the CLI | admin |
| Credentials and endpoints | Environment variables from Kubernetes Secrets, or the host environment | operator of the cluster |

This document is the reference for all three. [providers.md](providers.md) explains the model credentials.

## Contents

- [`WORKFLOW.md` format](#workflowmd-format)
- [How Symphony loads `WORKFLOW.md`](#how-symphony-loads-workflowmd)
- [`tracker`](#tracker)
- [`polling`](#polling)
- [`workspace`](#workspace)
- [`hooks`](#hooks)
- [`agent`](#agent)
- [`engine`](#engine)
- [`kubernetes`](#kubernetes)
- [`telemetry`](#telemetry)
- [`review_watch`](#review_watch)
- [`notify`](#notify)
- [`assistant`](#assistant)
- [`retention`](#retention)
- [`backup`](#backup)
- [Retired keys](#retired-keys)
- [Prompt template](#prompt-template)
- [Team configuration](#team-configuration)
- [Environment variables](#environment-variables)
- [Kubernetes Secrets](#kubernetes-secrets)

## `WORKFLOW.md` format

`WORKFLOW.md` starts with YAML front matter between two `---` lines. The Liquid template of the agent prompt follows it.

```markdown
---
tracker:
  kind: linear
  provider:
    team_key: "ENG"
    api_key: $LINEAR_API_KEY
  required_labels: [symphony]
kubernetes:
  default_repo: example-org/example-repo
---
You are working on **{{ issue.identifier }}**: {{ issue.title }}.

{{ issue.description }}
```

The [`WORKFLOW.md`](../WORKFLOW.md) at the root of the repository is a complete example with Linear. [`docs/examples/WORKFLOW.github.md`](examples/WORKFLOW.github.md) is the same policy with the GitHub tracker. The other files in [`docs/examples/`](examples/) show the build steps of other stacks.

Rules:

- A value that starts with `$` is a reference to an environment variable. Symphony reads the variable on the trusted host when it uses the value. A missing variable is an error.
- Symphony ignores unknown keys.
- A wrong type is an error. For example, `true` for an integer key is an error.
- A `null` value or an absent key gives the default.

## How Symphony loads `WORKFLOW.md`

Each process reads `WORKFLOW.md` from `$SYMPHONY_WORKFLOW_MOUNT/WORKFLOW.md` (default `/workflow/WORKFLOW.md`). In local mode, the process reads the file from `--workflow`.

- Symphony reloads the file when it changes. It parses and validates the new version first.
- If the new version is not valid, Symphony keeps the last valid version and records the error. An invalid version never takes effect.
- On Kubernetes, publish a new version with `PUT /v1/workflow` (token role `admin`, with `If-Match` and a reason). The API validates the change and the daemon writes the ConfigMap as a durable, audited command. `POST /v1/workflow/validate` checks a change with no publication. [api.md](api.md) gives the request format. Use `scripts/sync_workflow.sh` only for recovery.
- A new install has an empty ConfigMap `symphony-workflow`. Until the first publication, the API serves the WORKFLOW that the image ships (`SYMPHONY_DEFAULT_WORKFLOW`). The first-run setup publishes the `tracker` section and `kubernetes.default_repo` into it directly, with the reason `setup: <step>`. In local mode, setup writes the `--workflow` file.
- The kubelet updates the ConfigMap mounts in about one minute.

> **Note:** The API publication applies more checks than the parser. For example, `kubernetes.runner.wallclock_guard_s` must be less than both `kubernetes.runner.timeout_s` and `kubernetes.sandbox.timeout_s`.

## `tracker`

| Key | Type | Default | Effect |
|---|---|---|---|
| `tracker.kind` | string | required | `linear`, `github` or `memory`. `github` uses issues on a GitHub Projects (v2) board. `memory` keeps tickets in the process (tests, kind, local mode). With `memory`, a ticket with no `repo:` label gets the repository `memory/repo`, and `kubernetes.default_repo` does not apply. |
| `tracker.provider.api_key` | string | `$LINEAR_API_KEY` | Linear only. The Linear API key, as an environment reference. With `linear`, it must resolve to a non-empty value. |
| `tracker.provider.project_slug` | string | `""` | Linear only. Scope the board to one Linear project. Use the project's slug ID (the text after the last `-` in its URL). This scope wins when both scopes are set. |
| `tracker.provider.team_key` | string | none | Linear only. Scope the board to all projects of one Linear team (for example `ENG`). With `linear`, set `project_slug` or `team_key`. |
| `tracker.provider.assignee` | string | none | Linear only. Dispatch only tickets assigned to the person with this name. |
| `tracker.provider.owner` | string | required with `github` | The GitHub organization or user that owns the project (a valid GitHub login). |
| `tracker.provider.project_number` | integer | required with `github` | The positive number of the project: the `N` in `https://github.com/orgs/<owner>/projects/N`. |
| `tracker.provider.status_field` | string | `Status` | The single-select field of the project whose options are the ticket states. |
| `tracker.active_states` | list of strings | `[Todo, In Progress, Rework]` | The states where the implementer works. |
| `tracker.terminal_states` | list of strings | `[Closed, Cancelled, Canceled, Duplicate, Done]` | The states that end a ticket. They trigger the cleanup and the retrospective. |
| `tracker.required_labels` | list of strings | `[]` | A ticket needs all these labels to be dispatched. The comparison ignores case. |
| `tracker.states.review` | string | `In Review` | The state that the implementer sets when it hands over to the merger. It must not be active. |
| `tracker.states.rework` | string | `Rework` | The state that the merger sets when it blocks a pull request. It must be active. |
| `tracker.states.merging` | string | `Merging` | The state that the merger sets when the train takes the pull request. It must not be active. |
| `tracker.states.done` | string | `Done` | The state of a merged ticket. With Linear, the Linear–GitHub integration sets it. With GitHub, the project's **Item closed** workflow sets it when the pull request closes the issue. It must be terminal. |
| `tracker.investigation_label` | string | `investigation` | Tickets with this label are investigations. It must not be empty. The value is compared in lowercase. |
| `tracker.issues` | list | `[]` | Only with `kind: memory`. The seed tickets. Each item has `id`, `identifier` and `title` (required, non-empty), `description`, `state` (default `Todo`) and `labels`. Other keys are errors. IDs and identifiers must be unique. |

`tracker.states` accepts only the keys `review`, `rework`, `merging` and `done`.

With `kind: github`, `tracker.provider` accepts only `owner`, `project_number` and `status_field`. Another key is an error.

### GitHub tracker

With `tracker.kind: github`, a ticket is an issue on the project board. Draft items and pull requests on the board are not tickets.

| Ticket property | GitHub source |
|---|---|
| State | The option of the `status_field` field. Each state name in `WORKFLOW.md` must be an option of that field. The comparison ignores case. A closed issue is terminal, whatever its option is. |
| Identifier | `<repository name>-<issue number>` in lowercase, for example `app-42`. |
| Branch name (`issue.branch_name`) | `symphony/<identifier>`, for example `symphony/app-42`. |
| Repositories | The issue's own repository, unless the issue has `repo:owner/name` labels. `kubernetes.default_repo` does not apply. |
| Priority | The optional project field `Priority`: a single-select field (the first option is the most urgent) or a number field. |
| Blockers | The issue's "blocked by" dependencies. A blocker is terminal when it is closed or its option on this board is terminal. |
| Sub-issues | `symphony_create_issues` makes GitHub sub-issues in the parent's repository, adds them to the board in the first entry of `tracker.active_states`, and links their order as "blocked by" dependencies. |
| Evidence | `symphony_attach_evidence` posts text evidence (`text/*`, JSON, logs) as a collapsible comment of at most 65,536 characters. GitHub has no public API to upload a file to an issue, so binary evidence is refused. |
| Agent tool | `github_graphql`, which the runner executes on the trusted side. |
| Close and reopen | When Symphony moves a ticket to a terminal state, it closes the issue: `completed` for `tracker.states.done`, `not_planned` for the other terminal states. A move to any other state reopens the issue. |
| Pull request link | The merger finds the ticket of a pull request by its branch `symphony/<identifier>`, or by `owner/repo#n`, the issue URL, or a closing keyword with `#n` (for example `Closes #12`) in the body. |
| Credential | The GitHub App installation token. [install.md](install.md#5-make-the-github-app) lists the permissions. |

## `polling`

| Key | Type | Default | Effect |
|---|---|---|---|
| `polling.interval_ms` | integer | `15000` | The safety poll of the daemon. Webhooks wake the daemon at once, so this value limits only the delay of a missed webhook. It must be positive. |

## `workspace`

| Key | Type | Default | Effect |
|---|---|---|---|
| `workspace.root` | string | `/workspace` | The working directory of the agent in the workspace pod. The runner also uses it to check which repository the workspace holds. |

## `hooks`

Hooks are shell scripts that run with `bash -lc` in the workspace.

| Key | Type | Default | When it runs | Failure |
|---|---|---|---|---|
| `hooks.after_create` | string | none | Once, when the runner makes a new workspace. Use it to clone the repositories. | Fatal: the attempt fails. |
| `hooks.before_run` | string | none | At the start of each attempt. | Fatal: the attempt fails. |
| `hooks.after_run` | string | none | At the end of each attempt. | Logged (`hook_failed_ignored`) and ignored. |
| `hooks.before_remove` | string | none | Before the workspace is deleted. | Logged and ignored. |
| `hooks.timeout_ms` | integer | `60000` | The time limit of each hook. It must be positive. | — |

The hooks get these variables:

| Variable | Value |
|---|---|
| `SYMPHONY_TARGET_REPO` | The primary repository, `owner/name`. |
| `SYMPHONY_TARGET_REPOS` | All the ticket's repositories, primary first, separated by spaces. |
| `SYMPHONY_BASE_BRANCHES` | Space-separated `owner/name=branch` pairs for target repos with a base branch override (`kubernetes.base_branches`). Absent when no repo has an override. |
| `SYMPHONY_WORKSPACE` | Local mode only: the workspace directory (also the current directory). |
| `GITHUB_SERVER_URL` and the other endpoint variables | Only when they are set on the host. See [Endpoints](#endpoints). |

The git credential helper is ready before `after_create` runs, so `git clone` over HTTPS works with no token in the URL.

## `agent`

| Key | Type | Default | Effect |
|---|---|---|---|
| `agent.max_concurrent_agents` | integer | `5` | The maximum number of attempts that run at the same time. |
| `agent.max_concurrent_agents_by_state` | map of state to integer | `{}` | A lower limit for one state, for example `{Rework: 1}`. State names ignore case. Symphony ignores values that are not positive integers. |
| `agent.max_turns` | integer | `20` | The turn cap of one attempt. At the cap, the attempt ends as a continuation. It must be positive. |
| `agent.max_retry_backoff_ms` | integer | `300000` | The maximum wait before a retry after a failed attempt. |
| `agent.input_timeout_ms` | integer | `900000` | How long `symphony_ask` waits for a person. Also how long an interrupted agent waits for a message. It must be positive. |
| `agent.stall_timeout_ms` | integer | `300000` | The silence that counts as a stall. Two windows in sequence stop the attempt. Zero or a negative value turns stall detection off. |

> **Caution:** The engine sends no events while a tool runs. Set `agent.stall_timeout_ms` longer than the longest build or test command. The example `WORKFLOW.md` uses 30 minutes.

## `engine`

| Key | Type | Default | Effect |
|---|---|---|---|
| `engine.command` | string | `omp --mode rpc` | The engine command. The runner adds `--no-title --auto-approve --session-dir <dir> --provider <p> --model <m> --thinking <t>`. In local mode, use the absolute path of the pinned `omp`. |
| `engine.provider` | string | none | The fallback provider of the implementer and the reflector before the first team configuration publication. Set it together with `engine.model`. |
| `engine.model` | string | none | The fallback model. Set it together with `engine.provider`. |
| `engine.thinking` | string | none | The fallback thinking level: `off`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`. Without it, the built-in default level of the role applies. |
| `engine.turn_timeout_ms` | integer | `3600000` | The maximum silence of the engine stream in one turn. Each event resets it. When it passes, the attempt fails. |
| `engine.read_timeout_ms` | integer | `5000` | The time limit of one engine command and its response. Startup commands get at least 30 seconds. |
| `engine.auto_retry` | boolean | `true` | Symphony parses this key. The current code does not use it. |

## `kubernetes`

On Kubernetes, these keys control the workspaces and the Jobs. Local mode ignores all of them except `kubernetes.learning`.

| Key | Type | Default | Effect |
|---|---|---|---|
| `kubernetes.app_name` | string | `symphony` | The `app` label of the pods that Symphony makes, and the prefix of derived names (`<app_name>-runner`, `<app_name>-workflow`, `<app_name>-gateway`). |
| `kubernetes.namespace` | string | `null` | The namespace of the workspaces and the Jobs. `null` means the namespace of the daemon. |
| `kubernetes.agent_runtime` | string | `node` | The image family of the workspace: `node`, `dotnet9`, `dotnet10` or `polyglot`. It applies to all tickets. An unknown value falls back to `node` with the warning `unknown_agent_runtime_using_default`. |
| `kubernetes.agent_images` | map of runtime to image | `{}` | Image references for each runtime. They replace the built-in values `symphony-agent:<runtime>`. Point them at your registry. |
| `kubernetes.agent_image` | string | `null` | One image for all workspaces. It wins over `agent_images` and `agent_runtime`. |
| `kubernetes.privileged_agents` | boolean | `false` | Run the workspace container as privileged, so `dockerd` can start. |
| `kubernetes.runtime_class` | string | `null` | The RuntimeClass of the workspace pods, for example `sysbox-runc` or a Kata class. It does not apply to Symphony's own Jobs. |
| `kubernetes.sessions_enabled` | boolean | `true` | Keep the engine sessions on a PVC for each workspace (`ws-<ticket>-omp`). With `false`, an `emptyDir` holds them and they end with the pod. |
| `kubernetes.storage_class` | string | `null` | The StorageClass of the session PVCs. `null` means the cluster default. |
| `kubernetes.session_volume_size` | string | `1Gi` | The size of each session PVC. |
| `kubernetes.image_pull_secrets` | list of strings | `[]` | The pull Secrets of the workspace pods. |
| `kubernetes.default_repo` | string | `null` | The repository (`owner/name`) of tickets that have no `repo:` label. A ticket's own label always wins. The merger also uses it when `SYMPHONY_MERGER_REPO` is empty. |
| `kubernetes.base_branches` | map of repo to branch | `{}` | Per-repository base branch override (`owner/name` → branch). When set, the merger, assistant, reflector and workspace treat this branch as the repository's main branch instead of the auto-detected remote default. |
| `kubernetes.egress_allowlist` | list of strings | `[]` | Symphony parses this key. The current code does not enforce it. A plain NetworkPolicy filters by IP address, not by domain name. |
| `kubernetes.redispatch_cooldown_s` | integer | `900` | The cooldown of a continuation when the ticket did not change. See [concepts.md](concepts.md#cooldown). |
| `kubernetes.sandbox.cpu` | number | `2` | The CPU request and limit of a workspace pod. |
| `kubernetes.sandbox.memory_mib` | integer | `4096` | The memory request and limit of a workspace pod, in MiB. |
| `kubernetes.sandbox.timeout_s` | integer | `14400` | The maximum life of a workspace pod (`activeDeadlineSeconds`). |
| `kubernetes.sandbox.idle_timeout_s` | integer | `1800` | The daemon deletes a workspace pod that is idle for longer than this. The session PVC stays. |
| `kubernetes.runner.timeout_s` | integer | `14400` | The maximum life of a runner Job (`activeDeadlineSeconds`). |
| `kubernetes.runner.wallclock_guard_s` | integer | `12600` | The runner ends the attempt cleanly as a continuation after this time. Keep it below both timeouts. |
| `kubernetes.learning.enabled` | boolean | `true` | Run retrospectives on terminal tickets. |
| `kubernetes.learning.reflect_on` | list of strings | `null` | The terminal states that start a retrospective. `null` means all `tracker.terminal_states`. |
| `kubernetes.learning.agents_md_budget_lines` | integer | `100` | The line budget of the curated `AGENTS.md`. |
| `kubernetes.learning.learnings_label` | string | `symphony-learnings` | The label of learnings pull requests. |
| `kubernetes.learning.decay_after_incorporations` | integer | `10` | A lesson that no retrospective used in this number of changes decays. |

### Docker in the workspace

The agent image includes the Docker engine. Choose one posture:

| Posture | Settings | Isolation |
|---|---|---|
| No Docker | `privileged_agents: false`, `runtime_class: null` | Strongest default. `before_run` reports `docker: unavailable`. |
| Sysbox | `privileged_agents: false`, `runtime_class: sysbox-runc` | `dockerd` runs without privileged mode through user namespaces. The nodes need Sysbox. |
| Kata | `privileged_agents: true`, `runtime_class: <kata class>` | A VM for each pod contains the privileged mode. The nodes need virtualization. |
| Privileged | `privileged_agents: true`, `runtime_class: null` | Weakest. Root in a privileged pod is close to root on the node. |

> **Warning:** Privileged mode without a VM runtime lets agent code control the node. Use it only on a node that runs nothing else of value.

When Docker is possible, the workspace gets an `emptyDir` at `/var/lib/docker`.

## `telemetry`

The engine sends OpenTelemetry spans. By default it sends them to the gateway. The orchestrator keeps a bounded rollup and posts one report to the ticket when the work parks for review, with the token cost.

| Key | Type | Default | Effect |
|---|---|---|---|
| `telemetry.enabled` | boolean | `true` | Send engine telemetry. |
| `telemetry.endpoint` | string | `null` | An absolute OTLP/HTTP traces URL of your own collector. When set, the spans go there instead of the gateway, and the ticket report has no span summary. |
| `telemetry.capture_content` | string | `none` | `none`, `summary` or `full`. `summary` and `full` put prompts and tool payloads in the spans. |

> **Caution:** Keep `capture_content: none` unless your collector may store source code and ticket text.

## `review_watch`

| Key | Type | Default | Effect |
|---|---|---|---|
| `review_watch.enabled` | boolean | `false` | Start answer runs when people comment on the pull request of a parked ticket. See [concepts.md](concepts.md#review-watch). |
| `review_watch.interval_s` | integer | `120` | The safety poll of each ticket. GitHub comment webhooks make the check immediate. |
| `review_watch.states` | list of strings | `[]` | The parked states to watch. Empty means all parked states. |
| `review_watch.answer_max_turns` | integer | `3` | The turn cap of an answer run. It must be positive when the watch is on. |
| `review_watch.answer_prompt` | string | `""` | The Liquid prompt of an answer run. It is required when the watch is on. |

## `notify`

| Key | Type | Default | Effect |
|---|---|---|---|
| `notify.users.slack` | map of string to string | `{}` | GitHub login to Slack member ID (`U…`), for mentions in Slack notifications. |
| `notify.users.discord` | map of string to string | `{}` | GitHub login to Discord user ID (a number, for example `"123456789012345678"`), for mentions in Discord notifications. Quote the ID. Symphony also accepts an unquoted number. |

Rules:

- `notify.users` accepts only the keys `slack` and `discord`. The old flat map (login to Slack ID) is an error.
- Logins ignore case. Keys and values must be non-empty.
- A Discord user ID must contain only digits.

```yaml
notify:
  users:
    slack:
      example-login: U01234567
    discord:
      example-login: "123456789012345678"
```

Symphony sends each notification to every configured channel: Slack, Discord, or both. With no channel configured, it sends nothing. See [Environment variables](#environment-variables).

## `assistant`

The chat assistant on Slack and Discord ([assistant.md](assistant.md)). It is off unless `enabled` is true. `symphony setup assistant` writes this section.

| Key | Type | Default | Effect |
|---|---|---|---|
| `assistant.enabled` | bool | `false` | Turns the assistant on. Off, it idles and logs `assistant_disabled` once a minute. |
| `assistant.slack.channels` | list of channel IDs | `[]` | Slack channels where it answers @mentions. DMs always work when it is on. |
| `assistant.discord.channels` | list of channel IDs | `[]` | Discord channels where it answers @mentions. An ID is a number. Quote it. |
| `assistant.repositories` | list of `owner/name` | every repository Symphony knows | The repositories it reads, as read-only worktrees. Unset: the default repository, the merger repositories and those of tracked tickets. |
| `assistant.max_concurrent` | int, above 0 | `3` | Turns that run at the same time. More turns wait. |
| `assistant.turn_timeout_s` | int, above 0 | `600` | The longest turn, in seconds. |
| `assistant.ticket_state` | string | `"Backlog"` | The state of tickets created with **Create**. |
| `assistant.ticket_labels` | list of strings | `[]` | Extra labels on created tickets. **Create and start** also adds `tracker.required_labels`. |
| `assistant.soul.repo` | `owner/name` | the first `assistant.repositories` entry, else `kubernetes.default_repo` | The repository of the soul file ([assistant.md](assistant.md#soul)). It is mirrored read-only. |
| `assistant.soul.path` | relative path | `".symphony/assistant.md"` | The soul file. It is read from `origin/main` at each new session, at most 8 KiB. Missing: the built-in text. |
| `assistant.memory.enabled` | bool | `true` | Per-person memory for people mapped in `notify.users` ([assistant.md](assistant.md#memory)). |
| `assistant.memory.ttl_days` | int, above 0 | `90` | Days a person's memory lives. Each write and each session that reads it renews it. |

Unknown keys, also inside `slack`, `discord`, `soul` and `memory`, are an error.

```yaml
assistant:
  enabled: true
  slack: {channels: ["C0123ABCD"]}
  discord: {channels: ["123456789012345678"]}
  repositories: ["acme/app"]
  max_concurrent: 3
  turn_timeout_s: 600
  ticket_state: "Backlog"
  ticket_labels: []
  soul: {repo: "acme/app", path: ".symphony/assistant.md"}
  memory: {enabled: true, ttl_days: 90}
```

Only a person mapped in [`notify.users`](#notify) can create tickets from a proposal.

## `retention`

How long Redis keeps what ended. The lease holder publishes the values on each tick, so every
component applies the same ones ([backup.md](backup.md#the-archive)).

| Key | Default | Bounds | Meaning |
|---|---|---|---|
| `sessions_days` | `7` | 2-365 | An ended session (meta, events, transcript, intents) expires this many days after it ends. |
| `audit_days` | `30` | 2-3650 | Finished commands and audit entries leave Redis after this many days. |

The nightly archive stores both before Redis drops them. The lower bound of 2 days makes sure a
daily run sees each entry first.

## `backup`

The target of the nightly backup and archive ([backup.md](backup.md)). Give `s3` or `dir`,
not both. Without either, the `symphony-backup` CronJob idles.

| Key | Default | Meaning |
|---|---|---|
| `keep` | `7` | The number of backups to keep (1-365). The archive is never pruned. |
| `s3.endpoint` | — | An `http(s)` URL with no path, for example `https://s3.eu-west-1.amazonaws.com`. The store must accept path-style URLs. |
| `s3.bucket` | — | The bucket name. |
| `s3.prefix` | `""` | A key prefix, for example `symphony/`. Letters, digits, `.`, `_`, `-` and `/`. |
| `s3.region` | `us-east-1` | The signing Region. |
| `dir` | — | An absolute directory instead of S3 (local mode, or a volume you mount). |

The S3 access keys are never in `WORKFLOW.md`. They are `SYMPHONY_BACKUP_ACCESS_KEY_ID` and
`SYMPHONY_BACKUP_SECRET_ACCESS_KEY` in the `symphony-backup` Secret.

```yaml
retention: {sessions_days: 7, audit_days: 30}
backup:
  keep: 7
  s3: {endpoint: "https://s3.eu-west-1.amazonaws.com", bucket: acme-symphony-backups,
       prefix: symphony/, region: eu-west-1}
```

## Retired keys

These keys make the file invalid, so a migrated file cannot keep a setting that has no effect.

| Key | Reason |
|---|---|
| `codex` (section) | Renamed to `engine`. |
| `modal` (section) | Renamed to `kubernetes`. `snapshot_image` is now `agent_image`, `vm_runtime` is now `privileged_agents`, `environment` is now `namespace`. |
| `engine.approval_policy`, `engine.thread_sandbox`, `engine.turn_sandbox_policy` | The engine has no approval or sandbox layer. The container is the isolation. |
| `kubernetes.pre_pr_review` | The merger reviews every pull request. |

## Prompt template

The body after the front matter is a strict Liquid template. An unknown variable or filter is an error. Symphony checks the syntax when it loads the file, and the variables when it renders the prompt.

| Variable | Type | Value |
|---|---|---|
| `attempt` | integer or null | The run number. It is null on the first run, so `{% if attempt %}` detects a later run. |
| `issue.id` | string | The tracker ID. |
| `issue.identifier` | string | The ticket key, for example `ENG-123`. |
| `issue.title` | string | The title. |
| `issue.description` | string or null | The description. |
| `issue.state` | string | The current state name. |
| `issue.state_type` | string | `active`, `terminal` or `unknown`. |
| `issue.priority` | integer or null | The priority. |
| `issue.created_at`, `issue.updated_at` | string or null | ISO-8601 times. |
| `issue.labels` | list of strings | The labels in lowercase. |
| `issue.assignee`, `issue.assignee_id` | string or null | The assignee. |
| `issue.url` | string or null | The ticket URL. |
| `issue.branch_name` | string or null | The tracker's branch name for the ticket (with GitHub, `symphony/<identifier>`). |
| `issue.native_ref` | string or null | The tracker's own reference. With GitHub it is `owner/repo#number`, which a pull request can close with `Closes {{ issue.native_ref }}`. |
| `issue.blocked_by` | list | The tickets that block this one and are not terminal. |
| `issue.target_repo` | string or null | The primary repository. |
| `issue.target_repos` | list of strings | All repositories, primary first. |
| `issue.investigation` | boolean | True when the ticket has `tracker.investigation_label`. |

On the first turn, the runner adds the failure summary of the previous attempt after the rendered prompt, when there is one.

## Team configuration

The team configuration gives each agent role a model. It lives in Redis, not in `WORKFLOW.md`. An admin publishes it from Tern (Admin › Models) or with the CLI:

```sh
symphony admin config show
symphony admin config set reviewer=anthropic/claude-sonnet-5:high --reason "Use Anthropic for reviews"
symphony admin config observed
```

The document has two sections:

| Section | Content |
|---|---|
| `models` | One entry for each of the ten agent roles: `{provider, model, thinking}`. All ten must be present. |
| `omp` | `version` and `settings`. `symphony omp sync` writes the settings into a person's `omp` profile `symphony`. |

The built-in default (generation 0) is `DEFAULT` in [`src/symphony/teamconfig.py`](../src/symphony/teamconfig.py). It uses the provider `amazon-bedrock` for all ten agent roles. The `oracle` and the `arbiter` use model families that are different from the `reviewer`'s family. An account without these models must publish its own team configuration. Before the first publication, the fallbacks are `engine.provider` and `engine.model` (implementer and reflector) and `models` in `.symphony/merger.md` (merger roles).

The roles are `implementer`, `reviewer`, `oracle`, `arbiter`, `developer`, `reflector`, `probe`, and the three assistant roles:

| Role | Default model | Thinking | Used for |
|---|---|---|---|
| `assistant` | `amazon-bedrock/global.anthropic.claude-haiku-5-5` | `low` | Questions in chat |
| `researcher` | `amazon-bedrock/global.moonshotai.kimi-k3.1` | `high` | `research …` messages |
| `planner` | `amazon-bedrock/global.anthropic.claude-haiku-5-5` | `medium` | `plan …` messages and ticket proposals |

Each role can also get MCP servers ([mcp.md](mcp.md#roles)).

Publication rules:

- Each publication needs a reason of 1 to 500 characters.
- Each publication needs the current generation (`If-Match`). A stale generation gives HTTP 412. A concurrent publication gives HTTP 409.
- The `oracle` and the `arbiter` must differ from the `reviewer` in provider or model.
- Provider and model IDs must match `[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}`.
- Symphony keeps the last 50 generations.

The precedence of each consumer is in [concepts.md](concepts.md#precedence).

## Environment variables

### Credentials and services

| Variable | Used by | Purpose |
|---|---|---|
| `REDIS_URL` | all processes | The Redis connection, for example `redis://symphony-redis:6379/0`. |
| `LINEAR_API_KEY` | API, daemon, runner, merger, sweep, reflector | The Linear API key. Not used with `tracker.kind: github`. |
| `LINEAR_WEBHOOK_SECRET` | API | The signing secret of the Linear webhook. |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_INSTALLATION_ID` | daemon, runner, merger, sweep, reflector | The GitHub App credential. With `tracker.kind: github`, it is also the tracker credential. |
| `GITHUB_WEBHOOK_SECRET` | API | The secret of the GitHub App webhook. |
| `SYMPHONY_BOOTSTRAP_TOKEN` | API, local mode | The first admin API token, `sym_<8 hex>_<43 characters>`. |
| `SYMPHONY_PUBLIC_URL` | API | The public base URL of the API, for example `https://symphony.example.com`. Setup builds the webhook URLs from it. Set it behind a proxy, or when the webhook URLs must use a public host. When empty, setup uses the address of the request. On Kubernetes it is in the ConfigMap `symphony-api-config`. |
| `SLACK_BOT_TOKEN` and `SLACK_CHANNEL` | all notifiers | Slack Web API (`chat.postMessage`, scope `chat:write`). This method wins when both methods are set. |
| `SLACK_WEBHOOK_URL` | all notifiers | A Slack incoming webhook. |
| `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` | all notifiers | A Discord bot (`POST /channels/{id}/messages`). The bot needs the permissions View Channel and Send Messages in the channel. This method wins when both Discord methods are set. |
| `DISCORD_WEBHOOK_URL` | all notifiers | A Discord channel webhook (`https://discord.com/api/webhooks/<id>/<token>`). |
| `AWS_REGION`, `AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, … | engines (through the `model-provider` Secret or the host environment) | Host-level model credentials. See [providers.md](providers.md#host-fallback). |
| `SYMPHONY_BACKUP_ACCESS_KEY_ID`, `SYMPHONY_BACKUP_SECRET_ACCESS_KEY` | API, backup CronJob | The access keys of the S3 backup target (`backup.s3`). See [backup.md](backup.md). |

### Endpoints

The defaults are the public services. Set these variables for GitHub Enterprise Server or for the test mocks in `scripts/mocks/`. The daemon and the runner pass the set values to the Jobs, the workspace pods and the engine.

| Variable | Default | Used for |
|---|---|---|
| `LINEAR_API_URL` | `https://api.linear.app/graphql` | The Linear tracker, the host tools, the merger. |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub App tokens, REST calls of the runner, the reflector and the merger. |
| `GITHUB_GRAPHQL_URL` | `https://api.github.com/graphql` | GraphQL calls of the merger and of the GitHub tracker. |
| `GITHUB_SERVER_URL` | `https://github.com` | git remotes (clone, mirror, push), pull request URLs. |
| `SLACK_API_URL` | `https://slack.com/api` | The Slack Web API. |
| `DISCORD_API_URL` | `https://discord.com/api/v10` | The Discord API of the bot method. |

For GitHub Enterprise Server at `ghe.example.com`:

```sh
GITHUB_API_URL=https://ghe.example.com/api/v3
GITHUB_GRAPHQL_URL=https://ghe.example.com/api/graphql
GITHUB_SERVER_URL=https://ghe.example.com
```

### Deployment settings

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `POD_NAMESPACE` | none | all pods | The pod's own namespace (downward API). It is also the Redis key namespace. |
| `SYMPHONY_WORKFLOW_MOUNT` | `/workflow` | all pods | The directory of `WORKFLOW.md`. |
| `SYMPHONY_WORKFLOW_CONFIGMAP` | `<app_name>-workflow` | daemon, `scripts/sync_workflow.sh` | The ConfigMap name of `WORKFLOW.md`. The daemon writes it when an admin publishes a new version. |
| `SYMPHONY_ORCHESTRATOR_IMAGE` | none (required) | daemon, sweep | The image of the runner and reflector Jobs. `deploy/kustomization.yaml` keeps it equal to the daemon's image. |
| `SYMPHONY_RUNNER_SERVICE_ACCOUNT` | `<app_name>-runner` | daemon | The ServiceAccount of the runner Jobs. |
| `SYMPHONY_APP_NAME` | `symphony` | all pods | The application name for derived names. |
| `SYMPHONY_GATEWAY_URL` | `http://<app_name>-gateway.<namespace>.svc.cluster.local:8090` | runner | The gateway URL that the engine uses. |
| `SYMPHONY_SECRET_TRACKER` | `linear-api-key` | daemon | The Secret that the runner Jobs get for Linear. |
| `SYMPHONY_SECRET_GITHUB` | `github-app` | daemon | The Secret that the runner Jobs get for GitHub. |
| `SYMPHONY_SECRET_SLACK` | `symphony-slack` | daemon | The Slack Secret of the runner Jobs. |
| `SYMPHONY_SECRET_DISCORD` | `symphony-discord` | daemon | The Discord Secret of the runner Jobs. |
| `SYMPHONY_SECRET_PROVIDER` | `model-provider` | runner, reflector | The host-level model credential Secret. |
| `SYMPHONY_SECRET_PROVIDERS` | `symphony-providers` | API, daemon, runner, merger | The Secret of the provider keys saved from Tern. |
| `SYMPHONY_OMP_CATALOG` | `/opt/symphony/omp-catalog/models.json` | API | The model catalog of the pinned `omp`. |
| `SYMPHONY_DEFAULT_WORKFLOW` | `/opt/symphony/WORKFLOW.default.md` | API | The WORKFLOW that the image ships. The API serves it until the first publication writes the ConfigMap. |
| `SYMPHONY_HEALTH_PORT` | `8081` | daemon, merger, assistant, gateway | The port of `GET /healthz` and `GET /metrics` ([observability.md](observability.md)). |
| `SYMPHONY_COMMIT` | `unknown` | daemon, merger, assistant, gateway | The commit label of `symphony_build_info`. The image build sets it. |
| `PORT` | `8080` | local mode | The API port when `--port` is absent. |

### Merger

| Variable | Default | Purpose |
|---|---|---|
| `SYMPHONY_MERGER_REPO` | empty (then `kubernetes.default_repo`) | The repository (`owner/name`) of this merger Deployment. |
| `SYMPHONY_MERGER_WORKDIR` | `/var/lib/symphony/merger` | The mirror, the worktrees and the merger's engine sessions. |
| `SYMPHONY_MERGER_REVIEWERS` | `2` | The number of review workers. |
| `SYMPHONY_OMP_BIN` | `omp` | The engine binary of the merger. |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | the merger's built-in identity | The git identity of rebased commits. |

### Client

| Variable | Purpose |
|---|---|
| `SYMPHONY_URL` | The API URL. It wins over the saved CLI configuration. |
| `SYMPHONY_TOKEN` | The API token. It wins over the saved CLI configuration. |
| `SYMPHONY_OMP` | The `omp` binary that `symphony omp sync` and `symphony takeover` use. |
| `TERN_CONFIG_DIR` | The Tern configuration directory that `symphony tern install` changes. |
| `XDG_CONFIG_HOME` | The base of the CLI configuration (`$XDG_CONFIG_HOME/symphony/config.toml`). |

## Kubernetes Secrets

`deploy/secrets.example.yaml` is the template of the two Secrets that you make: `symphony-api` and `model-provider`. `deploy/integrations.yaml` makes the integration Secrets empty, and the first-run setup fills them. To fill them by hand, use `deploy/integrations.example.yaml` with `kubectl replace`, never `kubectl apply` ([install.md](install.md#10-configure-by-hand-without-setup)). Each variable lives in exactly one Secret.

| Secret | Keys | Required | Mounted in |
|---|---|---|---|
| `linear-api-key` | `LINEAR_API_KEY`, `LINEAR_WEBHOOK_SECRET` | no. Made empty; setup fills it. With `tracker.kind: github`, it stays empty. | API, daemon, merger, sweep, runner and reflector Jobs |
| `github-app` | `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_APP_INSTALLATION_ID` | no. Made empty; setup fills it. Required for work | API, daemon, merger, sweep, runner and reflector Jobs |
| `symphony-api` | `SYMPHONY_BOOTSTRAP_TOKEN` (you), `GITHUB_WEBHOOK_SECRET` (setup) | yes | API |
| `symphony-slack` | `SLACK_BOT_TOKEN` and `SLACK_CHANNEL`, or `SLACK_WEBHOOK_URL`; `SLACK_APP_TOKEN` (the assistant) | no | API, daemon, merger, sweep, assistant, Jobs |
| `symphony-discord` | `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`, or `DISCORD_WEBHOOK_URL` | no | API, daemon, merger, sweep, assistant, Jobs |
| `model-provider` | `AWS_REGION`, `AWS_BEARER_TOKEN_BEDROCK`, other provider keys | no | merger (`envFrom`); the runner and the reflector read it through the Kubernetes API |
| `symphony-providers` | `providers.json` | made empty by `deploy/providers.yaml` | written by the API; read by the runner, the reflector and the merger |
| `symphony-mcp` | `servers.json`, `headers.<name>.json` | made empty by `deploy/integrations.yaml` | written by the API (Admin › MCP); mounted read-only in the gateway; read by the runner, the reflector, the merger and the assistant ([mcp.md](mcp.md)) |
| `symphony-backup` | `SYMPHONY_BACKUP_ACCESS_KEY_ID`, `SYMPHONY_BACKUP_SECRET_ACCESS_KEY` | no. Made empty by `deploy/integrations.yaml`; fill it by hand with `kubectl replace` ([backup.md](backup.md#configure-the-target)) | API, backup CronJob |
| `cloudflared-token` | `TUNNEL_TOKEN` | only with `deploy/cloudflared/` | `symphony-cloudflared` |

> **Caution:** Do not edit `symphony-providers` by hand, and do not add `data` to `deploy/providers.yaml` or `deploy/integrations.yaml`. A later `kubectl apply` would then delete the keys that admins saved.

None of these Secrets enters a workspace pod. [security.md](security.md) explains what a workspace gets.
