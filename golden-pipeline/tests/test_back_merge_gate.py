"""Tests for the back-merge in gp-release.yml: the jq program that decides whether develop's required checks are
green for the back-merge, run for real on files shaped as the workflow writes them, and the step's own checks, run
with a stubbed gh.

The job merges main into develop's tip itself, so the back-merge PR's head is a commit no other branch has: every
result on it tested it, and the gate reads only that commit.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "gp-release.yml"
JQ = shutil.which("jq")
pytestmark = pytest.mark.skipif(JQ is None, reason="jq is not installed; the back-merge gate is a jq program")

EARLY = "2026-11-08T10:05:00Z"
LATER = "2026-11-08T10:09:00Z"
ACTIONS = (15368, "github-actions")
SONAR = (12526, "sonarqubecloud")
MERGE = "m" * 40  # the back-merge commit the job made
BASE = "d" * 40  # develop's tip, its first parent


def _gate_program() -> str:
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "<<'GATE_JQ'" in line)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "GATE_JQ")
    return "\n".join(lines[start + 1 : end])


def _required_filter() -> str:
    text = WORKFLOW.read_text()
    # The rules are read page by page, one rule per line, into rules.json; the filter combines them with jq -s.
    assert "rules/branches/develop?per_page=100\" --paginate --jq '.[]' > rules.json" in text
    match = re.search(r"required=\$\(jq -cs '([^']*)' rules\.json\)", text)
    assert match, "the required-checks filter on rules/branches/develop is gone"
    return match.group(1)


def _run(rid, name, app=ACTIONS, status="completed", conclusion="success", started=EARLY) -> dict:
    return {
        "id": rid,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started,
        "app": {"id": app[0], "slug": app[1]},
    }


def _status(context, state="success", created=EARLY) -> dict:
    return {"context": context, "state": state, "created_at": created, "creator": "ci-bot"}


def _write_lines(path: pathlib.Path, items) -> None:
    # gh api --paginate --jq '...[]' writes one JSON value per line
    path.write_text("".join(json.dumps(i) + "\n" for i in items))


def _gate(tmp_path, required, runs=(), statuses=()) -> dict:
    """Run the gate as the step does; return {label: (state, details)}."""
    _write_lines(tmp_path / "runs.json", runs)
    _write_lines(tmp_path / "statuses.json", statuses)
    r = subprocess.run(
        [JQ, "-rn", "--argjson", "req", json.dumps(required)]
        + ["--slurpfile", "runs", "runs.json", "--slurpfile", "statuses", "statuses.json", _gate_program()],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        assert len(parts) == 3, f"report line isn't state<TAB>check<TAB>details: {line!r}"
        out[parts[1]] = (parts[0], parts[2])
    assert len(out) == len(required), r.stdout
    return out


def test_all_green_passes(tmp_path):
    required = [{"context": "build", "integration_id": ACTIONS[0]}, {"context": "ci/jenkins"}]
    report = _gate(tmp_path, required, [_run(11, "build")], [_status("ci/jenkins")])
    assert report["build [app 15368]"][0] == "success"
    assert report["ci/jenkins"][0] == "success"


def test_any_apps_result_on_the_back_merge_commit_counts(tmp_path):
    # The commit is the back-merge's own, so SonarCloud's run counts whichever suite or event it came from.
    report = _gate(tmp_path, [{"context": "SonarCloud Code Analysis"}], [_run(20, "SonarCloud Code Analysis", SONAR)])
    assert report["SonarCloud Code Analysis"][0] == "success"


def test_nothing_reported_yet_waits(tmp_path):
    state, details = _gate(tmp_path, [{"context": "build"}])["build"]
    assert state == "pending"
    assert "no result on the back-merge commit yet" in details


def test_pinned_rule_is_not_satisfied_by_a_status(tmp_path):
    required = [{"context": "lint", "integration_id": ACTIONS[0]}]
    state, details = _gate(tmp_path, required, statuses=[_status("lint")])["lint [app 15368]"]
    assert state == "pending"
    assert "pinned to app 15368" in details


def test_pinned_rule_needs_the_pinned_app(tmp_path):
    report = _gate(tmp_path, [{"context": "build", "integration_id": 999}], [_run(11, "build")])
    assert report["build [app 999]"][0] == "pending"


def test_two_rules_on_one_context_with_different_apps_both_need_a_result(tmp_path):
    required = [{"context": "build", "integration_id": ACTIONS[0]}, {"context": "build", "integration_id": 999}]
    report = _gate(tmp_path, required, [_run(11, "build")])
    assert report["build [app 15368]"][0] == "success"
    assert report["build [app 999]"][0] == "pending"


def test_unpinned_rule_needs_every_apps_latest_run_to_pass(tmp_path):
    runs = [_run(11, "build"), _run(12, "build", SONAR, conclusion="failure")]
    assert _gate(tmp_path, [{"context": "build"}], runs)["build"][0] == "failure"


def test_required_list_keeps_rules_that_differ_only_in_app():
    rules = [
        {"type": "pull_request", "parameters": {}},
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "build", "integration_id": 15368}]},
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "build", "integration_id": 999},
                    {"context": "build", "integration_id": 15368},
                    {"context": "ci/jenkins"},
                ]
            },
        },
    ]
    # As gh api --paginate --jq '.[]' writes them: one rule per line, the rules of every page one after another.
    lines = "".join(json.dumps(rule) + "\n" for rule in rules)
    r = subprocess.run([JQ, "-cs", _required_filter()], input=lines, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    keys = sorted((c["context"], c.get("integration_id") or 0) for c in json.loads(r.stdout))
    assert keys == [("build", 999), ("build", 15368), ("ci/jenkins", 0)]


def test_required_list_reads_rules_from_every_page():
    # 30 rules to a page: the required checks can sit on the second page.
    page1 = [{"type": "pull_request", "parameters": {}}] * 30
    page2 = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "build", "integration_id": 15368}]},
        }
    ]
    lines = "".join(json.dumps(rule) + "\n" for rule in page1 + page2)
    r = subprocess.run([JQ, "-cs", _required_filter()], input=lines, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [{"context": "build", "integration_id": 15368}]


def test_check_run_success_and_status_failure_fails(tmp_path):
    report = _gate(tmp_path, [{"context": "build"}], [_run(11, "build")], [_status("build", state="failure")])
    assert report["build"][0] == "failure"


def test_check_run_success_and_status_pending_waits(tmp_path):
    report = _gate(tmp_path, [{"context": "build"}], [_run(11, "build")], [_status("build", state="pending")])
    assert report["build"][0] == "pending"


def test_check_run_success_and_status_success_passes(tmp_path):
    report = _gate(tmp_path, [{"context": "build"}], [_run(11, "build")], [_status("build")])
    assert report["build"][0] == "success"


def test_latest_run_and_latest_status_win(tmp_path):
    runs = [_run(11, "build", conclusion="failure"), _run(12, "build", started=LATER)]
    statuses = [_status("build", state="error"), _status("build", created=LATER)]
    assert _gate(tmp_path, [{"context": "build"}], runs, statuses)["build"][0] == "success"


def test_neutral_passes_skipped_waits_and_cancelled_fails(tmp_path):
    required = [{"context": "a"}, {"context": "b"}, {"context": "c"}]
    runs = [
        _run(1, "a", conclusion="skipped"),
        _run(2, "b", conclusion="neutral"),
        _run(3, "c", conclusion="cancelled"),
    ]
    report = _gate(tmp_path, required, runs)
    assert [report[k][0] for k in "abc"] == ["pending", "success", "failure"]


def test_a_push_runs_skip_does_not_hide_the_pr_runs_result(tmp_path):
    # Review: the branch's push can fire a workflow whose required job runs only on pull_request; its skip must not
    # pass the gate before the PR's run exists, nor outrank that run once it does
    skip = _run(1, "build", conclusion="skipped", started=LATER)
    assert _gate(tmp_path, [{"context": "build"}], [skip])["build"][0] == "pending"
    failed = _run(2, "build", conclusion="failure")
    assert _gate(tmp_path, [{"context": "build"}], [skip, failed])["build"][0] == "failure"
    assert _gate(tmp_path, [{"context": "build"}], [skip, _run(3, "build")])["build"][0] == "success"


def test_run_with_no_start_time_waits_and_is_named(tmp_path):
    runs = [_run(11, "build"), _run(13, "build", status="queued", conclusion=None, started=None)]
    state, details = _gate(tmp_path, [{"context": "build"}], runs)["build"]
    assert state == "pending"
    assert "run 13" in details and "github-actions" in details and "queued" in details
    assert "no started_at" in details


def test_the_gate_reads_only_the_back_merge_commit():
    # James's fourth review: results must be bound to what they tested, not to a time; the gate reads only the
    # commit the job made, never main's (which main's own push checks share)
    text = WORKFLOW.read_text()
    assert 'commits/$merge/check-runs?per_page=100"' in text
    assert 'commits/$merge/statuses?per_page=100"' in text
    assert "commits/$main/check-runs?per_page=100" not in text and "commits/$main/statuses?per_page=100" not in text
    assert "since" not in _gate_program() and "since" not in text.split("<<'GATE_JQ'")[0].split("Merge main into")[1]
    assert '-f merge_method=merge -f "sha=$merge"' in text


def _snippet(first: str, last: str, nth: int = 1) -> str:
    """The back-merge step's lines from the one starting with `first` to the nth after it starting with `last`."""
    lines = [line.strip() for line in WORKFLOW.read_text().splitlines()]
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    ends = [i for i in range(start, len(lines)) if lines[i].startswith(last)]
    return "\n".join(lines[start : ends[nth - 1] + 1]) + "\n"


READ_API = _snippet("read_api() {", "}")


def _with_gh(tmp_path, script: str, answers: dict[str, str], **env) -> subprocess.CompletedProcess:
    """Run a bash snippet with gh answering from `answers`, keyed on the repos/ path it is given. An answer starting
    with HTTP is an error: gh prints it to stderr and fails."""
    stub = 'gh() {\n  local p=""\n  for a in "$@"; do case "$a" in repos/*) p="$a"; break ;; esac; done\n'
    stub += '  echo "$*" >> gh.log\n  case "$p" in\n'
    for path, out in answers.items():
        if out.startswith("HTTP"):
            stub += f"    {path!r}) echo 'gh: failed ({out})' >&2; return 1 ;;\n"
        else:
            stub += f"    {path!r}) printf '%s\\n' {out!r} ;;\n"
    stub += '    *) echo "unexpected gh $*" >&2; return 1 ;;\n  esac\n}\n'
    return subprocess.run(
        ["bash", "-c", "set -eo pipefail\n" + stub + READ_API + script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "REPO": "o/r", "GITHUB_STEP_SUMMARY": "/dev/null", **env},
        cwd=tmp_path,
    )


def test_the_job_merges_main_into_develops_tip_on_a_scratch_branch(tmp_path):
    script = _snippet('scratch="', "read -r merge parents") + 'echo "merge=$merge"\n'
    env = {"main": "a" * 40, "base": BASE, "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "2"}
    answers = {
        "repos/o/r/git/refs": "{}",
        "repos/o/r/merges": f"{MERGE} {BASE},{'a' * 40}",
        "repos/o/r/git/refs/heads/back-merge/build-9-2": "",
    }
    r = _with_gh(tmp_path, script, answers, **env)
    assert r.returncode == 0, r.stderr
    assert f"merge={MERGE}" in r.stdout
    log = (tmp_path / "gh.log").read_text()
    assert f"-f ref=refs/heads/back-merge/build-9-2 -f sha={BASE}" in log
    assert f"-f base=back-merge/build-9-2 -f head={'a' * 40}" in log
    assert "-X DELETE repos/o/r/git/refs/heads/back-merge/build-9-2" in log  # the scratch branch goes on exit


def test_a_conflict_stops_the_job_and_says_how_to_merge_by_hand(tmp_path):
    script = _snippet('scratch="', "read -r merge parents")
    env = {"main": "a" * 40, "base": BASE, "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "1"}
    answers = {
        "repos/o/r/git/refs": "{}",
        "repos/o/r/merges": "HTTP 409",
        "repos/o/r/git/refs/heads/back-merge/build-9-1": "",
    }
    r = _with_gh(tmp_path, script, answers, **env)
    assert r.returncode == 1
    assert "main doesn't merge cleanly into develop" in r.stdout
    assert "-X DELETE repos/o/r/git/refs/heads/back-merge/build-9-1" in (tmp_path / "gh.log").read_text()


def test_a_develop_that_moved_stops_the_merge(tmp_path):
    script = _snippet('read_api "compare/develop...$merge"', "fi")
    path = f"repos/o/r/compare/develop...{MERGE}"
    moved = _with_gh(tmp_path, script, {path: "diverged"}, merge=MERGE, pr="7")
    assert moved.returncode == 1 and "develop moved while #7 was being checked" in moved.stdout
    same = _with_gh(tmp_path, script, {path: "ahead"}, merge=MERGE, pr="7")
    assert same.returncode == 0, same.stdout + same.stderr
    text = WORKFLOW.read_text()
    assert text.index('read_api "compare/develop...$merge"') < text.index('pulls/$pr/merge"')


def test_a_move_just_before_the_merge_is_reported_after_it(tmp_path):
    # The merge API has no base-SHA precondition, so the job checks the merge commit's first parent afterwards.
    (tmp_path / "api.out").write_text("x" * 40 + "\n")
    script = _snippet("done_sha=$(cat api.out)", "fi")
    answers = {f"repos/o/r/commits/{'x' * 40}": "e" * 40, f"repos/o/r/compare/{'e' * 40}...{MERGE}": "diverged"}
    r = _with_gh(tmp_path, script, answers, merge=MERGE, pr="7")
    assert r.returncode == 1 and "develop moved just before #7 was merged" in r.stdout
    (tmp_path / "api.out").write_text("x" * 40 + "\n")
    answers = {f"repos/o/r/commits/{'x' * 40}": BASE, f"repos/o/r/compare/{BASE}...{MERGE}": "ahead"}
    r = _with_gh(tmp_path, script, answers, merge=MERGE, pr="7")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_merge_of_anything_but_develops_tip_and_main_stops_the_job(tmp_path):
    # Review: someone pushing to the scratch branch before the merge would get their commit merged with main
    script = _snippet('scratch="', "fi", nth=4)
    env = {"main": "a" * 40, "base": BASE, "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "1"}
    answers = {
        "repos/o/r/git/refs": "{}",
        "repos/o/r/merges": f"{MERGE} {'x' * 40},{'a' * 40}",
        "repos/o/r/git/refs/heads/back-merge/build-9-1": "",
    }
    r = _with_gh(tmp_path, script, answers, **env)
    assert r.returncode == 1
    assert "not develop's tip and main" in r.stdout
    assert "-X DELETE repos/o/r/git/refs/heads/back-merge/build-9-1" in (tmp_path / "gh.log").read_text()


def _flaky_gh(tmp_path, script: str, failures: int, **env) -> subprocess.CompletedProcess:
    """Run a snippet with a gh that fails `failures` times, then answers; sleep is a no-op."""
    stub = (
        'gh() {\n  n=$(cat tries 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > tries\n'
        f'  if [ "$n" -le {failures} ]; then echo "gh: Server Error (HTTP 502)" >&2; return 1; fi\n'
        "  echo '{\"id\": 1}'\n}\nsleep() { :; }\n"
    )
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + stub + script],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "REPO": "o/r", **env},
        cwd=tmp_path,
    )


def test_the_wait_loop_retries_and_annotates_a_failed_read(tmp_path):
    # James's approval of #14: one API blip in the loop failed the job with a bare gh error
    text = WORKFLOW.read_text()
    assert text.count('read_results "commits/$merge/') == 2
    script = _snippet("read_results() {", "}") + 'read_results "commits/m/check-runs?per_page=100" . runs.json\n'
    r = _flaky_gh(tmp_path, script, failures=2)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("::warning::Reading commits/m/check-runs failed") == 2 and "HTTP 502" in r.stdout
    assert (tmp_path / "runs.json").read_text() == '{"id": 1}\n'
    (tmp_path / "tries").unlink()
    r = _flaky_gh(tmp_path, script, failures=3)
    assert r.returncode == 1
    assert "::error::Couldn't read commits/m/check-runs after 3 tries" in r.stdout
