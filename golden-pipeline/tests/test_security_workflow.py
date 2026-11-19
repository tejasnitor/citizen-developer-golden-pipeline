"""Tests for gp-security.yml's restore of the trusted suppressions: on a PR, .semgrepignore, .gitleaks.toml and
.gitleaksignore come from the base commit, and a PR must not be able to aim them somewhere else with a symlink."""

import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "gp-security.yml"


def _step_script(name: str) -> str:
    """The `run: |` block of the step called `name`, dedented."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    pad = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return "\n".join(line[pad:] for line in body) + "\n"


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")
    return _git(repo, "rev-parse", "HEAD")


def _run(step: str, repo: pathlib.Path, tmp_path: pathlib.Path, workdir=".", **env) -> subprocess.CompletedProcess:
    # docker is stubbed: it records its arguments, so the test sees what the scanner would have been given. The
    # gitleaks canary runs are recorded apart and exit CANARY_RC (default 99: gitleaks found the canary), or 0
    # for a kind named in CANARY_MISS.
    # The step runs where the runner would put it: the checkout's working_directory, symlinks followed.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text(
        "#!/bin/sh\n"
        'case "$*" in *"/canary:ro"*)\n'
        f'  printf "%s\\n" "$@" > "{tmp_path}/canary-args"\n'
        '  for k in $CANARY_MISS; do case "$*" in *"gitleaks-canary/$k:"*) exit 0;; esac; done\n'
        '  exit "${CANARY_RC:-99}";;\n'
        "esac\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/docker-args"\n'
    )
    (bin_dir / "docker").chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    full = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_WORKSPACE": str(repo),
        "WORKDIR": workdir,
        "TOOLS": str(ROOT / "golden-pipeline" / "tools"),  # what gp-home gives the step
    }
    full.update(env)
    script = _step_script(step)
    return subprocess.run(["bash", "-c", script], cwd=repo / workdir, capture_output=True, text=True, env=full)


def _semgrep(repo, tmp_path, base, workdir=".") -> subprocess.CompletedProcess:
    return _run("SAST (Semgrep)", repo, tmp_path, workdir, BASE=base, CONFIGS="p/python", EXCLUDES="tests")


def _repo(tmp_path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def test_semgrepignore_comes_from_the_base(tmp_path):
    repo = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / ".semgrepignore").write_text("base.py\n")
    (repo / "sub" / "app.py").write_text("x = 1\n")
    base = _commit(repo)
    (repo / "sub" / ".semgrepignore").write_text("*\n")
    (repo / ".semgrepignore").write_text("*\n")
    _commit(repo)
    r = _semgrep(repo, tmp_path, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / "sub" / ".semgrepignore").read_text() == "base.py\n"
    assert not (repo / ".semgrepignore").exists()


def test_semgrepignore_is_not_written_through_a_symlinked_folder(tmp_path):
    # The base ignores some files in sub/; the PR makes sub a symlink to the root, so restoring sub/.semgrepignore
    # would write the base's ignore list into the root's .semgrepignore and hide the whole project.
    repo = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "sub" / ".semgrepignore").write_text("*\n")
    (repo / "app.py").write_text("x = 1\n")
    base = _commit(repo)
    (repo / "sub" / ".semgrepignore").unlink()
    (repo / "sub").rmdir()
    (repo / "sub").symlink_to(".")
    _commit(repo)
    r = _semgrep(repo, tmp_path, base)
    assert r.returncode != 0
    assert "::error::sub is a symlink" in r.stdout
    assert not (repo / ".semgrepignore").exists()
    assert not (tmp_path / "docker-args").exists()


def test_semgrepignore_is_not_written_outside_the_checkout(tmp_path):
    repo = _repo(tmp_path)
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / ".semgrepignore").write_text("x\n")
    base = _commit(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "a" / "b" / ".semgrepignore").unlink()
    (repo / "a" / "b").rmdir()
    (repo / "a").rmdir()
    (repo / "a").symlink_to(tmp_path)
    (tmp_path / "b").symlink_to(outside)
    _commit(repo)
    r = _semgrep(repo, tmp_path, base)
    assert r.returncode != 0
    assert not (outside / ".semgrepignore").exists()


def test_gitleaks_suppressions_come_from_the_base_and_symlinks_are_not_followed(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    (repo / ".gitleaksignore").write_text("# base\n")
    base = _commit(repo)
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    (repo / ".gitleaks.toml").write_text("[allowlist]\npaths = ['.*']\n")
    (repo / ".gitleaksignore").unlink()
    (repo / ".gitleaksignore").symlink_to(victim)
    head = _commit(repo)
    r = _run("Secret scan of the commits this run adds (gitleaks)", repo, tmp_path, BASE=base, HEAD=head, TRUSTED=base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert victim.read_text() == "untouched\n"
    assert not (repo / ".gitleaks.toml").exists() and not (repo / ".gitleaksignore").is_symlink()
    trust = tmp_path / "runner-temp" / "gitleaks-trust"
    assert (trust / ".gitleaks.toml").read_text() == "[extend]\nuseDefault = true\n"
    assert (trust / ".gitleaksignore").read_text() == "# base\n"
    args = (tmp_path / "docker-args").read_text().splitlines()
    assert args[args.index("--config") + 1] == "/trust/.gitleaks.toml"
    assert args[args.index("--gitleaks-ignore-path") + 1] == "/trust"


def test_semgrepignore_include_reads_the_base_not_the_pr(tmp_path):
    # Semgrep reads an :include'd file from the working tree. With the base's `:include .gitignore`, a PR adding
    # `*.py` to .gitignore would hide every Python file, so the base's .gitignore is written in its place.
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n")
    (repo / ".semgrepignore").write_text(":include .gitignore\nlegacy/\n")
    (repo / "sub").mkdir()
    (repo / "sub" / ".semgrepignore").write_text("  :include ../.gitignore\n:include missing\n:include ../../x\n")
    (repo / "app.py").write_text("x = 1\n")
    base = _commit(repo)
    (repo / ".gitignore").write_text("*.py\n:include sub/evil\n")
    (repo / "sub" / "evil").write_text("*\n")
    _commit(repo)
    r = _semgrep(repo, tmp_path, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / ".semgrepignore").read_text() == "build/\nlegacy/\n"
    assert (repo / "sub" / ".semgrepignore").read_text() == "build/\n"


def test_included_file_cannot_include_again(tmp_path):
    # Included files are plain gitignore syntax to Semgrep; once inlined, their :include lines must not come alive.
    repo = _repo(tmp_path)
    (repo / "extra").write_text("a/\n:include .gitignore\n")
    (repo / ".semgrepignore").write_text(":include extra\n")
    base = _commit(repo)
    (repo / ".gitignore").write_text("*.py\n")
    _commit(repo)
    r = _semgrep(repo, tmp_path, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / ".semgrepignore").read_text() == "a/\n"


def _symlinked_workdir(tmp_path) -> tuple[pathlib.Path, str]:
    # The base has app/ with its .semgrepignore; the PR makes app a symlink to another folder of the repo.
    repo = _repo(tmp_path)
    (repo / "app").mkdir()
    (repo / "app" / ".semgrepignore").write_text("vendor/\n")
    (repo / "elsewhere").mkdir()
    (repo / "elsewhere" / "clean.py").write_text("x = 1\n")
    base = _commit(repo)
    (repo / "app" / ".semgrepignore").unlink()
    (repo / "app").rmdir()
    (repo / "app").symlink_to("elsewhere")
    _commit(repo)
    return repo, base


def test_sast_fails_when_working_directory_is_a_symlink(tmp_path):
    repo, base = _symlinked_workdir(tmp_path)
    r = _semgrep(repo, tmp_path, base, workdir="app")
    assert r.returncode != 0
    assert "::error::app is a symlink" in r.stdout
    assert not (tmp_path / "docker-args").exists()


def test_sast_fails_when_a_folder_above_working_directory_is_a_symlink(tmp_path):
    repo = _repo(tmp_path)
    (repo / "real" / "svc").mkdir(parents=True)
    (repo / "real" / "svc" / "a.py").write_text("x = 1\n")
    (repo / "proj").symlink_to("real")
    base = _commit(repo)
    r = _semgrep(repo, tmp_path, base, workdir="proj/svc")
    assert r.returncode != 0
    assert "::error::proj is a symlink" in r.stdout
    assert not (tmp_path / "docker-args").exists()


def test_sast_runs_in_a_real_working_directory(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app").mkdir()
    (repo / "app" / ".semgrepignore").write_text("vendor/\n")
    base = _commit(repo)
    r = _semgrep(repo, tmp_path, base, workdir="app")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / "app" / ".semgrepignore").read_text() == "vendor/\n"


def test_dependencies_and_image_fail_when_working_directory_is_a_symlink(tmp_path):
    repo, _ = _symlinked_workdir(tmp_path)
    for step in ("Vulnerable dependencies (pip-audit --strict, from uv.lock)", "Image builds"):
        r = _run(step, repo, tmp_path, "app", DOCKERFILE="Dockerfile", PLATFORM="linux/amd64")
        assert r.returncode != 0, step
        assert "::error::app is a symlink" in r.stdout, step
    assert not (tmp_path / "docker-args").exists()


def test_semgrepignore_include_in_a_crlf_file_still_reads_the_base(tmp_path):
    # James's fourth review: with CRLF line ends the \r was read into the :include path, so the file was dropped
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_text("build/\n")
    (repo / ".semgrepignore").write_bytes(b":include .gitignore\r\nlegacy/\r\n")
    base = _commit(repo)
    (repo / ".gitignore").write_text("*.py\n")
    _commit(repo)
    r = _semgrep(repo, tmp_path, base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (repo / ".semgrepignore").read_text() == "build/\nlegacy/\n"


def test_semgrepignore_containment_check_still_works_if_the_symlink_walk_misses(tmp_path):
    # James's fourth review: the "resolves outside" check compared against an empty variable, so it passed anything.
    # The symlink walk stops every real case first, so the walk is made to miss one: dirname is replaced (through
    # BASH_ENV) so that link/. looks like a plain folder at the top of the checkout.
    repo = _repo(tmp_path)
    (repo / "link").mkdir()
    (repo / "link" / ".semgrepignore").write_text("*\n")
    base = _commit(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / "link" / ".semgrepignore").unlink()
    (repo / "link").rmdir()
    (repo / "link").symlink_to(outside)
    _commit(repo)
    env_file = tmp_path / "bash_env"
    env_file.write_text(
        'dirname() { case "$2" in link/.semgrepignore) echo link/. ;; link/.) echo . ;; *) command dirname "$@" ;; '
        "esac; }\n"
    )
    r = _run("SAST (Semgrep)", repo, tmp_path, BASE=base, CONFIGS="p/python", EXCLUDES="", BASH_ENV=str(env_file))
    assert r.returncode != 0
    assert "resolves outside the checkout" in r.stdout
    assert not (outside / ".semgrepignore").exists()


@pytest.mark.parametrize("toml", ['[extend]\npath = "/repo/more.toml"\n', '[Extend]\nPATH = "/repo/more.toml"\n'])
def test_gitleaks_refuses_a_trusted_config_that_extends_another_file(tmp_path, toml):
    # James's fourth review: [extend] path makes gitleaks read a config outside the trusted copy (the PR's, at /repo);
    # gitleaks ignores the case of keys, so the check does too
    repo = _repo(tmp_path)
    (repo / ".gitleaks.toml").write_text(toml)
    base = _commit(repo)
    r = _run("Secret scan of the commits this run adds (gitleaks)", repo, tmp_path, BASE=base, HEAD=base, TRUSTED=base)
    assert r.returncode != 0
    assert "extends with path" in (r.stdout + r.stderr).lower()
    assert not (tmp_path / "docker-args").exists()


@pytest.mark.parametrize(
    "toml",
    [
        '[extend]\ndisabledRules = ["generic-api-key"]\n',  # James reproduced this: zero rules, every scan passes
        "[EXTEND]\nUseDefault = false\n",
        '[allowlist]\npaths = ["docs/"]\n',
        '[extend]\nuseDefault = "yes"\n',
    ],
)
def test_gitleaks_refuses_a_trusted_config_that_loads_no_rules(tmp_path, toml):
    # James's approval of #14: without useDefault = true and with no [[rules]] of its own, gitleaks loads zero rules
    repo = _repo(tmp_path)
    (repo / ".gitleaks.toml").write_text(toml)
    base = _commit(repo)
    r = _run("Secret scan of the commits this run adds (gitleaks)", repo, tmp_path, BASE=base, HEAD=base, TRUSTED=base)
    assert r.returncode != 0
    assert "loads no rules" in r.stdout + r.stderr
    assert not (tmp_path / "docker-args").exists()


@pytest.mark.parametrize(
    "toml",
    [
        '[Extend]\nUSEDEFAULT = true\ndisabledRules = ["generic-api-key"]\n',
        '[[Rules]]\nid = "ours"\nregex = "ours-[0-9]{8}"\n',
        '[extend]\nuseDefault = "true"\n',  # viper reads the string as true, so gitleaks loads its defaults
    ],
)
def test_gitleaks_runs_with_default_or_own_rules(tmp_path, toml):
    repo = _repo(tmp_path)
    (repo / ".gitleaks.toml").write_text(toml)
    base = _commit(repo)
    r = _run("Secret scan of the commits this run adds (gitleaks)", repo, tmp_path, BASE=base, HEAD=base, TRUSTED=base)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "docker-args").exists()


def _gitleaks(tmp_path, toml, **env) -> subprocess.CompletedProcess:
    repo = _repo(tmp_path)
    (repo / ".gitleaks.toml").write_text(toml)
    base = _commit(repo)
    step = "Secret scan of the commits this run adds (gitleaks)"
    return _run(step, repo, tmp_path, BASE=base, HEAD=base, TRUSTED=base, **env)


def test_gitleaks_canaries_run_the_trusted_config_on_three_kinds_of_secret(tmp_path):
    r = _gitleaks(tmp_path, "[extend]\nuseDefault = true\n")
    assert r.returncode == 0, r.stdout + r.stderr
    args = (tmp_path / "canary-args").read_text().splitlines()  # the last canary run
    assert args[args.index("--config") + 1] == "/trust/.gitleaks.toml"
    assert args[args.index("--source") + 1] == "." and args[args.index("-w") + 1] == "/canary" and "--no-git" in args
    canary = tmp_path / "runner-temp" / "gitleaks-canary"
    files = {k: (canary / k / "src/app/settings.py").read_text() for k in ("github-token", "aws-key", "private-key")}
    assert files["github-token"].startswith('GITHUB_TOKEN = "ghp_')
    assert files["aws-key"].startswith('AWS_ACCESS_KEY_ID = "AKIA')
    assert "BEGIN RSA PRIVATE KEY" in files["private-key"]
    again = tmp_path / "again"
    again.mkdir()
    _gitleaks(again, "[extend]\nuseDefault = true\n")
    token = (again / "runner-temp/gitleaks-canary/github-token/src/app/settings.py").read_text()
    assert token != files["github-token"]  # random each run, so an allowlist can't name it
    assert (tmp_path / "docker-args").exists()  # then the real scan


ALL = "github-token aws-key private-key"


@pytest.mark.parametrize(
    ("toml", "missed"),
    [
        # #18's review and #20's: each reads as a valid config; real gitleaks 8.18.4 misses these canaries (by hand)
        ('[extend]\nuseDefault = true\n[allowlist]\npaths = [".*"]\n', ALL),
        ("[extend]\nuseDefault = true\n[allowlist]\npaths = ['''^[a-zA-Z]''']\n", ALL),
        ("[extend]\nuseDefault = true\n[allowlist]\npaths = ['''\\.(py|js)$''']\n", ALL),
        ("[[rules]]\nid = \"github-pat\"\nregex = '''ghp_[0-9a-zA-Z]{36}'''\n", "aws-key private-key"),
    ],
)
def test_gitleaks_refuses_a_config_that_misses_a_canary(tmp_path, toml, missed):
    r = _gitleaks(tmp_path, toml, CANARY_MISS=missed)
    assert r.returncode != 0
    assert f"doesn't catch a test {missed} in src/app/settings.py" in r.stdout + r.stderr
    assert not (tmp_path / "docker-args").exists()


@pytest.mark.parametrize("key", ["regexes = ['''[A-Z]''']", "StopWords = ['token']"])
def test_gitleaks_refuses_a_global_regex_or_stopword_allowlist(tmp_path, key):
    # #20's review: [allowlist] regexes = ['[A-Z]'] exempts nearly every real token from every rule
    r = _gitleaks(tmp_path, f"[extend]\nuseDefault = true\n[Allowlist]\n{key}\n")
    assert r.returncode != 0
    assert "global [allowlist]" in r.stdout + r.stderr
    assert not (tmp_path / "canary-args").exists() and not (tmp_path / "docker-args").exists()


def test_gitleaks_stops_when_a_canary_run_errors(tmp_path):
    r = _gitleaks(tmp_path, "[extend]\nuseDefault = true\n", CANARY_RC="1")
    assert r.returncode != 0
    assert "failed on the github-token canary (exit 1)" in r.stdout + r.stderr
    assert not (tmp_path / "docker-args").exists()
