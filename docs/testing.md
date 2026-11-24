# Testing

This document tells you how the tests are organized and how to run each suite. It also
describes the vendor mocks and the three certification harnesses.

## Prerequisites

- Python 3.12 and `uv`.
- `redis-server` on `PATH`. The `redis_client` fixture starts a real disposable Redis.
- `git` on `PATH`.
- The `websockets` package (a runtime dependency in `pyproject.toml`, installed by `uv sync`).
  The assistant's transports and the Slack and Discord mocks use it for Socket Mode and the
  Discord Gateway.
- For opt-in suites only: the pinned omp (18.20.2), Tern, `kubectl`, `kind`, `docker`, and
  model credentials.

## Run the default suite

1. Install the dependencies:

   ```bash
   uv sync --extra dev
   ```

2. Run all default tests:

   ```bash
   uv run pytest
   ```

3. Run one file or one test:

   ```bash
   uv run pytest tests/test_api_sse.py
   uv run pytest tests/test_hold.py -k release
   ```

4. Run the linter:

   ```bash
   uv run ruff check src tests scripts
   ```

The default suite makes no model call and needs no network. `pyproject.toml` sets
`testpaths = ["tests"]` and `pythonpath = ["src", "."]`.

## Test layout

| Path | Covers |
|---|---|
| `tests/conftest.py` | Shared fixtures (see [Fixtures](#fixtures)) |
| `tests/api_support.py` | API test helpers: services on a real Redis, tokens, bearer headers, a fresh daemon heartbeat |
| `tests/omp_fakes.py` | A fake omp RPC process for runner and merger tests |
| `tests/fixtures/omp-catalog/` | A copy of the omp catalog (`models.json`, `rules.json`) for catalog tests |
| `tests/test_api_*.py` | Every `/v1` route, authentication, roles, ETag, SSE, MCP, webhooks, hold, latency |
| `tests/test_admin_*.py` | Durable commands, workflow publication, providers, read projections |
| `tests/test_dispatch.py`, `test_fencing.py`, `test_eligibility.py`, `test_reconcile.py`, `test_retry.py` | Daemon: dispatch, fencing, eligibility, reconciliation, retries |
| `tests/test_runner*.py`, `test_omp_rpc.py`, `test_hosttools.py`, `test_linear_tools.py` | Runner, omp RPC, host tools |
| `tests/test_linear.py`, `test_github_tracker.py`, `test_memory_tracker.py` | The Linear, GitHub and memory trackers against the tracker interface. The GitHub tests run against the GitHub mock |
| `tests/test_notify.py` | Slack and Discord notifications, mentions and message limits |
| `tests/test_merger_*.py`, `test_review_*.py` | Merger review, train, units and support |
| `tests/test_cli_*.py` | CLI commands against a fake API, a real API, and the watch viewer |
| `tests/test_acp_bridge.py` | The ACP bridge |
| `tests/test_deploy.py` | `deploy/`, `docker/` and `scripts/` checked against the code and [internals.md](internals.md#9-services-ports-and-variables) |
| `tests/test_endpoints.py` | Every external endpoint follows its variable |
| `tests/mocks/` | The vendor mocks themselves |
| `tests/tern/` | The Tern plugin, with a fake API, a real API and a real stack |
| `tests/test_e2e_mocked.py` | Opt-in wrapper for the mocked certification |

## Fixtures

| Fixture | Scope | Gives |
|---|---|---|
| `redis_client` | Function | A real Redis client on a disposable server (session-scoped `_redis_server`). The database is empty at the start of each test |
| `kv` | Function | `FakeKV`, an in-memory `state.KV` |
| `clock` | Function | `FakeClock` with `now_ms()` and `advance(ms)` |
| `tracker` | Function | `MemoryTracker`, an in-memory tracker |

## Opt-in suites

These suites are skipped unless you set the condition.

| Suite | Condition | Needs |
|---|---|---|
| `tests/test_runner_real.py` | `SYMPHONY_REAL_OMP=1` | The pinned omp and model credentials. `SYMPHONY_REAL_PROVIDER` and `SYMPHONY_REAL_MODEL` select the model (default Bedrock) |
| Real-engine tests in `tests/mocks/test_mock_models.py` | `SYMPHONY_REAL_OMP=1` | The pinned omp. The model calls go to the model mock |
| `tests/test_e2e_mocked.py` | `SYMPHONY_E2E_MOCKED=1` | See [Mocked end-to-end certification](#mocked-end-to-end-certification) |
| `tests/tern/test_tern_plugin.py` | Tern is installed | Tern |
| `tests/tern/test_tern_real_stack.py` | Tern and `redis-server` are installed | Tern and `redis-server` |
| Parts of `tests/test_deploy.py` | `kubectl` is installed | `kubectl` |

Example:

```bash
SYMPHONY_REAL_OMP=1 uv run pytest tests/test_runner_real.py
```

## Vendor mocks

The mocks in `scripts/mocks/` copy the documented behavior of each vendor. They are for tests
and certification only. Each mock checks each request against the vendor specification in
`scripts/mocks/specs/`. `scripts/mocks/specs/SOURCES.md` gives the source URL, the commit or
fetch date, and the sha256 of each file.

| Mock | Start command | Default port | Serves | Validates against |
|---|---|---|---|---|
| Linear | `uv run python -m scripts.mocks.linear --port 4100 --state DIR` | 4100 | GraphQL `POST /graphql`, file uploads (`fileUpload` with a signed `PUT`), assets, signed webhooks, rate limit headers | `linear.graphql` (executed with graphql-core) and the Linear developer pages |
| GitHub | `uv run python -m scripts.mocks.github --port 4200 --state DIR [--https-port 4201]` | 4200 | REST (also under `/api/v3`), GraphQL, git smart HTTP, Actions CI, job logs, signed webhooks, App JWT and installation tokens. For the GitHub tracker: Issues REST, sub-issues, dependencies and Projects v2 GraphQL | `github-api.github.com.json.gz` (OpenAPI) and `github-schema.docs.graphql` |
| Slack | `uv run python -m scripts.mocks.slack --port 4300 --state DIR` | 4300 | Web API `chat.postMessage`, `chat.update`, `chat.postEphemeral`, `conversations.replies`, `auth.test`, `users.lookupByEmail`, `apps.connections.open`, the external file upload (`files.getUploadURLExternal`, the upload URL, `files.completeUploadExternal`); Socket Mode over a websocket it serves; incoming webhooks | `slack_web_openapi_v2.json` and the `slack-*.md` pages (Socket Mode, `app_mention`, `message.im`, `block_actions`, the methods, working with files) |
| Models | `uv run python -m scripts.mocks.models --port 4400` | 4400 | Anthropic Messages at `/anthropic`, OpenAI Responses at `/openai/v1`, both streamed, from a deterministic script | `anthropic_messages.schema.json`, `openai.openapi.yaml.gz` and the vendor pages |
| Discord | `uv run python -m scripts.mocks.discord --port 4500 --state DIR` | 4500 | Execute Webhook, Create and Edit Message (`/api` and `/api/v10`; Create Message with `multipart/form-data` files too), allowed mentions, rate limits, `GET /gateway/bot`, `GET /applications/@me`, the interaction callback, and a Gateway websocket (Hello, Identify, Heartbeat, Resume, `READY`, `MESSAGE_CREATE`, `INTERACTION_CREATE`, op 7, op 9) | `discord-openapi.json.gz` and the `discord-*.md` reference pages |
| MCP | `uv run python -m scripts.mocks.mcp --port 4600` (token in `MCP_MOCK_TOKEN`, default `mock-mcp-token`) | 4600 | MCP 2025-11-25 Streamable HTTP at `/mcp`: `initialize`, `ping`, `tools/list`, `tools/call` (`echo`, `lookup_ticket_context`), sessions (`MCP-Session-Id`), JSON or SSE answers. It requires `Authorization: Bearer <token>`, so tests prove that the gateway adds it | `mcp-2025-11-25-schema.json.gz` and the `mcp-2025-11-25-*.md` pages |
| S3 | `uv run python -m scripts.mocks.s3 --port 4700 --bucket symphony-backups` (keys in `S3_MOCK_ACCESS_KEY_ID` and `S3_MOCK_SECRET_ACCESS_KEY`, default the example keys of the AWS SigV4 page) | 4700 | PutObject, GetObject, ListObjectsV2 and DeleteObject on path-style URLs, with SigV4 header auth (signature, scope, clock, signed headers, payload hash). `GET /_mock/objects`, `GET /_mock/requests` (never a header value), `POST /_mock/buckets`, `POST /_mock/credentials`, `POST /_mock/reset` | The request syntax of the `aws-s3-*.md` pages, the codes of `aws-s3-error-responses.md`, and the `aws-s3-sigv4-header-auth.md` and `aws-sigv4-*.md` pages |

Rules that all mocks follow:

- A request outside the specification gets the documented error. Linear: HTTP 400 with
  `errors[].extensions`. GitHub: 404 `Not Found` or 422 `Invalid request` with
  `documentation_url`. Slack: HTTP 200 with `ok: false` and `error`. Discord: 4xx with
  `{code, message, errors?}`.
- A feature that a mock does not model gets an explicit "not implemented" error. The mock never
  invents data.
- Authentication is as documented. Linear accepts `Authorization: <api key>` and refuses
  `Bearer`. GitHub mints installation tokens from an RS256 App JWT, scoped to the requested
  repositories and permissions, and enforces the scope on REST, GraphQL and git. A read-only
  token cannot push (403). Slack accepts `Bearer xoxb-…`. Discord accepts `Bot <token>`.
- Webhooks are signed as documented (`Linear-Signature`; `X-Hub-Signature-256`).
- A control API under `/_mock/*` serves the certification script. The script can make tickets
  and comments, set CI results and set branch protection. It can also read the state, the
  requests and the recorded violations.
- The GitHub mock has control routes for the GitHub tracker: `/_mock/projects`,
  `/_mock/issues`, `/_mock/project-items` and `/_mock/project-items/move`.
- The Discord mock records each message (`GET /_mock/messages`) and can return 429 on request
  (`POST /_mock/ratelimit`).
- The Slack mock drives Socket Mode from `/_control/socket*`: `POST /_control/socket/event`
  pushes an `events_api` envelope, `POST /_control/socket/interactive` a `block_actions` click,
  `POST /_control/socket/disconnect` a `disconnect`, and `POST /_control/socket/drop` cuts the
  connection. `GET /_control/socket` returns the connections, envelopes, acks and errors.
- The Discord mock drives the Gateway from `/_control/gateway*`: `hello`, `heartbeat_ack`,
  `heartbeat_request`, `reconnect` (op 7), `invalid_session` (op 9), `close`, `drop`,
  `dispatch`, `message`, `thread` and `interaction`. `GET /_control/gateway` returns the
  connections, identifies, resumes, heartbeats and closes. `GET /_mock/callbacks` and
  `GET /_mock/edits` return the interaction answers and the message edits.
- The MCP mock has `GET /_control/requests` (each request's header names, never values, and
  `has_auth`), `GET /_control/streams`, `POST /_control/config` (`{sse, sse_delay_s}`) and
  `POST /_control/reset`. An unserved method or path gets 501.
- The mock tests in `tests/mocks/` check the responses against the response schemas of the
  specifications.

To point Symphony at the mocks, set the endpoint variables of
[internals.md](internals.md#endpoints). Example for GitHub:

```bash
GITHUB_API_URL=http://127.0.0.1:4200
GITHUB_GRAPHQL_URL=http://127.0.0.1:4200/graphql
GITHUB_SERVER_URL=http://127.0.0.1:4200
```

## Mocked end-to-end certification

`scripts/e2e_mocked/run.py` runs Symphony end to end against the vendor mocks. It uses the
real omp and a real model.

1. Set the model credentials:

   ```bash
   export AWS_REGION=us-east-1
   export AWS_BEARER_TOKEN_BEDROCK=…
   ```

2. Optional: set `SYMPHONY_OMP_BIN` to the pinned omp.
3. Run all scenarios:

   ```bash
   uv run python -m scripts.e2e_mocked.run --out result.json
   ```

4. Run some scenarios only:

   ```bash
   uv run python -m scripts.e2e_mocked.run --scenario 1 --scenario 8
   ```

5. Add `--keep` to keep the temporary directory with logs, mock state and workspaces.

The script starts a disposable Redis, the Linear, GitHub and Slack mocks, Symphony local mode
(`python -m symphony.entry.local`) and the merger, all on `127.0.0.1` and free ports.

| # | Scenario |
|---|---|
| 1 | Implement and merge. The ticket goes through webhook, dispatch, App token, push and a ready pull request. A person answers a question through the API. CI is green. The merger reviews and merges. Linear moves to `Merging`, then `Done` |
| 2 | Multi-repo: one ticket with two `repo:` labels → one token for both → one pull request each |
| 3 | Investigation: read-only token, refused push, report comment, no pull request |
| 4 | Sub-issues and evidence: `symphony_create_issues` and `symphony_attach_evidence` |
| 5 | Review comment answer: a person's inline comment → webhook → answer run → reply |
| 6 | Learning pull request: the retrospective of scenario 1 opens a learnings pull request |
| 7 | Main red → one Linear ticket with routing labels and the log → green closes it |
| 8 | Rejection: a pull request with a real bug goes back (draft, `Rework`, report) |

Scenarios 5 and 6 need scenario 1, so the script runs scenario 1 first. Each scenario checks the
mock state, not only the logs. The script prints one JSON summary with checks, timings and
state excerpts. It prints no credential value.

The pytest wrapper runs the same script:

```bash
SYMPHONY_E2E_MOCKED=1 uv run pytest tests/test_e2e_mocked.py
```

The results are in [mocked-certification.md](mocked-certification.md).

## Kubernetes certification on kind

`scripts/e2e_kind.sh` deploys Symphony on a local kind cluster and runs one real attempt.

> **Caution:** the script makes a kind cluster and Docker images on your machine. Use
> `E2E_TEARDOWN=1` to delete the cluster at the end.

1. Install `kind`, `kubectl`, `docker`, `uv`, `curl` and `python3`.
2. Optional: set `AWS_REGION` and `AWS_BEARER_TOKEN_BEDROCK` for the real attempt.
3. Run the script:

   ```bash
   E2E_TEARDOWN=1 scripts/e2e_kind.sh
   ```

| Variable | Effect |
|---|---|
| `E2E_KEEP=0` | Stop after the checks (default: keep the port-forward open) |
| `E2E_ATTEMPT=0` | Deploy only, with no model call. This is the default when `AWS_BEARER_TOKEN_BEDROCK` is not set |
| `E2E_TEARDOWN=1` | Delete the cluster at the end (sets `E2E_KEEP=0`) |
| `E2E_SKIP_BUILD=1` | Use the images that you built before |
| `E2E_DELETE=1` | Delete the cluster and stop |
| `E2E_MERGER_REPO=owner/name` | Start the merger (needs a real GitHub App) |
| `E2E_PROVIDER`, `E2E_MODEL` | Change the model (default: `amazon-bedrock` and the model set in `scripts/e2e_kind.sh`) |

The script does these steps:

1. It makes the cluster from `scripts/e2e/kind-cluster.yaml`.
2. It builds the images and loads them into kind.
3. It makes the namespace, the Secrets and a bootstrap admin token.
4. It publishes the certification workflow from `scripts/e2e/WORKFLOW.md.tmpl` (memory tracker,
   local git hook, `max_turns: 1`, learning off).
5. It applies the overlay `scripts/e2e` and waits for Redis, the API, the gateway and the daemon.
6. It checks `/healthz` and `/v1/me` through a port-forward.
7. With model credentials, it runs `scripts/e2e/certify.py`: one real attempt with a steer,
   telemetry, a second attempt, terminal cleanup and the nightly sweep.

The results are in [certification.md](certification.md#kubernetes-on-kind).

## Kubernetes setup certification on kind

`scripts/e2e_kind_setup.sh` certifies a minimal install on kind. The operator makes only the
Secrets `symphony-api` and `model-provider`. The setup API does the rest, against the vendor
mocks that run as pods in the namespace `symphony-e2e-mocks`.

> **Caution:** the script makes a kind cluster (`symphony-e2e-setup`) and Docker images on your
> machine. The attempt and the assistant call real models. Use `E2E_TEARDOWN=1` to delete the
> cluster at the end.

1. Install `kind`, `kubectl`, `docker`, `uv` and `python3`.
2. Set `AWS_REGION` and `AWS_BEARER_TOKEN_BEDROCK`. The script stops without them.
3. Run the script:

   ```bash
   E2E_TEARDOWN=1 scripts/e2e_kind_setup.sh
   ```

| Variable | Effect |
|---|---|
| `E2E_TEARDOWN=1` | Delete the cluster at the end |
| `E2E_SKIP_BUILD=1` | Use the images that you built before (the mock image is always built) |
| `E2E_DELETE=1` | Delete the cluster and stop |
| `E2E_STATE_DIR` | The private directory of the token, the mock tokens and the summary (default `~/.cache/symphony-e2e-setup`) |

The script does these steps:

1. It makes the cluster with the Kubernetes audit log on (`scripts/e2e_kind/audit-policy.yaml`:
   Metadata level, the Symphony ServiceAccounts only, never a body). kindnet enforces
   NetworkPolicy.
2. It builds every image, and the mock image from `scripts/e2e_kind/mocks.Dockerfile`.
3. It makes the namespace and only the Secrets `symphony-api` and `model-provider`. Then it runs
   `kubectl apply -k scripts/e2e_kind`: `deploy/` with the test deviations of
   [certification.md](certification.md#deviations).
4. It starts the mocks (`scripts/e2e_kind/mocks.yaml`) with random bearer tokens.
5. It runs `scripts/e2e_kind/certify.py`. The areas are NetworkPolicy, the setup steps, MCP
   through the gateway, the assistant Deployment on Slack Socket Mode, and Seiji with the lease
   cleanup. The summary is in `$E2E_STATE_DIR/certification.json`.

The results are in [certification.md](certification.md#kubernetes-setup-on-kind).

## Merger certification

`scripts/e2e_merger/harness.py` runs the production merger code against a real GitHub
repository, with real GitHub Actions CI and real agents.

> **Caution:** the harness pushes branches, opens pull requests and merges them. Use a repository that
> holds no other work.

The harness differs from a deployment in these ways:

- It uses the token of `gh auth token`, not a GitHub App.
- It runs a local disposable Redis under `$E2E_ROOT`.
- It gives the agents the model credentials of its own environment.
- It writes tracker changes to `tracker.jsonl` (`RecordingTracker`) and notifications to
  `notify.jsonl`.
- No webhook wakes the merger. The safety poll runs every `--poll` seconds.

1. Set `E2E_REPO=<owner>/<name>` to your test repository.
2. Set the model credentials in the environment.
3. Seed the repository:

   ```bash
   uv run python scripts/e2e_merger/harness.py setup
   ```

4. Open the pull requests of a scenario (`a` to `f`):

   ```bash
   uv run python scripts/e2e_merger/harness.py open a
   ```

5. Run the merger until a goal:

   ```bash
   uv run python scripts/e2e_merger/harness.py run --until merged:1 drafted:2
   ```

6. Print the timings:

   ```bash
   uv run python scripts/e2e_merger/harness.py evidence
   ```

7. Stop the local Redis:

   ```bash
   uv run python scripts/e2e_merger/harness.py stop
   ```

Use `run --no-train` to review pull requests without merging them, for example to form a batch.

The results are in [certification.md](certification.md#merger-on-a-real-github-repository).

## Local mode certification

`scripts/e2e_local.sh [--out DIR] [--keep]` starts Redis, local mode, the real omp and a
headless Tern. Then it does these steps:

1. It makes an in-memory ticket and signs in.
2. It runs `symphony tern install --no-plugin` in an isolated home.
3. It opens the live ACP chat and sends a steer.
4. It checks that the runner applied the steer.
5. It checks `symphony status --json` and the MCP tool `list_sessions`.
