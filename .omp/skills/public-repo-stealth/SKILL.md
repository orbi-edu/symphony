---
name: public-repo-stealth
description: MANDATORY for every change to this repository. It is public — code, tests, docs, commit messages, branch names, pull request text and comments, CI logs and release notes are published. Keeps every artifact generic so nothing reveals who runs it, their tickets, customers, accounts, infrastructure or people.
---

# Public repository: stealth mode

This repository is public. Everything you write here is published and permanent: code,
comments, tests, fixtures, docs, CHANGELOG, commit messages, branch names, pull request
titles, descriptions and comments, review comments, CI logs, release notes and image
metadata. Write as if an outsider with no context reads all of it.

These rules are not advice. A change that breaks one is not done: fix it before you push,
and if something already went out, say so at once (see "If something leaked").

## 1. Never write

| Category | Leave out |
|---|---|
| The operator | the name of the company or team that runs Symphony, its products and projects, its customers |
| Tickets | ticket identifiers (`ABC-123`), ticket titles, URLs and text, the tracker workspace's name, the team key |
| Source control | names of other repositories of the operator, their branches, PR or issue links outside this repository |
| Infrastructure | account numbers (any 12-digit number), ARNs, role, bucket, cluster or namespace names, hostnames, domains, IPs, registry hosts, region choices tied to a deployment |
| People and chat | names, logins, e-mails, Slack or Discord channel and user ids |
| Operations | incidents, real metrics, costs, logs or screenshots of a deployment |
| Secrets | tokens, keys, passwords — never, anywhere |

Copying is leaking too: text from a ticket, a log, a chat or a deployment carries its names.

## 2. Write it generically

- Neutral examples only: `acme/app`, `ENG-12`, `alice`, `example.com`, `123456789012`,
  `registry.example.com`.
- Describe the class of problem, not the deployment where it happened: "a runner Job whose
  pod was evicted", not who, where or when.
- Deployment-specific values are configuration the code reads at run time (WORKFLOW.md,
  environment, secrets), never literals or defaults in this repository.

## 3. Branches, commits and pull requests

- Symphony's agents: name the branch `symphony/<ticket id>` — the opaque id the prompt
  gives you, never the ticket identifier. Symphony links the pull request to the ticket
  through it. Link the pull request from the ticket (its Workpad or a comment), never the
  ticket from the pull request: no `Fixes ABC-123`, no `Refs ABC-123`, no ticket URL.
- People: a short descriptive branch (`fix/runner-evictions`), never a ticket identifier.
- Titles and descriptions say what changed, why, and how it was verified. Nothing else.
- Commit messages follow the same rules; a trailer that names a ticket is a leak.

## 4. Check before every push, PR edit, comment or release

Build the denylist from what you know about the deployment you work for: the operator's
name, the tracker's team key, the identifier of your ticket, its repositories, domains,
accounts and people. It lives in your head or in an environment variable, never in this
repository. Then:

```sh
DENY='operator-name|TEAMKEY-[0-9]+|internal\.example|[0-9]{12}'   # fill in the real terms
git diff origin/main...HEAD | grep -inE "$DENY"
git log origin/main..HEAD --format='%B' | grep -inE "$DENY"
git branch --show-current | grep -iE "$DENY"
```

Scan the pull request title and description, and any comment, before you send them.
Review hits by hand: a substring can be harmless (`forbid`), a placeholder can look real.
Fix every real hit — amend commits before pushing; once pushed, rewrite the branch before
the merge.

The `stealth` CI check runs the operator's denylist (a repository secret) over the pull
request's title, description, branch, commit messages and added lines. A red `stealth`
check blocks the merge; it prints where it found a hit, never the text.

## 5. If something leaked

Say it immediately, in the ticket (private), with where it is: the commit, the PR title,
the branch. Edit what can be edited (titles, descriptions, comments), rewrite unmerged
branches, and leave history rewrites of `main` to a maintainer. Never hide it.
