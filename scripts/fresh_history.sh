#!/usr/bin/env bash
# Make a LOCAL orphan branch `release` whose one commit holds the tree of HEAD, for a
# public history that starts at the release. Commit or stash your work first: only HEAD's
# tree goes in, never uncommitted changes.
#
#   scripts/fresh_history.sh
#
# The script checks that the tree has no `internal/` path and no path that .gitignore
# ignores, prints the size of the history before and after, and prints the commands that
# make the branch the new `main`. It never runs them: it never changes `main`, the index,
# the working tree or a remote. Delete the branch with `git branch -D release`.
set -euo pipefail
cd "$(dirname "$0")/.."

BRANCH=release

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
[ -n "$version" ] || { echo "could not read the version from pyproject.toml" >&2; exit 1; }
message="Symphony $version"

name=$(git config user.name || true)
email=$(git config user.email || true)
if [ -z "$name" ] || [ -z "$email" ]; then
  echo "set git config user.name and user.email: they are the author of the commit" >&2
  exit 1
fi
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "branch $BRANCH exists; delete it first: git branch -D $BRANCH" >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "note: the working tree has uncommitted changes; they are not in the commit"
fi

tree=$(git rev-parse 'HEAD^{tree}')

internal=$(git ls-tree -r --name-only "$tree" | grep -E '^internal(/|$)' || true)
if [ -n "$internal" ]; then
  echo "HEAD has internal/ paths; remove them from the tree first:" >&2
  printf '%s\n' "$internal" | sed 's/^/  /' >&2
  exit 1
fi
ignored=$(git ls-tree -r -z --name-only "$tree" | git check-ignore --no-index -z --stdin |
  tr '\0' '\n' || true)
if [ -n "$ignored" ]; then
  echo "HEAD has paths that .gitignore ignores; remove them from the tree first:" >&2
  printf '%s\n' "$ignored" | sed 's/^/  /' >&2
  exit 1
fi

commit=$(git commit-tree "$tree" -m "$message")
git branch "$BRANCH" "$commit"

# The size of the pack that a clone of a revision downloads (every object it reaches).
pack_bytes() {
  git rev-list --objects "$1" | git pack-objects --stdout -q | wc -c | tr -d ' '
}
mib() { awk -v b="$1" 'BEGIN { printf "%.1f MiB", b / 1048576 }'; }

echo "measuring the clone size of HEAD and of ${BRANCH}..."
before=$(pack_bytes HEAD)
after=$(pack_bytes "$BRANCH")
commits=$(git rev-list --count HEAD)
files=$(git ls-tree -r --name-only "$tree" | wc -l | tr -d ' ')

echo "made branch $BRANCH: commit $(git rev-parse --short "$commit") \"$message\", $files files"
echo "author: $name <$email>"
echo "clone of HEAD:    $commits commits, $(mib "$before")"
echo "clone of $BRANCH: 1 commit, $(mib "$after")"
echo "difference:       $(mib $((before - after))) smaller"

remote=origin
git remote | grep -qx origin || remote=$(git remote | head -n 1)
echo
if [ -z "$remote" ]; then
  echo "There is no remote. Add one, then push $BRANCH as its main."
  exit 0
fi
lease="--force-with-lease=main"
if old=$(git rev-parse --verify --quiet "refs/remotes/$remote/main"); then
  lease="--force-with-lease=main:$old"
fi
echo "To make $BRANCH the new main of $remote, run these yourself (this script runs none):"
echo
echo "  git branch backup/main-before-$version main"
echo "  git push $lease $remote $BRANCH:main"
echo "  git tag -a v$version -m \"$message\" $BRANCH"
echo "  git push $remote v$version"
echo "  git checkout -B main $BRANCH && git branch --set-upstream-to=$remote/main main"
echo
echo "Branch protection on main can refuse the forced push. Allow it for this push only."
echo "Old tags and forks keep the old history."
