#!/usr/bin/env python3
"""Type check, shrink-only (Golden Pipeline architecture §10.2 type check, §1 "shrink-only debt").

Runs pyright (pinned centrally) over the given source paths against the consumer's own virtual environment (.venv,
as `uv sync` makes it) so imports of its dependencies resolve. The consumer's own pyright settings apply: those in
pyrightconfig.json or, without one, [tool.pyright] in pyproject.toml (pyright's own order), both in the current
folder, with the files they `extend` merged in as pyright merges them (a base must be a file in the repo; a cycle
fails). The check sets only what it must: which files (`include`, the given paths), the import paths it adds
(`extraPaths`, added to the consumer's), the Python version (--python-version) and the virtual environment. The mode
is at least `standard`: a consumer's `strict` applies, a `basic` or `off` doesn't. Settings that would hide errors in
the given paths are dropped (and named in the output): `ignore`, `exclude` (the usual folders, .venv, node_modules,
__pycache__ and dot folders, are left out instead), analyzeUnannotatedFunctions = false, useLibraryCodeForTypes =
false, any rule set below "error",
the settings that make code unreachable or swap its stubs (`defineConstant`, `pythonPlatform`, `stubPath`,
`typeshedPath`, and an execution environment's `pythonPlatform` or `pythonVersion`), from the config or any base it
extends; stubs in pyright's default stub folder, typings/, still apply, as files a reviewer sees. A consumer config
that relaxes a rule is checked in `standard` mode, not `strict`. File-level comments that lower the checks
(`# pyright: basic`, `# pyright: reportX=false`, a `# type: ignore` before the first statement) are allowed, like
inline ones, and the files with them are named. A run
that analysed no file, or in which pyright itself failed, fails. Files counted in the baseline
(--baseline, default .github/golden-pipeline/types-baseline.txt; `path count` per line, missing means empty) had that
many type errors when the check was adopted and are tolerated up to that count; every other file must have none. A
count can only go down: a file with more errors than its entry fails, and a file with fewer (or none, or gone) fails
until its entry is lowered or removed, so a fix stays fixed. On a pull request (GITHUB_BASE_REF set), an entry higher
than the base branch's fails too, so a PR can't add to the list.

  check_types.py [--baseline PATH] [--python-version 3.12] PATH [PATH ...]
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _git import base_text  # noqa: E402

BASELINE = ".github/golden-pipeline/types-baseline.txt"
EXCLUDE = ["**/.venv", "**/node_modules", "**/__pycache__", "**/.*"]


def errors(paths: list[str], python_version: str) -> list[dict]:
    """pyright's errors, each with a `path` relative to the current folder."""
    root = pathlib.Path.cwd().resolve()
    missing = [p for p in paths if not (root / p).exists()]
    if missing:
        sys.exit(f"source path(s) not found: {', '.join(missing)}")
    # Folders of tests and scripts import their siblings (`import conftest`): pytest and `python scripts/x.py` put
    # the file's own folder on the path, so pyright gets them too.
    siblings = sorted(
        {
            d.relative_to(root).as_posix()
            for p in paths
            for d in (root / p).rglob("*")
            if d.is_dir() and d.name in ("tests", "scripts")
        }
        | {p for p in paths if pathlib.PurePosixPath(p).name in ("tests", "scripts")}
    )
    config, dropped, relaxed = without_relaxations(consumer_config(root))
    mode = config.get("typeCheckingMode", "standard")
    if mode not in ("standard", "strict") or relaxed:
        mode = "standard"
    if dropped:
        print(f"ignoring the consumer's pyright settings that would hide errors: {'; '.join(dropped)}")
    config |= {
        "include": paths,
        "exclude": EXCLUDE,
        "extraPaths": [*dict.fromkeys([*config.get("extraPaths", []), *paths, *siblings])],
        "pythonVersion": python_version,
        "typeCheckingMode": mode,
    }
    config.pop("venvPath", None)
    config.pop("venv", None)
    if (root / ".venv").is_dir():
        config |= {"venvPath": ".", "venv": ".venv"}
    # pyright takes only paths relative to its config file, so the config goes in the current folder for the run.
    with tempfile.NamedTemporaryFile("w", dir=root, prefix=".pyright-", suffix=".json") as cfg:
        json.dump(config, cfg)
        cfg.flush()
        r = subprocess.run(["pyright", "--outputjson", "-p", cfg.name], capture_output=True, text=True)
    try:
        report = json.loads(r.stdout)
    except json.JSONDecodeError:  # pyright itself failed (not installed, bad config): fail closed
        sys.exit(f"pyright failed:\n{r.stdout}{r.stderr}")
    if r.returncode not in (0, 1):  # 0 no errors, 1 errors; anything else is pyright failing
        sys.exit(f"pyright failed (exit {r.returncode}):\n{r.stdout}{r.stderr}")
    analysed = report.get("summary", {}).get("filesAnalyzed")
    if not isinstance(analysed, int) or analysed == 0:
        sys.exit(f"pyright analysed no Python file in {' '.join(paths)}: nothing was type-checked")
    out = []
    for d in report.get("generalDiagnostics", []):
        if d.get("severity") == "error":
            path = pathlib.Path(d["file"]).resolve().relative_to(root).as_posix()
            out.append({**d, "path": path})
    return out


def _toml(text: str) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        sys.exit("reading [tool.pyright] from pyproject.toml needs Python 3.11 or later (python_version)")
    return tomllib.loads(text)


def consumer_config(root: pathlib.Path) -> dict:
    """The consumer's pyright settings where pyright itself reads them: pyrightconfig.json, else pyproject.toml,
    with any `extends` chain merged in (see resolved)."""
    cfg, pyproject = root / "pyrightconfig.json", root / "pyproject.toml"
    if cfg.is_file():
        return resolved(cfg, root)
    if pyproject.is_file() and re.search(r"^\s*\[\s*tool\s*\.\s*pyright\b", pyproject.read_text(), re.M):
        return resolved(pyproject, root)
    return {}


def _read_config(path: pathlib.Path, root: pathlib.Path) -> dict:
    """One pyright config file: a JSON object, or a .toml file's [tool.pyright] table."""
    name = _shown(path, root)
    if path.suffix == ".toml":
        out = _toml(path.read_text()).get("tool", {}).get("pyright", {})
    else:
        try:  # pyright allows comments and trailing commas here; plain JSON is what we can read
            out = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            sys.exit(f"{name}: can't read it as JSON ({e}); remove comments and trailing commas")
    if not isinstance(out, dict):
        sys.exit(f"{name}: the pyright settings must be a table / object")
    return out


# Settings that name paths, which pyright reads relative to the config file that sets them.
_PATH_LISTS = ("extraPaths", "strict", "include", "exclude", "ignore")
_PATHS = ("stubPath", "typeshedPath", "venvPath")


def _rebased(config: dict, folder: pathlib.Path, root: pathlib.Path) -> dict:
    """config with its relative paths, relative to `folder`, made relative to `root` (where the gate's config goes)."""
    if folder == root:
        return config

    def move(p: object) -> object:
        if not isinstance(p, str) or pathlib.PurePath(p).is_absolute():
            return p
        return pathlib.Path(os.path.relpath(os.path.normpath(folder / p), root)).as_posix()

    out = dict(config)
    for k in _PATH_LISTS:
        if isinstance(out.get(k), list):
            out[k] = [move(p) for p in out[k]]
    for k in _PATHS:
        if k in out:
            out[k] = move(out[k])
    envs = out.get("executionEnvironments")
    if isinstance(envs, list):
        out["executionEnvironments"] = [
            {
                **e,
                **({"root": move(e["root"])} if "root" in e else {}),
                **({"extraPaths": [move(p) for p in e["extraPaths"]]} if isinstance(e.get("extraPaths"), list) else {}),
            }
            if isinstance(e, dict)
            else e
            for e in envs
        ]
    return out


def _shown(path: pathlib.Path, root: pathlib.Path) -> str:
    """path as the consumer would write it, from the folder the check runs in (../ for a file above it)."""
    return pathlib.Path(os.path.relpath(path, root)).as_posix()


def repo_top(root: pathlib.Path) -> pathlib.Path:
    """The git checkout `root` is in (where `extends` may reach), or `root` itself outside one."""
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
    return pathlib.Path(top.stdout.strip()).resolve() if top.returncode == 0 and top.stdout.strip() else root


def resolved(first: pathlib.Path, root: pathlib.Path) -> dict:
    """The settings of `first` merged over those of the files it `extends`, as pyright merges them (a key set in a
    file replaces the base's), with every path made relative to `root`. The gate writes one config without
    `extends`, so a base file can't bring back what the gate drops. A base must be a file inside the repo (the git
    checkout, so a monorepo app's config may extend one above the folder the check runs in), and a chain that comes
    back to a file it has read is refused."""
    repo = repo_top(root)
    layers, seen, path = [], [], first
    while True:
        real = path.resolve()
        if not real.is_relative_to(repo):
            sys.exit(f"pyright config: extends {_shown(path, root)}, which is outside the repo")
        if real in seen:
            chain = " -> ".join(_shown(p, root) for p in [*seen, real])
            sys.exit(f"pyright config: extends makes a cycle: {chain}")
        if not real.is_file():
            sys.exit(f"pyright config: extends {_shown(real, root)}, which is not a file")
        seen.append(real)
        config = _read_config(real, root)
        base = config.pop("extends", None)
        layers.append(_rebased(config, real.parent, root))
        if base is None:
            break
        if not isinstance(base, str) or not base:
            sys.exit(f"{_shown(real, root)}: extends must be a path")
        path = real.parent / base
    out: dict = {}
    for layer in reversed(layers):
        out |= layer
    return out


# A pyright rule set to one of these reports nothing the gate counts (it counts errors only).
_BELOW_ERROR = (False, "none", "information", "warning")
# Settings that stop pyright analysing files the gate was given: `ignore` drops their diagnostics, `exclude` drops
# matching files from `include` (the gate's paths), and unannotated function bodies go unchecked without
# analyzeUnannotatedFunctions. Others skip code or change what it is checked against: `defineConstant` and
# `pythonPlatform` make a branch unreachable (`if DEBUG:`, `if sys.platform == ...`), so pyright reports nothing in it,
# and `stubPath` and `typeshedPath` swap the stubs the code's imports and builtins are typed by. The gate sets the
# Python version itself (--python-version); an execution environment's own pythonVersion or pythonPlatform would
# override it for its files, so those go too.
_HIDING = ("ignore", "exclude", "defineConstant", "pythonPlatform", "stubPath", "typeshedPath")
_ENV_HIDING = ("pythonPlatform", "pythonVersion")
# Switched off, these hide errors too: unannotated function bodies go unchecked, and a dependency without stubs or
# py.typed has no types at all (Unknown), so a wrong use of it reports nothing.
_OFF_HIDING = ("analyzeUnannotatedFunctions", "useLibraryCodeForTypes")


def without_relaxations(config: dict) -> tuple[dict, list[str], bool]:
    """The consumer's settings minus those that would hide errors from the gate, what was dropped, and whether a
    dropped setting was a rule override.

    The gate's floor is pyright's `standard` mode. A consumer may raise it (`strict`, a rule set to "error" or true)
    but not lower it, so every rule override below "error" (false, "none", "information", "warning"), at the top
    level or in an execution environment, is dropped. Dropping it under `strict` would make the rule an error, though
    the consumer relaxed it on purpose (reportMissingTypeStubs = false is common), and knowing which rules `standard`
    already makes errors would mean copying pyright's table here; so a config that relaxes any rule is checked in
    `standard` mode instead (the caller does that), never stricter than the consumer asked and never below the floor.
    """
    out = dict(config)
    dropped = [k for k in _HIDING if out.pop(k, None) is not None]

    def strip(rules: dict, where: str) -> dict:
        kept = {}
        for k, v in rules.items():
            if k.startswith("report") and v in _BELOW_ERROR:
                dropped.append(f"{where}{k} = {json.dumps(v)}")
            elif k in _OFF_HIDING and v is False:
                dropped.append(f"{where}{k} = false")
            else:
                kept[k] = v
        return kept

    out = strip(out, "")
    envs = out.get("executionEnvironments")
    if isinstance(envs, list):
        kept = []
        for i, e in enumerate(envs):
            if isinstance(e, dict):
                where = f"executionEnvironments[{i}]."
                dropped.extend(f"{where}{k}" for k in _ENV_HIDING if k in e)
                e = strip({k: v for k, v in e.items() if k not in _ENV_HIDING}, where)
            kept.append(e)
        out["executionEnvironments"] = kept
    return out, dropped, any("report" in d for d in dropped)


# A comment on its own line that sets pyright's mode or rules for the whole file, and one that lowers them: a mode
# below standard or a rule set below "error".
_FILE_COMMENT = re.compile(r"^\s*#\s*pyright\s*:\s*(.*)$")
_LOWERS = re.compile(r"\b(?:basic|off)\b|=\s*(?:false|none|information|warning)\b")
# Before the first statement, `# type: ignore` makes pyright skip the whole file.
_TYPE_IGNORE = re.compile(r"^\s*#\s*type\s*:\s*ignore\b")


def file_suppressions(paths: list[str], root: pathlib.Path) -> list[str]:
    """`path:line: comment` for every file in the paths whose own comments lower pyright's checks for the whole file.
    They are allowed (like an inline `# type: ignore`, a reviewer sees them in the diff); the gate names them."""
    files: set[pathlib.Path] = set()
    for p in paths:
        top = root / p
        if top.is_file():
            files.add(top)
            continue
        for f in [*top.rglob("*.py"), *top.rglob("*.pyi")]:
            parts = f.relative_to(top).parts[:-1]
            if not any(d.startswith(".") or d in ("node_modules", "__pycache__") for d in parts):
                files.add(f)
    out = []
    for f in sorted(files):
        rel = f.relative_to(root).as_posix()
        leading = True  # still before the first statement
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            stripped = line.strip()
            if leading and _TYPE_IGNORE.match(line):
                out.append(f"{rel}:{n}: {stripped}")
            m = _FILE_COMMENT.match(line)
            if m and _LOWERS.search(m[1]):
                out.append(f"{rel}:{n}: {stripped}")
            if stripped and not stripped.startswith("#"):
                leading = False
    return out


def entries(text: str) -> dict[str, int]:
    out = {}
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) == 2 and not ln.startswith("#"):
            out[parts[0]] = int(parts[1])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--python-version", default="3.12")
    ap.add_argument("paths", nargs="+")
    a = ap.parse_args(argv)
    found = errors(a.paths, a.python_version)
    if notes := file_suppressions(a.paths, pathlib.Path.cwd().resolve()):
        print("These files lower pyright's checks for the whole file (allowed, but check them in review):")
        print("\n".join(f"  {n}" for n in notes))
    now = Counter(d["path"] for d in found)
    path = pathlib.Path(a.baseline)
    allowed = entries(path.read_text()) if path.exists() else {}
    before_text = base_text(a.baseline)
    before = entries(before_text) if before_text is not None else None
    worse = sorted(p for p in now if now[p] > allowed.get(p, 0))
    better = sorted(p for p in allowed if now.get(p, 0) < allowed[p])
    grown = sorted(p for p in allowed if before is not None and allowed[p] > before.get(p, 0))
    if grown:
        print(f"Raised in {a.baseline}, which only goes down (fix the errors instead):\n  " + "\n  ".join(grown))
    for p in worse:
        print(f"{p}: {now[p]} type error(s), {allowed.get(p, 0)} allowed")
        for d in (d for d in found if d["path"] == p):
            line = d["range"]["start"]["line"] + 1
            print(f"  {p}:{line}: {d['message'].splitlines()[0]} ({d.get('rule', 'error')})")
            print(f"::error file={p},line={line}::{d['message'].splitlines()[0]}")
    if better:
        print(f"Fewer type errors now, so lower or remove these in {a.baseline} (it only goes down):")
        print("\n".join(f"  {p} {now[p]}" if now.get(p) else f"  {p} (remove)" for p in better))
    if worse or better or grown:
        return 1
    print(f"types ok: {sum(allowed.values())} error(s) in {len(allowed)} file(s) still on the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
