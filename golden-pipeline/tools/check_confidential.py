#!/usr/bin/env python3
"""Confidential-content scan (Golden Pipeline architecture §12).

Scans every tracked text file under the current folder, and with --range the lines added in that commit range (so
content added and removed again inside a PR is caught too), against the patterns in the consumer repo's denylist
(--denylist, one case-insensitive regular expression per line, # comments allowed). A missing denylist, or one with no
patterns, fails.

With --range, the run can't loosen its own judge: the patterns are those of the denylist as it is at BASE plus any
the run's own copy adds, so a pattern removed in the PR still applies until that removal is merged. When BASE has no
denylist yet (the PR that adopts the check), the PR's own copy is used: it must have patterns, and it is reviewed with
the PR like any other change under .github. A pattern that doesn't compile is named: in the run's own copy it fails
the check; one only BASE has (the PR fixes or removes it) is skipped with a warning, so the fix can merge.

A hit in the tree always fails. A hit in a commit that is already on develop or main, and so can't be rewritten, can
be accepted once the content is gone from the tree: add `<full commit sha> <pattern>` to the accepted list
(--accepted, default: confidential-accepted.txt next to the denylist). Entries for commits not yet on origin/develop
or origin/main are ignored.

  check_confidential.py [--denylist PATH] [--accepted PATH] [--range BASE..HEAD]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _git import file_at  # noqa: E402

DENYLIST = ".github/golden-pipeline/confidential-denylist.txt"
ACCEPTED = "confidential-accepted.txt"  # next to the denylist unless --accepted names another file
MERGED = ("origin/develop", "origin/main")
# A commit name git can take, never an option. --range is BASE..HEAD or BASE...HEAD of these; CI passes a SHA, HEAD.
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@^~{}-]*")


def parse(text: str) -> list[str]:
    return [s for s in (ln.strip() for ln in text.splitlines()) if s and not s.startswith("#")]


def patterns(root: pathlib.Path, denylist: str, base: str | None = None) -> list[re.Pattern]:
    """The denylist's patterns: this checkout's, plus BASE's when BASE has the file (a PR can add, never remove).

    A pattern that doesn't compile is named. In this checkout's list it fails the check, so the PR fixes it; one only
    BASE has (the PR fixes or removes it) is skipped with a warning, or the PR that fixes it could never pass."""
    mine = parse((root / denylist).read_text())  # missing list: fail closed with the traceback
    lines = mine
    if base is not None and (before := file_at(base, denylist)) is not None:
        lines = [*dict.fromkeys([*parse(before), *mine])]
    if not lines:
        sys.exit(f"{denylist} has no patterns, so it would pass anything: add at least one")
    out, broken = [], []
    for ln in lines:
        try:
            out.append(re.compile(ln, re.I))
        except re.error as e:
            if ln in mine:
                broken.append(f"{ln!r}: {e}")
            else:
                print(
                    f"::warning::{denylist} on the base has a pattern that doesn't compile, skipped as this "
                    f"change removes it: {ln!r} ({e})"
                )
    if broken:
        sys.exit(f"{denylist}: these patterns don't compile; fix or remove them:\n  " + "\n  ".join(broken))
    if not out:
        sys.exit(f"{denylist} has no patterns that compile, so it would pass anything: add at least one")
    return out


def merged(root: pathlib.Path, sha: str) -> bool:
    return any(
        subprocess.run(["git", "merge-base", "--is-ancestor", sha, ref], cwd=root, capture_output=True).returncode == 0
        for ref in MERGED
    )


def accepted(root: pathlib.Path, path: str) -> set[tuple[str, str]]:
    """(commit, pattern) pairs from the accepted list whose commit is already on develop or main."""
    p = root / path
    if not p.exists():
        return set()
    ok = set()
    for ln in p.read_text().splitlines():
        sha, _, pat = ln.strip().partition(" ")
        if re.fullmatch(r"[0-9a-f]{40}", sha) and merged(root, sha):
            ok.add((sha, pat.strip()))
    return ok


def tree(root: pathlib.Path, pats: list[re.Pattern], skip: tuple[str, ...]) -> list[str]:
    files = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True).stdout
    hits = []
    for name in filter(None, files.split("\0")):
        p = root / name
        if name in skip or not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue  # binary
        text = data.decode("utf-8", errors="replace")  # text in another encoding is still scanned
        for no, line in enumerate(text.splitlines(), 1):
            hits += [f"{name}:{no}: {pat.pattern}" for pat in pats if pat.search(line)]
    return hits


def ref(arg: str) -> str:
    """The argument as a commit name, checked where git gets it (SonarCloud pythonsecurity:S8705)."""
    m = REF.fullmatch(arg)
    if not m:
        sys.exit(f"--range must be BASE..HEAD, each a commit SHA or ref: {arg!r}")
    return m.group(0)


def split(rng: str) -> tuple[str, str, str]:
    """BASE, the dots and HEAD of a --range, each checked."""
    base, dots, head = rng.partition("..")
    dots = "..." if head.startswith(".") else ".." if dots else ""
    if not dots:
        sys.exit(f"--range must be BASE..HEAD, each a commit SHA or ref: {rng!r}")
    return ref(base), dots, ref(head.removeprefix("."))


def added(
    root: pathlib.Path, rng: str, pats: list[re.Pattern], ok: set[tuple[str, str]], skip: tuple[str, ...]
) -> list[str]:
    base, dots, head = split(rng)
    log = subprocess.run(
        [
            "git",
            "log",
            "-p",
            "--no-merges",
            "--format=commit %H",
            "--end-of-options",
            f"{base}{dots}{head}",
            "--",
            ".",
            *(f":!{s}" for s in skip),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        errors="replace",
    ).stdout
    hits, commit = [], "?"
    for line in log.splitlines():
        if line.startswith("commit "):
            commit = line[7:]
        elif line.startswith("+") and not line.startswith("+++"):
            hits += [
                f"commit {commit}: {pat.pattern}"
                for pat in pats
                if pat.search(line) and (commit, pat.pattern) not in ok
            ]
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--range", help="commit range whose added lines to scan, e.g. BASE..HEAD")
    ap.add_argument("--denylist", default=DENYLIST, help=f"the patterns, relative to here (default {DENYLIST})")
    ap.add_argument("--accepted", help=f"accepted merged hits (default {ACCEPTED} next to the denylist)")
    a = ap.parse_args(argv)
    root = pathlib.Path.cwd()
    acc = a.accepted or (pathlib.PurePosixPath(a.denylist).parent / ACCEPTED).as_posix()
    skip = (pathlib.PurePosixPath(a.denylist).as_posix(), pathlib.PurePosixPath(acc).as_posix())
    pats = patterns(root, a.denylist, split(a.range)[0] if a.range else None)
    hits = tree(root, pats, skip) + (added(root, a.range, pats, accepted(root, acc), skip) if a.range else [])
    if hits:
        print(f"Confidential content. Remove it from the tree; for a commit hit already merged, see {acc}:")
        print("  " + "\n  ".join(sorted(set(hits))))
        return 1
    print(f"confidential ok: {len(pats)} pattern(s)" + (f", tree and {a.range}" if a.range else ", tree"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
