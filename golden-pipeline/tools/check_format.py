#!/usr/bin/env python3
"""Python formatting, shrink-only (Golden Pipeline architecture §1 "shrink-only debt", §19 spike tier: format check).

Runs `ruff format --check` over the current folder (ruff is pinned centrally, since its style changes between
versions; the consumer's own ruff settings apply). Files listed in the baseline (--baseline, default
.github/golden-pipeline/format-baseline.txt; missing means empty) were unformatted when the check was adopted and are
tolerated; every other Python file must be formatted. The list can only shrink: a listed file that is now formatted,
or no longer exists, fails until it is removed from the list, so formatting a file once keeps it formatted. On a pull
request (GITHUB_BASE_REF set), an entry that isn't in the base branch's list fails too, so a PR can't add to it.

  check_format.py [--baseline PATH]
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _git import base_text  # noqa: E402

BASELINE = ".github/golden-pipeline/format-baseline.txt"


def unformatted() -> set[str]:
    r = subprocess.run(["ruff", "format", "--check", "--output-format", "concise", "."], capture_output=True, text=True)
    if r.returncode not in (0, 1):  # 2: ruff itself failed (bad config, syntax error); fail closed
        sys.exit(f"ruff format failed:\n{r.stdout}{r.stderr}")
    return {m.group(1) for m in re.finditer(r"^(.+?):\d+:\d+: unformatted:", r.stdout, re.M)}


def entries(text: str) -> set[str]:
    return {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--baseline", default=BASELINE)
    a = ap.parse_args(argv)
    path = pathlib.Path(a.baseline)
    allowed = entries(path.read_text()) if path.exists() else set()
    before_text = base_text(a.baseline)
    before = entries(before_text) if before_text is not None else None
    now = unformatted()
    new = sorted(now - allowed)
    fixed = sorted(allowed - now)
    grown = sorted(allowed - before) if before is not None else []
    if grown:
        print(f"Added to {a.baseline}, which only shrinks (format the file instead):\n  " + "\n  ".join(grown))
    if new:
        print("Not formatted (run `ruff format <file>`):\n  " + "\n  ".join(new))
    if fixed:
        print(f"Formatted or gone, so remove from {a.baseline} (the list only shrinks):\n  " + "\n  ".join(fixed))
    if new or fixed or grown:
        return 1
    print(f"format ok: {len(allowed)} file(s) still on the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
