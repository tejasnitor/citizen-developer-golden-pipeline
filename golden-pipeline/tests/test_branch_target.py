"""Tests for check_branch_target.py's head-repo rule: a PR into main must come from a branch of the repo itself."""

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / "golden-pipeline" / "tools" / "check_branch_target.py"
REPO = "Procurementpartners/some-app"
BOT = "pp-release-bot[bot]"


def _target(base, head, base_repo, head_repo, author="gauravpcu") -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("BASE_REPO", "HEAD_REPO", "RELEASE_APP")}
    env.update(BASE_REF=base, HEAD_REF=head, PR_AUTHOR=author, BASE_REPO=base_repo, HEAD_REPO=head_repo)
    return subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, env=env)


def test_same_repo_branches_pass_into_main():
    for head in ["develop", "release/askme-0.3.0", "hotfix/ppcd-0.2.1"]:
        r = _target("main", head, REPO, REPO)
        assert r.returncode == 0, (head, r.stdout)
    assert _target("main", "release-please--branches--main", REPO, REPO, BOT).returncode == 0
    # GitHub repo names are case-insensitive
    assert _target("main", "develop", REPO, REPO.lower()).returncode == 0


def test_a_forks_allowed_branch_name_fails_into_main():
    for head, author in [
        ("develop", "gauravpcu"),
        ("release/askme-0.3.0", "gauravpcu"),
        ("hotfix/ppcd-0.2.1", "gauravpcu"),
        ("release-please--branches--main", BOT),
    ]:
        r = _target("main", head, REPO, "someone/some-app", author)
        assert r.returncode == 1, head
        assert "someone/some-app" in r.stdout


def test_a_deleted_head_repo_fails_into_main():
    # head.repo is null when the fork was deleted, so HEAD_REPO arrives empty
    r = _target("main", "develop", REPO, "")
    assert r.returncode == 1
    assert "deleted repo" in r.stdout


def test_a_fork_may_target_other_bases():
    assert _target("develop", "feat/x", REPO, "someone/some-app").returncode == 0


def test_the_workflow_passes_both_repos():
    # The tool trusts head.ref only when it is told both repos; the workflow is where they come from.
    text = (ROOT / ".github" / "workflows" / "gp-pr-checks.yml").read_text()
    step = text[text.index("\n  branch-target:") :]
    assert "BASE_REPO: ${{ github.repository }}" in step
    assert "HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}" in step


def test_base_repo_is_required():
    env = {k: v for k, v in os.environ.items() if k not in ("BASE_REPO", "HEAD_REPO", "RELEASE_APP")}
    env.update(BASE_REF="main", HEAD_REF="develop", PR_AUTHOR="gauravpcu")
    run = subprocess.run([sys.executable, str(TOOL)], capture_output=True, text=True, env=env)
    assert run.returncode != 0
    assert "BASE_REPO" in (run.stdout + run.stderr)
