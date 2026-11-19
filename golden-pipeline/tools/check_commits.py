#!/usr/bin/env python3
"""Checks every commit a pull request adds against the commit standard (Golden Pipeline architecture §8, §12).

Each non-merge commit in BASE..HEAD must have a subject `type(scope): summary` of 72 characters at most, the same
grammar as the PR title (tools/check_pr_title.py).

Exempt (architecture §8): merge commits (back-merges, "Update branch") and GitHub's revert commits
('Revert "…"'). A repo that adopts the check with older history can grandfather it: COMMITS_GRANDFATHERED names a
file in the consumer repo (relative to the repo root) listing full commit SHAs (one per line, # comments allowed)
whose history isn't checked, so a release PR doesn't fail on commits made before the check. Unset or empty: nothing
is grandfathered.

The PR can't grandfather its own commits. The list is read as it is on BASE, or, when BASE doesn't have it yet (the
first release PR after adoption, into main), as it is on origin/develop: both already reviewed and merged, never the
PR's own copy. A PR that adds the list for the first time therefore grandfathers nothing until it is merged. Each
entry must be a full 40-hex SHA of a commit already merged: an ancestor of BASE or of origin/develop. A ref such as
HEAD, a short SHA, or a commit only the PR has is an error.

Fails closed: a range, a list or a grandfathered commit git can't read is an error, not an empty list; a list that is
named but on neither BASE, origin/develop nor the PR is an error too.

  check_commits.py BASE HEAD
"""

import os
import pathlib
import re
import subprocess
import sys

TOOLS = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from _git import file_at  # noqa: E402
from check_pr_title import problems  # noqa: E402

GRANDFATHERED = os.environ.get("COMMITS_GRANDFATHERED", "").strip()
SQUASH_SUFFIX = re.compile(r" \(#\d+\)$")
# A commit name git can take, never an option: CI passes SHAs and branch names.
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@^~{}-]*")
SHA = re.compile(r"[0-9a-f]{40}")
# Where a list not yet on BASE may come from: the integration branch, which only takes reviewed PRs.
TRUSTED = "origin/develop"


def _git_ok(*args: str) -> bool:
    return subprocess.run(["git", *args], capture_output=True).returncode == 0


def _merged(sha: str, into: list[str]) -> bool:
    return any(_git_ok("merge-base", "--is-ancestor", sha, ref) for ref in into)


def _merged_list(trusted: list[str]) -> str | None:
    """The grandfathered list as on the first trusted ref that has it, or None when none has it yet."""
    for ref in trusted:
        if (text := file_at(ref, GRANDFATHERED)) is not None:
            print(f"grandfathered commits: {GRANDFATHERED} as on {ref}")
            return text
    return None


def _check_grandfathered(sha: str, trusted: list[str]) -> None:
    """Stops the check unless sha is a full SHA of a commit in this clone, merged into a trusted ref."""
    if not SHA.fullmatch(sha):
        sys.exit(f"{GRANDFATHERED}: {sha!r} is not a full 40-character commit SHA")
    if not _git_ok("cat-file", "-e", f"{sha}^{{commit}}"):
        sys.exit(f"grandfathered commit {sha} isn't in this clone (fetch-depth 0?)")
    if not _merged(sha, trusted):
        sys.exit(f"grandfathered commit {sha} isn't merged ({' or '.join(trusted)}); only older history can be")


def grandfathered(base: str) -> list[str]:
    if not GRANDFATHERED:
        return []
    trusted = [base] + ([TRUSTED] if _git_ok("rev-parse", "--verify", "-q", f"{TRUSTED}^{{commit}}") else [])
    text = _merged_list(trusted)
    if text is None:
        if not pathlib.Path(GRANDFATHERED).is_file():  # named but nowhere: a wrong path, fail closed
            sys.exit(f"COMMITS_GRANDFATHERED names {GRANDFATHERED}, which isn't in this repo")
        print(f"{GRANDFATHERED} isn't merged yet ({' or '.join(trusted)}): nothing is grandfathered until it is")
        return []
    shas = [s for s in (line.strip() for line in text.splitlines()) if s and not s.startswith("#")]
    for sha in shas:
        _check_grandfathered(sha, trusted)
    return shas


def ref(arg: str) -> str:
    """The argument as a commit name, checked where git gets it (SonarCloud pythonsecurity:S8705)."""
    m = REF.fullmatch(arg)
    if not m:
        sys.exit(f"not a commit SHA or ref: {arg!r}")
    return m.group(0)


def subjects(base: str, head: str) -> list[tuple[str, str]]:
    base, head = ref(base), ref(head)
    old = [f"^{sha}" for sha in grandfathered(base)]
    r = subprocess.run(
        ["git", "log", "--no-merges", "--format=%H%x1f%s", "--end-of-options", f"{base}..{head}", *old],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        sys.exit(f"can't read the commits {base}..{head}: {r.stderr.strip()}")
    return [(sha, subject) for sha, _, subject in (line.partition("\x1f") for line in r.stdout.splitlines() if line)]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit("usage: check_commits.py BASE HEAD (each a commit SHA or ref)")
    commits = subjects(*argv)
    # A squash merge appends ' (#NN)' to the PR title, which pr-title already checked; don't count it against 72.
    bad = [(sha, errs) for sha, subject in commits if (errs := problems(SQUASH_SUFFIX.sub("", subject)))]
    if bad:
        print("Commit message check failed:")
        for sha, errs in bad:
            print(f"  {sha[:10]}: " + "; ".join(errs))
        print(
            "Reword them (git rebase -i, then reword) and force-push the branch. The squash commit on develop "
            "takes the PR title, but the commits in the PR are checked too."
        )
        return 1
    print(f"commits ok: {len(commits)} checked")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
