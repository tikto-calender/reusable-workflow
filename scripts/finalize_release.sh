#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "::error::$*" >&2
  exit 1
}

branch="release/$VERSION"
main="refs/remotes/origin/main"
tag="refs/tags/$VERSION"
remote_branch="refs/remotes/origin/$branch"

git cat-file -e "$SOURCE_SHA^{commit}" ||
  fail "The released source commit is missing from this repository."

needs_pr=true
if git merge-base --is-ancestor "$SOURCE_SHA" "$main"; then
  needs_pr=false
elif ! git merge-base --is-ancestor "$main" "$SOURCE_SHA"; then
  fail "Source main and the released commit have diverged. Reconcile their history before finalizing."
fi

if git show-ref --verify --quiet "$remote_branch" &&
  [[ "$(git rev-parse "$remote_branch")" != "$SOURCE_SHA" ]]; then
  fail "Branch $branch already points to another commit."
fi

if git show-ref --verify --quiet "$tag"; then
  [[ "$(git rev-parse "$tag^{commit}")" == "$SOURCE_SHA" ]] ||
    fail "Tag $VERSION already points to another commit."
else
  git push origin "$SOURCE_SHA:$tag"
fi

if ! gh release view "$VERSION" --repo "$GITHUB_REPOSITORY" >/dev/null; then
  gh release create "$VERSION" --repo "$GITHUB_REPOSITORY" --verify-tag \
    --title "$VERSION" --generate-notes --notes "Production image: $IMAGE"
fi

if [[ "$needs_pr" == false ]]; then
  echo "The released commit is already on main."
  exit 0
fi

if ! git show-ref --verify --quiet "$remote_branch"; then
  git push origin "$SOURCE_SHA:refs/heads/$branch"
fi

pr_url=$(gh api --method GET "repos/$GITHUB_REPOSITORY/pulls" \
  -f "head=$GITHUB_REPOSITORY_OWNER:$branch" -f base=main -f state=open \
  --jq '.[0].html_url // empty')
if [[ -z "$pr_url" ]]; then
  pr_url=$(gh pr create --repo "$GITHUB_REPOSITORY" --head "$branch" --base main \
    --title "Release $VERSION" --body "Record the source deployed to Production: $SOURCE_SHA.")
fi
echo "Source release PR: $pr_url"
