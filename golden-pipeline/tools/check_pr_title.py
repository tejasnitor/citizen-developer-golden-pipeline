#!/usr/bin/env python3
"""Checks a pull request title against the commit standard (Golden Pipeline architecture §8).

PRs are squash-merged, so the title becomes the commit on develop, and release-please reads it to pick the next
version and write the changelog. Reads the title from PR_TITLE. tools/check_commits.py applies the same grammar to
each commit in the PR.

  type(scope)!: summary     72 characters at most; scope optional, lowercase, no spaces; ! marks a breaking change

Dependabot's PRs (PR_AUTHOR dependabot[bot]) skip the length limit only: Dependabot writes the title itself, a grouped
update adds "in the <group> group", and no setting shortens it, so a long action name would fail every weekly bump.
Their commits keep to 72 (Dependabot's commit subjects are short), and the title must still follow the grammar.
"""

import os
import re
import sys

TYPES = "feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert"
TITLE = re.compile(rf"^(?:{TYPES})(?:\([a-z0-9][a-z0-9._/-]*\))?!?: \S.*$")
MAX = 72
LENGTH_EXEMPT = {"dependabot[bot]"}


def problems(title: str, author: str = "") -> list[str]:
    """What's wrong with a title or commit subject; empty when it follows the standard."""
    # GitHub's revert button titles the PR 'Revert "<original title>"': accepted as is, and its 9 extra characters
    # don't count, so reverting a PR with a 72-character title doesn't need a hand edit.
    revert = title.startswith('Revert "')
    limit = MAX + len('Revert ""') if revert else MAX
    errs = []
    if not revert and not TITLE.match(title):
        errs.append(
            f"'{title}' isn't 'type(scope): summary'. type is one of {TYPES.replace('|', ', ')}; "
            "scope is optional, lowercase, no spaces. Example: fix(recruit-kit): keep the rubric order"
        )
    if len(title) > limit and author not in LENGTH_EXEMPT:
        errs.append(f"'{title}' is {len(title)} characters; keep it to {limit}")
    return errs


if __name__ == "__main__":
    title = os.environ.get("PR_TITLE", "").strip()
    errs = problems(title, os.environ.get("PR_AUTHOR", "").strip())
    if errs:
        print("PR title check failed:\n  - " + "\n  - ".join(errs))
        print("Edit the PR title; this check re-runs when the title changes.")
        sys.exit(1)
    print(f"title ok: {title}")
