# Security policy

## Supported versions

Only the latest release on the `main` branch gets security fixes.

## Report a vulnerability

Do not open a public issue for a security problem.

1. On the repository page, open **Security**, then **Report a vulnerability**. This opens a
   private report that only the maintainers can see.
2. Describe the problem, the affected component and the steps to reproduce it.
3. Do not include real credentials. If a credential is part of the problem, rotate it first.

The maintainers answer each report. They tell you if they accept it, and they agree a
disclosure date with you.

## Scope

These problems are in scope:

- A credential that leaves the host side or appears in a log, Redis, an API response or a
  command line. Host-side credentials are the Linear key, the GitHub App key, Slack tokens and
  Symphony API tokens.
- A workspace that gets a credential other than its model credential and its ticket-scoped
  GitHub token.
- An agent that writes to a repository or a ticket outside the scope of its ticket.
- A runner of an old dispatch that can still write state.
- An API route that accepts a request from a token with a lower role than the route needs.
- Webhook signature checks that a forged request can pass.

The security model is in [docs/security.md](docs/security.md).
