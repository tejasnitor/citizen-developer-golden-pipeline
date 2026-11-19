#!/usr/bin/env python3
"""Local commands (Golden Pipeline requirement PGP-20.5): the repo has a justfile that sets up, lints and tests it the
way CI does. The kit's template is golden-pipeline/kit/templates/justfile.

Looks in the current folder (working_directory) for the file `just` reads there (justfile, .justfile, any case) and
for the recipes `setup`, `lint`, `test` and `check` in it, or in a file it imports (`import 'x.just'`, `import?`). An
alias (`alias check := all`) counts as the recipe it names. Modules (`mod x`) don't: their recipes are `just x::y`.
The file is read, never run, so the check needs no `just` on the runner.

  check_justfile.py [--mode report|require|off]

report (the default for one release) warns and passes; require fails; off skips.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REQUIRED = ("setup", "lint", "test", "check")
MODES = ("report", "require", "off")
TEMPLATE = "golden-pipeline/kit/templates/justfile"

NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
# A recipe header starts in column 0: [@]name [params]: [deps]. Its colon is not part of `:=` (an assignment).
RECIPE = re.compile(rf"^@?({NAME})\b([^:]*):(?!=)")
ALIAS = re.compile(rf"^alias\s+({NAME})\s*:=")
IMPORT = re.compile(r"""^import(\??)\s+(['"])(.+?)\2\s*(?:#.*)?$""")
# Lines in column 0 that look like `word ...:` but are not recipes.
KEYWORDS = {"set", "export", "alias", "import", "mod", "unexport"}
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


def find(folder: pathlib.Path) -> pathlib.Path | None:
    """The file `just` reads in `folder`: `justfile` or `.justfile`, in any case."""
    names = {p.name.lower(): p for p in sorted(folder.iterdir()) if p.is_file()}
    return names.get("justfile") or names.get(".justfile")


def recipes(path: pathlib.Path, seen: set[pathlib.Path] | None = None) -> tuple[set[str], list[str]]:
    """The recipe and alias names `path` defines, with those of the files it imports, and any problems found."""
    seen = seen if seen is not None else set()
    real = path.resolve()
    if real in seen:
        return set(), []
    seen.add(real)
    names: set[str] = set()
    problems: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        if not line or line[0] in " \t#[":  # body, blank, comment or attribute
            continue
        if m := IMPORT.match(line):
            target = path.parent / m[3]
            if target.is_file():
                more, issues = recipes(target, seen)
                names |= more
                problems += issues
            elif not m[1]:
                problems.append(f"{path.name} imports {m[3]}, which is not a file")
            continue
        if m := ALIAS.match(line):
            names.add(m[1])
            continue
        first = line.split(None, 1)[0].lstrip("@")
        if first in KEYWORDS:
            continue
        if m := RECIPE.match(QUOTED.sub('""', line)):
            names.add(m[1])
    return names, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--mode", choices=MODES, default="report")
    a = ap.parse_args(argv)
    if a.mode == "off":
        print("justfile check off")
        return 0
    level = "error" if a.mode == "require" else "warning"
    hint = f"copy {TEMPLATE} from infra-github-workflows (README, Adopting it) and run `just check` before pushing"
    path = find(pathlib.Path.cwd())
    if path is None:
        print(f"::{level} title=justfile::no justfile in this folder; {hint}")
        return 1 if a.mode == "require" else 0
    names, problems = recipes(path)
    missing = [r for r in REQUIRED if r not in names]
    for p in problems:
        print(f"::{level} title=justfile::{p}")
    if missing:
        print(f"::{level} title=justfile::{path.name} has no {', '.join(missing)} recipe(s); {hint}")
    if missing or problems:
        return 1 if a.mode == "require" else 0
    print(f"justfile ok: {path.name} has {', '.join(REQUIRED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
