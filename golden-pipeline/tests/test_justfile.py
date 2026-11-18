"""Tests for the local-commands check (tools/check_justfile.py), the kit's justfile template and the job that runs
the check (gp-repo-checks.yml)."""

import pathlib
import re
import subprocess
import sys

import pytest

GP = pathlib.Path(__file__).resolve().parents[1]
ROOT = GP.parent
TOOL = GP / "tools" / "check_justfile.py"
TEMPLATE = GP / "kit" / "templates" / "justfile"


def run(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], cwd=cwd, capture_output=True, text=True)


FULL = """\
set shell := ["bash", "-c"]
python := "3.12"
export FOO := "a:b"

# all of them
check: lint test

setup:
    uv sync --frozen

@lint:
    ruff check .

[private]
test arg="x:y" *rest: setup
    pytest {{ arg }}
"""


def test_a_full_justfile_passes_in_every_mode(tmp_path):
    (tmp_path / "justfile").write_text(FULL)
    for mode in ("report", "require"):
        r = run(tmp_path, "--mode", mode)
        assert r.returncode == 0, r.stdout
        assert "justfile ok" in r.stdout and "::" not in r.stdout


def test_no_justfile_warns_in_report_and_fails_in_require(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0
    assert r.stdout.startswith("::warning title=justfile::no justfile")
    assert "kit/templates/justfile" in r.stdout
    r = run(tmp_path, "--mode", "require")
    assert r.returncode == 1
    assert r.stdout.startswith("::error title=justfile::no justfile")


def test_missing_recipes_are_named(tmp_path):
    (tmp_path / "Justfile").write_text("setup:\n    uv sync\nlint := 'not a recipe'\ntests:\n    pytest\n")
    r = run(tmp_path)
    assert r.returncode == 0
    assert "::warning title=justfile::Justfile has no lint, test, check recipe(s)" in r.stdout
    assert run(tmp_path, "--mode", "require").returncode == 1


def test_off_skips(tmp_path):
    r = run(tmp_path, "--mode", "off")
    assert r.returncode == 0 and "::" not in r.stdout


def test_an_unknown_mode_fails(tmp_path):
    assert run(tmp_path, "--mode", "warn").returncode == 2


def test_body_lines_comments_and_keywords_are_not_recipes(tmp_path):
    (tmp_path / ".justfile").write_text(
        "# check: commented out\n"
        "set dotenv-load := true\n"
        "export PATH := 'x'\n"
        "mod tools\n"
        "build:\n"
        "    setup: not a recipe, a body line\n"
        "\tlint: nor this\n"
        "    #!/usr/bin/env bash\n"
    )
    r = run(tmp_path, "--mode", "require")
    assert r.returncode == 1
    assert "has no setup, lint, test, check recipe(s)" in r.stdout


def test_aliases_and_imports_count(tmp_path):
    (tmp_path / "justfile").write_text(
        "import 'just/ci.just'\nimport? 'local.just'\nalias check := all\nall: lint test\nsetup:\n    uv sync\n"
    )
    (tmp_path / "just").mkdir()
    (tmp_path / "just/ci.just").write_text("import '../justfile'\nlint:\n    ruff check .\ntest:\n    pytest\n")
    r = run(tmp_path, "--mode", "require")
    assert r.returncode == 0, r.stdout


def test_a_missing_import_is_named(tmp_path):
    (tmp_path / "justfile").write_text("import 'gone.just'\n" + FULL)
    r = run(tmp_path)
    assert r.returncode == 0
    assert "::warning title=justfile::justfile imports gone.just, which is not a file" in r.stdout
    assert run(tmp_path, "--mode", "require").returncode == 1


def test_the_template_and_the_fixture_pass_require():
    for folder in (TEMPLATE.parent, GP / "fixture"):
        r = run(folder, "--mode", "require")
        assert r.returncode == 0, r.stdout


def test_the_fixture_uses_the_template_unchanged():
    assert (GP / "fixture" / "justfile").read_text() == TEMPLATE.read_text()


def _python_ci() -> str:
    return (ROOT / ".github" / "workflows" / "gp-python-ci.yml").read_text()


@pytest.mark.parametrize(
    "fragment",
    [
        # lint: the pinned tools from requirements-ci.txt, hash-checked; ruff check; the format check script
        "uv pip install -q --require-hashes -r",
        "requirements-ci.txt",
        "ruff check",
        'python3 "$TOOLS/check_format.py" --baseline',
        # types: the project from uv.lock, then the types check script on the source and test paths
        "--frozen --python",
        'python3 "$TOOLS/check_types.py" --baseline',
        "--python-version",
        # tests: pytest from uv.lock with the coverage floor, as Cobertura XML
        "uv run --frozen --no-sync pytest -q",
        '--cov=$s"',
        "--cov-report=term-missing",
        "--cov-report=xml:coverage.xml",
        "--cov-fail-under=",
    ],
)
def test_the_template_runs_what_python_ci_runs(fragment):
    assert fragment in _python_ci(), f"{fragment!r} is no longer in gp-python-ci.yml; update the template"
    assert fragment in TEMPLATE.read_text(), f"the template doesn't run {fragment!r} as gp-python-ci.yml does"


def test_the_template_defaults_match_python_ci():
    ci, template = _python_ci(), TEMPLATE.read_text()
    for name, var in [
        ("python_version", "python"),
        ("source_paths", "source_paths"),
        ("test_paths", "test_paths"),
        ("coverage_min", "coverage_min"),
        ("format_baseline", "format_baseline"),
        ("types_baseline", "types_baseline"),
    ]:
        default = re.search(rf"^      {name}:\n(?:        .*\n)*?        default: \"?([^\"\n]*)\"?$", ci, re.M)
        assert default, name
        assert re.search(rf'^{var} := "{re.escape(default[1])}"$', template, re.M), (name, default[1])


def test_the_template_holds_no_tool_version_and_reads_the_pin():
    template = TEMPLATE.read_text()
    assert not re.search(r"\b(?:ruff|pyright)==", template)
    assert "gp-python-ci\\.yml@[0-9a-f]{40}" in template


def test_repo_checks_runs_the_justfile_job_report_only_by_default():
    wf = (ROOT / ".github" / "workflows" / "gp-repo-checks.yml").read_text()
    assert re.search(r'^      justfile:\n(?:        .*\n)*?        default: "report"$', wf, re.M)
    job = wf[wf.index("\n  justfile:\n") :]
    assert "if: inputs.justfile != 'off'" in job
    assert 'check_justfile.py" --mode "$MODE"' in job
    assert 'MODE: "${{ inputs.justfile }}"' in job
