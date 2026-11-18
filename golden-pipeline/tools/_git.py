"""Shared git helpers: read a file as it is at another commit (the PR's base), failing closed on anything but a
file that is genuinely not there."""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys

# The git commands these helpers run; each call's options are fixed, and a revision, path or object id that comes
# from outside is checked first, so it can't be read as an option (SonarCloud pythonsecurity:S8705).
COMMANDS = {"rev-parse", "ls-tree", "cat-file", "merge-base"}
# A commit name git can take, never an option: a SHA, a branch such as origin/develop, or HEAD.
REV = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@^~{}-]*")
OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _git(command: str, *args: str) -> subprocess.CompletedProcess:
    if command not in COMMANDS:
        raise ValueError(f"_git runs {sorted(COMMANDS)} only, not {command!r}")
    return subprocess.run(["git", command, *args], capture_output=True, text=True)


def _rev(rev: str) -> str:
    """rev as a commit name git will never read as an option; anything else stops the check."""
    m = REV.fullmatch(rev)
    if not m:
        sys.exit(f"not a commit SHA or ref: {rev!r}")
    return m.group(0)


def repo_path(path: str) -> str:
    """`path` (relative to the current folder) as a path from the repo root, normalised: `./x`, `a/../x` and `x//y`
    name the same file, which `git show <commit>:<path>` would not find. A path that leaves the repo is an error."""
    if posixpath.isabs(path) or os.path.isabs(path):
        sys.exit(f"{path}: give a path relative to the folder the check runs in, not an absolute one")
    prefix = _git("rev-parse", "--show-prefix")
    if prefix.returncode != 0:
        sys.exit(f"not a git checkout, can't read {path} at another commit:\n{prefix.stderr}")
    full = posixpath.normpath(prefix.stdout.strip() + path.replace(os.sep, "/"))
    if full == ".." or full.startswith("../") or full == ".":
        sys.exit(f"{path}: not a file inside this repo")
    if full.startswith("-"):
        sys.exit(f"{path}: a path starting with '-' could be read as an option")
    return full


def file_at(commit: str, path: str) -> str | None:
    """The text of `path` (relative to the current folder) at `commit`, or None when that commit has no such file.
    Any other failure (an unknown commit, a folder at that path, git itself failing) exits non-zero."""
    full = repo_path(path)
    commit = _rev(commit)
    ls = _git("ls-tree", "-z", "--full-tree", "--end-of-options", commit, "--", full)
    if ls.returncode != 0:
        sys.exit(f"can't list {full} at {commit}: {ls.stderr.strip()}")
    if not ls.stdout:
        return None  # the one legitimate absence: the commit has no such file
    meta, _, name = ls.stdout.rstrip("\0").partition("\t")
    kind, obj = meta.split()[1:3]
    if kind != "blob" or name != full:
        sys.exit(f"{full} at {commit} is a {kind}, not a file")
    if not OBJECT_ID.fullmatch(obj):
        sys.exit(f"{full} at {commit}: git listed an unexpected object id {obj!r}")
    show = _git("cat-file", "--end-of-options", "blob", obj)
    if show.returncode != 0:
        sys.exit(f"can't read {full} at {commit}: {show.stderr.strip()}")
    return show.stdout


def base_text(path: str) -> str | None:
    """The baseline file `path` (relative to the current folder) as it is on the PR's base branch, or None when there
    is nothing to compare with: not a PR, or the base has no such file yet.

    GITHUB_BASE_REF is set by GitHub Actions on pull_request events; BASELINE_BASE_REF names the base for a run
    started by hand on a PR branch. On a PR, everything else fails closed: no git checkout, a base that can't be
    found, or a git error reading the file.
    """
    ref = os.environ.get("GITHUB_BASE_REF") or os.environ.get("BASELINE_BASE_REF")
    if not ref:
        return None
    base = _git("merge-base", "--end-of-options", "HEAD", _rev(f"origin/{ref}"))
    if base.returncode != 0:
        sys.exit(f"can't find the base of this PR (origin/{ref}); is the checkout's fetch-depth 0?\n{base.stderr}")
    return file_at(base.stdout.strip(), path)
