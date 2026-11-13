# Contributing to Symphony

Thank you for your help. This file tells you how to make a change that the maintainers can
accept.

## Before you start

1. Read [AGENTS.md](AGENTS.md). Its rules apply to people and to AI agents.
2. For a large change, open an issue first. Describe the problem and the change that you
   propose. Wait for an answer before you write the code.
3. For a security problem, do not open an issue. Follow [SECURITY.md](SECURITY.md).

## Set up

You need Python 3.12 or later, [uv](https://docs.astral.sh/uv/), Redis 7 or later and git.
The Tern tests also need the `tern` binary; they skip without it.

```sh
git clone <repository URL> symphony
cd symphony
uv sync --extra dev
uv run pytest
uv run ruff check .
```

[docs/testing.md](docs/testing.md) describes the test suites, the vendor mocks and the
end-to-end runs.

## Make a change

1. Make a branch from `main`.
2. Make the smallest change that solves the problem.
3. Add or change tests. A bug fix needs a test that fails before the fix.
4. Update the documentation that your change makes wrong. The contract between components is
   [docs/internals.md](docs/internals.md).
5. Run `uv run pytest` and `uv run ruff check .`. Both must pass. If you changed a document,
   run `python3 scripts/check_links.py` too.

## Open a pull request

- Use one pull request for one purpose.
- Use a title in the imperative mood with a type prefix, for example
  `fix: keep a hold after a daemon restart`.
- In the description, say what changed, why, how you verified it, and what is risky.
- Link the issue.

A maintainer reviews the pull request. Answer the comments with a change or with evidence.

## Continuous integration

`.github/workflows/ci.yml` runs on each push to `main` and on each pull request. It runs
`uv run ruff check .`, `uv run pytest`, `bash -n` on each shell script, `kubectl kustomize`
on `deploy/`, `deploy/cloudflared/` and `scripts/e2e/`, and `scripts/check_links.py`. CI has
no Tern, no real `omp`, no Docker, no kind and no model credential, so those tests skip. Run
them on your machine when your change touches them ([docs/testing.md](docs/testing.md)).

## Make a release

Maintainers make releases. Symphony uses [Semantic Versioning](https://semver.org/). The one
version is `version` in `pyproject.toml`; `symphony --version` and the MCP and ACP server
information read it.

1. In `CHANGELOG.md`, move the `Unreleased` entries under a new heading
   `## [<version>] - <YYYY-MM-DD>`.
2. Set `version` in `pyproject.toml`, then run `uv lock` to update `uv.lock`.
3. Check the release notes: `.github/scripts/release_notes.sh <version>`.
4. Commit `chore: release <version>` and merge it to `main`. CI must pass.
5. Tag the merged commit and push the tag:
   ```sh
   git tag -a v<version> -m "Symphony <version>"
   git push origin v<version>
   ```
6. The tag starts two workflows. Both fail when the tag does not match `pyproject.toml`.
   - `images.yml` builds every image of `scripts/build_images.sh` for `linux/amd64` and
     `linux/arm64`, and pushes it to `ghcr.io/<owner>/`, with an SBOM and provenance.
   - `release.yml` makes the GitHub Release with the notes of that version.
7. After the first release, make the `symphony-orchestrator` and `symphony-agent` packages
   public in the package settings on GitHub.

## License

You agree that your contribution is licensed under the [MIT License](LICENSE).
