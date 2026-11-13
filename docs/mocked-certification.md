# Mocked end-to-end certification

This run certifies Symphony end to end without real Linear, GitHub App, Slack or Discord
credentials. The vendor mocks in `scripts/mocks/` serve each vendor's published API
(internals §14). The agents are real: the pinned omp 18.20.2 on Amazon Bedrock.

The run has five configurations:

- The default: tickets in Linear, notices on Slack. It runs scenarios 1 to 8.
- `--tracker github --notify discord`: tickets are GitHub issues on a Projects board, and
  notices go to Discord. It runs scenarios 1, 3, 4 and 7.
- `--assistant`: the chat assistant on Slack with Linear (scenarios 9, 10 and 14), and MCP
  through the gateway (scenario 12).
- `--assistant --tracker github --notify discord`: the assistant on the Discord Gateway with
  the GitHub tracker (scenario 11).
- `--scenario 13`: an implementer uses Seiji (the device MCP server) through the gateway, with
  Linear and Slack.

## Run it

```bash
uv run python -m scripts.e2e_mocked.run                 # scenarios 1 to 8
uv run python -m scripts.e2e_mocked.run --scenario 3    # one scenario (repeatable, or 1,5,6)
uv run python -m scripts.e2e_mocked.run --keep --out /tmp/summary.json
uv run python -m scripts.e2e_mocked.run --tracker github --notify discord   # scenarios 1, 3, 4, 7
uv run python -m scripts.e2e_mocked.run --assistant                         # scenarios 9, 10, 12, 14
uv run python -m scripts.e2e_mocked.run --scenario 14                       # the assistant's soul and memory
uv run python -m scripts.e2e_mocked.run --assistant --tracker github --notify discord   # scenario 11
uv run python -m scripts.e2e_mocked.run --scenario 13                       # Seiji through the gateway
SYMPHONY_E2E_MOCKED=1 uv run pytest tests/test_e2e_mocked.py   # the five configurations, as an opt-in test
```

- Set `AWS_REGION` and `AWS_BEARER_TOKEN_BEDROCK`. The driver never prints them.
- The driver uses `SYMPHONY_OMP_BIN` for omp. The default is the Tern-managed 18.20.2 binary.
- `E2E_MODEL`, `E2E_THINKING` and `E2E_MAX_TURNS` change the implementer model, its thinking
  level and the turn cap. The default model is set in `scripts/e2e_mocked/run.py`. The other
  defaults are `low` and 3.
- `--keep` keeps the temporary directory: logs, mock state and workspaces.
- Scenarios 5 and 6 build on scenario 1. The driver runs scenario 1 first when you select them.
- `--scenario` (or `--scenarios`) also takes 9 to 14. Without `--scenario` or `--assistant`,
  the driver runs the same scenarios as before. Scenario 13 runs only when you name it.
- The driver prints one JSON summary. Each scenario lists its checks, timings and mock-state
  excerpts. `--out` also writes the summary to a file.

## What the driver starts

All processes listen on `127.0.0.1` on free ports.

| Part | How it runs |
|---|---|
| Redis | `redis-server` on a Unix socket, no persistence |
| Linear mock | `python -m scripts.mocks.linear`, team `ENG`; it signs webhooks to Symphony's `/v1/webhooks/linear` with a shared secret |
| GitHub mock | `python -m scripts.mocks.github`; the driver generates the App's RSA key and installs the App on `acme/app` and `acme/lib`; webhooks go to `/v1/webhooks/github` |
| Slack mock | `python -m scripts.mocks.slack`; bot token `xoxb-mock-symphony`, channel `C0SYMPHONY` |
| Discord mock | `python -m scripts.mocks.discord`; bot token `mock-discord-bot-token`, channel `1100000000000000001` |
| Symphony | `python -m symphony.entry.local` with the workflow in `scripts/e2e_mocked/WORKFLOW.md.tmpl`: `tracker.kind: linear` (team `ENG`), or `tracker.kind: github` with a board the driver creates for `acme` |
| Merger | `python -m symphony.entry.merger` for `acme/app`, started by the first scenario that needs it |
| Assistant | `python -m symphony.entry.assistant` on the same Redis, started by the first assistant scenario; Slack Socket Mode (app token `xapp-mock-symphony`) and the Discord Gateway, both pointed at the mocks; `SYMPHONY_MCP_FILE` is the local mode's `mcp.json`, and `SYMPHONY_GATEWAY_URL` is the local gateway |
| MCP mocks | two `python -m scripts.mocks.mcp`, each with its own random bearer (`MCP_MOCK_TOKEN`): `mock`, set in Admin › MCP, and `project`, named only in the repository's own `.omp/mcp.json` |
| Seiji mock | `python -m scripts.mocks.seiji` for scenario 13, with a random `sj_` token (`SEIJI_MOCK_TOKEN`) and `--public-url https://seiji.e2e.invalid`, an origin other than its API root, so its links need `rewrite_origins` |

With an assistant scenario, the workflow gains an `assistant:` section (both channels,
`repositories: [acme/app]`, `ticket_state` `Backlog` for Linear or `Todo` for the board, which
has no Backlog option). With an assistant scenario or scenario 13, every process also gets a
private `HOME`, so a developer's own omp config (`~/.omp/agent/mcp.json`) cannot add servers.
`SYMPHONY_MCP_ALLOW_HTTP_HOSTS=127.0.0.1` lets Admin accept the `http://` mock URLs.

For scenario 13, omp runs behind a small bash wrapper (`<tmp>/bin/omp`). For each launch with
`SEIJI_TOKEN` set, it records the SHA-256 of the value, its prefix before the first `_`, and
`SEIJI_URL`. Then it runs the pinned omp. So the driver reads the agent's real environment and
never holds a credential value from it. macOS hides other processes' environments from `ps`,
so a wrapper is the way to read it.

Symphony and the merger get the same environment: the GitHub App variables, the variables of
one notice channel (Slack bot or Discord bot), every endpoint variable (`LINEAR_API_URL`,
`GITHUB_API_URL`, `GITHUB_GRAPHQL_URL`, `GITHUB_SERVER_URL`, `SLACK_API_URL`,
`DISCORD_API_URL`) and the Bedrock credentials.

The App's installation has the permissions that Symphony asks for: `contents: write`,
`pull_requests: write`, `checks: read`, `actions: write`, `statuses: read`, `issues: write`,
`organization_projects: write` and `metadata: read`. The mock refuses a token request that asks
for more than the installation has.

`acme/app` is a small Python package with one function, one pytest test, an `AGENTS.md` and a
`.symphony/merger.md`. The policy has one review lane, a rebase merge, the `test` check and a
main watch that opens a ticket (`ticket: true`). The reviewer runs on an Anthropic model on
Amazon Bedrock. The oracle and the arbiter run on a model of another family on Amazon Bedrock.
All run at thinking level `low`. `acme/lib` is a second small package.

The mock CI runs `python -m pytest -q` on each head. `POST /_mock/ci` overrides the result.

The driver acts as a person (`octocat`) only through the mocks: it creates tickets, comments,
pushes to main and opens one PR. It runs its own git with `GIT_CONFIG_NOSYSTEM=1` and an empty
global config, so no host credential helper takes part.

Scenarios 4, 7 and 8 create tickets with the `symphony` label. The driver pauses admission
(`POST /v1/commands pause`) for them, so those tickets cost no agent runs. After each scenario,
the driver cancels what it created and resumes admission.

## Results

### Default configuration (Linear, Slack)

First full run: 2026-11-16, 04:52–04:57 UTC, on a test machine (macOS, arm64). omp 18.20.2,
implementer an Anthropic model on Amazon Bedrock at thinking level `low`. Setup took 2.1 s.
The eight scenarios took 265 s in total. Every check passed. The tables below show this run.

Regression run, after the workspace token and the GitHub tracker landed: 2026-11-16, 06:20 UTC.
All eight scenarios passed again in 233 s. Scenario 1 now also checks that the workspace token
has exactly `ghapp.WORKSPACE`: `contents: write`, `pull_requests: write`, `checks: read`,
`actions: read`, `statuses: read` and `metadata: read`.

In both runs, the GitHub mock recorded no request outside its specification. The Linear mock
served 126 and 118 requests with no error.

| # | Scenario | Feature ids | Result |
|---|---|---|---|
| 1 | Implement → PR → review and merge → Merging → Done | A1, A7, A8, B10, B11, B14, B15, O3, X1, X3, X4, R1, R2, R4, R5, R7, R12, T1, T3 | passed |
| 2 | One ticket, two repositories | O1, A2, B15 | passed |
| 3 | Investigation with a read-only token | O6, B15 | passed |
| 4 | Sub-issues and evidence | O5, O2 | passed |
| 5 | Review comment → agent answer | C17 | passed |
| 6 | Learnings PR (retrospective) | C18 | passed |
| 7 | Red main → ticket; green → closed | T8, X3 | passed |
| 8 | A real bug goes back to the author | R6, R8, R12, X3 | passed |

### 1. Implement → merge

| Check (on mock state) | Evidence | Time |
|---|---|---|
| The ticket exists in the Linear mock | `ENG-1`, `Todo`, labels `symphony` and `repo:acme/app` | — |
| The Linear webhook reached Symphony | `Issue create`, status 202 | — |
| The webhook woke the daemon | the session started 44 ms after the ticket (target < 3 s) | 0.04 s |
| The runner minted an App token | the mock's token record: `repos: [acme/app]`, login `symphony[bot]`; in the regression run, exactly `ghapp.WORKSPACE` | — |
| The token stays private | token file `0600`, helper `0700` | — |
| `symphony_ask` reached Slack | `chat.postMessage` to `C0SYMPHONY`: ":raising_hand: *Agent is waiting for an answer* … Which word should farewell() start with?" | — |
| The API answer reached the agent | `POST /v1/sessions/{sid}/messages` (`auto`) → 202; the input request cleared | < 0.01 s |
| The agent opened a ready PR | PR #1 "ENG-1: Add farewell() to app/greet.py", by `symphony[bot]`, not draft, label `symphony`, branch `eng-1-farewell` | 18.8 s |
| Git used smart HTTP | `info/refs` (401, then 200), `git-upload-pack` 200, `git-receive-pack` 200 | — |
| The Workpad went through `linear_graphql` | a `## Workpad` comment by the API key's user | — |
| The ticket moved to In Review | state `In Review` | 21.8 s |
| The change follows the answer | the diff adds `farewell()` returning `"Goodbye, {name}!"` and `test_farewell` | — |
| The mock CI passed | workflow `CI`, event `pull_request`, `success` | — |
| The merger reviewed the PR | report by the App: "**symphony-merger** on `d45226c47`: reviewed … No reviewer findings." | 34.5 s after the merger started |
| The train merged with rebase | PR merged by `symphony[bot]`; main head `dd34cb0 ENG-1: add farewell()` has one parent | 64.8 s after the merger started |
| The merger moved the ticket | state `Merging` | — |
| No error reached Slack | no `run_error` message | — |
| Done cleans up | the driver set `Done` through `/_mock` (the Linear↔GitHub integration); Symphony removed the workspace | — |

The attempt used 1 turn, 75,044 tokens and an estimated US$ 0.13.

### 2. Multi-repo

| Check | Evidence | Time |
|---|---|---|
| One token covers both repositories | token record `repos: [acme/app, acme/lib]` | — |
| One PR per repository, by the App | `acme/app#2` "ENG-2: Add VERSION to app" and `acme/lib#1` "ENG-2: Add VERSION to lib", both labelled `symphony` | 31.0 s |
| The ticket moved to In Review | state `In Review` | — |

### 3. Investigation

| Check | Evidence | Time |
|---|---|---|
| The token is read-only | token record `permissions: {contents: read, pull_requests: read, metadata: read}`, `repos: [acme/app]` | — |
| A push with the attempt's token fails | "remote: Write access to repository not granted." … "error: 403" | — |
| A PR with the attempt's token fails | 403 "Resource not accessible by integration" | — |
| The report is a Linear comment | "## Report … **Answer:** None. `app/greet.py` defines one public function, `greet` (app/greet.py:4) …" | — |
| The ticket is Done, with no PR or branch | no new PR; no `ENG-3` branch; no `probe-read-only` branch | 15.7 s |

The driver read the token from the attempt's credential file while the attempt ran. Then it
tried the push and the PR itself.

### 4. Sub-issues and evidence

| Check | Evidence | Time |
|---|---|---|
| `symphony_create_issues` created children | `ENG-5` "Add shout() to app/greet.py" and `ENG-6` "Document shout() in README.md", parent `ENG-4`, labels `symphony` and `repo:acme/app` | — |
| The children have a `blocks` relation | `ENG-5 blocks ENG-6` | — |
| `fileUpload` and the signed PUT stored the file | one upload, `text/plain`, 33 bytes; the asset bytes equal the file the agent wrote | — |
| `attachmentCreate` attached it | attachment "pytest evidence", subtitle `log` | — |
| The session lists the evidence | `meta.evidence[]`: "pytest evidence", kind `log` | — |
| The planning ticket is Done | state `Done` | 16.1 s |

### 5. Review comment answer

| Check | Evidence | Time |
|---|---|---|
| A person comments inline | `octocat` on `tests/test_greet.py` line 1 | — |
| The GitHub webhook reached Symphony | `pull_request_review_comment`, status 202 | — |
| An answer run started | session kind `answer` | 0.24 s |
| The App replied in the thread | reply by `symphony[bot]`, `in_reply_to_id` = the comment: "Yes, I ran the whole suite … `2 passed in 0.01s`" | 9.7 s |
| The ticket stayed in In Review | state `In Review` | — |

### 6. Learnings PR

| Check | Evidence | Time |
|---|---|---|
| The retrospective opened a PR | outcome `learnings-pr-opened`; the retro token is `done` and points at the PR | 7.5 s |
| The PR is the App's, with the label | PR #3 "learnings: ENG-1", by `symphony[bot]`, label `symphony-learnings`, branch `symphony/learnings/ENG-1` | — |
| It changes `AGENTS.md` | new lesson "Paste the exact test command and its output in the PR description", learned from the scenario 5 thread | — |

### 7. Red main

| Check | Evidence | Time |
|---|---|---|
| A red run on main opens one ticket | `ENG-7` "Main CI red: CI on d82d75555", `Todo`, labels `repo:acme/app`, `symphony`, `ci` | 2.3 s |
| The ticket carries the log | "FAILED tests/test_greet.py::test_greet - AssertionError: mocked red main" | — |
| Slack says main is red | ":red_circle: *main is broken* …" | — |
| A second red run opens no second ticket | only `ENG-7` | — |
| A green run closes it | comment "Main CI is green again …"; state `Done` | 2.3 s |
| Slack says main is green | ":large_green_circle: *main is green again* …" | — |

### 8. Rejection

| Check | Evidence | Time |
|---|---|---|
| A person opens a PR with a real bug | PR #4 "ENG-8: add is_even": `return n % 2 == 1` under "Return True when n is even" | — |
| The report blocks it | "### A: blocking (bug), confirmed … **Location:** app/parity.py:6" | — |
| The PR goes back to draft | draft, label `review-requested` | 30.3 s |
| Linear moves to Rework, with a comment | "The merger sent the PR back: not merged: #4 A: confirmed …" | — |
| Slack mentions the author | ":leftwards_arrow_with_hook: *PR returned to its author* <@U0OCTOCAT> …" (from `notify.users`) | — |


### GitHub tracker and Discord (`--tracker github --notify discord`)

Run of 2026-11-16, 06:20–06:23 UTC, on the same test machine. Scenarios 1, 3, 4 and 7 took
181 s in total. Every check passed. The GitHub mock recorded no request outside its
specification.

| # | Scenario | Result |
|---|---|---|
| 1 | Implement → PR → review and merge → Merging → closed | passed |
| 3 | Investigation with a read-only token | passed |
| 4 | Sub-issues, text evidence, binary evidence refused | passed |
| 7 | Red main → issue on the board; green → Done | passed |

**1. Implement → merge**

| Check (on mock state) | Evidence | Time |
|---|---|---|
| The ticket is an issue on the board | `acme/app#5` (`app-5`), Status `Todo`, label `symphony` | — |
| GitHub webhooks reached Symphony | `issues` (opened, closed) and `projects_v2_item` (created, edited), status 202 | — |
| The webhook woke the daemon | the session started 44 ms after the issue | 0.04 s |
| The workspace token has only workspace permissions | `repos: [acme/app]`, exactly `ghapp.WORKSPACE`: no `issues`, no projects | — |
| `symphony_ask` reached Discord | "🙋 **Agent is waiting for an answer** app-5 needs an answer: Which word should farewell() start with?" | — |
| The API answer reached the agent | `POST /v1/sessions/{sid}/messages` → 202 | — |
| The agent moved the board item | Status `In Review` | 25.1 s |
| The agent opened a ready PR | PR #6 "app-5: Add farewell() to app/greet.py", by `symphony[bot]`, label `symphony`, branch `app-5-farewell` | — |
| The Workpad is one issue comment | a `## Workpad` comment by `symphony[bot]` | — |
| The change follows the answer | `farewell()` returns `"Goodbye, {name}!"`, with `test_farewell` | — |
| The merger reviewed and merged | report "**symphony-merger** … No reviewer findings."; merged by `symphony[bot]` | 55.8 s after the merger started |
| The merger moved the board item | Status `Merging` | — |
| No error reached Discord | no `run_error` message | — |
| A closed issue is terminal | the driver closed the issue (GitHub does this for "Closes #5"); Symphony removed the workspace | — |

**3. Investigation**

| Check | Evidence | Time |
|---|---|---|
| The code token is read-only | `permissions: {contents: read, pull_requests: read, metadata: read}`, `repos: [acme/app]` | — |
| A PR with the attempt's token fails | 403 "Resource not accessible by integration" | — |
| The report is an issue comment by the App | "## Report … **Answer:** None. The module has one public function, `greet` (app/greet.py:4) …" | — |
| The ticket is Done, with no PR | Status `Done`; no new PR | 21.8 s |

**4. Sub-issues and evidence**

| Check | Evidence | Time |
|---|---|---|
| `symphony_create_issues` created sub-issues | `#3` "Add shout() to app/greet.py" and `#4` "Document shout() in README.md", sub-issues of `#2`, on the board in `Todo`, label `symphony` | — |
| The order is an issue dependency | `#4` is blocked by `#3` | — |
| Text evidence is a collapsible comment | `<!-- symphony:evidence {"title": "pytest evidence", "kind": "log"} -->`, `<details>` with the file's text | — |
| Binary evidence is refused | the tool answered "cannot attach shot.png (image/png): GitHub has no API to upload files to an issue …" | — |
| The session lists only the text evidence | `meta.evidence[]`: "pytest evidence", kind `log` | — |
| The planning ticket is Done | Status `Done` | 22.3 s |

**7. Red main**

| Check | Evidence | Time |
|---|---|---|
| A red run on main opens one issue on the board | `#7` "Main CI red: CI on c69350021", Status `Todo`, labels `ci`, `repo:acme/app`, `symphony` | 2.7 s |
| The issue carries the log | "FAILED tests/test_greet.py::test_greet - AssertionError: mocked red main" | — |
| Discord says main is red | "🔴 **main is broken** … (ticket app-7)" | — |
| A second red run opens no second issue | only `#7` | — |
| A green run closes the alert | comment "Main CI is green again …"; Status `Done` | 3.5 s |
| Discord says main is green | "🟢 **main is green again** …" | — |

### Assistant and MCP (`--assistant`)

Runs of 2026-11-16, 08:03–08:05 UTC, on the same test machine, omp 18.20.2. The two
configurations ran at the same time. The assistant, the researcher and the planner use the
role defaults of `teamconfig.DEFAULT`. Every check passed. The GitHub mock recorded no
request outside its specification. The Linear mock served 35 requests with no error.

| Configuration | Scenarios | Time | Result |
|---|---|---|---|
| `--assistant` (Linear, Slack) | 9, 10, 12 | 69.5 s | passed |
| `--assistant --tracker github --notify discord` | 11 | 43.2 s | passed |
| Regression: default (Linear, Slack), 07:58 UTC | 1–8 | 278.4 s | passed |
| Regression: `--tracker github --notify discord`, 07:58 UTC | 1, 3, 4, 7 | 182.9 s | passed |

The two regression runs ran with the W10 code in place. In the default run the Linear mock
answered one request with 400: an agent's own `linear_graphql` mutation with a bad escape. The
agent sent it again correctly. That is not a Symphony defect.

Before each ask, the driver pushes `docs/FACTS.md` to `acme/app` main. Line 3 says "The release
codename of acme/app is HERON-4127."

**9. Ask on Slack (Socket Mode)**

| Check (on mock state) | Evidence | Time |
|---|---|---|
| The `app_mention` envelope is acked | the Slack mock's ack record for the `envelope_id`: 0.003 s (limit 3 s) | — |
| "Working on it…" comes at once | `chat.postMessage` in the thread, first text "Working on it…" | 0.01 s |
| The same message gets the answer | 2 `chat.update` calls on that `ts`; final text "The release codename of acme/app is *HERON-4127*. It's written in <…/acme/app/blob/3755b2d…/docs/FACTS.md#L3\|acme/app:docs/FACTS.md:3>." | 10.1 s |
| The link is a code citation | `acme/app:docs/FACTS.md:3`, linked to `GITHUB_SERVER_URL` at the mirror's commit | — |
| The session store has an `assist` session | `assist:slack:C0SYMPHONY:<thread>:…`, meta `transport: slack`, `channel`, `thread`, `role: assistant`, `asker: octocat` | — |
| A thread follow-up uses the same session | one session for the thread; its events show 2 `running` turns; the answer is "acme/app:docs/FACTS.md" | 7.1 s |

**10. Plan and confirm on Slack, tickets in Linear**

| Check (on mock state) | Evidence | Time |
|---|---|---|
| The proposal has three buttons | an actions block with `proposal:<id>:create` "Create", `…:start` "Create and start", `…:dismiss` "Dismiss" | 10.1 s |
| An unmapped click is refused | `block_actions` from `U0STRANGER` → `chat.postEphemeral` to that user: "Only people mapped in notify.users can create tickets."; no new Linear issue 5 s later | — |
| A mapped click creates the tree | `U0OCTOCAT` (`octocat` in `notify.users`): `ENG-1` "Shout support" with children `ENG-2` and `ENG-3`, all `Backlog`, labels `repo:acme/app` and no `symphony` | 0.51 s |
| The order is a Linear relation | `ENG-2 blocks ENG-3` | — |
| The message lists the tickets | `chat.update`: "Created by octocat: - ENG-1: Shout support http://…/issue/ENG-1/… - ENG-2 … - ENG-3 …", no actions block | — |
| A second click creates nothing | the double click got "These tickets are being created." (ephemeral); a later click cannot reach the app, because the mock finds no button (`action_not_found`); still 3 tickets | — |
| The audit has the creation | one `assistant.tickets_created`, actor `octocat`, target `proposal:<id>`, reason "ENG-1, ENG-2, ENG-3" | — |

**11. Discord Gateway, GitHub tracker**

| Check (on mock state) | Evidence | Time |
|---|---|---|
| Identify asks for message content | the Gateway mock's Identify record: `intents` 37377 = `GUILDS \| GUILD_MESSAGES \| DIRECT_MESSAGES \| MESSAGE_CONTENT` | — |
| Ask: "Working on it…" then the answer | create message (reply to the mention) "Working on it…", 2 edits; final "The release codename of acme/app is **HERON-4127**. It's written in `[acme/app:docs/FACTS.md:3](<…/docs/FACTS.md#L3>)`." | 11.1 s |
| The session and the follow-up | one `assist:discord:…` session (`asker: octocat`); a reply to the bot's answer ran as its second turn | 6.1 s |
| The proposal has three buttons | components `proposal:<id>:create\|start\|dismiss`, "Create", "Create and start", "Dismiss" | 10.1 s |
| An unmapped click is refused | interaction callback type 4, flags 64 (ephemeral), "Only people mapped in notify.users can create tickets.", after 0.002 s; no new issue | — |
| A mapped click is answered | interaction callback type 6 after 0.007 s | — |
| The tree is on the board | `acme/app#1` "Shout support" with sub-issues `#2` and `#3`, all Status `Todo` on the Projects board, labels `repo:acme/app` | 0.51 s |
| The order is an issue dependency | `#3` is blocked by `#2` | — |
| The message lists the issues | edit "Created by octocat: - app-1: Shout support http://…/acme/app/issues/1 …", no components | — |
| A second click creates nothing | double click: "These tickets are being created." (ephemeral callback); a later click: the mock finds no button (`button_not_found`); still 3 issues | — |
| The audit has the creation | `assistant.tickets_created`, actor `octocat`, reason "app-1, app-2, app-3" | — |

**12. MCP through the gateway**

| Check (on mock state) | Evidence | Time |
|---|---|---|
| Admin stores the server | `PUT /v1/admin/mcp/mock` → 200, `http`, roles `implementer`, `assistant` | — |
| Admin stores the headers write-only | `PUT …/headers` → `headers_set: [Authorization, X-Acme-Tenant]`; `GET /v1/admin/mcp` holds no header value | — |
| Admin's test reaches the mock | `POST …/test` → `ok: true`, tools `echo`, `lookup_ticket_context`, protocol `2025-11-25` | — |
| The repository has its own MCP config | `acme/app` main gains `.omp/mcp.json` with a server `project` (the second mock) | — |
| The staged config names only the gateway | `<root>/credentials/<sandbox>/mcp/symphony/.mcp.json` (0600): one server `mock`, URL `http://127.0.0.1:<gateway>/v1/mcp/mock`, header `Authorization` with the minted bearer | — |
| The result reached the ticket | the Workpad: "Summary (verbatim): `ENG-4: no linked incidents; owner team ENG.`"; the report names `mcp__symphony_mock_lookup_ticket_context` | 16.1 s |
| The mock saw the call with the stored headers | every recorded request has `has_auth: true` and `x-acme-tenant`; one `tools/call` | — |
| The agent's bearer never reached the mock | the mock accepts only its own token, and the call succeeded; no file under `<root>` other than the local `mcp.json` holds that token; at the end, the agent's bearer gets 403 `mcp_forbidden` at the gateway (revoked) and 401 at the mock | — |
| The repository's own server is ignored | the `project` mock recorded no request | — |
| The assistant role uses the same route | the Slack answer is "ENG-4242: no linked incidents; owner team ENG."; the mock saw a second `tools/call` with the stored headers | 9.1 s |

### Seiji through the gateway (`--scenario 13`)

Run of 2026-11-16, 10:17 UTC, on the same test machine, omp 18.20.2, Linear and Slack. Setup
took 2.4 s. The scenario took 53.4 s. Every check passed. The GitHub mock recorded no request
outside its specification. The Linear mock served 27 requests with no error.

The regression run of scenarios 1 to 8 ran at the same time, with the W11 code in place. All
eight passed. The GitHub mock recorded no request outside its specification. The Linear mock
served 123 requests with no error.

| Configuration | Scenarios | Time | Result |
|---|---|---|---|
| `--scenario 13` (Linear, Slack) | 13 | 55.9 s | passed |
| Regression: default (Linear, Slack), 10:17 UTC | 1–8 | 262.2 s | passed |

The driver starts the Seiji mock with a random `sj_` token and the public URL
`https://seiji.e2e.invalid`. Admin stores the server with `preset: seiji`, the URL of the
mock's `/mcp`, the role `implementer`, and `rewrite_origins: [https://seiji.e2e.invalid]`.
The driver pushes `build/MyApp.zip` (4461 bytes, a simulator-app-shaped zip) to `acme/app`
main. The ticket asks the agent to acquire an iPhone, upload the zip through `$SEIJI_URL`, run
one screenshot step with `run_actions`, call `export_evidence`, put the Markdown in the PR
body, attach `report.md`, and NOT release the device.

**13. Seiji through the gateway**

| Check (on mock state) | Evidence | Time |
|---|---|---|
| The preset stores a plain definition | `PUT /v1/admin/mcp/seiji` → 200: `transport: http`, `http_base` = the mock's root, `timeout_ms: 3900000`, `agent_env {SEIJI_TOKEN: "{token}", SEIJI_URL: "{http_url}"}`, `cleanup {acquire_device, lease_id, release_device, lease_id}`, no `preset` field | — |
| The header is write-only | `PUT …/headers` → `headers_set: [Authorization]`; the response holds no token | — |
| Admin's test reaches Seiji | `POST …/test` → `ok: true`, the six mock tools, protocol `2025-11-25`, `me: {user: mock-agent, quota: {monthlyMinutes: 1200, …}}` | — |
| The staged config names only the gateway | the plugin's `.mcp.json`: server `seiji`, URL `http://127.0.0.1:<gateway>/v1/mcp/seiji`, `timeout: 3900000`, a bearer with prefix `mcp_`; no `sj_` token in the file | — |
| The agent's env holds a session token | the omp wrapper's record: `SEIJI_TOKEN` has prefix `mcp`, its SHA-256 is the staged bearer's, and Redis has a live token record for it (`role: implementer`, the attempt's session); `SEIJI_URL` = `http://127.0.0.1:<gateway>/v1/mcp/seiji/http` | dispatch 0.04 s |
| The agent acquired an iPhone | the mock's lease `l_…`, `iphone-16`, `ready` | 15.7 s after the ticket |
| The viewer works while the lease is held | `POST /v1/mcp/seiji/viewer {lease_id, control: false}` → 200, `url` `https://seiji.e2e.invalid/view/open/…` (absolute), `expires_in: 60`, `control: false` | — |
| Sessions show the held lease | session meta `mcp_leases: [{server: seiji, id: l_…, model: iphone-16, platform: ios}]` | — |
| The session cleanup released the lease | log `mcp_cleanup held=1 released=1 failed=0`, after `runner_finished`; the mock's lease ended `released` (reason `released`); no `mcp:held:*` key left in Redis. `held=1` also shows that the agent did not release it: an agent's `release_device` removes the id | 0.02 s after the attempt |
| The viewer refuses a released lease | `POST /v1/mcp/seiji/viewer` → 404 `lease_not_held` | — |
| The mock saw the whole flow | `tools/call`: `acquire_device`, `run_actions`, `export_evidence`, then one `release_device` (the cleanup) | — |
| The mock saw only the stored token | 13 requests, every one with `auth: valid`; `other_bearers` is empty, so the session token never reached Seiji | — |
| The upload arrived intact | the mock's app `MyApp.zip`: 4461 bytes, the same SHA-256 as the seeded zip | — |
| `report.md` came through `$SEIJI_URL` | the mock recorded `GET /v1/runs/r_…/report.md` with `auth: valid` | — |
| The PR body has the report | PR #1 by `symphony[bot]`: a "Seiji evidence" section with `### ✅ Home screen`, the run id and the evidence.zip link | session ended 51.4 s after the ticket |
| The links point at the gateway | every `/v1/runs/…` link in the PR body starts with `http://127.0.0.1:<gateway>/v1/mcp/seiji/http/` (`files/app.log`, `files/video.mp4`, `evidence.zip`); neither the public URL nor the mock's origin appears | — |
| The ticket has the evidence | Linear attachment "Seiji report"; the uploaded file is the report with the run id and gateway links; `meta.evidence[]` lists it with kind `report` | — |
| The `sj_` token stays in Symphony | no file under `<root>` other than the local `mcp.json` holds it; neither the PR nor the ticket does | — |


### Assistant soul and memory (`--scenario 14`)

Run of 2026-11-16, 12:09 UTC, on the same test machine, omp 18.20.2, Linear and Slack, the
assistant role on `amazon-bedrock/global.anthropic.claude-opus-5-5`. Setup took 2.8 s. The
scenario took 89.9 s. Every check passed. The GitHub mock recorded no request outside its
specification. The Linear mock served 7 requests with no error.

The assistant regression ran after the soul and memory code landed. Every check passed, and the
GitHub mock recorded no request outside its specification:

| Configuration | Scenarios | Time | Result |
|---|---|---|---|
| `--assistant` (Linear, Slack), 12:30 UTC | 9, 10, 12, 14 | 158.6 s | passed |
| `--assistant --tracker github --notify discord`, 12:33 UTC | 11 | 44.2 s | passed |

That run found one defect, fixed with a test (`test_the_start_up_sync_does_not_delay_the_first_session_fetch`):
the start-up mirror sync (which reads the soul for the heartbeat) started the 5-minute refresh
window, so the first session did not fetch a commit pushed after the start (scenario 9's fact).
The start-up sync no longer starts that window.

The driver pushes `.symphony/assistant.md` to `acme/app` main: front matter `name: Maestro`,
and the rule "end every reply with a last line that says exactly "— Maestro"". It restarts the
assistant, so its mirror syncs at start.

**14. Soul and memory**

| Check (on mock state) | Evidence | Time |
|---|---|---|
| The soul's version is visible | the heartbeat and `GET /v1/assistant/memory` both carry `soul: {repo: acme/app, path: .symphony/assistant.md, sha: 24579781…}`, the blob sha of the pushed file | — |
| The reply follows the persona rule | "Got it, octocat. I've noted that you deploy on Fridays. — Maestro" | 14.2 s |
| A remember is visible and stored | the reply ends with "I'll remember: octocat deploys on Fridays."; `GET /v1/assistant/memory/octocat` has one `explicit` item | — |
| A new thread uses the memory | a second `assist` session answers "You deploy on Fridays. — Maestro" | 7.1 s |
| Forget removes it | the reply ends with "I forgot: octocat deploys on Fridays."; the item list is empty | — |
| A secret is refused | "Remember my GitHub token …" with a made-up `ghp_` token: no memory key holds it and the reply says it never saves tokens | — |
| An unmapped person gets no memory | no memory key for the stranger; the reply says "Memory needs a mapped user: ask an admin to add you to notify.users in WORKFLOW.md." | — |
| The team fact proposal | a message with **Open PR** and **Dismiss** (`fact:<id>:pr\|dismiss`) for `acme/app:.symphony/assistant.md (Team facts)` | 6.0 s |
| An unmapped click opens nothing | the stranger gets "Only people mapped in notify.users can open team fact PRs." privately; no PR | — |
| A mapped click opens one PR | a double click; PR #1 by the App from `symphony/assistant-fact-<id>` to `main`; the file on the branch is the soul file with `## Team facts` and the fact; the message names the PR and has no buttons | 1.0 s |
| The audit | one `assistant.team_fact_pr` by `octocat`, target `fact:<id>`, reason `acme/app#1 <url>` | — |
| Wipe | `DELETE /v1/assistant/memory/octocat` → 200, the list is empty | — |

## Defects found and fixed

Each fix has a regression test.

1. **The local mode gave no GitHub App credential to its attempts** (`effects_local.py`).
   Only a pod got the scoped installation token. A local attempt used the host's git
   credentials, so B15 and O6 did not hold in the local mode. Now, with `GITHUB_APP_*` set,
   each attempt mints the same token as a pod. The token goes to a `0600` file under
   `<root>/credentials/`, a helper reads it, and git receives the helper through
   `GIT_CONFIG_*`. The token is removed when the attempt ends.
   (`test_with_a_github_app_an_attempt_gets_a_scoped_token_through_its_own_helper_only`)
2. **A system-wide credential helper ran before Symphony's** (`workspace.py`). Git asks
   system helpers first. On a host with Homebrew git, `osxkeychain` sent its own password
   and stored the attempt's token. The pod helper now resets the helper list with an empty
   entry before it adds its own. The local mode does the same through `GIT_CONFIG_*`.
   (`test_a_system_wide_helper_is_never_asked_before_ours` and the local test above)
3. **The merger outside a cluster read the user's kubeconfig** (`entry/merger.py`). It
   loaded the kubeconfig to read the providers Secret, so it reached whatever cluster the
   kubeconfig named, or it failed. Without `KUBERNETES_SERVICE_HOST`, it now skips the Secret,
   and the agents use the host environment.
   (`test_a_merger_outside_a_cluster_never_reaches_for_a_kubeconfig`)
4. **A comment in the handover window was never answered** (`prwatch.py`,
   `orchestrator.py`). A person commented one second after the PR was ready, before the
   daemon parked the ticket. The first review-watch check counted that comment as backlog.
   The backlog now ends at the run's `last_event_at`. A newer comment gets an answer at the
   first check. (`test_a_comment_after_the_run_ended_but_before_the_first_check_is_answered`)
5. **`symphony_attach_evidence` failed in the local mode** (`runner.py`). The runner gave the
   host tools `workspace.root` (`/workspace`, the pod's mount) for a host sandbox too. Every
   local attachment failed with "could not read … (exit 2)". A host sandbox now reads from its
   own directory. (`test_in_the_local_mode_evidence_is_read_from_the_workspace_directory`)
6. **The merger did not find a GitHub ticket from its PR** (`trackers/github.py`,
   `identifier_in`). The agent named the branch `app-5-farewell` and the PR
   "app-5: Add farewell()", as the prompt asks. The tracker only knew `symphony/<identifier>`
   branches, `owner/repo#n` refs and issue URLs, so the board item never moved to `Merging`.
   A branch or a title that starts with the identifier now links the PR. A structured ref
   still wins. (`test_a_pull_request_named_after_the_ticket_is_linked_to_it`)
7. **Every assistant turn failed at once** (`assistant/runtime.py`, found in scenario 9,
   fixed by AssistantCore). The tool list was `read,grep,find,web_search`. omp 18.20.2 only
   offers `find` with a native judge model, so on Amazon Bedrock omp exited with "Built-in
   tool unavailable in this session: find". The list is now `read,grep,glob,web_search`.
   (`test_one_process_serves_the_thread_and_streams_text` pins the argv;
   `test_every_tool_is_a_built_in_the_engine_always_has` checks each name against the real
   omp's `--help`, opt-in with `SYMPHONY_REAL_OMP=1`.)
8. **The viewer route always answered 422** (`admin_commands.py`, found in scenario 13,
   fixed by SeijiCore). `POST /v1/mcp/{name}/viewer` audits `mcp.viewer`, but that action was
   not in `STANDALONE_ACTIONS`, so the audit raised `invalid_audit` for every held lease. The
   action is now allowed.
   (`test_seiji_gateway.py::test_the_viewer_opens_only_a_lease_that_a_live_session_holds`)

## What this run does not certify

- **Real vendors.** The mocks follow the published specifications, but they are not the
  vendors. Behavior outside those specifications is not tested, for example rate limits
  under real load or webhook retries.
- **The Linear↔GitHub integration and GitHub's closing keywords.** The driver sets `Done`
  through `/_mock`, or closes the issue on the GitHub tracker. The merger does not close the
  ticket of a merged PR.
- **The reflector in the local mode.** The local mode only logs `reflector_skipped_local_mode`.
  Scenario 6 runs `reflector.run_retrospective` in `scripts/e2e_mocked/reflect.py`. It uses
  the App credential, `HttpxGitHub` and the tracker's Workpad, as `entry/reflect.py` does.
  Only the distillation differs: the same `omp -p` call runs on the host, not in a pod.
- **Kubernetes.** Pods, Jobs, NetworkPolicies and the providers Secret are not in this run.
- **Blocked dispatch (A1 for blocked children).** Admission was paused while the
  sub-issues existed, so the run does not show that only unblocked children start.
- **Discord by incoming webhook and Slack by incoming webhook.** The run uses the bot
  transports only.
- **A user-owned Projects board.** The board belongs to an organization (`acme`), so the run
  covers `organization_projects`, not `repository_projects`.
- **Assistant edges.** The run does not cover DMs, the busy queue over `max_concurrent`, the
  30-minute idle end and its fresh session with the thread's history, the "Create and start"
  and "Dismiss" buttons, a Socket Mode `disconnect` or a Gateway resume during a turn, or Tern
  following an `assist` session. A click after the edit cannot reach the app, because the
  mock only delivers clicks on buttons that a message still has. So the double click shows
  that creation is idempotent.
- **The assistant in a cluster.** It runs on the host. The `symphony-mcp` Secret and the
  NetworkPolicy are not in this run: the MCP servers live in the local mode's `mcp.json`.
- **Other MCP shapes.** The mock answers in JSON mode. SSE responses, `sse` servers and
  `stdio` servers, and the merger and reflector roles are not in this run.
- **Real Seiji and real devices.** Scenario 13 uses the Seiji mock: leases are ready at once,
  and runs record no real video. The 30–90 s boot under omp's MCP timeout, `install_app`, a
  relative viewer URL (Seiji without `listen.publicUrl`), the crash sweep for expired tokens,
  an upload near 1 GiB, and the Tern "Watch device" action are not in this run. The opt-in
  real-Seiji test covers auth, the protocol and an upload through the gateway.

## Findings fixed after the run

- The production `WORKFLOW.md` said that "the GitHub API works", but it did not say how the
  agent gets a token. It now tells the agent to read the token with `git credential fill`
  and to call `$GITHUB_API_URL`, the same as this workflow.
- The `input_requested` Slack text contained Portuguese. It is now English
  (`hosttools.py`, `_notify`).
