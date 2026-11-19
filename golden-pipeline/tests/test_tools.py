"""Tests for the golden-pipeline tools. Each check runs as CI runs it: a script in a working folder.

Taken from citizen-developer's tools/tests/test_tools.py with the tools, and changed where the tools were made
repo-agnostic (baseline and denylist paths are inputs, check_repo no longer knows about apps/).
"""

import json
import os
import pathlib
import subprocess
import sys

TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
GP = ".github/golden-pipeline"


def run(tool: str, cwd: pathlib.Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    env = (
        env
        if env is not None
        else {k: v for k, v in os.environ.items() if k not in ("GITHUB_BASE_REF", "BASELINE_BASE_REF")}
    )
    return subprocess.run([sys.executable, str(TOOLS / tool), *args], cwd=cwd, capture_output=True, text=True, env=env)


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True
    )


PINNED_CI = f"jobs:\n  x:\n    steps:\n      - uses: actions/checkout@{'c' * 40} # v4.4.0\n"


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/x.py").write_text("x = 1\n")
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/ci.yml").write_text(PINNED_CI)
    return tmp_path


def _add(repo):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)


def test_repo_ok(tmp_path):
    repo = _repo(tmp_path)
    _add(repo)
    assert run("check_repo.py", repo).returncode == 0


def test_repo_rejects_secrets_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".env").write_text("TOKEN=x\n")
    (repo / "deploy.pem").write_text("x\n")
    (repo / ".env.example").write_text("TOKEN=\n")
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    assert ".env:" in r.stdout and "deploy.pem" in r.stdout and ".env.example" not in r.stdout


def test_repo_requires_pinned_actions(tmp_path):
    repo = _repo(tmp_path)
    wf = repo / ".github/workflows"
    sha = "a" * 40
    (wf / "ok.yml").write_text(
        f"jobs:\n  x:\n    steps:\n      - uses: actions/checkout@{sha} # v4.4.0\n"
        "      - uses: ./.github/actions/local\n"
        f"  y:\n    uses: org/home/.github/workflows/gp-python-ci.yml@{sha} # golden-pipeline-v1\n"
        f"  z:\n    uses: org/home/.github/workflows/gp-security.yml@{sha} # golden-pipeline-v1.2.0\n"
    )
    (wf / "bad.yml").write_text(
        "jobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n"
        f"      - uses: actions/setup-python@{sha}\n"
        f"      - uses: actions/cache@v4 # was @{sha} # v4.1.0\n"
        f"      - uses: actions/cache@{sha} # latest\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    for n in (4, 5, 6, 7):
        assert f"bad.yml:{n}" in r.stdout
    assert "ok.yml" not in r.stdout


def _title(title: str, author: str = "someone") -> subprocess.CompletedProcess:
    env = {**os.environ, "PR_TITLE": title, "PR_AUTHOR": author}
    return subprocess.run([sys.executable, str(TOOLS / "check_pr_title.py")], capture_output=True, text=True, env=env)


def test_pr_title_accepts_the_standard():
    for t in [
        "feat(recruit-kit): rubric-scored hiring pipeline app",
        "ci: Kiro auto-fix for blocking review findings",
        "fix(front)!: drop the old sign-in route",
        "chore(develop): release 1.2.0",
        'Revert "feat: x"',
        "docs: " + "x" * 66,
        'Revert "docs: ' + "x" * 66 + '"',
    ]:
        assert _title(t).returncode == 0, t


def test_pr_title_rejects_free_text_bad_scope_and_long_titles():
    for t in [
        "Update sign_in.html",
        "feature: add x",
        "fix(Recruit Kit): x",
        "fix:no space",
        "",
        "docs: " + "x" * 70,
        'Revert "docs: ' + "x" * 67 + '"',
    ]:
        assert _title(t).returncode == 1, t


def test_pr_title_lets_dependabot_run_long_but_not_off_grammar():
    long = "ci: bump SonarSource/sonarqube-scan-action from 8.4.0 to 8.4.1 in the actions group"
    assert len(long) > 72
    assert _title(long, "dependabot[bot]").returncode == 0
    assert _title(long, "gauravpcu").returncode == 1
    assert _title("Bump x from 1 to 2 in the actions group", "dependabot[bot]").returncode == 1


def _confidential_repo(tmp_path):
    (tmp_path / GP).mkdir(parents=True)
    (tmp_path / GP / "confidential-denylist.txt").write_text("# list\n\\bproject bluebird\\b\n")
    (tmp_path / "a.md").write_text("hello\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")


def test_confidential_scans_the_tree(tmp_path):
    _confidential_repo(tmp_path)
    assert run("check_confidential.py", tmp_path).returncode == 0  # the list itself is never a finding
    (tmp_path / "a.md").write_text("About Project Bluebird\n")
    _git(tmp_path, "add", "-A")
    r = run("check_confidential.py", tmp_path)
    assert r.returncode == 1 and "a.md:1" in r.stdout


def test_confidential_catches_content_added_then_removed_in_the_range(tmp_path):
    _confidential_repo(tmp_path)
    for text in ("project bluebird notes\n", "hello\n"):
        (tmp_path / "a.md").write_text(text)
        _git(tmp_path, "commit", "-qam", "edit")
    assert run("check_confidential.py", tmp_path).returncode == 0  # the tree is clean again
    r = subprocess.run(
        [sys.executable, str(TOOLS / "check_confidential.py"), "--range", "HEAD~2..HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1 and "commit " in r.stdout


def _confidential(cwd, *args):
    return subprocess.run(
        [sys.executable, str(TOOLS / "check_confidential.py"), *args], cwd=cwd, capture_output=True, text=True
    )


def test_confidential_scans_text_that_is_not_utf8(tmp_path):
    _confidential_repo(tmp_path)
    (tmp_path / "a.md").write_bytes(b"Caf\xe9 \x96 Project Bluebird\n")  # Windows-1252
    r = _confidential(tmp_path)
    assert r.returncode == 1 and "a.md:1" in r.stdout


def test_confidential_accepts_a_listed_hit_only_once_merged(tmp_path):
    _confidential_repo(tmp_path)
    shas = []
    for text in ("project bluebird notes\n", "hello\n"):
        (tmp_path / "a.md").write_text(text)
        _git(tmp_path, "commit", "-qam", "edit")
        shas.append(
            subprocess.run(
                ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            ).stdout.strip()
        )
    (tmp_path / GP / "confidential-accepted.txt").write_text(f"# accepted\n{shas[0]} \\bproject bluebird\\b\n")
    assert _confidential(tmp_path, "--range", "HEAD~2..HEAD").returncode == 1  # not on develop or main yet
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    assert _confidential(tmp_path, "--range", "HEAD~2..HEAD").returncode == 0  # merged and listed
    (tmp_path / "a.md").write_text("project bluebird is back\n")
    assert _confidential(tmp_path, "--range", "HEAD~2..HEAD").returncode == 1  # the tree is never accepted


def test_confidential_range_takes_only_commit_names(tmp_path):
    _confidential_repo(tmp_path)
    for bad in ("--output=x", "HEAD", "a..b c", "a....b"):
        assert _confidential(tmp_path, "--range", bad).returncode != 0
    assert not (tmp_path / "x").exists()


def test_codeowners_fails_closed_and_reports_errors():
    def check(answer: str) -> int:
        return subprocess.run(
            [sys.executable, str(TOOLS / "check_codeowners.py")], input=answer, capture_output=True, text=True
        ).returncode

    assert check('{"errors": []}') == 0
    assert check('{"errors": [{"line": 4, "path": ".github/CODEOWNERS", "message": "Unknown owner @nobody"}]}') == 1
    for broken in ("", "not json", '{"message": "Not Found"}', '{"errors": null}'):  # never passes by default
        assert check(broken) != 0


def _commits(repo, base, head, grandfathered="") -> subprocess.CompletedProcess:
    """grandfathered: the path, relative to the repo root, of the list the check is told to read."""
    env = {k: v for k, v in os.environ.items() if k != "COMMITS_GRANDFATHERED"}
    if grandfathered:
        env["COMMITS_GRANDFATHERED"] = grandfathered
    return subprocess.run(
        [sys.executable, str(TOOLS / "check_commits.py"), base, head], capture_output=True, text=True, env=env, cwd=repo
    )


def _commit(repo, subject):
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_commits_checks_each_commit_in_the_range(tmp_path):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, "chore: start")
    _commit(tmp_path, "feat(app): one")
    _commit(tmp_path, 'Revert "feat(app): one"')
    ok = _commit(tmp_path, "fix: two")
    assert _commits(tmp_path, base, ok).returncode == 0
    bad = _commit(tmp_path, "Update sign_in.html")
    r = _commits(tmp_path, base, bad)
    assert r.returncode == 1 and bad[:10] in r.stdout


def test_commits_ignores_the_squash_suffix(tmp_path):
    _git(tmp_path, "init", "-q")
    base = _commit(tmp_path, "chore: start")
    _commit(tmp_path, "docs: " + "x" * 66 + " (#22)")  # a 72-character PR title, squash-merged
    ok = _commit(tmp_path, 'Revert "docs: ' + "x" * 66 + '" (#23)')
    assert _commits(tmp_path, base, ok).returncode == 0
    bad = _commit(tmp_path, "docs: " + "x" * 67 + " (#24)")  # the title itself is over 72
    r = _commits(tmp_path, base, bad)
    assert r.returncode == 1 and bad[:10] in r.stdout


def _list(repo, *lines, message="ci: grandfather old commits"):
    (repo / "gf.txt").write_text("# test\n" + "".join(f"{ln}\n" for ln in lines))
    _git(repo, "add", "gf.txt")
    return _commit(repo, message)


def test_commits_skips_merges_and_grandfathered_history(tmp_path):
    # The first release PR after adoption: develop (the PR head) has old commits and the list; main (BASE) has neither.
    _git(tmp_path, "init", "-q", "-b", "develop")
    base = _commit(tmp_path, "chore: start")
    old = _commit(tmp_path, "Update heading")
    _git(tmp_path, "checkout", "-q", "-b", "side", base)
    _commit(tmp_path, "docs: side")
    _git(tmp_path, "checkout", "-q", "develop")
    _git(tmp_path, "merge", "-q", "--no-ff", "-m", "Merge branch 'side'", "side")
    _list(tmp_path, old)
    head = _commit(tmp_path, "ci: after")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", head)
    assert _commits(tmp_path, base, head).returncode == 1
    r = _commits(tmp_path, base, head, grandfathered="gf.txt")
    assert r.returncode == 0
    assert "as on origin/develop" in r.stdout


def test_commits_a_pr_cant_grandfather_its_own_commits(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "develop")
    base = _commit(tmp_path, "chore: start")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", base)
    bad = _commit(tmp_path, "wip stuff")
    head = _list(tmp_path, bad)  # the PR adds the list naming its own commit
    r = _commits(tmp_path, base, head, grandfathered="gf.txt")
    assert r.returncode == 1
    assert bad[:10] in r.stdout
    assert "isn't merged yet" in r.stdout


def test_commits_grandfathers_only_merged_full_shas_from_the_base(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "develop")
    start = _commit(tmp_path, "chore: start")
    for entry, why in [("HEAD", "not a full"), (start[:12], "not a full"), ("1" * 40, "isn't in this clone")]:
        _git(tmp_path, "checkout", "-q", "--detach", start)
        base = _list(tmp_path, entry)
        head = _commit(tmp_path, "wip stuff")
        r = _commits(tmp_path, base, head, grandfathered="gf.txt")
        assert r.returncode != 0, entry
        assert why in r.stderr, entry
    # a full SHA of a commit only the PR has, listed on BASE by a later rebase of the list: still not merged
    _git(tmp_path, "checkout", "-q", "--detach", start)
    head = _commit(tmp_path, "wip stuff")
    _git(tmp_path, "checkout", "-q", "--detach", start)
    base = _list(tmp_path, head)
    r = _commits(tmp_path, base, head, grandfathered="gf.txt")
    assert r.returncode != 0
    assert "isn't merged" in r.stderr
    # the base's list wins over the PR's copy: a PR editing it changes nothing
    _git(tmp_path, "checkout", "-q", "--detach", start)
    base = _list(tmp_path, start)
    bad = _commit(tmp_path, "wip stuff")
    head = _list(tmp_path, start, bad, message="ci: list more")
    r = _commits(tmp_path, base, head, grandfathered="gf.txt")
    assert r.returncode == 1
    assert bad[:10] in r.stdout


def test_commits_fails_closed(tmp_path):
    _git(tmp_path, "init", "-q")
    head = _commit(tmp_path, "chore: start")
    assert _commits(tmp_path, "0" * 40, head).returncode != 0
    base = _list(tmp_path, "1" * 40)
    assert _commits(tmp_path, base, base, grandfathered="gf.txt").returncode != 0
    assert _commits(tmp_path, "--output=x", head).returncode != 0  # an option, not a commit
    assert not (tmp_path / "x").exists()


def _target(base, head, author="gauravpcu") -> subprocess.CompletedProcess:
    repo = "Procurementpartners/askme"
    env = {**os.environ, "BASE_REF": base, "HEAD_REF": head, "PR_AUTHOR": author, "BASE_REPO": repo, "HEAD_REPO": repo}
    return subprocess.run(
        [sys.executable, str(TOOLS / "check_branch_target.py")], capture_output=True, text=True, env=env
    )


def test_branch_target_into_main():
    for head in ["develop", "release/askme-0.3.0", "hotfix/ppcd-0.2.1"]:
        assert _target("main", head).returncode == 0, head
    assert _target("main", "release-please--branches--main--components--askme", "pp-release-bot[bot]").returncode == 0
    for head, author in [
        ("feat/x", "gauravpcu"),
        ("back-merge/main", "pp-release-bot[bot]"),
        ("release-please--branches--main--components--askme", "gauravpcu"),
    ]:
        assert _target("main", head, author).returncode == 1, head


def test_branch_target_other_bases_pass():
    for base, head in [("develop", "feat/x"), ("develop", "back-merge/main"), ("feat/x", "fix/y")]:
        assert _target(base, head).returncode == 0
    assert _target("", "feat/x").returncode != 0


def test_repo_requires_pinned_hooks(tmp_path):
    repo = _repo(tmp_path)
    sha = "b" * 40
    (repo / ".pre-commit-config.yaml").write_text(
        f"repos:\n  - repo: https://github.com/x/ok\n    rev: {sha}  # v8.18.4, same as CI\n"
        "  - repo: https://github.com/x/tag\n    rev: v8.18.4\n"
        f"  - repo: https://github.com/x/nover\n    rev: {sha}\n  - repo: local\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    assert ".pre-commit-config.yaml:5" in r.stdout and ".pre-commit-config.yaml:7" in r.stdout
    assert ".pre-commit-config.yaml:3" not in r.stdout


def test_repo_requires_pinned_hooks_in_any_layout(tmp_path):
    repo = _repo(tmp_path)
    sha = "b" * 40
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - rev: v8.18.4\n    repo: https://github.com/x/revfirst\n"
        "  - {repo: https://github.com/x/flow, rev: v1.2.3, hooks: [{id: hook}]}\n"
        f"  - {{repo: https://github.com/x/flowok, rev: {sha}, hooks: [{{id: hook}}]}}  # v1.2.3\n"
        f"  - rev: {sha}  # v1.2.3\n    repo: https://github.com/x/revfirstok\n"
        "  # an old entry: {rev: main}\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    assert ".pre-commit-config.yaml:2" in r.stdout and ".pre-commit-config.yaml:4" in r.stdout
    for n in (5, 6, 8):
        assert f".pre-commit-config.yaml:{n}" not in r.stdout


def test_commits_named_grandfathered_file_must_exist(tmp_path):
    _git(tmp_path, "init", "-q")
    head = _commit(tmp_path, "chore: start")
    env = {**os.environ, "COMMITS_GRANDFATHERED": "missing.txt"}  # on no commit and not in the tree
    r = run("check_commits.py", tmp_path, head, head, env=env)
    assert r.returncode != 0


def test_format_is_shrink_only(tmp_path):
    (tmp_path / GP).mkdir(parents=True)
    base = tmp_path / GP / "format-baseline.txt"
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "old.py").write_text("x=1\n")
    base.write_text("# header\nold.py\n")
    assert run("check_format.py", tmp_path).returncode == 0  # unformatted, but on the baseline
    (tmp_path / "new.py").write_text("y=2\n")
    r = run("check_format.py", tmp_path)
    assert r.returncode == 1 and "new.py" in r.stdout  # a file not on the baseline must be formatted
    (tmp_path / "new.py").write_text("y = 2\n")
    (tmp_path / "old.py").write_text("x = 1\n")
    r = run("check_format.py", tmp_path)
    assert r.returncode == 1 and "remove from" in r.stdout and "old.py" in r.stdout
    base.write_text("# header\n")
    assert run("check_format.py", tmp_path).returncode == 0
    base.unlink()
    assert run("check_format.py", tmp_path).returncode == 0  # no baseline: everything must be formatted, and is


def test_format_baseline_cannot_grow_in_a_pr(tmp_path):
    sub = tmp_path / "pkg"  # run from a subfolder, as a monorepo's working_directory does
    (sub / "base").mkdir(parents=True)
    base = sub / "base/format.txt"
    (sub / "old.py").write_text("x=1\n")
    base.write_text("old.py\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    (sub / "new.py").write_text("y=2\n")
    base.write_text("old.py\nnew.py\n")  # the PR lists its own unformatted file
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "pr")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_BASE_REF", "BASELINE_BASE_REF")}
    r = run("check_format.py", sub, "--baseline", "base/format.txt", env={**env, "GITHUB_BASE_REF": "develop"})
    assert r.returncode == 1 and "only shrinks" in r.stdout and "new.py" in r.stdout
    assert run("check_format.py", sub, "--baseline", "base/format.txt", env=env).returncode == 0  # not a PR


def _types_repo(tmp_path, code):
    (tmp_path / GP).mkdir(parents=True)
    (tmp_path / "src/demo").mkdir(parents=True)
    (tmp_path / "src/demo/a.py").write_text(code)
    return tmp_path / GP / "types-baseline.txt"


def test_types_are_shrink_only(tmp_path):
    base = _types_repo(tmp_path, 'x: int = "one"\n')
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 1 and "src/demo/a.py: 1 type error(s), 0 allowed" in r.stdout  # a new error fails
    assert "src/demo/a.py:1:" in r.stdout  # and is shown
    base.write_text("# header\nsrc/demo/a.py 1\n")
    assert run("check_types.py", tmp_path, "src").returncode == 0  # on the baseline
    (tmp_path / "src/demo/a.py").write_text("x: int = 1\n")
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 1 and "src/demo/a.py (remove)" in r.stdout  # fixed: lower the count
    base.write_text("# header\n")
    assert run("check_types.py", tmp_path, "src").returncode == 0
    assert run("check_types.py", tmp_path, "nope").returncode != 0  # no such path


def test_types_resolve_sibling_imports_in_tests_and_scripts(tmp_path):
    _types_repo(tmp_path, "x = 1\n")
    for d in ("tests", "scripts"):
        (tmp_path / d).mkdir()
        (tmp_path / f"{d}/helper_{d}.py").write_text("VALUE: int = 1\n")
        (tmp_path / f"{d}/use.py").write_text(f"from helper_{d} import VALUE\n\ny: int = VALUE\n")
    r = run("check_types.py", tmp_path, "src", "tests", "scripts")
    assert r.returncode == 0, r.stdout


def test_types_baseline_cannot_grow_in_a_pr(tmp_path):
    base = _types_repo(tmp_path, 'x: int = "one"\n')
    base.write_text("src/demo/a.py 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    (tmp_path / "src/demo/a.py").write_text('x: int = "one"\ny: str = 2\n')
    base.write_text("src/demo/a.py 2\n")  # the PR raises its count
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "pr")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_BASE_REF", "BASELINE_BASE_REF")}
    r = run("check_types.py", tmp_path, "src", env={**env, "GITHUB_BASE_REF": "develop"})
    assert r.returncode == 1 and "only goes down" in r.stdout and "src/demo/a.py" in r.stdout
    assert run("check_types.py", tmp_path, "src", env=env).returncode == 0  # not a PR: no base
    r = run("check_types.py", tmp_path, "src", env={**env, "BASELINE_BASE_REF": "develop"})
    assert r.returncode == 1 and "only goes down" in r.stdout  # a run started by hand


def test_confidential_takes_the_consumers_denylist_path(tmp_path):
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy/deny.txt").write_text("\\bproject bluebird\\b\n")
    (tmp_path / "a.md").write_text("Project Bluebird\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    r = run("check_confidential.py", tmp_path, "--denylist", "policy/deny.txt")
    assert r.returncode == 1 and "a.md:1" in r.stdout and "deny.txt" not in r.stdout.split(":", 1)[1]
    assert run("check_confidential.py", tmp_path).returncode != 0  # the default list is missing: fail closed


def _home_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "golden-pipeline/tools").mkdir(parents=True)
    (tmp_path / ".github/actions/gp-home").mkdir(parents=True)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "golden-pipeline/tools/t.py").write_text("x = 1\n")
    (tmp_path / ".github/actions/gp-home/action.yml").write_text("name: x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "tools")
    return subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_home_pin_follows_the_tools(tmp_path):
    sha = _home_repo(tmp_path)
    wf = tmp_path / ".github/workflows/gp-a.yml"
    use = "      - uses: org/home/.github/actions/gp-home@{} # golden-pipeline-v1 tools\n"
    wf.write_text("steps:\n" + use.format(sha) + use.format(sha))
    assert run("check_home_pin.py", tmp_path).returncode == 0
    (tmp_path / ".github/workflows/gp-b.yml").write_text("steps:\n" + use.format("f" * 40))
    assert run("check_home_pin.py", tmp_path).returncode == 1  # two different pins
    (tmp_path / ".github/workflows/gp-b.yml").unlink()
    (tmp_path / "golden-pipeline/tools/t.py").write_text("x = 2\n")
    _git(tmp_path, "commit", "-qam", "change a tool")
    r = run("check_home_pin.py", tmp_path)
    assert r.returncode == 1 and "--fix" in r.stdout  # the pinned commit has older tools
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert run("check_home_pin.py", tmp_path, "--fix", head).returncode == 0
    assert head in wf.read_text() and sha not in wf.read_text()


def test_repo_checks_the_whole_repo_from_a_subfolder(tmp_path):
    repo = _repo(tmp_path)  # a monorepo: the caller's working_directory is apps/foo
    (repo / ".github/workflows/other.yml").write_text("jobs:\n  x:\n    steps:\n      - uses: actions/checkout@v4\n")
    (repo / "apps/foo").mkdir(parents=True)
    (repo / "apps/foo/x.py").write_text("x = 1\n")
    (repo / "deploy.pem").write_text("x\n")
    _add(repo)
    r = run("check_repo.py", repo / "apps/foo")
    assert r.returncode == 1
    assert ".github/workflows/other.yml:4" in r.stdout
    assert "deploy.pem" in r.stdout


def test_repo_fails_with_no_workflows(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".github/workflows/ci.yml").unlink()
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    assert "no workflows" in r.stdout


def test_repo_requires_pinned_flow_steps_and_docker_digests(tmp_path):
    repo = _repo(tmp_path)
    sha, digest = "a" * 40, "d" * 64
    (repo / ".github/workflows/flow.yml").write_text(
        "jobs:\n  x:\n    steps:\n"
        "      - { uses: actions/checkout@v4, with: { fetch-depth: 0 } }\n"
        f"      - {{ uses: actions/checkout@{sha}, with: {{ fetch-depth: 0 }} }}  # v4.4.0\n"
        f"      - {{ name: co, uses: 'actions/checkout@{sha}' }} # v4.4.0\n"
        "      - uses: docker://alpine:3.20\n"
        f"      - uses: docker://alpine:3.20@sha256:{digest}\n"
        "      - { uses: docker://alpine:latest }\n"
        f"      - {{ name: co, uses: actions/checkout@{sha} }}\n"
        "      - uses:\n          actions/checkout@v4\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    for n in (4, 7, 9, 10, 11):
        assert f"flow.yml:{n}:" in r.stdout, n
    for n in (5, 6, 8):
        assert f"flow.yml:{n}:" not in r.stdout, n


def test_confidential_fails_with_no_patterns(tmp_path):
    _confidential_repo(tmp_path)
    for text in ("", "# none yet\n", "  # an indented comment\n\n"):
        (tmp_path / GP / "confidential-denylist.txt").write_text(text)
        r = run("check_confidential.py", tmp_path)
        assert r.returncode != 0, repr(text)
        assert "no patterns" in r.stderr, repr(text)


def test_confidential_indented_comment_is_a_comment(tmp_path):
    _confidential_repo(tmp_path)
    (tmp_path / GP / "confidential-denylist.txt").write_text("  # note\n\\bproject bluebird\\b\n")
    (tmp_path / "a.md").write_text("# note\n")
    assert run("check_confidential.py", tmp_path).returncode == 0


def test_confidential_a_pr_cant_loosen_its_denylist(tmp_path):
    _confidential_repo(tmp_path)  # the base commit has the pattern
    (tmp_path / GP / "confidential-denylist.txt").write_text("\\bsomething else\\b\n")  # the PR drops it
    (tmp_path / "a.md").write_text("project bluebird notes\n")  # and adds content it would catch
    _git(tmp_path, "commit", "-qam", "pr")
    r = _confidential(tmp_path, "--range", "HEAD~1..HEAD")
    assert r.returncode == 1
    assert "a.md:1" in r.stdout
    assert _confidential(tmp_path).returncode == 0  # no range (a push without a before): this checkout's list


def test_confidential_a_pr_can_add_the_denylist_and_patterns(tmp_path):
    (tmp_path / "a.md").write_text("hello\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")  # no denylist yet: the PR adopts the check
    (tmp_path / GP).mkdir(parents=True)
    (tmp_path / GP / "confidential-denylist.txt").write_text("\\bproject bluebird\\b\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "adopt")
    assert _confidential(tmp_path, "--range", "HEAD~1..HEAD").returncode == 0
    (tmp_path / GP / "confidential-denylist.txt").write_text("\\bproject bluebird\\b\n\\bproject heron\\b\n")
    (tmp_path / "a.md").write_text("project heron\n")
    _git(tmp_path, "commit", "-qam", "more")
    assert _confidential(tmp_path, "--range", "HEAD~1..HEAD").returncode == 1  # an added pattern applies at once


def test_types_fail_when_nothing_was_analysed(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/README.txt").write_text("no python here\n")
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode != 0
    assert "analysed no Python file" in r.stderr


def test_types_fail_when_pyright_fails(tmp_path):
    _types_repo(tmp_path, "x = 1\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "pyright").write_text(
        '#!/bin/sh\necho \'{"generalDiagnostics": [], "summary": {"filesAnalyzed": 1}}\'\nexit 3\n'
    )
    (fake / "pyright").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_BASE_REF", "BASELINE_BASE_REF")}
    r = run("check_types.py", tmp_path, "src", env={**env, "PATH": f"{fake}{os.pathsep}{env['PATH']}"})
    assert r.returncode != 0
    assert "exit 3" in r.stderr


def test_types_apply_the_consumers_pyright_settings(tmp_path):
    _types_repo(tmp_path, 'x: int = "one"\n')
    (tmp_path / "pyproject.toml").write_text('[tool.pyright]\nreportMissingImports = "error"\n')
    (tmp_path / "src/demo/b.py").write_text("import not_a_module_anywhere\n")
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 1 and "src/demo/b.py: 1 type error" in r.stdout  # the consumer's own settings apply
    (tmp_path / "src/demo/b.py").unlink()
    (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode": "strict"}')  # pyright prefers this file
    (tmp_path / "src/demo/a.py").write_text("def f(x):\n    return x\n")
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 1
    assert "reportMissingParameterType" in r.stdout  # strict applies
    (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode": "off", "include": ["nowhere"]}')
    (tmp_path / "src/demo/a.py").write_text('x: int = "one"\n')
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 1
    assert "src/demo/a.py: 1 type error" in r.stdout  # never below standard; our include


def test_baseline_path_is_normalised_against_the_base(tmp_path):
    sub = tmp_path / "pkg"
    (sub / "base").mkdir(parents=True)
    (sub / "old.py").write_text("x=1\n")
    (sub / "base/format.txt").write_text("old.py\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    (sub / "new.py").write_text("y=2\n")
    (sub / "base/format.txt").write_text("old.py\nnew.py\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "pr")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_BASE_REF", "BASELINE_BASE_REF")}
    r = run(
        "check_format.py", sub, "--baseline", "./base/../base/format.txt", env={**env, "GITHUB_BASE_REF": "develop"}
    )
    assert r.returncode == 1
    assert "only shrinks" in r.stdout
    assert "new.py" in r.stdout


def test_baseline_base_fails_closed_unless_the_file_is_absent(tmp_path):
    (tmp_path / "old.py").write_text("x=1\n")
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_BASE_REF", "BASELINE_BASE_REF")}
    pr = {**env, "GITHUB_BASE_REF": "develop"}
    (tmp_path / "base.txt").write_text("old.py\n")
    r = run("check_format.py", tmp_path, "--baseline", "base.txt", env=pr)  # a PR run, but no git checkout
    assert r.returncode != 0
    assert "can't find the base" in r.stderr
    (tmp_path / "format.txt").mkdir()  # at the base, the baseline path is a folder: a git error, not "no file"
    (tmp_path / "format.txt/x").write_text("x\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    _git(tmp_path, "rm", "-rq", "format.txt")
    (tmp_path / "format.txt").write_text("old.py\n")
    r = run("check_format.py", tmp_path, "--baseline", "format.txt", env=pr)
    assert r.returncode != 0
    assert "not a file" in r.stderr


def test_types_consumer_settings_cant_hide_errors(tmp_path):
    _types_repo(tmp_path, 'x: int = "one"\n')
    (tmp_path / "src/demo/gen").mkdir()
    (tmp_path / "src/demo/gen/g.py").write_text("y: str = 2\n")
    (tmp_path / "src/demo/u.py").write_text('def f():\n    z: int = "two"\n    return z\n')
    for config in (
        {"ignore": ["src"]},
        {"exclude": ["src/demo/gen"]},
        {"reportAssignmentType": "none"},
        {"reportAssignmentType": "warning", "typeCheckingMode": "strict"},
        {"executionEnvironments": [{"root": "src", "reportAssignmentType": False}]},
        {"analyzeUnannotatedFunctions": False},
        {"executionEnvironments": [{"root": "src", "analyzeUnannotatedFunctions": False}]},
    ):
        (tmp_path / "pyrightconfig.json").write_text(json.dumps(config))
        r = run("check_types.py", tmp_path, "src")
        assert r.returncode == 1, (config, r.stdout, r.stderr)
        for f in ("a.py", "gen/g.py", "u.py"):
            assert f"src/demo/{f}: 1 type error" in r.stdout, (config, f, r.stdout)
        assert "ignoring the consumer's pyright settings" in r.stdout, config


def test_types_a_strict_config_that_relaxes_a_rule_is_checked_as_standard(tmp_path):
    _types_repo(tmp_path, "def f(x):\n    return x\n")  # an error in strict only
    (tmp_path / "pyrightconfig.json").write_text('{"typeCheckingMode": "strict", "reportMissingTypeStubs": false}')
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 0, r.stdout
    assert "reportMissingTypeStubs = false" in r.stdout


def test_repo_requires_pinned_uses_under_a_quoted_key(tmp_path):
    repo = _repo(tmp_path)
    sha = "a" * 40
    (repo / ".github/workflows/quoted.yml").write_text(
        "jobs:\n  x:\n    steps:\n"
        '      - "uses": actions/checkout@v4\n'
        "      - 'uses': actions/checkout@v4\n"
        '      - { "uses": actions/checkout@v4 }\n'
        "      - { name: co, 'uses': \"actions/checkout@v4\" }\n"
        '      - {"uses":actions/checkout@v4}\n'
        '      - name: co\n        "uses" : actions/checkout@v4\n'
        '  y:\n    "uses": org/home/.github/workflows/gp-python-ci.yml@main\n'
        "  z:\n    'uses':\n      org/home/.github/workflows/gp-python-ci.yml@main\n"
        f'  ok:\n    "uses": org/home/.github/workflows/gp-python-ci.yml@{sha} # golden-pipeline-v1\n'
        f"    steps:\n      - {{ 'uses': 'actions/checkout@{sha}' }}  # v4.4.0\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    for n in (4, 5, 6, 7, 8, 10, 12, 14):
        assert f"quoted.yml:{n}:" in r.stdout, n
    for n in (17, 19):
        assert f"quoted.yml:{n}:" not in r.stdout, n


def test_repo_requires_pinned_hooks_under_a_quoted_key(tmp_path):
    repo = _repo(tmp_path)
    sha = "b" * 40
    (repo / ".pre-commit-config.yaml").write_text(
        'repos:\n  - repo: https://github.com/x/a\n    "rev": v1.2.3\n'
        "  - {repo: https://github.com/x/b, 'rev': v1.2.3}\n"
        f'  - repo: https://github.com/x/ok\n    "rev": {sha}  # v1.2.3\n'
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    assert ".pre-commit-config.yaml:3" in r.stdout and ".pre-commit-config.yaml:4" in r.stdout
    assert ".pre-commit-config.yaml:6" not in r.stdout


def test_types_a_base_config_cant_hide_errors_through_extends(tmp_path):
    _types_repo(tmp_path, 'x: int = "one"\n')
    (tmp_path / "cfg").mkdir()
    for base, at in (
        ({"ignore": ["src"]}, "base.json"),
        ({"ignore": ["../src"]}, "cfg/base.json"),
        ({"typeCheckingMode": "off"}, "base.json"),
        ({"reportAssignmentType": "none"}, "cfg/base.json"),
    ):
        (tmp_path / at).write_text(json.dumps(base))
        for child, name in (
            (json.dumps({"extends": at}), "pyrightconfig.json"),
            (f'[tool.pyright]\nextends = "{at}"\n', "pyproject.toml"),
        ):
            (tmp_path / "pyrightconfig.json").unlink(missing_ok=True)
            (tmp_path / name).write_text(child)
            r = run("check_types.py", tmp_path, "src")
            assert r.returncode == 1, (base, name, r.stdout, r.stderr)
            assert "src/demo/a.py: 1 type error" in r.stdout, (base, name, r.stdout)


def test_types_extends_is_resolved_with_its_paths_or_refused(tmp_path):
    _types_repo(tmp_path, "import helper\n\nx: int = helper.N\n")
    (tmp_path / "cfg/lib").mkdir(parents=True)
    (tmp_path / "cfg/lib/helper.py").write_text("N: int = 1\n")
    (tmp_path / "cfg/base.json").write_text('{"extraPaths": ["lib"], "reportMissingImports": "error"}')
    (tmp_path / "pyrightconfig.json").write_text('{"extends": "cfg/base.json"}')
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 0, r.stdout + r.stderr  # the base's extraPaths are relative to the base file
    (tmp_path / "cfg/base.json").write_text('{"extends": "../pyrightconfig.json"}')
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode != 0 and "extends" in r.stderr and "cycle" in r.stderr
    (tmp_path / "pyrightconfig.json").write_text('{"extends": "cfg/missing.json"}')
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode != 0 and "cfg/missing.json" in r.stderr
    (tmp_path / "pyrightconfig.json").write_text('{"extends": "../outside.json"}')
    (tmp_path.parent / "outside.json").write_text("{}")
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode != 0 and "outside" in r.stderr


def test_types_names_file_level_suppressions(tmp_path):
    _types_repo(tmp_path, "x: int = 1\n")
    (tmp_path / "src/demo/b.py").write_text("# pyright: reportAssignmentType=false\ny: int = 'a'\n")
    # after a docstring, `# type: ignore` is not file-level
    (tmp_path / "src/demo/c.py").write_text('"""Doc."""\n# type: ignore\nz: int = 1\n')
    (tmp_path / "src/demo/d.py").write_text("# type: ignore\nz: int = 'a'\n")
    (tmp_path / "src/demo/e.py").write_text("# pyright: basic\n")
    (tmp_path / "src/demo/f.py").write_text("# pyright: strict\nq = 1  # type: ignore\n")
    r = run("check_types.py", tmp_path, "src")
    assert r.returncode == 0, r.stdout
    for f in ("b.py:1", "d.py:1", "e.py:1"):
        assert f"src/demo/{f}" in r.stdout, (f, r.stdout)
    assert "src/demo/f.py" not in r.stdout and "src/demo/c.py" not in r.stdout


def test_repo_checks_every_uses_on_a_line_and_odd_keys(tmp_path):
    repo = _repo(tmp_path)
    sha = "a" * 40
    (repo / ".github/workflows/odd.yml").write_text(
        "jobs:\n  x:\n    steps:\n"
        f"      - [{{uses: actions/checkout@{sha}}}, {{uses: evil/x@main}}] # v1\n"
        "      - !!str uses: evil/x@main\n"
        "      - &a uses: evil/x@main\n"
        '      - "u\\x73es": evil/x@main\n'
        "      - ? uses\n        : evil/x@main\n"
        "      - *k : evil/x@main\n"
        f"    steps2: [uses: actions/checkout@{sha}, uses: evil/x@main] # v1\n"
        f"      - [{{uses: actions/checkout@{sha}}}, {{uses: actions/cache@{sha}}}] # v4\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    for n in (4, 5, 6, 7, 8, 10, 11):
        assert f"odd.yml:{n}:" in r.stdout, (n, r.stdout)
    assert "odd.yml:12:" not in r.stdout


def test_repo_ignores_uses_inside_block_scalars(tmp_path):
    # James's review: a run: script that prints "{ uses: actions/checkout@v4 }" is text, not a step
    repo = _repo(tmp_path)
    sha = "a" * 40
    (repo / ".github/workflows/scripts.yml").write_text(
        "jobs:\n  x:\n    steps:\n"
        "      - run: |\n"
        "          echo '{ uses: actions/checkout@v4 }'\n"
        "\n"
        "          echo '[uses: evil/x@main]'\n"
        "      - name: folded\n"
        "        run: >-\n"
        "          { uses: evil/x@main }\n"
        "      - with:\n"
        "          script: |2\n"
        "              - uses: evil/x@main\n"
        "          list:\n"
        "            - |\n"
        "              - uses: evil/x@main\n"
        "          own_line:\n"
        "            |\n"
        "              - uses: evil/x@main\n"
        "          dash_own_line:\n"
        "            -\n"
        "              >-\n"
        "                { uses: evil/x@main }\n"
        f"      - uses: actions/checkout@{sha} # v4.4.0\n"
    )
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n  - repo: local\n    hooks:\n      - id: x\n        entry: |\n          rev: main\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 0, r.stdout


def test_repo_still_reads_keys_after_a_block_scalar(tmp_path):
    # the scalar ends at the first line not indented past its key, so a sibling or later step is still checked
    repo = _repo(tmp_path)
    (repo / ".github/workflows/after.yml").write_text(
        "jobs:\n  x:\n    steps:\n"
        "      - run: |\n"
        "          echo hi\n"
        "        uses: evil/x@main\n"
        "      - run: >\n"
        "          echo hi\n"
        "      - uses: evil/y@main\n"
        "  y:\n"
        "    uses: |\n"
        "      evil/z@main\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    for n in (6, 9, 11):
        assert f"after.yml:{n}:" in r.stdout, (n, r.stdout)


def test_repo_reads_on_after_an_indicator_alone_on_its_line(tmp_path):
    # James's fourth review: a lone "|" under "run:" made every later line text, so unpinned actions passed
    repo = _repo(tmp_path)
    (repo / ".github/workflows/lone.yml").write_text(
        "jobs:\n  x:\n    steps:\n"
        "      - run:\n"
        "          |\n"
        "          echo '{ uses: evil/text@main }'\n"
        "      - uses: evil/x@main\n"
        "      -\n"
        "        |\n"
        "        echo '{ uses: evil/text@main }'\n"
        "      - uses: evil/y@main\n"
    )
    _add(repo)
    r = run("check_repo.py", repo)
    assert r.returncode == 1
    assert sorted(
        {int(ln.split("lone.yml:")[1].split(":")[0]) for ln in r.stdout.splitlines() if "lone.yml:" in ln}
    ) == [
        7,
        11,
    ], r.stdout


def test_types_a_monorepo_app_may_extend_a_base_above_it(tmp_path):
    # James's review: containment is the git checkout, not the working_directory the check runs in
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    app = tmp_path / "apps/a"
    app.mkdir(parents=True)
    _types_repo(app, 'x: int = "one"\n')
    (tmp_path / "pyrightconfig.base.json").write_text('{"ignore": ["apps"], "reportAssignmentType": "none"}')
    (app / "pyrightconfig.json").write_text('{"extends": "../../pyrightconfig.base.json"}')
    r = run("check_types.py", app, "src")
    assert "outside the repo" not in r.stderr, r.stderr
    assert r.returncode == 1 and "src/demo/a.py: 1 type error" in r.stdout, r.stdout + r.stderr  # relaxations still go
    (app / "pyrightconfig.json").write_text('{"extends": "../../../outside.json"}')
    (tmp_path.parent / "outside.json").write_text("{}")
    r = run("check_types.py", app, "src")
    assert r.returncode != 0 and "outside the repo" in r.stderr, r.stderr


def test_repo_checks_every_tracked_pre_commit_config(tmp_path):
    # A monorepo keeps a .pre-commit-config.yaml per app; each is checked from the root, untracked ones are not.
    repo = _repo(tmp_path)
    sha = "b" * 40
    (repo / "apps/web").mkdir(parents=True)
    (repo / "apps/web/.pre-commit-config.yaml").write_text("repos:\n  - repo: https://github.com/x/tag\n    rev: v1\n")
    (repo / "apps/api").mkdir(parents=True)
    (repo / "apps/api/.pre-commit-config.yaml").write_text(
        f"repos:\n  - repo: https://github.com/x/ok\n    rev: {sha}  # v1.2.3\n"
    )
    _add(repo)
    (repo / "scratch").mkdir()
    (repo / "scratch/.pre-commit-config.yaml").write_text("repos:\n  - repo: https://github.com/x/tag\n    rev: v1\n")
    r = run("check_repo.py", repo / "src")
    assert r.returncode == 1
    assert "apps/web/.pre-commit-config.yaml:3" in r.stdout
    assert "apps/api" not in r.stdout and "scratch" not in r.stdout


def test_types_settings_that_skip_code_are_dropped_and_named(tmp_path):
    # James's approval of #14: defineConstant (reproduced) and pythonPlatform make pyright treat a block as
    # unreachable, so its errors go unreported; an execution environment's pythonVersion does the same for a version
    # check, and stubPath (and typeshedPath) swap the types the code is checked against. Each config below hides one
    # file's error from the old gate.
    _types_repo(tmp_path, 'DEBUG = False\nif DEBUG:\n    pass\nelse:\n    x: int = "one"\n')
    demo = tmp_path / "src/demo"
    (demo / "p.py").write_text('import sys\nif sys.platform != "win32":\n    y: int = "two"\n')
    (demo / "v.py").write_text('import sys\nif sys.version_info >= (3, 9):\n    v: int = "three"\n')
    (demo / "b.py").write_text('def g() -> str:\n    return "four"\n')
    (demo / "c.py").write_text("from demo.b import g\n\nz: int = g()\n")
    (tmp_path / "stubs/demo").mkdir(parents=True)
    (tmp_path / "stubs/demo/__init__.pyi").write_text("")
    (tmp_path / "stubs/demo/b.pyi").write_text("from typing import Any\n\ndef g() -> Any: ...\n")
    for config, named in (
        ({"defineConstant": {"DEBUG": True}}, ["defineConstant"]),
        ({"pythonPlatform": "Windows"}, ["pythonPlatform"]),
        (
            {"executionEnvironments": [{"root": "src", "pythonPlatform": "Windows", "pythonVersion": "3.8"}]},
            ["executionEnvironments[0].pythonPlatform", "executionEnvironments[0].pythonVersion"],
        ),
        ({"stubPath": "stubs", "typeshedPath": "typeshed"}, ["stubPath", "typeshedPath"]),
    ):
        (tmp_path / "pyrightconfig.json").write_text(json.dumps(config))
        r = run("check_types.py", tmp_path, "src")
        assert r.returncode == 1, (config, r.stdout, r.stderr)
        for f in ("a.py", "p.py", "v.py", "c.py"):
            assert f"src/demo/{f}: 1 type error" in r.stdout, (config, f, r.stdout)
        note = next(line for line in r.stdout.splitlines() if "would hide errors" in line)
        for k in named:
            assert k in note, (k, r.stdout)


def test_types_library_code_for_types_off_is_dropped_and_named(tmp_path):
    # #18's review: with useLibraryCodeForTypes = false, a dependency without stubs or py.typed is Unknown to pyright,
    # so a wrong use of it reports nothing
    _types_repo(tmp_path, "from thing import g\n\nz: int = g()\n")
    site = tmp_path / ".venv/lib/python3.12/site-packages"
    (site / "thing").mkdir(parents=True)
    (site / "thing/__init__.py").write_text('def g() -> str:\n    return "five"\n')
    (tmp_path / ".venv/pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.0\n")
    (tmp_path / "pyrightconfig.json").write_text(json.dumps({"useLibraryCodeForTypes": False}))
    r = run("check_types.py", tmp_path, "--python-version", "3.12", "src")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "src/demo/a.py: 1 type error" in r.stdout
    note = next(line for line in r.stdout.splitlines() if "would hide errors" in line)
    assert "useLibraryCodeForTypes = false" in note


def test_confidential_names_a_pattern_that_does_not_compile_and_a_pr_can_fix_it(tmp_path):
    # James's approval of #14: a broken base pattern crashed the check for every PR, the fix included
    _confidential_repo(tmp_path)
    deny = tmp_path / GP / "confidential-denylist.txt"
    deny.write_text("\\bproject bluebird\\b\nproject (heron\n")
    _git(tmp_path, "commit", "-qam", "a broken pattern lands on the base")
    (tmp_path / "b.md").write_text("notes\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "an unrelated PR")
    r = _confidential(tmp_path, "--range", "HEAD~1..HEAD")
    assert r.returncode != 0 and "Traceback" not in r.stderr
    assert "project (heron" in r.stdout + r.stderr and "don't compile" in r.stderr
    deny.write_text("\\bproject bluebird\\b\n\\bproject heron\\b\n")  # the PR that fixes it
    (tmp_path / "a.md").write_text("project heron\n")
    _git(tmp_path, "commit", "-qam", "fix the pattern")
    r = _confidential(tmp_path, "--range", "HEAD~1..HEAD")
    assert r.returncode == 1 and "a.md:1" in r.stdout  # runs, with the fixed pattern
    assert "project (heron" in r.stdout + r.stderr  # the base's broken one is named, and skipped
