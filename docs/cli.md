# `symphony` (CLI, ACP bridge and viewer)

`symphony` is the client each person uses. It stores the credential, talks to the control API
(`/v1`, see [api.md](api.md)), and is the process Tern starts as an ACP agent.

Every command accepts `--json`, before or after the subcommand. With `--json`, errors also go
to stdout as JSON: `{"error": {"code", "message"}}`, with exit code 1. Without `--json`, an API
error code becomes one short sentence (the single table `ERROR_TEXT` in
`symphony/cli/render.py`). An unknown code falls back to the sentence for its HTTP status. The
server's `message` never appears in text output. It stays only in the JSON error, which the CLI
never changes.

The CLI checks reasons (`--reason`) before it sends anything, with the same rules as the API:

- Commands (`pause`, `resume`, `refresh`, `reevaluate`, `release`) need 8 to 1000 characters,
  not counting spaces at the ends.
- `admin config set` needs 1 to 500 characters.
- `interrupt` accepts at most 1000 characters.
- Merger actions (`merger bypass`, `merger requeue`, `merger release`) need 1 to 1000
  characters, not counting spaces at the ends.

A reason that breaks a rule fails with `invalid_reason` and never reaches the server.

Install from the repository: `uv tool install .` (or `uv sync` and `.venv/bin/symphony`).
`symphony --version` prints the version, which is `version` in `pyproject.toml`.

## Credential

| Command | What it does |
|---|---|
| `symphony login [--url URL] [--token TOKEN\|-]` | Checks the token with `GET /v1/me` and saves it only then. `--token -` reads the token from stdin, so it stays out of `ps`. Without flags, it asks |
| `symphony logout` | Removes the token (keychain or file) and the config |
| `symphony whoami` | Name, role and id of the current token |

- `~/.config/symphony/config.toml` (or `$XDG_CONFIG_HOME/symphony`) stores `url`, `token_id`,
  `name`, `role` and `storage`. The token goes to the system keychain (`keyring`, service
  `symphony`, account = URL). The login reads the secret back. If the keychain fails or drops
  the secret, the token goes to `~/.config/symphony/token` with mode `0600`.
- `SYMPHONY_URL` and `SYMPHONY_TOKEN` take precedence over the saved login. If only the URL
  comes from the environment and it is a different server, the CLI does not use the saved
  token.

### `symphony token print` (only for the Tern plugin)

The CLI is the only owner of the credential. The Luau plugin host does not store the token.
Each time, it runs `symphony token print --json`, gets `{"url", "token", "name", "role"}`,
keeps them in memory only and calls the API. Without a login, the command exits with 1 and
`{"error":{"code":"not_logged_in",…}}`. Do not use this command in scripts. For automation,
use `SYMPHONY_TOKEN`.

## Read and operate

| Command | What it does |
|---|---|
| `symphony status` | Overview: daemon, counts (held tickets included), today's tokens and cost, merger, config generation. When present, it also shows the provider limits of the last hour (`rate_limits`: model, stage, quota left, reset time) and the tickets a person holds (who, when, reason) |
| `symphony sessions [--state S] [--limit N]` | Session table (ticket, kind, state, turn, tokens, cost, session) |
| `symphony watch <session\|ticket>` | Live view. In Tern it draws natively (`tern-sdk`). Elsewhere it prints plain text. `--json` prints one SSE event per line |
| `symphony chat <session\|ticket>` | In Tern, opens the native ACP chat (`tern acp symphony --resume <session>`). Elsewhere, falls back to `watch` |
| `symphony say <ref> <text> [--steer\|--follow-up]` | Sends a message to the agent. Without a flag, the mode is `auto` |
| `symphony interrupt <ref> [--reason R]` | Stops the current turn (RPC `abort`) |
| `symphony answer <ref> <text>` | Answers the agent's open question (`symphony_ask`). Fails if there is no open question |
| `symphony takeover <ticket> [--dir D] [--no-hold] [--no-launch]` | **Holds the ticket** (`hold`, see [internals §12](internals.md#12-hold-a-ticket), reason `taken over by <name>`): Symphony stops dispatching, retrying and continuing work on it. Then it interrupts the turn, downloads the transcript (`export.jsonl`) to `~/.cache/symphony/takeover/`, fetches the work branch into the clone in `--dir` (it switches branch only with a clean tree), and opens `omp --resume <file>` (with `--profile symphony` when the profile exists). `--no-hold` does not hold or interrupt: it only downloads and opens |
| `symphony release <ticket> [--reason R]` | Returns a held ticket to Symphony (default reason `returned by <name>`). Releasing a ticket that is not held fails with "This ticket is not held." |
| `symphony issues` | Tracked tickets: Symphony phase, tracker state (the Linear state or the GitHub Projects status), who holds it, why it cannot be dispatched (no repository named, malformed name, more than one repository, repository not authorized), and PR |
| `symphony pause\|resume\|refresh [--reason R]` | Act on all of Symphony: stop or resume taking new tickets, or reread the tracker (`POST /v1/commands`) |
| `symphony reevaluate <ticket> [--reason R]` | Reevaluates a blocked or retrying ticket |
| `symphony merger status [--repo owner/name]` | One section per repository a merger serves, sorted by repository, each marked "merger running" or "merger not responding". Each section shows reviews (step: preparing, reviewing, verifying findings, arbitrating, waiting for CI, resolving a conflict), queue, parked PRs by title (clean review waiting for CI or for approval, or report; `PR #n` for a record without a title), train (lease and the state of each PR) and batch, with the same words as the Tern panel. `--repo` shows only that repository |
| `symphony merger bypass\|requeue\|release <PR> --reason R [--repo owner/name]` | Operator actions on a PR in the merger: skip its review, put it back in the queue, or release it. The reason needs 1 to 1000 characters. `--repo` is required when more than one repository has a merger; without it, the command fails with "More than one repository has a merger. Name one with --repo." A repository with no merger fails with "No merger serves this repository." Repository names are compared in lower case |
| `symphony repos` | Repositories Symphony works on: runtime, where Symphony knows them from (ticket, retrospective, workflow default), and how many tickets |
| `symphony infra` | Services (role, health, detail) and Kubernetes resources (kind, phase, detail, component) |
| `symphony telemetry [--limit N]` | Token and cost totals, per model, monthly counters and recent sessions |
| `symphony learning` | Learning proposals: ticket, repository, status, source (retrospective or retained), outcome (nothing to learn, PR opened, …) and PR |
| `symphony audit [--cursor C] [--limit N]` | Audit events, newest first. The last line gives the cursor of the next page |

The views (`issues`, `repos`, `infra`, `learning`) carry stable codes from the API, not prose.
The CLI shows each code as a label. A new code appears as it came. `--json` passes the API
document through unchanged. A stale or unavailable source appears as "Warning: the … source
is stale." In that case, an empty list means "unknown", not "none". The `note` of each source
is operator detail for logs, and the CLI never shows it.

`<ref>` is the session id (`<issue_id>:<seq>`, `merger:<pr>:<role>:<n>`) or the ticket
(`ENG-12`), which resolves to its newest live session.

## Administration

| Command | What it does |
|---|---|
| `symphony token create <name> --role viewer\|operator\|admin [--expires 90d\|never]` | Prints the token once |
| `symphony token list` / `symphony token revoke <id>` | Lists and revokes tokens (revoking takes effect now) |
| `symphony admin config show` | The published team config |
| `symphony admin config set role=provider/model[:thinking]… --reason R` | Publishes with `If-Match: <generation>`. The suffix is a thinking level only when it names a valid level, because model ids can contain `:` |
| `symphony admin config observed` | The generation each consumer applied (marks the ones out of date) |
| `symphony admin provider list` | Providers stored in the `symphony-providers` Secret (see [internals §13](internals.md#13-providers)): id, type (omp built-in or custom), name, protocol, and whether a key is set. The key never appears, not even in `--json` |
| `symphony admin provider key <id>` | Stores the API key of an omp built-in provider (`anthropic`, `openai`, … any id of `GET /v1/admin/catalog`) as `{builtin: true, apiKey}`, with `If-Match` from the revision it read. The key comes from stdin (`echo "$KEY" \| symphony admin provider key anthropic`) or from a hidden prompt, never from an argument. An id outside the catalog fails with "This provider is not in the omp catalog." Each agent uses the key from its next attempt, with no restart |
| `symphony admin mcp list` | MCP servers for agents (`GET /v1/admin/mcp`): name, transport, on or off, roles, the names of the stored secret headers, and the URL or command. Header values never appear, not even in `--json` |
| `symphony admin mcp set NAME (--transport http\|sse\|stdio \| --preset seiji) [--url U] [--command C] [--arg A]… [--env NAME=VALUE]… --role R… [--disabled] [--timeout-ms N] [--instructions] [--http-base URL] [--agent-env NAME=TEMPLATE]… [--rewrite-origin ORIGIN]… [--cleanup ACQUIRE_TOOL:ID_FIELD:RELEASE_TOOL:RELEASE_ARG]` | Adds or replaces a server (`PUT /v1/admin/mcp/NAME`). It is a full replace: give every field each time (take them from `list`). The stored secret headers stay. `http` and `sse` need `--url`; `stdio` needs `--command`, and `--env` is for values that are not secret. Write an argument that starts with `-` as `--arg=-y`. `--disabled` saves it turned off. `--preset seiji` fills what the other flags leave out, and needs no `--transport` ([mcp.md](mcp.md#presets)). The companion flags are for `http` and `sse` servers: `--agent-env` templates take `{token}`, `{http_url}` and `{mcp_url}` only ([mcp.md](mcp.md#variables-for-the-agent-agent_env)). Prints the stored definition |
| `symphony admin mcp headers NAME` | Stores secret headers that the gateway adds to each request. The headers come on stdin as JSON, `{"Authorization": "Bearer …"}` or `{"headers": {…}}`; `null` removes a header. Values never appear in arguments or in the output. Prints `{"headers_set":[…]}` |
| `symphony admin mcp test NAME` | Connects through the gateway (`initialize`, then `tools/list`) and prints the tools, or the error (`unreachable`, `rejected`, `protocol_error`). For a Seiji server it also reads `/v1/me` and prints the account (`me: {user, quota}` in `--json`). Exits 1 when the test failed. A stdio server is tested when an agent starts |
| `symphony admin mcp remove NAME` | Removes the server and its stored headers. Prints `{"removed":"NAME"}` |
| `symphony mcp viewer NAME LEASE [--control]` | Operator. A viewer link to a device that a live agent session holds on the MCP server NAME (`POST /v1/mcp/NAME/viewer`). Prints `{url, expires_in, control}` with `--json`. The link opens once, within `expires_in` seconds. `--control` lets the link drive the device; everyone who watches the device sees the input. A lease that no live session holds: `lease_not_held` |
| `symphony assistant status` | The assistant (the `assistant` step of `GET /v1/setup`): whether it runs (from its heartbeat), when it last beat, each transport with its channels and whether it is connected, the active threads and the repositories it reads. `--json` prints `{enabled, running, heartbeat_at, threads, repositories, slack: {configured, connected, channels}, discord: {configured, connected, channels, intent}, status, error}` |
| `symphony assistant memory list` | Admin. The soul in use (`owner/name:path` at its blob sha, or the built-in text) and each person with memory: login, items, last change (`GET /v1/assistant/memory`) |
| `symphony assistant memory show LOGIN` | Admin. One person's items: id, kind (`explicit` or `inferred`), when, text |
| `symphony assistant memory forget LOGIN ID` | Admin. Removes one item. An unknown id: `not_found` |
| `symphony assistant memory wipe LOGIN` | Admin. Removes every item of one person. Every `memory` command is audited |
| `symphony backup status` | Admin. The backup target (never a credential), `keep`, the Redis retention, and the last backup, archive and restore runs; the reason when no target is set ([backup.md](backup.md)) |
| `symphony backup export [-o FILE]` | Admin. Downloads a backup file, checks it (format, SHA-256, counts), then moves it into place. Default name: the server's `symphony-<ns>-<time>.ndjson.gz` in the current directory |
| `symphony backup restore FILE --confirm [--namespace-from NS]` / `symphony backup restore --object NAME --confirm` | Admin. Uploads a file, or restores a backup from the target. Without `--confirm` nothing is sent. The API refuses while the daemon runs unpaused (`symphony pause` first), and refuses another namespace without `--namespace-from`. Prints the counts; the pause flag does not change |
| `symphony backup run` | Admin. What the nightly CronJob does, now: archive, back up, delete the backups beyond `backup.keep` |
| `symphony backup list` | Admin. The backups on the target: name, size, when |
| `symphony backup archive list` / `symphony backup archive get NAME [-o FILE]` | Admin. The archive objects; `get` prints one as JSONL (sessions, events, transcript entries, audit entries) |
| `symphony telemetry backfill [--force]` | Admin. Rebuilds the session fields of the daily rollups (sessions, outcomes, tokens, cost, models, repositories, activity) from the ended sessions in Redis and the archive objects on the backup target, for the days that have no rollup; `--force` rebuilds every day they cover. Prints the days rebuilt and kept. Running it twice gives the same numbers ([api.md](api.md#telemetry-history)) |

There is no OAuth login and no provider subscription. Every model uses an API key.

## First-run setup: `symphony setup`

A new install has only Redis and the API. `symphony setup` finds what is missing and configures
it, one step at a time. The API verifies each value against the real service (Linear, GitHub,
Slack, Discord) before it saves the value. A value that fails verification is not saved. Only an
admin can change the setup; any token can read the checklist.

The checklist lists the steps in the API's order: tracker, GitHub App, repositories, models,
notifications (optional), assistant (optional) and webhooks (optional). Setup is complete when
every required step is done. The wizard does the GitHub App first, because the GitHub tracker
and the repository check use it.

| Command | What it does |
|---|---|
| `symphony setup` | The wizard. It needs a terminal: without one (or with `--json`) it fails with `not_a_terminal` and points to `--status` and the step commands. It shows a banner, then each step with a progress bar (`■■□□□□□ Step 2 of 7: Tracker`). A step that is done shows its summary and asks "Change it?". Answers that exist already (team key, owner, default repository) are the defaults. While the API verifies, a spinner runs. A refused value shows the reason and asks "Try this step again?". Secrets are read with hidden prompts; the private key is read from a file path. The models step lists each role with its credential and stores provider API keys. The assistant step asks for Slack, Discord, both or skip, then the Slack app token (hidden; empty keeps the stored one), the channel IDs and the repositories. The webhooks step creates the secrets, or asks before it replaces existing ones. At the end it prints the checklist and, when setup is complete, "Symphony is ready." with how to start the first ticket. A non-admin sees the checklist and "Ask an admin to run `symphony setup`." |
| `symphony setup --status` | The checklist (`GET /v1/setup`): one row per step with a mark (✓ done, ○ to do, ✗ error, – skipped), its status, and a summary of its detail or its error. `--json` prints the document as it comes |
| `symphony setup tracker --kind linear --team KEY` (or `--project SLUG`) | Sets the Linear tracker. The API key comes from stdin (first line) or a hidden prompt. Prints the states on the board and a warning for each WORKFLOW state that is not on the board |
| `symphony setup tracker --kind github --owner O --project-number N [--status-field F]` | Sets the GitHub Projects tracker. No secret: the API uses the GitHub App, so set the App first (`github_app_required` otherwise) |
| `symphony setup github-app --app-id N --installation-id N [--private-key-file PATH]` | Sets the GitHub App. The private key (PEM) comes from the file or from stdin, read to the end. Prints the repositories the installation can reach and any missing permission |
| `symphony setup repositories [--default-repo owner/name \| --clear-default-repo] [--branch owner/name=branch]… [--runtime owner/name=runtime]… [--component owner/name:name=path[@runtime]]…` | Sets the default repository, or clears it. `--branch` sets one repository's base branch (repeat for several) and keeps the others; `owner/name=` clears that entry. `--runtime` sets the per-repository runtime override; `owner/name=` clears it. `--component` adds or updates a component (`owner/name:api=api@dotnet10`); `owner/name:api=` removes it. The branch is verified through the GitHub App. When the base branches change, the merger and assistant Deployments restart. Without `--branch`, the base branches stay as they are; with only `--branch`/`--runtime`/`--component`, the default repository stays as it is |
| `symphony setup notifications slack --channel C` (or `--webhook`, or `--remove`) | Sets Slack: a bot token and a channel, or an incoming webhook URL. The token or URL comes from stdin (first line) or a hidden prompt. The API sends a test message first and saves nothing if it fails. `--remove` stops posting to Slack. Discord stays as it is |
| `symphony setup notifications discord --channel-id ID` (or `--webhook`, or `--remove`) | Sets Discord the same way. Slack stays as it is |
| `symphony setup notifications test` | Sends a test message to each configured channel and prints one line per channel, also when one failed. `--json` prints `{"results":[{"channel":"slack","ok":true,"at":<ms>,"error":null},{"channel":"discord","ok":false,"at":<ms>,"error":{"code":"unreachable","message":"…"}}]}`, Slack first. Exits 1 when a channel failed. With no channel it fails with "No Slack or Discord channel is configured." |
| `symphony setup assistant [--slack-channel C]… [--discord-channel ID]… [--repo owner/name]…` | Turns on the assistant in these channels. It reuses the bot tokens of the notifications step, so set those first. With a Slack channel, the Slack app-level token (`xapp-…`, Socket Mode, scope `connections:write`) comes from stdin (first line) or a hidden prompt; an empty answer keeps the stored one. The API checks the app token with `apps.connections.open` and the bot token with `auth.test`. For Discord it checks that the bot has the Message Content Intent. Then it publishes WORKFLOW `assistant:` and restarts `symphony-assistant`. A transport without a channel is turned off. Without `--repo` the repositories stay as they are; the default is every repository Symphony works on |
| `symphony setup webhooks [--rotate]` | Creates the Linear and GitHub webhook secrets and prints them with their URLs and the GitHub events. **Symphony shows the secrets only once.** A second call fails with "Webhook secrets exist already."; `--rotate` replaces them |

- Secrets never appear in arguments. They travel only in the request body.
- Each saved step prints the Kubernetes Deployments that restarted to apply it.
- When the server's message names what was wrong (a team key, a project, a repository, a
  Secret, the service that could not be reached), the error line ends with it, for example
  "Linear has no team with this key. Details: Linear has no team with key 'OPS'".
- `--json` prints the API's answer, or `{"error":{"code","message"}}` with exit code 1. The Tern
  Setup view uses these commands for every write.
- Color appears only on a terminal, and only for meaning: green done, yellow needs attention,
  red failed, dim optional. `NO_COLOR` or `TERM=dumb` turns color off; without a terminal the
  spinner becomes one plain line.

## For the Tern plugin: `symphony api`

`symphony api METHOD PATH [--data JSON\|-] [--header Name:value]…` makes a raw call and
prints the JSON body. `--data -` reads the body from stdin, so secrets stay out of `ps`. A
response outside 2xx exits with code 1 and prints the API's JSON error on stdout. Without a
connection, it prints `{"error":{"code":"unreachable",…}}`.

## On the person's machine

### `symphony omp sync`

Writes the team config into the **`symphony`** omp profile,
`~/.omp/profiles/symphony/agent/config.yml`, and never into the person's default profile. Use
it with `omp --profile symphony`. The file gets `modelRoles.default` (the `developer` role, as
`provider/model:thinking`), `defaultThinkingLevel`, and the team's `omp.settings` merged on top.
The command keeps keys and comments the person added to this profile. Then it reports the
applied generation as `member:<token name>`. It also checks the local omp version
(`SYMPHONY_OMP`, else the omp Tern manages, else the one on PATH) against `omp.version`. The
profile is isolated: provider logins of the default profile do not apply to it. With Bedrock,
the AWS credentials in the environment are enough.

### `symphony tern install [--plugin DIR] [--no-plugin]`

Merges into Tern's `settings.json` (`$TERN_CONFIG_DIR`, else
`~/Library/Application Support/Tern` on macOS) and changes nothing else:

- `agents.custom`: the agent `{id: "symphony", name: "Symphony", command: <absolute path of
  symphony>, args: ["acp"]}`. The path is absolute because Tern resolves `command` on its own
  PATH, not on the PATH of the shell that ran the install. Extra keys of the entry (such as
  `env`) stay.
- `agents.mcp`: `{name: "symphony", transport: "http", url: <URL>/mcp, headers:
  {Authorization: "Bearer <token>"}}`. The header is literal because Tern 0.8.3 did not
  expand `${VAR}` in the headless test. For this reason the file keeps mode `0600`.
- The plugin, from `<repo>/tern-plugin` (or `--plugin DIR`), through
  `tern plugin install DIR --force`. Only a source checkout has `<repo>/tern-plugin`: on another
  install, pass `--plugin DIR`, or `--no-plugin` when the plugin is already installed. The
  plugin's **Install in Tern** step uses `--no-plugin`.

Running it again changes nothing when the settings are already correct. The command does not
touch a `settings.json` that is not valid JSON (for example, one with comments).

## ACP bridge: `symphony acp`

Tern starts this process (stdio, JSON-RPC). Do not run it by hand.

- `initialize` announces `loadSession`, `session/list` and `session/resume`.
- `session/list` returns the sessions, with the title `ENG-1 · <title> · <state>`.
- `session/load` replays the transcript: messages, collapsed thinking, and tool calls with
  `kind`, `rawInput` and `locations`, so Tern draws bash, reads and edits natively. Then it
  follows the live stream over SSE from the `last_event_id` read before the replay.
  `session/resume` only follows the live stream.
- Each loaded session has one SSE connection. If the connection drops, the bridge reconnects
  with `Last-Event-ID`. It loses no events and repeats none.
- `session/prompt` posts the message in `auto` mode and replies at once. If there is an open
  question, the message is the answer. If the agent is running, it is a steer. Otherwise it is
  a follow-up. A short note says how the message was delivered. The agent's echo of your own
  message is not shown twice.
- The chat commands are `/interrupt`, `/pause`, `/resume`, `/status` and `/takeover`.
  `/pause` and `/resume` act on all of Symphony, because the API has no per-ticket pause.
  `/takeover` explains that a takeover holds the ticket and how to return it. The prompt
  replies at once, so Esc in Tern cancels only while a message is being sent. To stop the
  agent, use `/interrupt`. `session/cancel` also sends an interrupt.
- A question from the agent appears as a block titled "The agent asks", with its options.
- `session/new` does not create work. It explains that work starts with a ticket in the tracker
  (Linear or GitHub Projects).
- The bridge declares the MCP transports `http` and `sse` only so that Tern does not warn that
  it "can't reach" the `agents.mcp` servers. The agent runs remotely and does not use these
  servers.
