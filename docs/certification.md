# Certification results

This document gives the results of the certification runs. To run them yourself, read
[testing.md](testing.md). The results of the mocked end-to-end certification are in
[mocked-certification.md](mocked-certification.md).

## Kubernetes on kind

Run of 2026-11-16 on a test machine (macOS, arm64): kind v0.33, omp 18.20.2, and an Anthropic
model on Amazon Bedrock.

`E2E_TEARDOWN=1 scripts/e2e_kind.sh` stopped with exit code 0 in 275 s, from an empty machine to
a deleted cluster. The cluster took 34 s. The images took 68 s to build and load (with cache).
The rollouts took 15 s. The attempt took 155 s.

| Step | Evidence | Time |
|---|---|---|
| Ticket published → runner Job | `job/sym-run-kind-1-…-s1` | 7.7 s |
| → workspace Pod and sessions PVC | `ws-kind-1-…` (`symphony-agent:node`, no ServiceAccount token) and a 1 GiB PVC `…-omp`, `Bound` | 9.0 s |
| → `agent_start` on the API SSE stream, on the host through a port-forward | Session `kind-e2e-1:1` `running`, with the Anthropic model on Amazon Bedrock | 15.3 s |
| Live events | 264 events (`message_update`, `tool_execution_*`, `turn_end`, …). Delay from the Redis append to the host: p50 3 ms, p95 6 ms, maximum 15 ms | — |
| Steer through the API during a `sleep 40` | `202`, then `symphony.intent_applied` `{kind: steer, delivered: steer}` on SSE. The agent made `b.txt` = `beta` | 13 ms to the applied intent |
| End of the first attempt | `outcome: continuation`, transcript with 28 entries, `export.jsonl` with 29 lines, estimated cost USD 0.096 | 68 s |
| Telemetry | 5 `POST /v1/traces` on the gateway. Rollup in `telemetry:kind-e2e-1` with 16 spans, 8 model calls, 7 tool calls, 0 errors | — |
| Runaway guard | The continuation stayed held by `cooldown_hold` until a `refresh` | — |
| `POST /v1/commands refresh` → second attempt | Same Pod and same PVC (same UIDs). omp resumed the session: the same `.jsonl` file went from 31 to 48 lines. The transcript of session 2 has 45 entries | 2.8 s to `agent_start` |
| Ticket `Done` → terminal cleanup | Pod and PVC deleted | 18 s |
| Nightly sweep | `kubectl create job sweep-e2e --from=cronjob/symphony-nightly-sweep`, `succeeded: 1` | 3.1 s |

Runtime images: `IMAGES=agent RUNTIMES="dotnet9 dotnet10" scripts/build_images.sh` finished
in 139 s. `symphony-agent:dotnet9` has omp 18.20.2, .NET 9.0.321, node 22 and git.
`symphony-agent:dotnet10` has omp 18.20.2, .NET 10.0.404, node 22 and git. The certification
uses the `node` image.

### Re-run after W9–W11

Run of 2026-11-16, same machine, after the W9–W11 features. All images were built first with
`TAG=e2e scripts/build_images.sh` (orchestrator, `node`, `dotnet9`, `dotnet10`) in 194 s.
`E2E_TEARDOWN=1 scripts/e2e_kind.sh` stopped with exit code 0 in 211 s. The cluster took 45 s.
The images took 31 s to build and load (cache). The rollouts took 16 s. The attempt took 113 s.
No regression.

| Step | Evidence | Time |
|---|---|---|
| Ticket published → runner Job | `job/sym-run-kind-1-…-s1` | 11.2 s |
| → workspace Pod and sessions PVC | `symphony-agent:node`, no ServiceAccount token; 1 GiB PVC | 12.5 s |
| → `agent_start` on the API SSE stream | Session `kind-e2e-1:1` | 20.1 s |
| Live events | 250 events. Redis append to host: p50 3 ms, p95 8 ms, maximum 17 ms | — |
| Steer during `sleep 40` | `symphony.intent_applied` `{kind: steer, delivered: steer}`; `b.txt` = `beta` | 16 ms |
| End of the first attempt | `outcome: continuation`, 28 transcript entries, 29 `export.jsonl` lines, USD 0.12 | 76 s |
| Telemetry | 5 `POST /v1/traces`; rollup with 16 spans, 8 model calls, 7 tool calls, 0 errors | — |
| `refresh` → second attempt | Same Pod and PVC UIDs; the session `.jsonl` went from 31 to 39 lines | 2.6 s |
| Ticket `Done` → terminal cleanup | Pod and PVC deleted | 18 s |
| Nightly sweep | `succeeded: 1` | 3.1 s |

## Kubernetes setup on kind

Run of 2026-11-16 on the same machine: kind v0.33 (kindnet), omp 18.20.2, Amazon Bedrock.
`E2E_TEARDOWN=1 scripts/e2e_kind_setup.sh` stopped with exit code 0 in 483 s, from no cluster
to a deleted cluster: 49 checks, 49 passed. The cluster took 35 s. The images took 34 s to build
and load (cache). The minimal install took 16 s to its rollouts. The certification took 399 s.
[testing.md](testing.md#kubernetes-setup-certification-on-kind) explains the run.

The vendor mocks (Linear, GitHub, Slack, MCP, Seiji) ran as pods in the namespace
`symphony-e2e-mocks`. The attempt and the assistant used real models on Bedrock.

**Environment fact.** On the test account, `global.anthropic.claude-haiku-5-5` returns Bedrock 403
(AWS Marketplace subscription) as of 2026-11-16. Opus returns 200. The default team
configuration puts the assistant, planner and probe roles on Haiku. So the run publishes Opus
for these three roles with `PUT /v1/admin/config` before the assistant step.

### NetworkPolicy

kindnet enforces NetworkPolicy, so no Calico or Cilium was installed. The probes connect with a
3 s timeout.

| Check | Result | Evidence |
|---|---|---|
| A pod with the workspace labels | Pass | Gateway open; GitHub mock open (D3a); Redis, API, Linear mock and MCP mock blocked (timeout) |
| A pod with the daemon labels | Pass | Redis and API open; the gateway blocked (its ingress admits agents only) |
| The gateway pod | Pass | Redis, MCP mock and Seiji mock open (D3b); API and Linear mock blocked |

### Minimal install and setup

The operator made only `symphony-api` (bootstrap token) and `model-provider` (Bedrock). Then
`kubectl apply -k scripts/e2e_kind`. One ticket, `ENG-1` (`symphony`, `investigation`,
`repo:acme/app`, Todo), waited on the Linear mock board from the start.

| Check | Result | Evidence | Time |
|---|---|---|---|
| Fresh install | Pass | The other integration Secrets exist and are empty. `GET /v1/setup`: mode `kubernetes`, not complete | — |
| Idle components | Pass | Daemon: `daemon_setup_incomplete` (`WORKFLOW.md is missing, empty or invalid.`), overview `daemon.setup: incomplete`, no Job. Merger: `merger_not_configured`. Assistant: `assistant_not_configured`. No pod restarted | — |
| `PUT /v1/setup/github-app` | Pass | Verified against the GitHub mock: `repositories: [acme/app]`, `missing_permissions: []` | 0.8 s; 32 s to the rollouts |
| `PUT /v1/setup/repositories` | Pass | `default_repo: acme/app`, no restart | 0.9 s |
| `PUT /v1/workflow` (node runtime, no privileged agents, learning off, 1 CPU and 2 GiB sandbox) | Pass | Applied by the idle daemon: `succeeded` | — |
| `PUT /v1/setup/notifications` (Slack bot) | Pass | The test message reached the Slack mock first | 0.6 s; 38 s to the rollouts |
| Restart order | Pass | `github-app`, `slack`, `linear` and the webhooks step each answered `[symphony-daemon, symphony-merger, symphony-assistant, symphony-api]`. The Kubernetes audit log shows the same patches in the same order for each step | — |
| `PUT /v1/setup/tracker` | Pass | Verified against the Linear mock; 9 states, no warning. The restarted assistant has `LINEAR_API_KEY` and `GITHUB_APP_ID` (names only) | 0.8 s |
| Dispatch after the tracker step | Pass | Overview `daemon.setup: ready`, then `event='dispatch' identifier='ENG-1'` and `job/sym-run-eng-1-…-s1` | 92 s to the Job (the new daemon waits for the old daemon's lease) |
| `PUT /v1/setup/assistant` | Pass | `slack: {connected: true, channels: [C0SYMPHONY]}` | 0.8 s |
| `POST /v1/setup/webhooks` | Pass | Both secrets returned once, URLs `http://symphony-api.symphony.svc:8080/v1/webhooks/…`. A Linear comment and a GitHub issue gave signed deliveries: both `last_delivery_at` set | 0.7 s |
| Setup complete | Pass | `GET /v1/setup`: `complete: true`, all 7 steps `done` | — |
| Secrets patched, not created | Pass | All 8 Secrets kept their UID and creation time. Audit log: 11 Secret writes by `symphony-api`, all `patch` (`github-app`, `linear-api-key`, `symphony-api`, `symphony-mcp`, `symphony-slack`), no `create`, no `delete` | — |
| RBAC | Pass | `kubectl auth can-i` as each ServiceAccount: 23 named grants allowed, 32 other requests refused (for example `create secrets`, `get secret/e2e-decoy`, `patch deployment/symphony-gateway`, `get secret/linear-api-key` as the runner). From the API pod with its own token: create Secret, get `e2e-decoy`, delete `linear-api-key`, patch `symphony-gateway` all 403; get `linear-api-key` 200 | — |

### MCP through the gateway

| Check | Result | Evidence |
|---|---|---|
| Admin view | Pass | `GET /v1/admin/mcp`: `mock` (`implementer`, `assistant`; headers `Authorization`, `X-Acme-Tenant`) and `seiji`; no header value |
| Admin test | Pass | `ok: true`, tools `echo` and `lookup_ticket_context`, protocol `2025-11-25` |
| Role check | Pass | Tokens minted in the assistant pod: `implementer` 200; `planner`, `reviewer` and an unknown token 403 `mcp_forbidden`; each token 403 after revoke |
| Stored header injected | Pass | The mock saw exactly one request from the probe, with `authorization` (its own token, which no agent holds) and `x-acme-tenant` |
| Workspace config | Pass | `/run/symphony/engine/mcp.json` (mode 0600): `mock` and `seiji` at `http://symphony-gateway.symphony.svc.cluster.local:8090/v1/mcp/<name>`, `Bearer mcp_…`; no stored value in the file |
| Implementer call | Pass | The workspace's `tools/call` reached the mock with the stored headers; the report comment on `ENG-1` has the tool's summary; the ticket is `Done` |

### Assistant Deployment

| Check | Result | Evidence | Time |
|---|---|---|---|
| Socket Mode | Pass | `apps.connections.open`, then one live WebSocket on the Slack mock; setup step `running`, `connected: true` | — |
| Envelope ack | Pass | `app_mention` acked | 2 ms |
| Mention answered | Pass | "Working on it…" in the thread, then the same message updated with `PONG-…`; one `assist` session, role `assistant`, asker `octocat` | 0.09 s to "Working on it…"; 7.2 s to the answer |
| MCP as the assistant role | Pass | The answer is `ENG-4242: no linked incidents; owner team ENG.`; the mock saw the `tools/call` with the stored headers | 9.2 s |

### Seiji and the lease cleanup

| Check | Result | Evidence | Time |
|---|---|---|---|
| Preset and admin test | Pass | `preset: seiji` stored as a plain definition (`http_base`, `agent_env`, `cleanup`, `timeout_ms: 3900000`); test `ok`, 6 tools, `me.user: mock-agent` | — |
| Lease held during the attempt | Pass | The implementer called `acquire_device` (`l_…`), `run_actions` and `export_evidence` through the gateway; Redis had `seiji/l_…` in `mcp:session-held` and one `mcp:held` set | 48 s from the lease to the end of the session |
| Cleanup at the end of the attempt | Pass | Runner log: `mcp_cleanup held=1 released=1 failed=0`, at the same millisecond as `runner_finished`; one `release_device`; the lease ended `released`; no `mcp:held` key left | < 1 ms |
| Only the stored token | Pass | 16 Seiji requests, all `auth: valid`, no other bearer | — |
| Attempt | Pass | `outcome: done`, 1 turn, 216,177 tokens | — |

### Regressions found

| Regression | Owner | Fix |
|---|---|---|
| Setup never restarted the merger: the `symphony-merger` Deployment had no `metadata.labels`, so `_mergers()` (`app=symphony,component=merger`) found none. `restarted` was `[symphony-daemon, symphony-assistant, symphony-api]` | `deploy/merger.yaml` (Main) | The Deployment has the labels; `tests/test_deploy.py` checks that each merger Deployment matches the selector |
| A tracker or GitHub App write did not restart the assistant, which reads both Secrets | `setup_secrets.CONSUMERS` (Main) | `linear` and `github-app` include the assistant; `tests/test_deploy.py` checks each Deployment that reads a setup Secret |
| A failed assistant turn left no log line (the Bedrock 403 above was visible only in the session) | Assistant engine (Main) | It logs `assistant_turn_ended` with the status and the error |

### Deviations

The overlay `scripts/e2e_kind` is `deploy/` with these test-only changes. Each one is in
`scripts/e2e_kind/kustomization.yaml` or `scripts/e2e_kind/deviations.yaml`.

| Id | Deviation | Why |
|---|---|---|
| D1 | Image tag `e2e`, loaded with `kind load` | No registry |
| D2 | ConfigMap `e2e-endpoints` (`LINEAR_API_URL`, `GITHUB_API_URL`, `GITHUB_GRAPHQL_URL`, `GITHUB_SERVER_URL`, `SLACK_API_URL`) on the API, daemon, merger, assistant and sweep | The vendors are the in-cluster mocks |
| D3a | NetworkPolicy `e2e-workspace-to-github-mock`: workspaces may reach the GitHub mock on port 4200 | The mock has a private pod IP; github.com is public in production |
| D3b | NetworkPolicy `e2e-gateway-to-mcp-mocks`: the gateway may reach the MCP mock (4600) and the Seiji mock (4780) | The same, for remote MCP servers |
| D4 | `SYMPHONY_MCP_ALLOW_HTTP_HOSTS` on the API and the gateway for the two mock hosts | The mocks speak plain HTTP; production MCP servers need `https://` |

Not deviations: `SYMPHONY_PUBLIC_URL` is the API Service URL (an operator setting), and the
workflow and team configuration changes go through the audited API. The cluster has the
Kubernetes audit log on (Metadata level, no bodies) to record the evidence.

## Merger on a real GitHub repository

Run of 2026-11-16 (00:40–01:20 UTC). The production merger code
(`symphony.entry.merger.build` and `start_loops`) ran against a private test repository with
real GitHub Actions CI and real agents on Amazon Bedrock. The harness is
`scripts/e2e_merger/harness.py`.

### Environment

| Part | Used | Difference from a deployment |
|---|---|---|
| Repository | A private test repository, seeded by `harness.py setup`: a small `calc` library, pytest tests, `.symphony/merger.md` with 2 lanes, `batch_max: 3`, `ci.checks: [test]` | — |
| CI | Workflow `CI`, job `test` (setup-python and pytest, 11–20 s), concurrency group with `train-ci` | — |
| Branch protection | From scenario e: `main` requires the check `test`, also for admins | — |
| GitHub token | A user token with the `repo` scope, given to `build(token=…)` | No GitHub App |
| Git | HTTPS mirror with the token in a credential helper (the same path as the App) | — |
| Agents | omp 18.20.2 on Amazon Bedrock. Reviewer and developer: an Anthropic model (medium). Oracle: `global.moonshotai.kimi-k3.1` (medium). Arbiter: `deepseek.v3.2`. Probe: thinking `low` | Models published in a local team configuration |
| Redis | Local `redis-server` | — |
| Tracker | `RecordingTracker`: tracker writes go to `tracker.jsonl` | Not verified against a tracker API |
| Slack | `notify_post` writes to `notify.jsonl` | `symphony.notify` not used |
| Wake-ups | No webhook; safety poll every 10 s | Production: webhook → `wake(merger:<repo>)` < 5 s |

Some model families were not available on the test account (403). Thus the oracle and the
arbiter ran on other families than the reviewer.

### Results by scenario

| Scenario | Pull requests | Result |
|---|---|---|
| a. Clean pull request → review → hand-off → train merges at the expected head | #1 | Pass: merged at the reviewed head |
| b. Real bug → reviewer and oracle agree → back to draft one time | #2 | Pass: draft and `review-requested`; report with blocking finding A |
| c. Two conflicting pull requests → the second conflicts after the first merges → developer and reviewer resolve → merged | #3, #4 | Pass on the third round (the first two rounds found bugs 2 and 4 below) |
| d. Batch of 3 clean pull requests, CI on the top only | #5, #6, #7 | Pass: three merges, CI only on the top |
| e. Batch of 2 on a protected main | #8, #9 | Fail before the fix: #9 was refused (`Required status check "test" is cancelled`), went the single path and merged 40 s later (bug 7) |
| f. Batch of 3 on a protected main, after the fix | #10, #11, #12 | Pass: three merges in the same batch; #11 and #12 waited for their own required checks |

Extract of the rejection report on #2 (reviewer and oracle agree, `blocking (bug), confirmed`):

> ### A: blocking (bug), confirmed
> **Location:** calc/parity.py:6
> **Claim:** `return n % 2 == 1` is an odd check, not an even check. …
> **Suggested fix:** - [ ] Change calc/parity.py:6 to `return n % 2 == 0`. …

Findings B and C of another lane named the same defect. The oracle gave `refuted` ("Duplicate
of A"), and they stayed as nits. The recorded tracker writes show the ticket moved to `Rework`
with a comment.

Extract of the conflict resolution on #3:

> rebased … onto main …: it conflicted with main in `calc/ops.py`, which an agent resolved
> keeping both sides, and a reviewer checked.

The final `calc/ops.py` on `main` has `add`, `sub`, `neg` and `mul`.

### Timings

`ready` is the pull request creation, or the last `ready_for_review` before the report. Each time
includes up to 10 s of safety poll, because no webhook was used.

| Pull request | Ready → report | CI green → merged | Note |
|---|---|---|---|
| #1 | 61 s after the merger started; the review took 40 s | 18 s after hand-off | The pull requests opened before the merger started; the first probes are in the time |
| #2 | 69 s after the merger started | — | 2 lanes and the oracle |
| #3 | 56 s (first review) | 14 s | Push of the resolution → CI → merge |
| #4 | 52 s | 16 s after hand-off | Left the batch with #3 and merged alone |
| #5–#7 | 36 s, 64 s, 57 s | Train start → merges at +45 s, +63 s, +83 s | The train started after the 3 reviews, to make a batch |
| #8, #9 | 60 s, 60 s | #8: 13 s after the train start; #9: 7 s after its own CI | Before the fix |
| #10–#12 | 35 s, 46 s, 57 s | #10 at once; #11: 10 s after its own CI; #12: 13 s | After the fix |

With no findings, a review (2 lanes) took 10–40 s. With findings, the oracle added about 20 s.

### Bugs found and fixed

Each fix has a regression test.

1. **Probe race** (`review.py`): two workers shared one probe file, so one read the nonce of the
   other. Now each probe has its own directory and nonce.
   (`test_workers_probing_the_same_role_at_once_each_read_their_own_nonce`)
2. **Invalid developer tools** (`agents.py`): omp 18.20.2 has no `ls`, and `find` needs fd, so
   omp stopped before its first frame. Now the list is `read,grep,bash,edit,write`, and the
   error has the stderr of omp. (`test_every_role_asks_omp_only_for_tools_it_has`,
   `test_omp_exiting_at_once_reports_its_own_error`)
3. **A failed probe during a resolution used a try** (`resolve.py`). Now `ProbeFailed` goes to
   the worker backoff. (`test_tools_down_during_a_resolution_back_off_without_spending_its_tries`)
4. **A finished rebase looked incomplete** (`git.py`): untracked test caches changed
   `git status --porcelain`. Now only tracked files count.
   (`test_a_conflict_with_main_is_resolved_by_agents_and_merged`)
5. **Long claims with no heartbeat, and owners that were not unique** (`store.py`, `review.py`,
   `entry/merger.py`). Now claims last 2 minutes with a heartbeat, fix slots 1 minute, and the
   owner is `<role>:<host>:<pid>[:<worker>]`.
   (`test_a_held_claim_is_renewed_and_a_dead_holders_claim_expires`,
   `test_workers_of_two_processes_never_share_a_claim`)
6. **CI cancelled before it existed** (`ci.py`, `train.py`). Now, after a push, the cancel waits
   for the run (up to 6 × 5 s).
   (`test_cancelling_a_just_pushed_head_waits_for_the_run_github_starts`)
7. **Batch on a protected main** (`train.py`). GitHub refused members whose run was cancelled.
   Now each member goes through `merge_member`: if GitHub requires checks, the train restarts the
   head CI, waits, and tries the merge again. Certified in scenario f.
   (`test_a_batch_on_a_protected_main_merges_each_member_once_its_required_checks_pass`)
8. **GraphQL `NOT_FOUND` with no status** (`github.py`). Now it raises
   `GitHubError(status=404)`.
   (`test_rest_github_maps_graphql_not_found_and_merges_on_the_expected_head`)

### Not verified in this run

- **Tracker writes:** `Rework` and `Merging` used the names of `tracker.states`, but against a
  recorder, not a tracker API. The mocked certification covers the Linear path.
- **Slack:** `symphony.notify.post` was not called.
- **Webhooks:** the < 5 s target needs `wake(merger:<repo>)` from the GitHub webhook
  (`test_a_wake_reaches_every_idle_loop_at_once`). This run used a 10 s poll.
- **GitHub App:** the run used a user token. The App uses the same endpoints with the
  permissions in [merger.md](merger.md#state-and-leases).
- **Main watch:** main stayed green, so no ticket was opened. Tests cover the red → ticket →
  green flow.

## Seiji through the gateway

On 2026-11-16, `tests/test_seiji_real.py` ran against a real Seiji built from commit `dd2fd71`, with
`seiji serve` in shared mode on a temporary data directory. Result: 3 passed. The run was on a Mac
with no simulator runtime.

| Check | Result |
|---|---|
| `initialize` through the gateway | Protocol `2025-11-25`, `serverInfo` `seiji 0.1.0`, the server instructions, no `Mcp-Session-Id` |
| `tools/list` | 23 tools |
| omp 18.20.2 (`--mode rpc`, `get_state`) | Lists the 23 `mcp__seiji_*` tools through the gateway |
| `budget_status` | `isError: false` |
| `GET /v1/me` through `…/http` | The user and the quota |
| `POST /v1/apps` through `…/http` | 200 with an `app_id` |
| The admin test | `ok`, 23 tools, `me.user` |
| The session token sent straight to Seiji | 401. Only the stored `sj_` token gets 200 |
| `acquire_device` | `isError: true`, "device failed to start (not billed)". No simulator runtime on this Mac |

Not verified: a booted device, the lease cleanup and the viewer link against a real Seiji. The
mocked scenario 13 covers them ([mocked-certification.md](mocked-certification.md)).

## Latency measurements

| Path | Target | Measured | Source |
|---|---|---|---|
| omp event → viewer (Redis append to host, kind) | < 300 ms | p50 3 ms, p95 6 ms, maximum 15 ms | Kind run above |
| Human steer → intent applied (kind) | < 300 ms | 13 ms | Kind run above |
| omp event → SSE viewer | < 300 ms p95 | Asserted | `tests/test_api_sse.py` |
| Wake → daemon tick | < 300 ms p95 | Asserted | `tests/test_daemon_wake.py` |
| Human message → omp | < 300 ms | Asserted | `tests/test_runner_live.py` |
| `GET /v1/overview` and session meta, with 1000 sessions | < 50 ms p95 | Asserted | `tests/test_api_latency.py` |
| Second attempt start after `refresh` (kind) | — | 2.8 s | Kind run above |
