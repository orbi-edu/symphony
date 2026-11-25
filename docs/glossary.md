# Glossary

All Symphony documents use these terms with these meanings only.

| Term | Meaning |
|---|---|
| **ACP** | Agent Client Protocol. Tern uses it to show an agent conversation as a native chat. `symphony acp` is the ACP bridge between Tern and the Symphony API. |
| **Admission** | The daemon's permission to start new attempts. `pause` closes admission. `resume` opens it again. Attempts that run continue. |
| **Agent** | One `omp` process that works on one task: an implementer in a workspace, a reviewer, oracle, arbiter, developer, reflector or probe, or an assistant, researcher or planner in chat. |
| **Answer run** | A short attempt that only answers a person's comment on a pull request while the ticket is parked. It does not change the ticket state and does not consume an attempt. |
| **API key** | A credential for a model provider (for example Anthropic, OpenAI or Amazon Bedrock). Symphony supports provider API keys, unauthenticated custom endpoints (`auth: none`), and host cloud credentials (for example Bedrock through IAM). It has no provider OAuth or subscription login. |
| **API token** | A Symphony credential for a person or a client, in the format `sym_<8 hex>_<43 characters>`. Each API token has one role: `viewer`, `operator` or `admin`. |
| **Arbiter** | A merger role. It decides a blocking finding after the one rejection of a pull request. |
| **Assistant** | 1. The chat bot (`symphony-assistant`) on Slack and Discord. It answers questions, researches and proposes tickets with read-only agents ([assistant.md](assistant.md)). 2. The agent role for a plain question to that bot. |
| **Attempt** | One run of the implementer for one ticket. A failure consumes an attempt and starts a backoff. A continuation keeps the same attempt number. |
| **Bootstrap token** | The first admin API token, from `SYMPHONY_BOOTSTRAP_TOKEN` or, in local mode, made and printed at the first start. |
| **Consumer** | A component that reads the team configuration: the daemon, a runner, a reflector, the merger review loop, the merger train loop, or a person's `symphony omp sync`. |
| **Continuation** | A clean end of an attempt before the work is complete (turn cap, wall-clock guard, or interrupt with no human message). The daemon starts the next run of the same attempt. |
| **Cooldown** | The time (`kubernetes.redispatch_cooldown_s`) that a continuation waits when the ticket did not change since the last clean exit. |
| **Daemon** | The orchestration process (`symphony-daemon`). It reads the tracker, starts runners, applies retries and cleans workspaces. |
| **Dispatch** | The start of a runner for a ticket. Each dispatch has a new dispatch sequence number. |
| **Dispatch sequence** (`dispatch_seq`) | A number that increases with each dispatch of a ticket. The fencing checks it on every write. |
| **Dispatch token** | A Redis key that one dispatch makes with put-if-absent. Only the owner of the token can write the ticket state. |
| **Engine** | The agent program, Oh My Pi (`omp`), in RPC mode (`omp --mode rpc`). The version is pinned in `src/symphony/edge.py` (`OMP_VERSION`). |
| **Fencing** | The rule that only the current dispatch can write. A runner from an old dispatch stops with no effect. |
| **Finding** | One problem that a merger reviewer reports on a pull request. |
| **Gateway** | The sandbox gateway (`symphony-gateway`). It is the only Symphony service that a workspace can reach. It accepts telemetry and proxies agents' MCP requests to remote MCP servers. |
| **Generation** | The version number of the published team configuration. Generation 0 is the built-in default. |
| **Hold** | A person's claim on a ticket. A held ticket gets no dispatch, retry or continuation. `release` ends the hold. |
| **Host tool** | A tool that the engine calls but the runner executes on the trusted side, for example the tracker tool, `symphony_create_issues`, `symphony_attach_evidence`, `symphony_ask` and `symphony_blocked`. |
| **Implementer** | The role of the agent that does the ticket's work and opens the pull request. |
| **Intent** | A durable request from a person to a running session: `steer`, `follow_up`, `interrupt` or `answer`. |
| **Investigation** | A ticket with the investigation label. The deliverable is a written report. The agent gets a read-only GitHub token. |
| **Learnings** | Lessons that the reflector writes into `AGENTS.md` and `.agents/skills/` through a pull request that a person must merge. |
| **Local mode** | One process (`symphony.entry.local`) that does the work of the API, gateway, daemon and runners on one machine, without Kubernetes or containers. |
| **MCP server** | 1. A server that gives agents extra tools over the Model Context Protocol. An admin picks them per agent role in Admin › MCP. Remote servers go through the gateway, which adds the stored headers ([mcp.md](mcp.md)). 2. Symphony's own MCP server for people, at `/mcp`. |
| **Merge train** | The merger loop that rebases, tests and merges reviewed pull requests one at a time or in batches. |
| **Merger** | The service (`symphony-merger`) that reviews and merges pull requests with AI agents only. One merger Deployment serves one repository. |
| **Notification channel** | A place where Symphony sends notifications: Slack, Discord, or both. Each event goes to every configured channel. |
| **Oracle** | A merger role. It checks the findings of the reviewers on a different model. |
| **Parked** | The status of a tracked ticket that is not in an active state (for example In Review or Merging). It holds no concurrency slot. The workspace stays. |
| **Planner** | The assistant role for a `plan …` message. It proposes tickets and never creates them. |
| **Probe** | A merger role. It runs a short check of each role's model and tools before a review. |
| **Project board** | With `tracker.kind: github`, the GitHub Projects (v2) project that holds the tickets. |
| **Proposal** | A set of tickets that the planner proposes in chat, with "Create", "Create and start" and "Dismiss" buttons. Only a person mapped in `notify.users` can confirm it. It expires after 7 days. |
| **Provider** | A source of models: a built-in provider of `omp`, or a custom provider that an admin defines. |
| **Public URL** | The address where people and vendors reach the API (`SYMPHONY_PUBLIC_URL`). Setup builds the webhook URLs from it. When it is unset, setup uses the address of the request. |
| **Reflector** | The role and the Job that runs a retrospective on a terminal ticket and proposes learnings. |
| **Researcher** | The assistant role for a `research …` message. It digs into a question with read-only tools. |
| **Retrospective** | The analysis of a finished ticket: diff, review threads, CI results, Workpad and attempts. |
| **Review watch** | The daemon feature that starts an answer run when a person comments on the pull request of a parked ticket. |
| **Role** | 1. The level of an API token: `viewer`, `operator`, `admin`. 2. The job of an agent: `implementer`, `reviewer`, `oracle`, `arbiter`, `developer`, `reflector`, `probe`, `assistant`, `researcher`, `planner`. The text always says "token role" or "agent role" when both can apply. |
| **Runner** | The process for one attempt (a Kubernetes Job, or a thread in local mode). It drives the engine and writes the attempt's state. |
| **Session** | The live record of one agent conversation: metadata, event stream, transcript and intents. An attempt has one session. |
| **Setup** | The first-run checklist and wizard (`GET /v1/setup`, `symphony setup`, **Setup** in Tern). It has seven steps: tracker, GitHub App, repositories, models, notifications, assistant and webhooks. The first four are required. Each value is verified before it is saved. |
| **Status field** | With `tracker.kind: github`, the single-select field of the project board whose options are the ticket states (`tracker.provider.status_field`, default `Status`). |
| **Team configuration** | The published document that gives each agent role a provider, a model and a thinking level, plus shared `omp` settings. |
| **Tern** | The terminal application that people use to operate Symphony through a plugin, the ACP chat and MCP. |
| **Ticket** | One issue in the tracker: a Linear issue, an issue on a GitHub project board, or a ticket of the memory tracker for tests and local mode. |
| **Tracker** | The source of tickets: `linear`, `github` or `memory`. |
| **Tracker tool** | The host tool that gives the agent the tracker's API: `linear_graphql` for Linear, `github_graphql` for GitHub. The runner executes it with the trusted-side credential. |
| **Wake** | A signal that makes the daemon or the merger act now instead of at the next poll. Webhooks and commands send wakes. |
| **Workpad** | The ticket comment headed `## Workpad` that the agent keeps current with its plan, notes and open questions. |
| **Workspace** | The isolated place where an agent works on one ticket: a pod with a session volume in Kubernetes, or a directory in local mode. |
| **`WORKFLOW.md`** | The file that holds the policy (YAML front matter) and the agent prompt (Liquid body). See [configuration.md](configuration.md). |
