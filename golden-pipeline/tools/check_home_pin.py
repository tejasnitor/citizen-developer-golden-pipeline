#!/usr/bin/env python3
"""This repo's own check: the golden-pipeline workflows load their tools from the commit they name, and that commit
has the tools this commit has.

A called workflow can't check its own repo out with the caller's token, so each job that needs the tools uses the
gp-home action at a fixed SHA of this repo (README, "How the workflows find their tools"). That pin is part of the
workflow file, so a consumer that pins a workflow by SHA also pins the tools. This check keeps the pin honest: every
gp-home pin in .github/workflows/gp-*.yml names the same commit, and that commit's golden-pipeline/tools and
.github/actions/gp-home are identical to HEAD's. Change a tool, commit, then run with --fix to move the pins to that
commit and commit again.

  check_home_pin.py [--fix SHA]      run from the repo root, in a full clone
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

PIN = re.compile(r"(uses:\s*[\w.-]+/[\w.-]+/\.github/actions/gp-home@)([0-9a-f]{40})")
TRACKED = ["golden-pipeline/tools", ".github/actions/gp-home"]


def workflows() -> list[pathlib.Path]:
    return sorted(pathlib.Path(".github/workflows").glob("gp-*.yml"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--fix", metavar="SHA", help="rewrite every gp-home pin to this commit")
    a = ap.parse_args(argv)
    if a.fix:
        if not re.fullmatch(r"[0-9a-f]{40}", a.fix):
            sys.exit("--fix takes a full 40-character commit SHA")
        for f in workflows():
            f.write_text(PIN.sub(lambda m: m[1] + a.fix, f.read_text()))
    pins = {(f.as_posix(), m[2]) for f in workflows() for m in PIN.finditer(f.read_text())}
    shas = {sha for _, sha in pins}
    if not pins:
        sys.exit("no gp-home pins found in .github/workflows/gp-*.yml")
    if len(shas) != 1:
        print("gp-home is pinned to more than one commit:\n  " + "\n  ".join(f"{f}: {s}" for f, s in sorted(pins)))
        return 1
    sha = shas.pop()
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode != 0:
        sys.exit(f"gp-home pin {sha} isn't a commit in this clone (fetch-depth 0?)")
    diff = subprocess.run(["git", "diff", "--stat", sha, "HEAD", "--", *TRACKED], capture_output=True, text=True)
    if diff.returncode != 0 or diff.stdout.strip():
        print(f"The tools changed since the pinned commit {sha[:10]}; commit, then run")
        print("  python3 golden-pipeline/tools/check_home_pin.py --fix $(git rev-parse HEAD)")
        print(diff.stdout + diff.stderr)
        return 1
    print(f"gp-home pin ok: {sha} ({len(pins)} use(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
