# Central skill sources

A skill source is a Git repository directory that holds one sub-directory per skill, each
containing a `SKILL.md` file. When configured, the orchestrator fetches the skills and
stages them into every agent workspace before the engine starts.

## Configuration

Add a `skills:` section to `WORKFLOW.md`:

```yaml
skills:
  sources:
    - repo: acme/agent-skills       # a repository the GitHub App can reach
      path: skills                   # directory with skill sub-dirs; default "skills"
      ref: main                      # optional; default: the repo's default branch
      roles: [implementer, reviewer] # optional; default: every role
      repos: [acme/app]             # optional; default: every repository (GLOBAL)
```

Or use the CLI:

```
symphony setup skills --repo acme/agent-skills --path skills --ref main \
    --role implementer --role reviewer --for-repo acme/app
```

## Scopes

- **Global source**: no `repos:` field — the skills apply to every repository.
- **Repository-specific source**: `repos: [acme/app, acme/lib]` — only agents working on
  those repositories get the skills.

## Roles

The `roles:` field restricts which agent roles receive the skills. When omitted, every
role gets them: implementer, reviewer, oracle, arbiter, developer, reflector, probe,
assistant, researcher, and planner.

The assistant runs multiple roles per session (assistant, researcher, planner). A source
with `roles: [planner]` reaches only plan-mode sessions. For the assistant, global sources
and sources whose `repos:` intersect the assistant's reachable repositories apply.

## Precedence

1. The target repository's own `.omp/skills/` and `.agents/skills/` always win.
2. Among central sources, **repository-specific** sources beat **global** ones.
3. Within the same scope, the **first** source in the list wins (for name collisions).
4. A skill name that already exists is not overwritten by a later source.

## Limits

| Limit                     | Value    |
|---------------------------|----------|
| File size per skill       | 256 KiB  |
| Total size per source     | 5 MiB    |
| Skills per source         | 100      |
| Sources per configuration | 50       |

## Caching

The ref → SHA resolution is cached in Redis (TTL 10 minutes). Once the SHA is known and
the tarball has been extracted, the extracted skills are cached by `(repo, sha)` for one
hour. This means a session start does not re-download an unchanged source.

## Fetching

Each source is fetched with a GitHub App installation token scoped to that source
repository with read-only permissions (`contents: read`, `metadata: read`). The
orchestrator downloads the tarball at the resolved SHA and extracts only
`<path>/*/SKILL.md` entries.

A source that fails to fetch is logged and skipped — it never fails a run.

## Security

A skill is an instruction the agent follows. Keep skill sources in repositories with
required code review so that changes are audited before they reach agents. The GitHub App
must be installed on each source repository.

## Visibility

`GET /v1/admin/skills` returns the configured sources, their resolved SHA, skill names,
scope (global or repos), roles, and any error. The Admin › Skills tab in the Tern plugin
shows the same information.

The session metadata includes a `skills` field listing which sources and skill names were
staged for each run.
