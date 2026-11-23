# Troubleshooting

Each section gives a symptom, the checks to do, and the fix. The log events are structured fields in the logs of the named process. Use these commands to read the logs:

```sh
kubectl -n symphony logs deploy/symphony-daemon --since=1h
kubectl -n symphony logs deploy/symphony-api --since=1h
kubectl -n symphony logs deploy/symphony-merger --since=1h
kubectl -n symphony logs job/<runner job name>
```

In local mode, the process writes the same events to its standard output. The engine's standard error is in `<root>/logs/`.

## Contents

- [Setup is incomplete](#setup-is-incomplete)
- [Setup refuses a value](#setup-refuses-a-value)
- [A push that changes workflow files is rejected](#a-push-that-changes-workflow-files-is-rejected)
- [A ticket is not dispatched](#a-ticket-is-not-dispatched)
- [Dispatch is slow](#dispatch-is-slow)
- [GitHub board problems](#github-board-problems)
- [Notifications do not arrive](#notifications-do-not-arrive)
- [Pods do not start](#pods-do-not-start)
- [A workspace pod stays Pending or fails to start](#a-workspace-pod-stays-pending-or-fails-to-start)
- [An attempt fails](#an-attempt-fails)
- [Attempts stop with "went silent"](#attempts-stop-with-went-silent)
- [A continuation waits for a long time](#a-continuation-waits-for-a-long-time)
- [A `WORKFLOW.md` change has no effect](#a-workflowmd-change-has-no-effect)
- [`WORKFLOW.md` errors](#workflowmd-errors)
- [The team configuration does not publish](#the-team-configuration-does-not-publish)
- [A provider key is refused](#a-provider-key-is-refused)
- [The API refuses a request](#the-api-refuses-a-request)
- [Tern or the CLI does not connect](#tern-or-the-cli-does-not-connect)
- [Docker is not available in the workspace](#docker-is-not-available-in-the-workspace)
- [The merger does not review or merge](#the-merger-does-not-review-or-merge)
- [Nobody answers a question on the pull request](#nobody-answers-a-question-on-the-pull-request)
- [No retrospective or learnings pull request](#no-retrospective-or-learnings-pull-request)
- [The assistant does not answer](#the-assistant-does-not-answer)
- [The assistant does not create tickets](#the-assistant-does-not-create-tickets)
- [An MCP server does not work](#an-mcp-server-does-not-work)
- [Backup or restore fails](#backup-or-restore-fails)
- [Local mode problems](#local-mode-problems)

## Setup is incomplete

Run `symphony setup --status`. Each step that is not `done` shows its error or what it lacks. While setup is incomplete, the components idle and log one of these events. None of them crash-loops.

| Event | Process | Meaning | Fix |
|---|---|---|---|
| `daemon_setup_incomplete` (`reason`) | daemon | No valid WORKFLOW, or no usable tracker: the credential or the board scope is missing. The overview shows `daemon.setup: "incomplete"`. Logged at most once a minute | Finish the tracker step (`symphony setup tracker ...`). With the GitHub tracker, set the GitHub App first. The daemon starts on the next step after the values are valid, and logs `daemon_setup_complete` |
| `merger_not_configured` (`missing`) | merger | The merger idles, with no heartbeat. `missing` names what it lacks: `workflow`, `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, `repository` (no `SYMPHONY_MERGER_REPO` and no `kubernetes.default_repo`), `github_app` (the credential mints no token) or `repository_access` (the App cannot reach the repository). Logged at most once a minute | Finish the GitHub App and repositories steps. Install the App on the repository. Setup restarts the merger |
| `sweep_setup_incomplete` (`reason`) | nightly sweep | Nothing to sweep yet. The CronJob exits 0 | Finish setup |
| `reflector_setup_incomplete` (`reason`) | reflector Job | No WORKFLOW or no usable tracker. The Job exits 0 and keeps the retrospective token | Finish setup. The nightly sweep starts the retrospective again |
| `runner_setup_incomplete` (`reason`) | runner Job | The attempt cannot start. It fails with the summary `Setup is incomplete: <reason>` and is retried later | Finish the tracker step |
| `assistant_not_configured` (`missing`) | assistant | The assistant idles. `missing` names what it lacks: `workflow`, `slack_app_token`, `slack_bot_token` or `discord_bot_token`. Either transport alone is enough. Logged once a minute | Run `symphony setup assistant` ([install.md](install.md#the-assistant-on-slack-and-discord-optional)) |
| `assistant_disabled` | assistant | The tokens exist, but WORKFLOW has no `assistant:` section or `assistant.enabled` is not true. Logged once a minute | Run `symphony setup assistant`, or set `assistant.enabled: true` |

A command that needs the tracker (for example `refresh`) answers 409 `setup_incomplete` until the tracker step is done.

The step errors in `symphony setup --status`:

| Code | Step | Fix |
|---|---|---|
| `github_permissions_missing` | GitHub App | The message lists each missing permission, for example `issues: write` or `organization_projects: write`. Give the App these permissions ([install.md](install.md#5-make-the-github-app)), then accept them in the installation. The check needs `contents: write`, `pull_requests: write`, `issues: write`, `actions: write`, `checks: read`, `statuses: read`, `metadata: read`, and a projects permission with the GitHub tracker |
| `merger_missing` | Repositories | A repository has no merger that runs. Add one ([install.md](install.md#11-add-a-merger-for-each-repository)). If the merger exists, read its `merger_not_configured` event |
| `model_credential_missing` | Models | Store a key for the provider, or put a host credential in the Secret `model-provider` |
| `notification_failed` | Notifications | The last test message failed. Fix the channel and run `symphony setup notifications test` |
| `workflow_unavailable` | Tracker, repositories | The API cannot read the ConfigMap `symphony-workflow`. Apply the manifests (`kubectl apply -k deploy/`) |
| `slack_app_token_missing`, `slack_app_token_rejected`, `message_content_intent_missing` | Assistant | See [Setup refuses a value](#setup-refuses-a-value) |

## Setup refuses a value

The API verifies each value before it saves it. A refused value is not saved. The CLI prints the code and the reason.

| Code | Cause | Fix |
|---|---|---|
| `github_app_required` | A GitHub tracker before the GitHub App. The GitHub tracker uses the App token | Run `symphony setup github-app ...` first |
| `repo_not_installed` | The GitHub App is not installed on the default repository | Install the App on the repository, or choose another one |
| `secret_missing` | The Secret does not exist. The API may only patch Secrets that the manifests made | Apply the manifests (`kubectl apply -k deploy/`), then retry |
| `secret_forbidden` | The API Role cannot patch the Secret | Apply `deploy/rbac.yaml` again. Do not remove the Secret names from the Role |
| `restart_forbidden` | The value was stored, but the API Role cannot restart a Deployment. Usually a merger that an overlay added is not in the Role | Add the Deployment name to the Role `symphony-api` ([install.md](install.md#11-add-a-merger-for-each-repository)). Restart it by hand now: `kubectl -n symphony rollout restart deployment/<name>` |
| `restart_failed` | The value was stored, but a restart failed | Restart the named Deployment by hand |
| `host_env_overrides` | Local mode: the host environment sets this variable, and it would win over `secrets.env` | Unset it in the host environment (or `/etc/symphony/env`) and restart Symphony, or keep the host value |
| `linear_unauthorized`, `linear_team_not_found`, `linear_project_not_found` | Linear refused the key, or has no such team or project | Check the key and the team key or project slug |
| `invalid_github_app`, `invalid_private_key`, `github_app_unauthorized`, `github_installation_not_found` | The App credential mints no token | Check the App ID, the installation ID and the `.pem` file |
| `github_project_not_found`, `github_status_field_missing` | The board is not there, or it has no such status field | Check the owner, the project number and `--status-field`. Give the App the Projects permission |
| `notification_unverified` | The test message failed | Fix the token, the channel or the webhook URL (see [Notifications do not arrive](#notifications-do-not-arrive)) |
| `webhooks_exist` | The webhook secrets exist, and they cannot be shown again | Run `symphony setup webhooks --rotate`, then update the secrets in Linear and GitHub |
| `vendor_unreachable` | The vendor could not be reached | Try again. Check the egress of the API pod |
| `slack_app_token_missing` | The assistant has a Slack channel, but no app-level token is stored | Give the token (`xapp-…`) on stdin or at the prompt |
| `slack_app_token_rejected` | Slack refused the app-level token | Turn on Socket Mode. Make an app-level token with the scope `connections:write` ([install.md](install.md#slack-app)) |
| `slack_bot_token_missing`, `discord_bot_token_missing` | The assistant reuses the notifications bot, and none is stored | Set up notifications with a bot token first ([install.md](install.md#7-prepare-notifications-optional)) |
| `slack_bot_token_rejected`, `discord_bot_token_rejected` | The vendor refused the stored bot token | Run `symphony setup notifications slack` or `discord` again |
| `message_content_intent_missing` | The Discord bot cannot read message content | In the Developer Portal, open the application, then **Bot**, and turn on **Message Content Intent** |
| `invalid_mcp_server` (`field`) | An MCP server definition is not valid. `field` names the key | Fix that key ([mcp.md](mcp.md#transports)). A stdio `env.<NAME>` refused because "this name looks like a credential" holds a secret: use a remote server with stored headers instead |

A tracker step can also warn with `state_missing`: a WORKFLOW state is not on the board. Add the state to the board, or change the state names in `WORKFLOW.md`.

## A push that changes workflow files is rejected

**Sign:** The agent's `git push` fails with a message like `refusing to allow a GitHub App to create or update workflow ... without workflows permission`.

**Cause:** The workspace token never has the `workflows` permission (`ghapp.WORKSPACE`). GitHub rejects a push that changes a file under `.github/workflows/`.

**Fix:** This is by design. A person makes the change to the CI workflow. Tell the agent in the ticket not to change `.github/workflows/`.

## A ticket is not dispatched

**Checks**

1. Run `symphony status`. Make sure that the daemon is alive and not paused.
2. Run `symphony issues`. Look at the ticket's status, `reason` and `held`.
3. Search the daemon log for the ticket ID with these events: `issue_not_routable`, `retry_held`, `cooldown_hold`, `tracker_unavailable`.

**Causes and fixes**

| Cause | Sign | Fix |
|---|---|---|
| Symphony is paused | `symphony status` shows paused | `symphony resume --reason "..."` |
| A required label is missing | no session, no event | Add each label of `tracker.required_labels`. |
| The state is not active | no session | Move the ticket to a state in `tracker.active_states`. |
| A blocker is not terminal | no session | Finish or remove the Linear `blocks` relation, or the GitHub "blocked by" dependency. |
| No repository | `issue_not_routable`, reason `no-repo-designation` | Add a `repo:owner/name` label, or set `kubernetes.default_repo` (Linear). A GitHub issue always has its own repository. |
| Malformed repository label | reason `malformed-repo-designation` | Fix the label. Use letters, digits, `.`, `_`, `-` and one `/`. |
| Repositories of two owners | reason `mixed-owner-repo-designation` | Split the ticket by owner. One GitHub App installation covers one owner. |
| The App is not installed on the repository | reason `repo-not-authorized` | Install the GitHub App on the repository. |
| Another assignee | no session | Check `tracker.provider.assignee` (Linear only). |
| A person holds the ticket | `held` in `symphony issues` | `symphony release <ticket>`. |
| All slots are busy | `symphony status` shows running = limit | Wait, or raise `agent.max_concurrent_agents` or `agent.max_concurrent_agents_by_state`. |
| The tracker is unavailable or limits the rate | `tracker_unavailable` with `backoff_ms` | Wait. The poll backoff grows to at most 5 minutes and resets after a successful poll. Check `LINEAR_API_KEY`, or the GitHub App (see [GitHub board problems](#github-board-problems)). |
| The board scope is wrong | no ticket is ever seen | Check `tracker.provider.team_key` or `project_slug` (Linear), or `owner` and `project_number` (GitHub). |
| The GitHub item is not an issue | no session | Convert the draft item to an issue. Draft items and pull requests on the board are not tickets. |
| The GitHub status option is not an active state | no session | Make the option names equal to `tracker.active_states`. See [GitHub board problems](#github-board-problems). |

## Dispatch is slow

The normal delay is under 3 seconds with webhooks, and at most `polling.interval_ms` (15 s) without them.

| Sign in the API log or in the webhook delivery page | Fix |
|---|---|
| `401 invalid_signature` | Make the secret in Linear or GitHub equal to `LINEAR_WEBHOOK_SECRET` or `GITHUB_WEBHOOK_SECRET`. |
| `401 stale_webhook` (Linear) | Correct the server clock. Linear's timestamp must be within 5 minutes. |
| `503 webhook_unconfigured` | Set the secret in the Secret `linear-api-key` or `symphony-api`, then restart `symphony-api`. |
| No delivery at all | Turn the webhook on, and check the public hostname ([install.md](install.md#14-expose-the-api-with-a-cloudflare-tunnel)). Set `SYMPHONY_PUBLIC_URL` when the webhook URLs must use a public host. The `webhooks` step of `symphony setup --status` shows the last delivery of each source. |
| GitHub tracker: board changes do not wake the daemon | Subscribe the App to `issues`, `issue_comment`, `sub_issues`, `issue_dependencies` and `projects_v2_item`. GitHub sends `projects_v2_item` only for organization projects. For a user project, the safety poll finds the change. |

## GitHub board problems

These problems apply to `tracker.kind: github`. The daemon log shows them as `tracker_unavailable`, and the merger log as `merger_ticket_failed`.

| Message | Cause | Fix |
|---|---|---|
| `GitHub project <owner>#<N> was not found or the GitHub App cannot read it (...)` | The owner or the number is wrong, or the App has no Projects permission. | Check `tracker.provider.owner` and `project_number`. Give the App the Projects (read and write) permission: the organization permission for an organization project, the repository permission for a user project. Accept the new permissions in the installation. |
| `GitHub project <owner>#<N> has no single-select field 'Status'` | The field name is different, or the field is not single-select. | Set `tracker.provider.status_field` to the field name. Use a single-select field. |
| `the 'Status' field of GitHub project <owner>#<N> has no option 'In Review'` (or another state) | A state in `WORKFLOW.md` has no option on the board. | Add the option to the field, or change the state name in `WORKFLOW.md`. Every name in `tracker.active_states`, `tracker.terminal_states` and `tracker.states` must be an option. |
| `request rejected 403: ...` or `Resource not accessible by integration` | The App has no Issues permission, or it is not installed on the issue's repository. | Give the App Issues (read and write). Install it on every repository whose issues are on the board. |
| `rate limited` | GitHub's rate limit. | Wait. Symphony waits for `retry-after` or `x-ratelimit-reset`. Lower the concurrency if it happens often. |
| `unknown labels in <repo>: ...` | `symphony_create_issues` names a label that the repository does not have. | Make the label in the repository. Symphony makes only the `repo:` labels and the required labels of the parent. |
| `cannot attach <file> (...): GitHub has no API to upload files to an issue ...` | The agent tried to attach a binary file. | None. The agent describes the result in the Workpad or commits the file. |
| `text evidence must fit in one GitHub comment (65536 characters)` | The text evidence is too long. | Attach an excerpt. |

If a merged ticket stays in Merging, check that the pull request closes the issue (`Closes owner/repo#n`, or `Closes #n` for an issue in the pull request's repository). A closed issue is terminal in any state. Also keep the project workflow **Item closed** on, so the board shows Done.

## Notifications do not arrive

Symphony sends each event to every configured channel. A failure is logged and never stops a run. Search the logs for `notify_rejected` (with `notify_channel` and `status`) and `notify_failed`.

| Sign | Cause | Fix |
|---|---|---|
| No message and no log event | No channel is configured in this process. | Run `symphony setup notifications slack ...` or `... discord ...`. Setup restarts the Deployments. The Jobs get it at their next start. |
| Discord `status=401` | The bot token is wrong or reset. | Copy the bot token again into `DISCORD_BOT_TOKEN`. |
| Discord `status=403` | The bot cannot see the channel or cannot send messages there. | Give the bot View Channel and Send Messages in the channel. Check `DISCORD_CHANNEL_ID`. |
| Discord `status=404` | The channel ID or the webhook URL is wrong, or someone deleted the webhook. | Check `DISCORD_CHANNEL_ID`, or make a new webhook and update `DISCORD_WEBHOOK_URL`. |
| Discord `status=429` | Discord's rate limit. Symphony retries once when `retry_after` is at most 5 seconds. Otherwise it drops the message. | Wait. Fewer identical events help: Symphony sends an identical message to a channel at most once in 60 seconds. |
| A mention does not ping the person | The person's GitHub login is not in `notify.users.slack` or `notify.users.discord`, or the ID is wrong. | Add the login with the correct member ID or Discord user ID. |
| `notify.users takes a slack and a discord map ...` | `WORKFLOW.md` uses the old flat map. | Move the entries under `notify.users.slack`. |

## Pods do not start

| Sign | Cause | Fix |
|---|---|---|
| `symphony-api` stays in `CreateContainerConfigError` | The Secret `symphony-api` does not exist. | Make it from `deploy/secrets.example.yaml` ([install.md](install.md#3-minimal-install-on-kubernetes)). |
| Pods stay in `ContainerCreating` | The ConfigMap `symphony-workflow` does not exist. | Apply the manifests with `kubectl apply -k deploy/`. They make it empty. |

If the daemon log shows `SYMPHONY_ORCHESTRATOR_IMAGE is not set`, apply the manifests with `kubectl apply -k deploy/`. The kustomization sets this variable.

## A workspace pod stays Pending or fails to start

**Checks**

```sh
kubectl -n symphony get pods -l component=workspace
kubectl -n symphony describe pod ws-<ticket>
kubectl -n symphony get pvc
```

| Sign | Fix |
|---|---|
| `ImagePullBackOff` | Point `kubernetes.agent_images` (or `agent_image`) at an image that exists. Add `kubernetes.image_pull_secrets` for a private registry. With images imported into k3s, use a tag other than `latest`. |
| `unknown_agent_runtime_using_default` in the runner log | Set `kubernetes.agent_runtime` to `node`, `dotnet9`, `dotnet10` or `polyglot`, or add the runtime to `kubernetes.agent_images`. |
| PVC `Pending` | Make sure that a default StorageClass exists, or set `kubernetes.storage_class`. |
| `Insufficient cpu` or `Insufficient memory` | Lower `kubernetes.sandbox.cpu` or `memory_mib`, lower the concurrency, or add nodes. |
| The runner log shows `workspace_pod_dead_recreating` | The pod reached `kubernetes.sandbox.timeout_s` or the node evicted it. The runner makes a new one. No action is necessary. |
| The runner log shows `zombie_survived_terminating_workspace` | An old engine did not stop. The runner deletes the pod and makes a new one. No action is necessary. |

## An attempt fails

A failed attempt has a failure summary. The next attempt's prompt includes it. Read the summary in the runner log (`runner_finished`).

| Failure summary | Cause | Fix |
|---|---|---|
| `hook 'after_create' failed (exit N): ...` or `hook 'before_run' ...` | The hook script failed. The text ends with the last 500 characters of its output. | Fix the hook in `WORKFLOW.md`. Make sure that the image has the tools. Raise `hooks.timeout_ms` for a slow clone. |
| `prompt render failed: ...` | The Liquid template uses an unknown variable or filter. | Use only the variables in [configuration.md](configuration.md#prompt-template). |
| `engine stream silent for ...` | The engine sent no event for `engine.turn_timeout_ms`. | Check the model provider. Read the engine standard error (`engine_stderr` events). |
| `No API key found for ...` or `prompt rejected: ...` | The role's model has no credential. | Store a key ([providers.md](providers.md)) or fix the model in the team configuration. |
| `could not renew the code-hosting credential` | The GitHub App token renewal failed. | Check that the App is still installed and that `GITHUB_APP_*` are correct. |
| `tracker unavailable during attempt: ...` | The tracker (Linear or GitHub) did not answer. | Wait. The next attempt retries. |
| A model error, for example `overloaded` or `throttled` | The provider refused the call. | `symphony status` shows the rate limits. Lower the concurrency or change the model. |
| `Interrupted by an authorized operator.` | An operator used the `interrupt` command. | None. The retry follows the normal backoff. |
| `runner died mid-attempt` | The runner Job stopped without a result. A `Runner pod` line on the triage ticket means that the cluster removed the pod (node drain, node loss, preemption, eviction or a manual delete) while the runner ran. | Read the Job's log and events (`kubectl describe job`). For a `Runner pod` line, check the events of the named node (`kubectl describe node`). |

> **Note:** If git shows `Repository not found` for a repository that exists, the GitHub token covers other repositories. The runner rebuilds a workspace whose clone does not match the ticket's primary repository (`workspace_repo_mismatch_recreating`). If the error stays, check the `repo:` labels and the App installation.

## Attempts stop with "went silent"

**Sign:** The ticket gets the comment "Attempt N ended without finishing — it went silent for more than M minutes and was stopped." The daemon log shows `stall_strike` and then `stall_killed`.

**Cause:** The engine sends no events while a tool runs. A long command (for example `npm ci`, `dotnet restore`, `docker pull`) passed `agent.stall_timeout_ms` two times.

**Fix:** Raise `agent.stall_timeout_ms` above your longest command. The example `WORKFLOW.md` uses 30 minutes. `kubernetes.runner.wallclock_guard_s` still limits a runaway attempt.

## A continuation waits for a long time

**Sign:** The daemon log shows `cooldown_hold` for the ticket.

**Cause:** The attempt ended cleanly but the work is not complete, and the ticket did not change since then. This is the anti-runaway rule.

**Fix:** Do one of these:

- Comment on the ticket or change it. The cooldown ends.
- Run `symphony refresh --reason "..."`. It ends the cooldown once for each queued run.
- Lower `kubernetes.redispatch_cooldown_s`.

If continuations repeat with no progress, read the Workpad and the session in Tern. The agent can need a clearer ticket or a higher `agent.max_turns`.

## A `WORKFLOW.md` change has no effect

1. Wait about one minute. The kubelet updates ConfigMap mounts with a delay.
2. Run `symphony api GET /v1/workflow`. Compare the revision and the consumer observations.
3. Search the logs for the configuration error. Symphony keeps the last valid version when a new version is not valid.
4. Parse the file on your machine:
   ```sh
   uv run python -c "import pathlib,sys; from symphony.config import parse_workflow; parse_workflow(pathlib.Path('WORKFLOW.md').read_text())"
   ```

If a runner log shows `no_valid_workflow_config`, no valid version was ever loaded. Publish a valid file with `scripts/sync_workflow.sh`.

## `WORKFLOW.md` errors

| Message | Fix |
|---|---|
| `WORKFLOW.md must start with YAML front matter (---)` | Put `---` on the first line. |
| `unterminated YAML front matter` | Add the closing `---`. |
| `invalid YAML front matter: ...` | Fix the YAML syntax. |
| `tracker.kind is required` / `unknown tracker.kind ...` | Set `tracker.kind` to `linear`, `github` or `memory`. |
| `tracker.provider has unknown keys for github: ...` | With `github`, use only `owner`, `project_number` and `status_field`. |
| `tracker.provider.owner must be the GitHub organization or user that owns the project` | Set `owner` to a valid GitHub login. |
| `tracker.provider.project_number must be the positive number of the GitHub project ...` | Set the number from the project URL. |
| `tracker.provider.status_field must name the project's single-select state field` | Set a non-empty field name, or remove the key. |
| `notify.users.discord.<login>: a Discord user id is a number` | Use the numeric Discord user ID, quoted. |
| `key 'X' must be int, got bool` (or another type) | Use the type in [configuration.md](configuration.md). |
| `environment variable 'X' not set (required by WORKFLOW.md)` | Set the variable, or remove the `$X` reference. |
| `tracker.provider.api_key must resolve to a nonempty string` | Set `LINEAR_API_KEY` in the Secret `linear-api-key`. |
| `tracker.states.rework (...) must be an active state` | Add the Rework state to `tracker.active_states`. |
| `tracker.states.review (...) must not be an active state` (or `merging`) | Remove that state from `tracker.active_states`. |
| `tracker.states.done (...) must be a terminal state` | Add the Done state to `tracker.terminal_states`. |
| `tracker.states has unknown keys ...` | Use only `review`, `rework`, `merging`, `done`. |
| `tracker.issues seeds the memory tracker only` | Remove `tracker.issues`, or use `kind: memory`. |
| `engine.provider and engine.model must be set together (or neither)` | Set both or neither. |
| `engine.thinking must be one of ...` | Use `off`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`. |
| `review_watch.answer_prompt is required when review_watch.enabled is true` | Add the prompt, or turn the watch off. |
| `telemetry.capture_content must be none, summary, full` | Use one of the three values. |
| `telemetry.endpoint must be an absolute URL` | Use a full URL with a scheme and a host. |
| `section 'codex' was retired ...` / `section 'modal' ...` / `engine.X was retired ...` / `kubernetes.pre_pr_review was retired ...` | See [Retired keys](configuration.md#retired-keys). |
| `kubernetes.agent_images must map runtime names to image references` | Use a map of strings to strings. |
| `agent.max_turns must be positive` (or another key) | Use a positive integer. |

## The team configuration does not publish

| Response | Cause | Fix |
|---|---|---|
| `412` | Another admin published first. | Reload the configuration in Tern and publish again. |
| `409` | A concurrent publication. | Reload and publish again. |
| `models.oracle must differ from the reviewer ...` | The oracle or the arbiter uses the reviewer's model. | Choose a different provider or model. |
| `models.X.thinking must be one of ...` | Wrong thinking level. | Use a listed level. |
| `a reason (1-500 characters) is required` | No reason. | Give a reason. |

If a consumer does not report the new generation (`symphony admin config observed`), it did not start a new unit of work yet. The runners and the merger read the configuration at the start of each attempt or pass.

## A provider key is refused

| Response | Cause | Fix |
|---|---|---|
| `422 unknown_provider` | The ID is not a built-in provider with credential kind `apiKey` in the pinned catalog. | Use an ID from Admin › Providers, or define a custom provider. |
| `provider_key_required` | `auth: apiKey` with no key stored or sent. | Send `apiKey`. |
| `invalid_provider` | A field of the custom provider is not valid. | Check the rules in [providers.md](providers.md#custom-providers). |
| `503 providers_unavailable` | The API cannot read or write the Secret `symphony-providers`. In local mode this route is always unavailable. | Apply `deploy/providers.yaml` and `deploy/rbac.yaml`. In local mode, use host environment variables. |
| `503 catalog_unavailable` | The catalog file is missing. | Use the official orchestrator image, or set `SYMPHONY_OMP_CATALOG`. |

## The API refuses a request

| Response | Cause | Fix |
|---|---|---|
| `401 unauthorized` | No `Authorization: Bearer` header. | Log in with `symphony login`. |
| `401` with a revoked or expired token | The token is not valid. | Ask an admin for a new token. |
| `403 forbidden` | The token role is too low. | Use a token with the role in [security.md](security.md#api-tokens-and-token-roles). |
| `409 already_held` / `409 not_held` | The hold state changed. | Refresh the view. |
| `409 stale_target` | The attempt changed since you selected it. | Select the current session again. |
| `409 session_ended` | The session ended. | Open the newest session of the ticket. |
| `429 rate_limited` | More than 600 requests in a minute for a token, or more than 30 failed authentications in a minute from one address. | Wait for `Retry-After`. |
| `503 unavailable` | Redis or Kubernetes is not reachable. | Check `symphony-redis` and the API's RBAC. |
| `503 merger_unavailable` | No merger is deployed. | Deploy `symphony-merger`. |

## Tern or the CLI does not connect

| Sign | Fix |
|---|---|
| The plugin shows **Sign in** or **disconnected** | Run `symphony whoami`. If it fails, run `symphony login` again. The plugin reads the token from `symphony token print`. |
| `symphony tern install` says that `settings.json` is not valid JSON | Fix the file. The command changes nothing until the file is valid. |
| `The tern command is not on PATH` | Install Tern, or use `--no-plugin`. |
| `The Tern plugin was not found in ...` | Install the CLI from a clone with `uv tool install --editable .`, or use `--plugin <clone>/tern-plugin`. |
| `omp was not found` | Install `omp` through Tern, or set `SYMPHONY_OMP`. |
| The MCP server answers 401 after a token change | Run `symphony tern install` again. The MCP entry holds the token. |
| A message in the chat has no effect | The token role must be `operator`. The session must be running or waiting. |

## Docker is not available in the workspace

**Sign:** The `before_run` output shows `docker: unavailable (see /tmp/dockerd.log)`.

**Fix:** Choose a Docker posture in [configuration.md](configuration.md#docker-in-the-workspace). `dockerd` needs `kubernetes.privileged_agents: true` or a runtime class such as `sysbox-runc`. Without one of them, the attempt continues with no Docker.

## The merger does not review or merge

| Sign | Fix |
|---|---|
| A pull request is never reviewed | It must be open, ready (not draft), based on `main`, and it must have a check run on its head. Check `symphony merger status` for the queue and the skip reason. |
| The merger log shows probe failures | A role's model or tools do not work. The worker waits from 60 seconds to 10 minutes, then retries. Fix the model or its key. |
| The pull request has `needs-maintainer` | The arbiter upheld a blocking finding after the one rejection. A person decides. In Tern, **Requeue** asks for a new review. **Bypass** sends it to the train without a review. |
| The train's CI run is cancelled | Your CI workflow does not use the `train-ci` concurrency group. See [install.md](install.md#ci-concurrency-group). |
| A pull request stays claimed after a merger crash | In Tern, select **Release** on the pull request, or wait: claims expire in minutes. |
| The merger moves no ticket | With Linear, the ticket key must be in the branch name, the title or the body of the pull request. With GitHub, the branch must be `symphony/<identifier>`, or the body must name the issue (`owner/repo#n`, the issue URL, or a closing keyword with `#n` such as `Closes #12`). Search the merger log for `merger_ticket_failed`. |
| The merger is idle | Search its log for `merger_not_configured`. See [Setup is incomplete](#setup-is-incomplete). |

[merger.md](merger.md) explains each rule.

## Nobody answers a question on the pull request

1. Check that `review_watch.enabled` is true. The default is false.
2. Check that `review_watch.answer_prompt` is set.
3. Check that the GitHub App sends `issue_comment`, `pull_request_review` and `pull_request_review_comment` events.
4. The watch does not answer comments that existed when it first saw the pull request. It answers only new comments from people.
5. Search the daemon log for `answer_dispatch` and `review_watch_unavailable`.

## No retrospective or learnings pull request

| Cause | Fix |
|---|---|
| `kubernetes.learning.enabled` is false | Set it to true. |
| The terminal state is not in `kubernetes.learning.reflect_on` | Add it, or set `reflect_on` to null. |
| Local mode | The reflector does not run (`reflector_skipped_local_mode`). |
| The retrospective found nothing to learn | No action. This is a normal result. |
| A retrospective Job failed | The nightly sweep retries it after one hour (`nightly_sweep_done`). |

## The assistant does not answer

Run `symphony assistant status`. It shows whether the assistant runs, each transport and its channels, and whether it is connected.

| Sign | Cause | Fix |
|---|---|---|
| `assistant_not_configured` or `assistant_disabled` in the log | Setup is not done | See [Setup is incomplete](#setup-is-incomplete) |
| `discord_gateway_fatal code=4004` | Discord refused the bot token. The transport stops | Store a valid bot token (`symphony setup notifications discord`), then restart `symphony-assistant` |
| `discord_gateway_fatal code=4013` | The intents are not valid | Use the shipped image. The assistant sends `GUILDS`, `GUILD_MESSAGES`, `DIRECT_MESSAGES` and `MESSAGE_CONTENT` |
| `discord_gateway_fatal code=4014` | The bot asks for an intent that the application does not allow, usually the Message Content Intent | Turn on **Message Content Intent** in the Developer Portal, then restart `symphony-assistant` |
| `slack_socket_api_error` | `apps.connections.open` failed | Check Socket Mode and the app-level token |
| It does not answer a mention in a channel | The channel is not in `assistant.slack.channels` or `assistant.discord.channels`, or the bot is not in the channel | Add the channel with `symphony setup assistant`. Invite the bot |
| It does not answer a DM on Slack | The Messages Tab is off, or `message.im` is not subscribed | See [install.md](install.md#slack-app) |
| It does not answer a thread reply | It answers replies only in threads it posted in. On Slack, `message.channels` must be subscribed | Mention it again, or subscribe the event |
| "I am busy with other questions." | `assistant.max_concurrent` turns already run | Wait, or raise `assistant.max_concurrent` |
| "I stopped: this took too long." | The turn passed `assistant.turn_timeout_s` | Ask a narrower question, or raise the limit |

## The assistant does not create tickets

| Sign | Cause | Fix |
|---|---|---|
| "Only people mapped in notify.users can create tickets." | The person who clicked is not in `notify.users` | Map their GitHub login to their Slack or Discord ID ([configuration.md](configuration.md#notify)) |
| "This proposal expired. Ask the planner again." | The proposal is older than 7 days | Ask the planner again |
| "These tickets are being created." | Another click is running | Wait. A double click never creates a ticket twice |
| A ticket has the comment "Blocked by ENG-12. The tracker did not record the link: …" | The tracker refused the blocking relation | Add the relation by hand in the tracker |
| `assistant_tickets_failed` in the log | The tracker refused a ticket | Read the error. Click again: the tickets that exist are not created again |

## An MCP server does not work

Run `symphony admin mcp test NAME`.

| Code or sign | Cause | Fix |
|---|---|---|
| `unreachable` | The gateway cannot connect to the server, or it timed out (10 s to connect) | Check the URL. The server needs a public address: the gateway cannot reach private ranges |
| `rejected` | The server answered 401 or 403 | Store the right header with `symphony admin mcp headers NAME` |
| `protocol_error` | The server's answer is not valid MCP (`initialize`, then `tools/list`) | Check the transport: `http` for Streamable HTTP, `sse` for the older HTTP+SSE |
| `{ok: null}` | A stdio server. It is tested when an agent starts | Check that the command exists in the agent's image |
| 403 `mcp_forbidden` in the agent | The token is not live, the role is not in the server's roles, or the server is off | Add the role and enable the server. Start a new session: the token belongs to the session |
| 502 `mcp_unreachable` in the agent | The gateway cannot reach the server | As for `unreachable` |
| A change has no effect | The gateway reads the mounted Secret, which Kubernetes updates after about a minute. A running agent keeps its servers | Wait a minute, and start a new session |
| 422 `invalid_mcp_server` | The definition is not valid | See [Setup refuses a value](#setup-refuses-a-value) |

## Backup or restore fails

`symphony backup status` shows the target, the reason when there is none, and the last runs.
[backup.md](backup.md) has the restore runbook.

| Sign | Cause | Fix |
|---|---|---|
| The CronJob logs `backup_not_configured` | No `WORKFLOW.md`, or no `backup.s3` or `backup.dir` | Set the target in `WORKFLOW.md` ([configuration.md](configuration.md#backup)) |
| `not_configured`: "the symphony-backup Secret has no …" | `backup.s3` is set, but the access keys are not | Fill the `symphony-backup` Secret, then restart the API |
| 502 `target_error` with `SignatureDoesNotMatch` or `InvalidAccessKeyId` | Wrong access keys, or a wrong `backup.s3.region` | Check the keys and the Region of the bucket |
| 502 `target_error` with `NoSuchBucket` or `AccessDenied` | The bucket does not exist, or the keys may not use the prefix | Make the bucket; allow `PutObject`, `GetObject`, `ListBucket` and `DeleteObject` on the prefix |
| 409 `daemon_running` | The daemon holds its lease and is not paused | `symphony pause`, wait until it applies, then restore. A daemon scaled to zero frees its lease within 60 s |
| 409 `namespace_mismatch` | The backup is of another namespace | Add `--namespace-from <ns>` if you mean it |
| 422 `checksum_mismatch` or `truncated` | The file changed or is incomplete | Use another copy. Nothing was written |
| 422 `outside_prefix` | The file holds a key outside the namespace prefixes | Do not use the file. Nothing was written |
| 500 `restore_incomplete` with "DUMP payload version" | The Redis that restores is older than the Redis that made the backup | Restore into the same Redis version or a newer one |

## Local mode problems

| Sign | Fix |
|---|---|
| `error: set REDIS_URL (...)` | Export `REDIS_URL`. |
| `/v1/admin/providers` answers 503 | This is normal in local mode. Use host environment variables for model keys. `/v1/workflow` and setup write the `--workflow` file. |
| Setup answers `host_env_overrides` | A host environment variable wins over `<root>/secrets.env`. See [Setup refuses a value](#setup-refuses-a-value). |
| The agent uses another `omp` version | Set `engine.command` to the absolute path of the pinned `omp`. |
| git asks the host keychain for a password | With a GitHub App, Symphony replaces the host's credential helpers for the attempt. Without an App, the agent uses the host user's git credentials. |
| A lost admin token | `uv run python -m symphony.entry.admin_token create --name <name> --role admin` |
| `bootstrap token must look like sym_<8 hex>_<43 chars>` | Make the token with the command in [install.md](install.md#3-minimal-install-on-kubernetes). |
