#!/usr/bin/env python3
"""CODEOWNERS validity (Golden Pipeline architecture §12). GitHub silently ignores an owner that doesn't exist or has
no write access, so code-owner review would quietly not happen. CI asks GitHub's CODEOWNERS-errors API and passes the
answer on stdin; this judges it.

  gh api "repos/$GITHUB_REPOSITORY/codeowners/errors?ref=$REF" | python tools/check_codeowners.py

Fails closed: an empty or unreadable answer fails the check.
"""

from __future__ import annotations

import json
import sys


def report(errs: list[dict]) -> int:
    if errs:
        print("CODEOWNERS errors (each makes GitHub ignore that rule):")
        for e in errs:
            print(f"  {e.get('path', 'CODEOWNERS')}:{e.get('line', '?')}: {str(e.get('message', e)).strip()}")
        return 1
    print("CODEOWNERS ok")
    return 0


def main() -> int:
    try:
        errs = json.loads(sys.stdin.read())["errors"]
    except (ValueError, KeyError, TypeError) as e:
        sys.exit(f"couldn't read the CODEOWNERS errors answer from GitHub: {e!r}")
    if not isinstance(errs, list):
        sys.exit("couldn't read the CODEOWNERS errors answer from GitHub: 'errors' isn't a list")
    return report(errs)


if __name__ == "__main__":
    sys.exit(main())
