# AGENTS.md

This file gives the rules for AI coding agents that work on this repository. Human contributors
must also obey these rules. Read all of this file before you change code.

## 1. What this repository is

Symphony runs coding agents (the `omp` engine) on Linear tickets. One agent works on one ticket
in an isolated workspace. A separate service, `symphony-merger`, reviews and merges the pull
requests that the agents open. People watch and steer the agents from the Tern terminal, the
`symphony` CLI, or MCP.

Read these documents before you change a component:

| You change | Read first |
|---|---|
| Any code | [docs/architecture.md](docs/architecture.md), [docs/internals.md](docs/internals.md) |
| The daemon, runner or workspace | [docs/concepts.md](docs/concepts.md), [docs/configuration.md](docs/configuration.md) |
| The HTTP API, MCP or webhooks | [docs/api.md](docs/api.md) |
| The merger | [docs/merger.md](docs/merger.md) |
| Models and credentials | [docs/providers.md](docs/providers.md), [docs/security.md](docs/security.md) |
| Kubernetes manifests or images | [docs/install.md](docs/install.md) |
| The Tern plugin or the CLI | [docs/tern.md](docs/tern.md), [docs/cli.md](docs/cli.md) |
| Tests and mocks | [docs/testing.md](docs/testing.md) |

`docs/internals.md` is the contract between the components. If your change makes the code
disagree with it, change the document in the same commit.

## 2. Repository layout

| Path | Contents |
|---|---|
| `src/symphony/` | The Python package. One module has one responsibility. |
| `src/symphony/entry/` | Process entry points: `daemon`, `serve` (API and gateway), `run_issue` (runner Job), `reflect`, `merger`, `sweep`, `local`. |
| `src/symphony/api/` | The control API (`/v1`), MCP, webhooks, the model catalog. |
| `src/symphony/merger/` | The merger: review, merge train, conflict resolution, main-branch watch. |
| `src/symphony/trackers/` | Ticket trackers: `linear` and `memory`. |
| `src/symphony/cli/` | The `symphony` CLI and the ACP chat bridge. |
| `tern-plugin/` | The Tern plugin (Luau). |
| `deploy/` | Kustomize manifests for Kubernetes and k3s. |
| `docker/` | Container images: `orchestrator` and `agent`. |
| `scripts/` | Image builds, end-to-end runs, vendor mocks (`scripts/mocks/`). |
| `tests/` | Unit, integration and end-to-end tests. |
| `WORKFLOW.md` | The default workflow: tracker, hooks, policy and agent prompt. |

## 3. Commands

Use `uv`. Do not use `pip` or a global Python.

| Task | Command |
|---|---|
| Install | `uv sync --extra dev` |
| Run all tests | `uv run pytest` |
| Run one test file | `uv run pytest tests/test_dispatch.py` |
| Lint | `uv run ruff check .` |
| Run the stack locally | `uv run python -m symphony.entry.local --root DIR` (needs `REDIS_URL`) |
| Run the mocked end-to-end certification | `uv run python -m scripts.e2e_mocked.run` |
| Build images | `scripts/build_images.sh` |

Many tests start a disposable `redis-server`. Install Redis before you run the tests. The Tern
tests need the `tern` binary; they skip when it is absent. Tests that call a real model or the
real `omp` binary skip unless `SYMPHONY_REAL_OMP=1`.

## 4. Rules that you must obey

### 4.1 Language

- Write all code, comments, documentation, commit messages, log events and user-facing text in
  English.
- Write short sentences. Use the active voice. Use one term for one thing in all files.
- Agents write pull request text and Linear comments in English.

### 4.2 Credentials

- Never print, log, store in Redis or put in a command line a credential value. This includes
  API keys, GitHub App keys and tokens, Linear keys, Slack tokens and API tokens.
- The Linear API key, the GitHub App private key, Slack tokens and Symphony API tokens stay on
  the host side. They must never enter a workspace pod. Agents reach Linear only through the
  `linear_graphql` host tool.
- A workspace gets only two credentials: the model credential of its role and a short-lived
  GitHub token scoped to the ticket's repositories. Keep them in private files, never in the
  environment of other processes or in a command line.
- Model API keys and integration credentials are write-only. The API, the CLI and the plugin
  never show a stored value. The API returns only two credentials, one time each, with
  `Cache-Control: no-store`: a new Symphony API token at creation, and new webhook secrets from
  `POST /v1/setup/webhooks`.
- Setup writes are admin-only, audited (`setup.<step>`), and verified against the real service
  before the value is stored. The API patches only the pre-created Secrets; never give it
  `create` or `delete`.
- Symphony has no OAuth, subscription or session-login flows for model providers. Do not add
  them.
- A test that needs a credential uses a dummy value or a mock in `scripts/mocks/`.

### 4.3 Correctness

- Every state change is fenced. A runner of an old dispatch must not write. Keep the dispatch
  token and the `dispatch_seq` checks in every new write path.
- Durable intents (pause, resume, hold, release, interrupt) go through `admin_commands`. Do not
  write those keys directly from another module.
- Read team configuration with `teamconfig` and bind role models with
  `config.bind_role_model`. Do not read models from another place.
- Keep the latency targets in [docs/internals.md](docs/internals.md): wake under 3 s, steer and
  live events under 300 ms. A change that can slow these paths needs a latency test.

### 4.4 Change discipline

- Make the smallest change that solves the task. Do not refactor code that the task does not
  need.
- Do a clean cutover. Remove the old code path, aliases and dead tests in the same change.
- Do not add a dependency without a clear need. Add it to `pyproject.toml` and commit `uv.lock`.
- Pin external versions. The `omp` engine version is `edge.OMP_VERSION`. The images, the
  catalog installer and the tests must agree with it.
- Do not edit generated or vendored files. The vendor specifications in `scripts/mocks/specs/`
  change only together with their row in `scripts/mocks/specs/SOURCES.md`.

### 4.5 Tests

- A bug fix comes with a test that fails before the fix.
- A test must check behavior that a user or a component can see. Do not test private wiring,
  log text or source text.
- Tests must be deterministic and isolated. Use the `redis_client` fixture for Redis. Do not
  depend on test order or on the network.
- Before you finish, run the tests for the modules that you changed and `uv run ruff check .`.
  Do not finish with a failing test or a lint error.

### 4.6 Mocks

- The mocks in `scripts/mocks/` must follow the vendor documentation. Each mock validates every
  request against the vendored specification.
- If Symphony needs a vendor behavior that a mock does not have, add it to the mock from the
  vendor documentation. Do not change Symphony to fit a wrong mock.

## 5. Commits and pull requests

- One commit has one purpose. Write the subject in the imperative mood, 72 characters or less,
  with a type prefix: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- The body says what changed and why. It does not repeat the diff.
- A pull request links its issue, says how you verified the change, and lists the risks.

## 6. When you are not sure

- Read the code and the documents first. Most answers are there.
- If a requirement is not clear, stop and ask. Do not guess about credentials, data loss or
  public interfaces.
