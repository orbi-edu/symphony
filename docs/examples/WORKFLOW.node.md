---
# Example policy for a Node.js service (npm, no Docker). Copy it to the root of your
# deployment as WORKFLOW.md and change the tracker scope and the build steps.
tracker:
  kind: linear
  provider:
    team_key: "ENG"
    api_key: $LINEAR_API_KEY
  active_states: [Todo, In Progress, Rework]
  terminal_states: [Done, Canceled, Duplicate]
  required_labels: [symphony]
  states: { review: In Review, rework: Rework, merging: Merging, done: Done }
  investigation_label: investigation
polling:
  interval_ms: 15000              # safety poll; webhooks wake the daemon
workspace:
  root: /workspace
hooks:
  after_create: |
    set -eu
    # Always exits 0: under `set -e`, `b=$(base_of …)` must not abort a repo without one.
    base_of() { for p in ${SYMPHONY_BASE_BRANCHES:-}; do [ "${p%%=*}" = "$1" ] && { echo "${p#*=}"; return 0; }; done; return 0; }
    server="${GITHUB_SERVER_URL:-https://github.com}"
    repos="${SYMPHONY_TARGET_REPOS:-$SYMPHONY_TARGET_REPO}"
    primary="${repos%% *}"
    b=$(base_of "$primary")
    git clone --depth 50 ${b:+--branch "$b"} "${server}/${primary}.git" /workspace
    if [ -n "$b" ]; then git -C /workspace remote set-head origin "$b"; fi
    git -C /workspace fetch --depth 50 origin '+refs/heads/*:refs/remotes/origin/*'
    echo "/repos/" >> /workspace/.git/info/exclude
    for repo in $repos; do
      [ "$repo" = "$primary" ] && continue
      b=$(base_of "$repo")
      git clone --depth 50 ${b:+--branch "$b"} "${server}/${repo}.git" "/workspace/repos/${repo#*/}"
      if [ -n "$b" ]; then git -C "/workspace/repos/${repo#*/}" remote set-head origin "$b"; fi
    done
    cd /workspace && npm ci
  before_run: |
    cd /workspace && git fetch origin
agent:
  max_concurrent_agents: 3
  max_turns: 20
  max_retry_backoff_ms: 300000
  stall_timeout_ms: 1800000
  input_timeout_ms: 900000
engine:
  command: omp --mode rpc
  # Models come from the team configuration that an admin publishes in Tern (Admin).
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  auto_retry: true
kubernetes:
  app_name: symphony
  agent_runtime: node             # Node.js 22 and git
  sandbox: { cpu: 2, memory_mib: 4096, timeout_s: 14400, idle_timeout_s: 1800 }
  runner: { timeout_s: 14400, wallclock_guard_s: 12600 }
  privileged_agents: false
  egress_allowlist: []
  redispatch_cooldown_s: 900
  learning:
    enabled: true
    reflect_on: null
    agents_md_budget_lines: 100
    learnings_label: symphony-learnings
    decay_after_incorporations: 10
notify:
  users: {}
---
You are an autonomous software engineer working on issue **{{ issue.identifier }}**{% if attempt %} (run {{ attempt }}){% endif %}: {{ issue.title }}.

## The ticket

{{ issue.description }}

## Build and test (this repository)

- Run the tests: `npm test`. Run the linter: `npm run lint`.
- Do not start the service. The tests are the verification loop.

{% if issue.component %}## Component

This ticket targets the **{{ issue.component }}** component at `{{ issue.component_path }}`.
Work in `{{ issue.component_path }}` and run that component's build and test commands.
Touch other paths only when the ticket requires it and say so in the pull request.
{% endif %}

## Objective, working agreements and review protocol

Copy the sections "Where you work", "Objective", "Working agreements", "Rework" and "Run"
from the root WORKFLOW.md into this file when you deploy it. Those sections hand the pull
request over to symphony-merger and define the investigation and Rework rules.
