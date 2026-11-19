#!/usr/bin/env python3
"""Checks which branches may open a pull request into main (Golden Pipeline architecture §2, §9, §12).

PRs into main may come only from develop, release/*, hotfix/*, or a release-please branch opened by the release
app, and only from a branch of the repo itself: a fork's `develop` is not this repo's develop. Any other base branch
passes. Reads BASE_REF, HEAD_REF, PR_AUTHOR and RELEASE_APP (the release app's bot login, default
pp-release-bot[bot]) from the environment, and BASE_REPO and HEAD_REPO (owner/name of the PR's base and head repos).
BASE_REPO is required (gp-pr-checks.yml sets both); a PR into main whose HEAD_REPO differs (or is empty, as for a
deleted fork) fails.
"""

import fnmatch
import os
import sys

ALLOWED = ["develop", "release/*", "hotfix/*"]
RELEASE_PLEASE = "release-please--*"


def problem(base: str, head: str, author: str, release_app: str, base_repo: str, head_repo: str) -> str:
    """Why this PR may not target its base; empty when it may."""
    if base != "main":
        return ""
    if head_repo.lower() != base_repo.lower():
        return (
            f"PRs into main come from a branch of {base_repo}, not from {head_repo or 'a deleted repo'}. "
            f"Push the branch to {base_repo}, or open the PR against develop"
        )
    if any(fnmatch.fnmatchcase(head, p) for p in ALLOWED):
        return ""
    if fnmatch.fnmatchcase(head, RELEASE_PLEASE):
        return (
            ""
            if author == release_app
            else (f"'{head}' is a release-please branch, but the PR is by {author or 'nobody'}, not {release_app}")
        )
    return f"PRs into main come from develop, release/* or hotfix/*, not '{head}'. Open it against develop instead"


if __name__ == "__main__":
    env = os.environ.get
    base, head = env("BASE_REF", "").strip(), env("HEAD_REF", "").strip()
    if not base or not head or not env("BASE_REPO", "").strip():
        sys.exit("BASE_REF, HEAD_REF and BASE_REPO are required")
    err = problem(
        base,
        head,
        env("PR_AUTHOR", "").strip(),
        env("RELEASE_APP", "").strip() or "pp-release-bot[bot]",
        env("BASE_REPO", "").strip(),
        env("HEAD_REPO", "").strip(),
    )
    if err:
        print(f"Branch targeting check failed: {err}")
        sys.exit(1)
    print(f"branch ok: {head} → {base}")
