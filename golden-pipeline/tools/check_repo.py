#!/usr/bin/env python3
"""Repo-wide checks for the golden pipeline (architecture §4.5, §12). Always checks the whole repo: run from any
folder inside it, it works from the repo root, because GitHub reads workflows only from the root's .github.

- no secrets files or keys committed, and no file over 5 MB
- every action or reusable workflow a workflow uses is pinned to a full commit SHA with a version comment
  (uses: owner/repo@<40-hex sha> # v1.2.3, or # <name>-v1[.2.3] for a prefixed tag such as golden-pipeline-v1), in
  block style (- uses: …, or a job's uses: for a reusable workflow) or flow style (- { uses: …, with: {…} }  # v1.2.3),
  the key plain or quoted ("uses": / 'uses':), every uses on a line ([{uses: a}, {uses: b}]); a key this can't
  read (an escape in a quoted key, an alias or ? key) fails; text inside a block scalar (run: | or > and the lines
  indented under it) is a string, not a key, so it isn't read; a docker:// action is pinned by digest
  (docker://image[:tag]@sha256:<64 hex>). Only local actions (./…) are exempt.
- every external hook in every tracked .pre-commit-config.yaml (the root's and any in subfolders, as a monorepo keeps
  one per app) is pinned the same way (rev: <sha>  # v1.2.3)
- there is at least one workflow: a repo that calls this check has one, so finding none means the wrong folder
"""

import os
import pathlib
import re
import subprocess
import sys

BANNED_NAMES = {".env", "id_rsa", "id_ed25519", "credentials", ".npmrc", ".pypirc"}
BANNED_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}
MAX_BYTES = 5 * 1024 * 1024
VERSION = r"# (?:[A-Za-z0-9._-]+-)?v\d+(?:\.\d+){0,2}\b"
# A moving tag like @v4 (or docker://x:latest) could change under us unreviewed; a SHA or digest can't.
# A key starts the line (a job's `uses:` calling a reusable workflow, or a step's after its first key), follows "- "
# or sits in a flow collection ({name: x, uses: …}, [uses: …]). YAML lets the key be quoted ("uses": or 'uses':) with
# spaces before its colon, and carry a tag or anchor (!!str uses:, &a uses:); a quoted key's colon may touch the
# value ({"uses":x}).
KEY_AT = r"(?:(?:^\s*(?:-\s+)?)|(?:[\[{,]\s*))"
PROPS = r"(?:[!&]\S*\s+)*"


def _key(name: str) -> str:
    return rf"{KEY_AT}{PROPS}(?:{name}|\"{name}\"|'{name}')\s*:"


# the value ends at , } ] or whitespace, and may be quoted; the rest of the line (for the version comment) is read
# without being consumed, so a second uses on the same line ([{uses: a}, {uses: b}]) is checked too
USES = re.compile(_key("uses") + r"\s*([\"']?)([^\s,}\]\"']+)\1(?=(.*)$)")
# Keys this check can't read, so it can't tell they aren't `uses`: a quoted key with an escape ("u\x73es"), an
# alias as a key (*a :) and an explicit key (? uses). Fail closed.
OPAQUE_KEY = re.compile(KEY_AT + PROPS + r'(?:"(?:[^"\\]|\\.)*\\(?:[^"\\]|\\.)*"\s*:|\*\S+\s+:|\?(?:\s|$))')
USES_ALONE = re.compile(_key("uses") + r"\s*(?:#.*)?$")
# after the reference: the version comment, maybe after the rest of a flow mapping (", with: {…} }")
USES_VERSION = re.compile(rf"(?:\s*[,}}\]].*)?\s{VERSION}.*")
SHA_REF = re.compile(r"[^@\s]+@[0-9a-f]{40}")
DOCKER = re.compile(r"docker://[^@\s]+@sha256:[0-9a-f]{64}")
# rev: can start the line, follow "- " (an entry that lists rev first) or sit in a flow mapping ({repo: …, rev: …})
HOOK_REV = re.compile(_key("rev") + r"\s*(\S.*)$")
# A key whose value is a block scalar (run: |, script: >-, - |): the lines indented past the key are text, not YAML,
# so a script that prints "{ uses: x@v4 }" isn't a step. The scalar's parent is the key (its column is the length of
# what precedes it) or, for a bare "- |", the sequence (the last dash's column); YAML ends the scalar at the first
# non-blank line not indented past its parent. An indicator alone on its line ("run:" then "|" below it) belongs to the
# key or dash that opened the line before, so that line's column is kept (OPENER).
BLOCK_START = re.compile(r"^(\s*(?:-\s+)*)([^\s#\-][^#]*?\s*:\s+)?" + PROPS + r"[|>][-+1-9]*\s*(?:#.*)?$")
OPENER = re.compile(r"^(\s*(?:-\s+)*)(?:[^\s#\-][^#]*?\s*:|-)\s*(?:#.*)?$")


def code_lines(text: str):
    """Yield (line number, line) for the lines that are YAML, skipping comments and block-scalar text."""
    block_col = opener_col = None
    for n, line in enumerate(text.splitlines(), 1):
        if block_col is not None:
            if not line.strip() or len(line) - len(line.lstrip()) > block_col:
                continue
            block_col = None
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        yield n, line
        indent = len(line) - len(line.lstrip())
        if m := BLOCK_START.match(line):
            if m[2]:
                block_col = len(m[1])  # key: |
            elif m[1].strip():
                block_col = len(m[1].rstrip()) - 1  # - |
            elif opener_col is not None and opener_col < indent:
                block_col = opener_col  # | alone, under the key or dash of the line before
            else:
                block_col = max(indent - 1, 0)  # no parent seen: never skip less-indented lines
        opener_col = len(m[1]) if (m := OPENER.match(line)) else None


# the SHA may be followed by the rest of a flow mapping (", hooks: [...]}") before the version comment
HOOK_PINNED = re.compile(rf"[0-9a-f]{{40}}(?:[\s,}}\]].*)?\s{VERSION}.*")


def uses_problem(ref: str, rest: str) -> str | None:
    if ref.startswith("./"):
        return None
    if ref.startswith("docker://"):
        return None if DOCKER.fullmatch(ref) else "pin the image by digest (uses: docker://image@sha256:<digest>)"
    if SHA_REF.fullmatch(ref) and USES_VERSION.fullmatch(rest):
        return None
    return "pin to a full commit SHA with a version comment (uses: owner/repo@<sha> # vX.Y.Z)"


def main() -> int:
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        sys.exit(f"not a git checkout: {top.stderr.strip()}")
    os.chdir(top.stdout.strip())
    errs = []
    hook_configs = []
    for f in subprocess.run(["git", "ls-files", "-z"], capture_output=True, text=True, check=True).stdout.split("\0"):
        if not f:
            continue
        p = pathlib.Path(f)
        if p.name == ".pre-commit-config.yaml" and p.is_file():
            hook_configs.append(p)
        if (
            p.name in BANNED_NAMES
            or (p.name.startswith(".env.") and p.name != ".env.example")
            or p.suffix in BANNED_SUFFIXES
        ):
            errs.append(f"{f}: secrets/key files must not be committed (use a secrets manager)")
        elif p.exists() and p.stat().st_size > MAX_BYTES:
            errs.append(f"{f}: {p.stat().st_size // 1024 // 1024} MB; files over 5 MB don't belong in git")

    github = pathlib.Path(".github")
    workflows = sorted(github.glob("workflows/*.y*ml"))
    if not workflows:
        errs.append(f"no workflows in {pathlib.Path.cwd() / '.github/workflows'}: nothing to check the pins of")
    for f in [*workflows, *sorted(github.glob("actions/*/action.y*ml"))]:
        for n, line in code_lines(f.read_text()):
            if OPAQUE_KEY.search(line):
                errs.append(f"{f}:{n}: write keys plainly (no escapes, aliases or ? keys), so a uses: can be checked")
            if USES_ALONE.search(line):
                errs.append(f"{f}:{n}: write the reference on the uses: line, so its pin can be checked")
            for m in USES.finditer(line):
                if problem := uses_problem(m[2], m[3]):
                    errs.append(f"{f}:{n}: {problem}")

    for hooks in sorted(hook_configs):
        for n, line in code_lines(hooks.read_text()):
            m = HOOK_REV.search(line)
            if m and not HOOK_PINNED.fullmatch(m[1]):
                errs.append(
                    f"{hooks}:{n}: pin the hook repo to a full commit SHA with a version comment (rev: <sha>  # vX.Y.Z)"
                )

    if errs:
        print("Repo checks failed:\n  - " + "\n  - ".join(errs))
        for e in errs:
            print(f"::error::{e}")
        return 1
    print("repo ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
