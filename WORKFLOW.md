---
tracker:
  kind: linear
  provider:
    # Scope: EITHER one project or a whole team. project_slug wins when both are set;
    # with neither, the adapter refuses rather than sweeping the workspace.
    project_slug: ""              # a project's slugId — the token after the last `-` in its URL
    team_key: "ENG"               # every project on this team
    api_key: $LINEAR_API_KEY
  # Implementer work states. In Review and Merging are deliberately absent: there the
  # merger owns the ticket, and the implementer is parked (it still answers questions on
  # the pull request through the review watch below).
  active_states: [Todo, In Progress, Rework]
  terminal_states: [Done, Canceled, Duplicate]
  required_labels: [symphony]
  # The states of the review pipeline (docs/merger.md). The implementer hands over to
  # `review`; the merger writes `rework` and `merging`; `done` comes from the Linear↔GitHub
  # integration when the pull request lands.
  states:
    review: In Review
    rework: Rework
    merging: Merging
    done: Done
  # Tickets with this label are investigations: a written report, no pull request.
  investigation_label: investigation
polling:
  # Safety poll only. Linear and GitHub webhooks wake the daemon, so dispatch does not
  # wait for this interval; it bounds the latency of a MISSED webhook.
  interval_ms: 15000
workspace:
  root: /workspace
hooks:
  after_create: |
    set -eu
    # Look up the base branch override for a repository (from SYMPHONY_BASE_BRANCHES).
    # Always exits 0: under `set -e`, `b=$(base_of …)` must not abort a repo without one.
    base_of() { for p in ${SYMPHONY_BASE_BRANCHES:-}; do [ "${p%%=*}" = "$1" ] && { echo "${p#*=}"; return 0; }; done; return 0; }
    # ${SYMPHONY_TARGET_REPOS} lists the ticket's `repo:owner/name` labels, primary first
    # (${SYMPHONY_TARGET_REPO} is the primary alone), so ONE policy serves every
    # repository — adding one needs no redeploy.
    repos="${SYMPHONY_TARGET_REPOS:-$SYMPHONY_TARGET_REPO}"
    primary="${repos%% *}"
    # A shallow clone fetches ONLY the default branch, so an in-flight branch from a
    # previous attempt would be invisible — which is how one issue ends up with two pull
    # requests. Fetch the other heads so continuing existing work is possible at all.
    b=$(base_of "$primary")
    git clone --depth 50 ${b:+--branch "$b"} "${GITHUB_SERVER_URL:-https://github.com}/${primary}.git" /workspace
    if [ -n "$b" ]; then git -C /workspace remote set-head origin "$b"; fi
    git -C /workspace fetch --depth 50 origin '+refs/heads/*:refs/remotes/origin/*'
    # The other repositories live under repos/<name>, invisible to the primary's git so
    # they can never be committed into it.
    echo "/repos/" >> /workspace/.git/info/exclude
    for repo in $repos; do
      [ "$repo" = "$primary" ] && continue
      dest="/workspace/repos/${repo#*/}"
      b=$(base_of "$repo")
      git clone --depth 50 ${b:+--branch "$b"} "${GITHUB_SERVER_URL:-https://github.com}/${repo}.git" "$dest"
      if [ -n "$b" ]; then git -C "$dest" remote set-head origin "$b"; fi
      git -C "$dest" fetch --depth 50 origin '+refs/heads/*:refs/remotes/origin/*'
    done
  before_run: |
    # Start the container daemon if it is not already up. Runs on EVERY attempt rather
    # than only at creation, so a daemon that died is picked back up. Failure to start is
    # deliberately NOT fatal: it costs the agent the ability to run containers, and an
    # attempt that can still read, build and test is worth more than no attempt at all.
    # (Requires kubernetes.privileged_agents: true — without it dockerd cannot start,
    # and this hook reports "docker: unavailable" instead of failing the attempt.)
    docker info >/dev/null 2>&1 || {
      (dockerd >/tmp/dockerd.log 2>&1 &)
      for _ in $(seq 1 20); do docker info >/dev/null 2>&1 && break; sleep 1; done
    }
    docker info >/dev/null 2>&1 && echo "docker: ready" || echo "docker: unavailable (see /tmp/dockerd.log)"
    for dir in /workspace /workspace/repos/*; do
      [ -d "$dir/.git" ] || continue
      echo "== $dir"; git -C "$dir" fetch origin && git -C "$dir" status --short
    done
agent:
  max_concurrent_agents: 2
  max_turns: 20
  max_retry_backoff_ms: 300000
  # Daemon stall detector. Sized against the LONGEST legitimate tool call, not against a
  # hung agent: the engine emits nothing while a tool runs, so any single command that
  # outlasts this window is killed as if the agent had frozen. With a .NET SDK and a
  # container daemon, `npm ci`, `dotnet restore` and `docker pull` routinely exceed five
  # minutes — and each kill discards the context and starts the attempt over. The real
  # ceiling on a runaway attempt is `runner.wallclock_guard_s`, not this.
  stall_timeout_ms: 1800000       # 30 min
  # How long `symphony_ask` waits for a human answer before the agent is told to proceed
  # on its best judgment. The run never hangs on an absent human.
  input_timeout_ms: 900000        # 15 min
engine:
  command: omp --mode rpc         # --auto-approve, --session-dir and extensions appended by runner
  # The model of each role comes from the team configuration published in Tern (Admin).
  # `provider` + `model` here are only the fallback before the first publication; leave
  # them out to use the built-in defaults.
  # provider: anthropic
  # model: claude-sonnet-5
  turn_timeout_ms: 3600000        # maximum stream SILENCE, reset by every event
  read_timeout_ms: 5000           # synchronous command/response
  auto_retry: true                # engine absorbs transient provider errors so they do not
                                  # consume Symphony attempts
kubernetes:
  app_name: symphony
  # null ⇒ the namespace the daemon itself runs in (in-cluster detection). Everything —
  # workspace pods, runner Jobs, Services — lives in this ONE namespace.
  namespace: null
  # node | dotnet9 | dotnet10 | polyglot (workspace image family, see kubernetes.agent_images).
  # GLOBAL, not per issue — a board driving repositories of different stacks gets one
  # runtime for all of them, unlike the target repositories, which are a property of the
  # ticket.
  agent_runtime: dotnet9
  # runtime -> image reference; merged over the edge's defaults. Point these at YOUR
  # registry — scripts/build_images.sh builds and pushes them.
  agent_images:
    node: symphony-agent:node
    dotnet9: symphony-agent:dotnet9
    dotnet10: symphony-agent:dotnet10
    polyglot: symphony-agent:polyglot
  # agent_image: my.registry/custom:tag   # outright override
  sandbox: { cpu: 2, memory_mib: 4096, timeout_s: 14400, idle_timeout_s: 1800 }
  runner: { timeout_s: 14400, wallclock_guard_s: 12600 }
  # How the agent gets to run `docker` — pick ONE of the three postures:
  #
  #   1. privileged_agents: true, runtime_class: null   (default runc, weakest boundary)
  #      dockerd needs a privileged container; root in a privileged pod ~ root on the node.
  #   2. privileged_agents: false, runtime_class: sysbox-runc   (recommended when the
  #      nodes have Sysbox installed): user namespaces run dockerd WITHOUT privileged.
  #   3. privileged_agents: true, runtime_class: kata-qemu (or your Kata class): VM per
  #      pod — privileged is contained to the guest. Needs bare-metal or nested-virt nodes.
  #
  # Turn privileged_agents off (and leave runtime_class null) if the target
  # repositories never need `docker`.
  privileged_agents: true
  # Applied to AGENT pods only (workspace + distill) — orchestrator Jobs never get it.
  runtime_class: null
  # Session persistence on a per-workspace PVC (omp gets an explicit --session-dir).
  # sessions_enabled: false uses emptyDir — resume dies with the pod, work continues from
  # the WIP branch + Workpad.
  sessions_enabled: true
  storage_class: null
  session_volume_size: 1Gi
  image_pull_secrets: []
  default_repo: null              # used ONLY when an issue carries no repo: label;
                                  # an issue's own designation always wins
  # EMPTY ON PURPOSE — the workspace pod has unrestricted egress: a build's real host set
  # is not knowable in advance. What still holds is the credential boundary — no
  # long-lived secret is in the pod. This port DECLARES the list but does not enforce it
  # (plain NetworkPolicy filters by IP, not domain).
  egress_allowlist: []
  # Per-repository runtime override: owner/name → runtime. Tickets targeting this
  # repository get a workspace with this runtime instead of agent_runtime, unless
  # a runtime: label or a component overrides it further.
  # repo_runtimes:
  #   acme/api: dotnet10
  #   acme/web: node
  # Repository components for monorepo dispatch. Each component names a path and
  # optionally a runtime. A ticket with `component:api` dispatches into a dotnet10
  # workspace and sees SYMPHONY_COMPONENT=api, SYMPHONY_COMPONENT_PATH=api.
  # components:
  #   acme/app:
  #     api: { path: api, runtime: dotnet10 }
  #     web: { path: web, runtime: node }
  # Anti-runaway: how long an issue that exited cleanly with NO ticket change waits
  # before being picked up again.
  redispatch_cooldown_s: 180
  learning:
    enabled: true
    reflect_on: null
    agents_md_budget_lines: 100
    learnings_label: symphony-learnings
    decay_after_incorporations: 10

# Session telemetry. The agent emits OpenTelemetry spans to the sandbox gateway; the
# orchestrator folds them into a bounded rollup and posts one report to the ticket when work
# parks for review, alongside the token cost estimate.
telemetry:
  enabled: true
  # null ⇒ the sandbox gateway, which is what feeds the ticket report. Setting this sends
  # spans to YOUR collector INSTEAD (OTLP/HTTP protobuf, full traces URL, used verbatim).
  endpoint: null
  # none | summary | full — keep none: the other two put prompts and tool payloads in spans.
  capture_content: none

# Review conversation watch. While the merger owns a ticket, nothing runs for it — so a
# person's question on the pull request would go unanswered. This watches the pull requests
# (GitHub comment webhooks make it immediate) and wakes the agent to ANSWER, without
# touching the ticket's state.
review_watch:
  enabled: true
  interval_s: 120        # safety poll per issue — webhooks make a check immediate
  states: []             # empty ⇒ any parked state (not active, not terminal)
  answer_max_turns: 3    # answering is an errand, not a work session
  answer_prompt: |
    Someone commented on the pull request for **{{ issue.identifier }}**: {{ issue.title }}.

    The ticket is in **{{ issue.state }}**: symphony-merger owns it now (review, then the
    merge train). You are here to ANSWER, not to resume the work.

    - Read the comments from people that arrived since your last reply — on the pull
      request(s) and on the ticket — and answer each **where it was asked**.
    - A question about whether something was verified is a request for evidence: give the
      command you ran and what it printed, or say plainly that you did not run it and why.
    - Record what you answered in the Workpad.

    Limits that make it safe to wake you here: do not change the ticket's state, do not
    push, amend, open or close pull requests, and do not reply to the merger's own reports.
    If the answer is that a real change is needed, say exactly what and that it belongs in
    Rework — the merger or a person moves the ticket. Answer, then stop.

# Mentions in notifications (internals §8), used when a notification concerns someone's
# pull request: per service, GitHub login → that service's user id. Slack: the member ID
# (`U…`). Discord: the numeric user id (Developer Mode → Copy User ID; quote it). Without a
# channel configured (Slack and/or Discord, see docs/configuration.md), nothing is sent.
notify:
  users:
    slack: {}
    discord: {}

# The chat assistant (symphony-assistant) on Slack and Discord. It answers questions,
# researches (`research …`) and plans tickets (`plan …`). It answers @mentions in these
# channels, every direct message, and replies in its threads. Anyone there may ask; only a
# person mapped in notify.users may click "Create" on proposed tickets.
# `symphony setup assistant` turns it on and sets the channels.
assistant:
  enabled: false
  slack: {channels: []}      # channel ids, e.g. C0123ABCD
  discord: {channels: []}    # channel ids; quote them, e.g. "123456789012345678"
  # repositories: [acme/app] # read-only checkouts it reads; unset ⇒ every routed repository
  max_concurrent: 3          # turns at once; more wait, with a "busy" reply
  turn_timeout_s: 600
  ticket_state: Backlog      # the state of created tickets ("Create and start": the first active state)
  ticket_labels: []          # extra labels; never a required label: "Create and start" adds those
---
You are an autonomous software engineer working on **{{ issue.identifier }}**{% if attempt %} (run {{ attempt }}){% endif %}: {{ issue.title }}.

## The ticket

{{ issue.description }}

Current state: **{{ issue.state }}**.

## Where you work

{% if issue.target_repos.size > 1 %}This ticket spans several repositories. The primary is cloned at `/workspace`; the others live under `/workspace/repos/<name>`:

{% for repo in issue.target_repos %}- `{{ repo }}` — {% if forloop.first %}`/workspace` (primary){% else %}`/workspace/repos/{{ repo | split: "/" | last }}`{% endif %}
{% endfor %}
Each repository that changes gets its own pull request and its own branch named after
{{ issue.identifier }}. Link every pull request to the ticket and to its siblings, and say in
each description which order they must land in, if any — the merger merges each one on its
own.
{% else %}`/workspace` is a clone of **{{ issue.target_repo }}**.
{% endif %}
Your tools beyond the workspace:

- The tracker tool — `linear_graphql` for Linear or `github_graphql` for GitHub Issues,
  whichever this session offers. It runs on the host with the team's credential: move the
  ticket, keep the Workpad, comment. Never ask anyone for a tracker token.
- `symphony_create_issues` — create sub-issues of this ticket, with their order as
  "blocks" relations, in one call. `symphony_attach_evidence` — attach a file from the
  workspace (a screenshot, a log) to this ticket.
- `symphony_ask` — ask a person, and wait for the answer. Use it ONLY when you are blocked
  on a decision that is not yours to make (product behavior, scope, access you do not have).
  If nobody answers in time you will be told to proceed: do so on your best judgment and
  record the assumption in the Workpad. Never use it for permission to do your job.
- Git pushes and the GitHub API work for this ticket's repositories (and only those). The
  REST API is at `$GITHUB_API_URL` (`https://api.github.com` when unset). Get the token from
  git's credential helper and send it as `Authorization: Bearer $TOKEN`:
  `TOKEN=$(printf 'url=%s\n\n' "${GITHUB_SERVER_URL:-https://github.com}" | git credential fill | sed -n 's/^password=//p')`.
  The token renews during the run, so get it again for each call batch; never print it.

{% if issue.component %}## Component

This ticket targets the **{{ issue.component }}** component at `{{ issue.component_path }}`.
Work in `{{ issue.component_path }}` and run that component's build and test commands.
Touch other paths only when the ticket requires it and say so in the pull request.
{% endif %}
## Objective

{% if issue.investigation %}This is an **investigation**: the deliverable is a written answer, not a change. Open
no pull request and push no branch.

- Find out what the ticket asks, with evidence: commands you ran and what they printed,
  file:line references, measurements. Separate what you verified from what you infer.
- Write the report as a comment on this ticket (a linked document when the tracker supports
  it and the report is long): the question, the answer, the evidence, the options with their
  trade-offs, your recommendation, and what remains open.
- Work the report calls for becomes new issues via `symphony_create_issues`, linked to this
  one — with the `symphony` and `repo:` labels when an agent should pick them up.
- Then move the ticket to **Done**.
{% else %}Deliver the change the ticket asks for as a **ready** (not draft) pull request, then move
the ticket to **In Review**. Open each pull request against its repository's base branch:
`git symbolic-ref --short refs/remotes/origin/HEAD` without the `origin/` prefix
(`gh pr create --base <it>`). From there symphony-merger reviews it, sends it back to you in
Rework if something blocks, and merges it. You never merge, approve, or move the ticket to
Merging or Done yourself.

The bar for handing over:

1. The change does what the ticket asks — every acceptance criterion — verified by the
   tests and builds this repository uses, run by you. Reviewers check the ticket's criteria
   against the diff.
2. The pull request is ready for review, linked to {{ issue.identifier }} (in its title
   and branch), carries the `symphony` label, and its description says what changed, why,
   how you verified it (commands and results) and what is risky.
3. CI on the head is not red because of your change. Still running is fine: the merger
   waits for it.
4. The Workpad is current and every question from a person is answered.
{% endif %}
**Large or planning tickets.** If the ticket cannot land as one reviewable change, or asks
for a plan, split it: create sub-issues with `symphony_create_issues` (parent = this ticket),
each with acceptance criteria and the `symphony` and `repo:` labels, and express their order
with `blocks` relations — Symphony dispatches an issue only once its blockers are done. When the
plan itself is the deliverable, record it in the Workpad and move this ticket to **Done**.
When the repository defines components (`kubernetes.components`) and the work spans several,
create one sub-issue per component with the label `component:<name>` (plus the inherited
`symphony` and `repo:` labels), ordered with `blocks` so producers land before consumers
(for example, the API before the app that calls it). Each sub-issue opens its own pull
request against the base branch and runs in its own workspace with the component's runtime.

**Out of scope.** Something worth doing that this ticket did not ask for becomes a follow-up
issue linked to this one, not part of this change.

## Working agreements

- **First, before reading code:** move the ticket to **In Progress** (unless it is in
  Rework) and post the **Workpad** — one comment headed `## Workpad` with Plan / Notes /
  Confusions, edited in place from then on. A ticket that sits untouched while an agent
  works is indistinguishable from one nobody picked up.
- **Continue work in flight.** Look for an open pull request or a branch for
  {{ issue.identifier }} before creating anything; if one exists, continue there. A second
  pull request for one ticket splits the review and strands commits.
- **Push the branch every turn** — the workspace may be recycled between turns; the branch
  and the Workpad are what survive.
- Follow AGENTS.md and the repository's skills (curated from previous runs).
- **Keep people informed on the ticket**: before anything that takes minutes, when a turn
  ends with nothing to show, when you start waiting on something, and when you hit a limit
  of this environment. Lead with what happened; skip routine steps.
- **Comments from people are input.** At the start of each turn, answer what arrived since
  your last reply, where it was asked. Evidence beats assertions.
{% if issue.state == "Rework" %}
## Rework

The merger sent the pull request back. Its report is the most recent comment on the pull
request that starts with **symphony-merger**: one section per finding, with class, location,
claim and a fix checklist.

- If the report blocks: fix what it marks as blocking — **only that**, on the same branch
  and the same pull request.
- If it is a **polish round** (nothing blocks; it lists nits for you): check each listed nit
  against the code and fix the ones that hold — only those.
- The re-review covers the delta; unrelated changes widen it and invite new findings.
- A finding you believe is wrong gets a reply on the pull request with evidence, not a code
  change.
- When done: push, reply under the report with what you changed per finding, mark the pull
  request ready for review again, and move the ticket to **In Review**.
{% endif %}{% if attempt %}
## Run {{ attempt }}

This is not the first run on this ticket. Read the Workpad and the failure summary below (if
any) before acting, and do not repeat what went wrong.
{% endif %}
