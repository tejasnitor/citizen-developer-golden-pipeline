"""Tests for the settings kit (golden-pipeline/kit/apply_settings.py), with the GitHub API faked."""

import base64
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "apply_settings", pathlib.Path(__file__).resolve().parents[1] / "kit" / "apply_settings.py"
)
assert _SPEC and _SPEC.loader
kit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(kit)


def _rule(rs, kind):
    return next(r["parameters"] for r in rs["rules"] if r["type"] == kind)


def test_develop_is_squash_only_and_main_merge_only():
    develop, main, _ = kit.rulesets(1, [])
    assert develop["conditions"]["ref_name"]["include"] == ["refs/heads/develop"]
    assert _rule(develop, "pull_request")["allowed_merge_methods"] == ["squash"]
    assert main["conditions"]["ref_name"]["include"] == ["refs/heads/main"]
    assert _rule(main, "pull_request")["allowed_merge_methods"] == ["merge"]


def test_rulesets_require_review_up_to_date_and_the_checks():
    for rs in kit.rulesets(1, []):
        pr = _rule(rs, "pull_request")
        assert pr["required_approving_review_count"] == 1
        assert pr["require_code_owner_review"] and pr["require_last_push_approval"]
        assert pr["dismiss_stale_reviews_on_push"] and pr["required_review_thread_resolution"]
        checks = _rule(rs, "required_status_checks")
        assert checks["strict_required_status_checks_policy"] is True
        names = [c["context"] for c in checks["required_status_checks"]]
        assert "golden-pipeline" in names and "pr-checks / branch-target" in names
        assert rs["bypass_actors"] == []
        assert {"type": "non_fast_forward"} in rs["rules"]


def test_zero_approvals_turns_off_owner_and_last_push_review():
    pr = _rule(kit.rulesets(0, [])[0], "pull_request")
    assert pr["required_approving_review_count"] == 0
    assert not pr["require_code_owner_review"] and not pr["require_last_push_approval"]


def test_extra_checks_are_added_once():
    names = [
        c["context"]
        for c in _rule(kit.rulesets(1, ["Kiro review result", "golden-pipeline"])[0], "required_status_checks")[
            "required_status_checks"
        ]
    ]
    assert names.count("golden-pipeline") == 1 and names[-1] == "Kiro review result"


def _required(rs):
    return [c["context"] for c in _rule(rs, "required_status_checks")["required_status_checks"]]


@pytest.mark.parametrize(
    "args,context",
    [
        (["--kiro-review"], "kiro-review / Kiro gate"),
        (["--kiro-review", "review / Kiro gate"], "review / Kiro gate"),
        (["--kiro-review", "--check", "kiro-review / Kiro gate"], "kiro-review / Kiro gate"),  # added once
    ],
)
def test_kiro_review_requires_the_kiro_gate_on_every_ruleset(monkeypatch, args, context):
    seen = []
    monkeypatch.setattr(kit, "plan", lambda repo, approvals, checks, *rest, **kw: seen.append(list(checks)) or [])
    assert kit.main(["o/r", *args]) == 0
    assert context in seen[0]
    for rs in kit.rulesets(1, seen[0]):  # develop, main and release/*
        names = _required(rs)
        assert names[: len(kit.CHECKS)] == kit.CHECKS and names.count(context) == 1


def test_without_kiro_review_the_kiro_gate_is_not_required(monkeypatch):
    seen = []
    monkeypatch.setattr(kit, "plan", lambda repo, approvals, checks, *rest, **kw: seen.append(list(checks)) or [])
    assert kit.main(["o/r"]) == 0
    assert seen == [[]]


@pytest.mark.parametrize("value", ["", "  ", "o/other"])
def test_kiro_review_refuses_an_empty_context_or_a_repo(monkeypatch, value):
    monkeypatch.setattr(kit, "plan", lambda *a, **kw: pytest.fail("planned with a bad --kiro-review"))
    with pytest.raises(SystemExit):
        kit.main(["o/r", "--kiro-review", value])


class FakeGitHub:
    def __init__(self, repo, main_exists, rulesets):
        self.repo, self.main_exists, self.rulesets = repo, main_exists, rulesets
        self.live: dict[int, dict] = {}
        self.codeowners = "* @o/team\n"
        self.env: dict | None = None  # the release environment, None for a 404
        self.policies: list[dict] = []
        self.repo_secret = False
        self.env_secret = False
        self.org_secrets: list[str] | None = []  # names; None for a 403/404
        self.fail: str | None = None  # a path whose call fails as gh would (gh() exits)

    def __call__(self, *args, body=None, ok_404=False, ok_403=False):
        path = args[-1]
        if path == self.fail:
            raise SystemExit(f"gh api {path} failed: HTTP 403")
        missing = None if ok_404 else AssertionError(f"404 on {path} without ok_404")
        if path.startswith("repos/o/r/actions/organization-secrets?"):
            if self.org_secrets is None:
                assert ok_403 and ok_404
                return None
            query = dict(kv.split("=") for kv in path.split("?", 1)[1].split("&"))
            size, page = int(query["per_page"]), int(query["page"])
            names = self.org_secrets[(page - 1) * size : page * size]
            return {"total_count": len(self.org_secrets), "secrets": [{"name": n} for n in names]}
        if path == "repos/o/r/actions/secrets/RELEASE_APP_PRIVATE_KEY":
            return {"name": "RELEASE_APP_PRIVATE_KEY"} if self.repo_secret else missing
        if path == "repos/o/r/environments/release/secrets/RELEASE_APP_PRIVATE_KEY":
            assert self.env is not None, "asked for a secret of an environment that does not exist"
            return {"name": "RELEASE_APP_PRIVATE_KEY"} if self.env_secret else missing
        if path == "repos/o/r/environments/release":
            return self.env if self.env is not None else missing
        if path.startswith("repos/o/r/environments/release/deployment-branch-policies?"):
            return {"total_count": len(self.policies), "branch_policies": self.policies}
        if path == "repos/o/r":
            return self.repo
        if path == "repos/o/r/branches/main":
            return {"name": "main"} if self.main_exists else None
        if path == "repos/o/r/git/ref/heads/develop":
            return {"object": {"sha": "a" * 40}}
        if path.startswith("repos/o/r/contents/.github/CODEOWNERS"):
            return {"content": base64.b64encode(self.codeowners.encode()).decode()}
        if path.startswith("repos/o/r/rulesets?"):
            return self.rulesets
        if path.startswith("repos/o/r/rulesets/"):
            return self.live.get(int(path.rsplit("/", 1)[1]), {})
        raise AssertionError(path)


def test_plan_for_a_new_repo_creates_main_and_both_rulesets(monkeypatch):
    repo = {"default_branch": "develop", "allow_squash_merge": True, "allow_merge_commit": False}
    fake = FakeGitHub(repo, False, [{"name": "develop: golden pipeline", "id": 7}])
    fake.live = {7: {"name": "develop: golden pipeline", "bypass_actors": [], "rules": []}}
    monkeypatch.setattr(kit, "gh", fake)
    calls = kit.plan("o/r", 1, [])
    descs = [c[0] for c in calls]
    assert descs[0].startswith("repo settings") and "allow_merge_commit" in calls[0][2]
    assert calls[1][1] == ["-X", "POST", "repos/o/r/git/refs"] and calls[1][2]["sha"] == "a" * 40
    # new rulesets (main, release/*) first, then the one of the same name is replaced
    assert [c[1] for c in calls[2:]] == [
        ["-X", "POST", "repos/o/r/rulesets"],
        ["-X", "POST", "repos/o/r/rulesets"],
        ["-X", "PUT", "repos/o/r/rulesets/7"],
    ]


def test_plan_refuses_a_repo_whose_default_is_not_develop(monkeypatch):
    monkeypatch.setattr(kit, "gh", FakeGitHub({"default_branch": "main"}, True, []))
    with pytest.raises(SystemExit):
        kit.plan("o/r", 1, [])


def test_a_ruleset_that_already_matches_is_left_alone(monkeypatch):
    repo = {"default_branch": "develop", **kit.REPO_SETTINGS}
    fake = FakeGitHub(
        repo,
        True,
        [
            {"name": "develop: golden pipeline", "id": 7},
            {"name": "main: golden pipeline", "id": 8},
            {"name": "release: golden pipeline", "id": 9},
        ],
    )
    develop, main, release = kit.rulesets(1, [])
    live = {**develop, "id": 7, "source": "o/r"}  # the API adds fields and defaults
    live["rules"] = [
        {**r, "parameters": {**r["parameters"], "required_reviewers": []}} if "parameters" in r else r
        for r in develop["rules"]
    ]
    fake.live = {7: live, 8: {**main, "enforcement": "evaluate"}, 9: release}
    monkeypatch.setattr(kit, "gh", fake)
    calls = kit.plan("o/r", 1, [])
    assert [c[1] for c in calls] == [["-X", "PUT", "repos/o/r/rulesets/8"]]  # only the drifted one


def test_approvals_need_a_team_in_codeowners(monkeypatch):
    fake = FakeGitHub({"default_branch": "develop"}, True, [])
    fake.codeowners = "# owners\n* @someone\n/.github/ @o/team\n"
    monkeypatch.setattr(kit, "gh", fake)
    with pytest.raises(SystemExit):
        kit.plan("o/r", 1, [])
    assert kit.plan("o/r", 0, [])  # a one-person repo can still take the rest of the kit


def test_release_app_may_bypass_develop_only():
    develop, main, release = kit.rulesets(1, [], 5568680)
    assert develop["bypass_actors"] == [{"actor_id": 5568680, "actor_type": "Integration", "bypass_mode": "always"}]
    assert main["bypass_actors"] == []
    assert release["bypass_actors"] == []
    assert kit.rulesets(1, [])[0]["bypass_actors"] == []


# A rerun never removes a check, or anything silently; a bypass the kit didn't grant is removed unless it is named.

APP = {"actor_id": 5568680, "actor_type": "Integration", "bypass_mode": "always"}
KIRO = {"context": "Kiro review result", "integration_id": 15368}


def _all_rulesets_live(fake, approvals=1, checks=(), app=None):
    """A repo where the kit has already run with these arguments."""
    names = {}
    for i, rs in enumerate(kit.rulesets(approvals, list(checks), app), start=7):
        fake.live[i], names[rs["name"]] = {**rs, "id": i}, i
    fake.rulesets = [{"name": n, "id": i} for n, i in names.items()]
    return names


def _settled_repo():
    return FakeGitHub({"default_branch": "develop", **kit.REPO_SETTINGS}, True, [])


APP_KEY = ("Integration", 5568680)


def test_a_rerun_removes_a_bypass_the_kit_was_not_given_and_says_so(monkeypatch):
    # James's approval of #14: a bypass weakens the ruleset, so one the kit didn't grant goes by default (v1.1.2)
    fake = _settled_repo()
    names = _all_rulesets_live(fake, app=5568680)
    fake.live[names["main: golden pipeline"]]["bypass_actors"] = [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
    ]
    monkeypatch.setattr(kit, "gh", fake)
    assert kit.plan("o/r", 1, [], 5568680)[-1][0] == (
        "replace ruleset 'main: golden pipeline':\n    REMOVE bypass actor RepositoryRole 5 (always)"
    )
    calls = kit.plan("o/r", 1, [])  # without the app id, its bypass goes too
    develop = next(c for c in calls if c[1][-1].endswith(f"/{names['develop: golden pipeline']}"))
    assert "REMOVE bypass actor Integration 5568680 (always)" in develop[0] and develop[2]["bypass_actors"] == []
    keep = frozenset({APP_KEY, ("RepositoryRole", 5)})
    assert kit.plan("o/r", 1, [], keep_bypass=keep) == []  # named: kept


def test_a_rerun_without_a_check_keeps_it_and_its_app(monkeypatch):
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    live = fake.live[names["develop: golden pipeline"]]
    kit._status_checks(live).append(KIRO)
    live["bypass_actors"] = [APP]
    fake.live[names["main: golden pipeline"]]["enforcement"] = "evaluate"  # drift elsewhere: a replacement happens
    monkeypatch.setattr(kit, "gh", fake)
    assert kit.plan("o/r", 1, [], keep_bypass=frozenset({APP_KEY})) == [
        (
            "replace ruleset 'main: golden pipeline':\n    change enforcement: evaluate -> active",
            ["-X", "PUT", f"repos/o/r/rulesets/{names['main: golden pipeline']}"],
            kit.rulesets(1, [])[1],
        )
    ]
    # and when develop does need replacing, the PUT body still carries the bypass and the check, pinned to its app
    live["rules"] = [r for r in live["rules"] if r["type"] != "non_fast_forward"]
    calls = kit.plan("o/r", 1, [], keep_bypass=frozenset({APP_KEY}))
    body = next(c[2] for c in calls if c[1][-1].endswith(f"/{names['develop: golden pipeline']}"))
    assert body["bypass_actors"] == [APP]
    assert KIRO in kit._status_checks(body)
    assert "REMOVE" not in "".join(c[0] for c in calls)


def test_drop_unlisted_removes_extras_and_says_so(monkeypatch):
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    live = fake.live[names["develop: golden pipeline"]]
    kit._status_checks(live).append(KIRO)
    live["bypass_actors"] = [APP]
    monkeypatch.setattr(kit, "gh", fake)
    [(desc, args, body)] = kit.plan("o/r", 1, [], drop_unlisted=True)
    assert "REMOVE bypass actor Integration 5568680 (always)" in desc
    assert "REMOVE required check 'Kiro review result'" in desc
    assert body["bypass_actors"] == []
    assert KIRO not in kit._status_checks(body)


def test_drop_unlisted_unpins_a_kit_check_and_says_so(monkeypatch):
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    kit._status_checks(fake.live[names["develop: golden pipeline"]])[0]["integration_id"] = 15368
    monkeypatch.setattr(kit, "gh", fake)
    assert kit.plan("o/r", 1, []) == []  # kept: the pin is a live extra
    [(desc, _, body)] = kit.plan("o/r", 1, [], drop_unlisted=True)
    assert "change required check 'golden-pipeline': app 15368 -> any app" in desc
    assert "integration_id" not in kit._status_checks(body)[0]


def test_every_kept_extra_is_listed_on_each_run(monkeypatch, capsys):
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    develop = fake.live[names["develop: golden pipeline"]]
    kit._status_checks(develop)[0]["integration_id"] = 15368
    kit._status_checks(develop).append(KIRO)
    kit._status_checks(fake.live[names["main: golden pipeline"]]).append({"context": "lint"})
    develop["bypass_actors"] = [APP, {"actor_id": 5, "actor_type": "Team", "bypass_mode": "pull_request"}]
    monkeypatch.setattr(kit, "gh", fake)
    kept: list[str] = []
    keep = frozenset({APP_KEY, ("Team", 5)})
    assert kit.plan("o/r", 1, [], kept=kept, keep_bypass=keep) == []  # nothing to change, every extra is listed
    assert sorted(kept) == sorted(
        [
            "ruleset 'develop: golden pipeline': bypass actor Integration 5568680 (always), kept by --keep-bypass",
            "ruleset 'develop: golden pipeline': bypass actor Team 5 (pull_request), kept by --keep-bypass",
            "ruleset 'develop: golden pipeline': required check 'golden-pipeline' pinned to app 15368",
            "ruleset 'develop: golden pipeline': required check 'Kiro review result' (app 15368)",
            "ruleset 'main: golden pipeline': required check 'lint' (any app)",
        ]
    )
    assert kit.main(["o/r", "--keep-bypass", "Integration:5568680", "--keep-bypass", "Team:5"]) == 0
    out = capsys.readouterr().out
    assert "--drop-unlisted removes the checks" in out
    for k in kept:
        assert k in out
    kept = []
    kit.plan("o/r", 1, [], drop_unlisted=True, kept=kept)
    assert kept == []  # nothing is kept, so nothing is listed


def test_changes_lists_what_is_added_and_removed():
    old_develop = kit.ruleset(
        "develop: golden pipeline",
        ["refs/heads/develop", "refs/heads/release/*"],
        ["squash"],
        1,
        kit.CHECKS,
        [APP],
    )
    want = kit.rulesets(1, ["Kiro review result"])[0]
    diff = kit.changes(want, old_develop)
    assert "REMOVE include refs/heads/release/*" in diff
    assert "REMOVE bypass actor Integration 5568680 (always)" in diff
    assert "add required check 'Kiro review result'" in diff


def test_unreadable_bypass_actors_stop_the_kit(monkeypatch):
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    del fake.live[names["develop: golden pipeline"]]["bypass_actors"]  # what a non-admin is shown
    monkeypatch.setattr(kit, "gh", fake)
    with pytest.raises(SystemExit, match="bypass actors"):
        kit.plan("o/r", 1, [])


# release/* can be created from develop and deleted after merge; develop keeps its protections.


def test_release_branches_can_be_created_and_deleted_but_develop_cannot():
    develop, main, release = kit.rulesets(1, [])
    assert release["conditions"]["ref_name"]["include"] == ["refs/heads/release/*"]
    assert "refs/heads/release/*" not in develop["conditions"]["ref_name"]["include"]
    assert {"type": "deletion"} not in release["rules"]
    assert _rule(release, "required_status_checks")["do_not_enforce_on_create"] is True
    assert {"type": "non_fast_forward"} in release["rules"]
    assert _rule(release, "pull_request")["allowed_merge_methods"] == ["squash"]
    for rs in (develop, main):
        assert {"type": "deletion"} in rs["rules"]
        assert _rule(rs, "required_status_checks")["do_not_enforce_on_create"] is False


def test_an_old_develop_ruleset_hands_release_branches_to_the_new_one_first(monkeypatch):
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    rid = names.pop("release: golden pipeline")
    del fake.live[rid]
    fake.rulesets = [{"name": n, "id": i} for n, i in names.items()]
    develop = fake.live[names["develop: golden pipeline"]]
    develop["conditions"] = {"ref_name": {"include": ["refs/heads/develop", "refs/heads/release/*"], "exclude": []}}
    monkeypatch.setattr(kit, "gh", fake)
    calls = kit.plan("o/r", 1, [])
    assert [c[1] for c in calls] == [
        ["-X", "POST", "repos/o/r/rulesets"],
        ["-X", "PUT", f"repos/o/r/rulesets/{names['develop: golden pipeline']}"],
    ]
    assert calls[0][2]["name"] == "release: golden pipeline"
    assert "REMOVE include refs/heads/release/*" in calls[1][0]


# The release app's key lives in the release environment, deployable from main only.

ENV = ["repos/o/r/environments/release"]
POLICIES = "repos/o/r/environments/release/deployment-branch-policies"


def test_the_app_id_creates_the_release_environment_for_main_only(monkeypatch):
    fake = _settled_repo()
    _all_rulesets_live(fake, app=5568680)
    monkeypatch.setattr(kit, "gh", fake)
    assert [(c[1], c[2]) for c in kit.plan("o/r", 1, [], 5568680)] == [
        (
            ["-X", "PUT", *ENV],
            {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}},
        ),
        (["-X", "POST", POLICIES], {"name": "main", "type": "branch"}),
    ]
    assert not any("environment" in c[0] for c in kit.plan("o/r", 1, []))  # no app, no environment


def test_the_release_environment_keeps_main_and_drops_other_branches(monkeypatch):
    fake = _settled_repo()
    _all_rulesets_live(fake, app=5568680)
    fake.env = {"name": "release", "deployment_branch_policy": dict(kit.RELEASE_ENV_POLICY)}
    fake.policies = [
        {"id": 1, "name": "develop", "type": "branch"},
        {"id": 2, "name": "main", "type": "branch"},
        {"id": 3, "name": "main", "type": "tag"},
    ]
    monkeypatch.setattr(kit, "gh", fake)
    assert [c[1] for c in kit.plan("o/r", 1, [], 5568680)] == [
        ["-X", "DELETE", f"{POLICIES}/1"],
        ["-X", "DELETE", f"{POLICIES}/3"],
    ]
    fake.policies = [{"id": 2, "name": "main", "type": "branch"}]
    assert kit.plan("o/r", 1, [], 5568680) == []


def test_a_release_environment_open_to_all_branches_is_narrowed(monkeypatch):
    fake = _settled_repo()
    _all_rulesets_live(fake, app=5568680)
    fake.env = {"name": "release", "deployment_branch_policy": None}
    monkeypatch.setattr(kit, "gh", fake)
    calls = kit.plan("o/r", 1, [], 5568680)
    assert [c[1][:2] for c in calls] == [["-X", "PUT"], ["-X", "POST"]]
    assert calls[0][0].startswith("update environment 'release'")


def test_an_environment_api_error_stops_the_kit(monkeypatch):
    fake = _settled_repo()
    _all_rulesets_live(fake, app=5568680)
    fake.fail = ENV[0]
    monkeypatch.setattr(kit, "gh", fake)
    with pytest.raises(SystemExit):
        kit.plan("o/r", 1, [], 5568680)


def test_the_kit_never_touches_a_secret(monkeypatch):
    fake = _settled_repo()
    _all_rulesets_live(fake)
    monkeypatch.setattr(kit, "gh", fake)
    calls = kit.plan("o/r", 1, [], 5568680)
    assert calls
    assert not any("secrets" in part for c in calls for part in c[1])


def test_a_repository_key_is_reported_with_the_command_to_delete_it(monkeypatch):
    fake = _settled_repo()
    fake.repo_secret = True
    fake.env = {"name": "release", "deployment_branch_policy": dict(kit.RELEASE_ENV_POLICY)}
    fake.policies = [{"id": 2, "name": "main", "type": "branch"}]
    fake.env_secret = True
    monkeypatch.setattr(kit, "gh", fake)
    [problem], [] = kit.release_key_findings("o/r")
    assert problem.startswith("ERROR")
    assert "gh secret delete RELEASE_APP_PRIVATE_KEY -R o/r" in problem
    fake.repo_secret = False
    assert kit.release_key_findings("o/r") == ([], [])


def test_a_missing_environment_key_is_reported_with_the_command_to_set_it(monkeypatch):
    fake = _settled_repo()
    monkeypatch.setattr(kit, "gh", fake)  # no environment yet
    [], [note] = kit.release_key_findings("o/r")
    assert "gh secret set RELEASE_APP_PRIVATE_KEY --env release -R o/r" in note
    fake.env = {"name": "release"}  # environment, but no secret in it
    [], [note] = kit.release_key_findings("o/r")
    assert "gh secret set RELEASE_APP_PRIVATE_KEY --env release -R o/r" in note


def test_a_secret_api_error_stops_the_kit(monkeypatch):
    fake = _settled_repo()
    fake.fail = "repos/o/r/actions/secrets/RELEASE_APP_PRIVATE_KEY"
    monkeypatch.setattr(kit, "gh", fake)
    with pytest.raises(SystemExit):
        kit.release_key_findings("o/r")


@pytest.mark.parametrize(
    ("stderr", "absent"), [("gh: Not Found (HTTP 404)", True), ("gh: Forbidden (HTTP 403)", False)]
)
def test_gh_reads_only_a_404_as_absent(monkeypatch, stderr, absent):
    class Done:
        returncode, stdout = 1, '{"message": "x"}'

    Done.stderr = stderr
    monkeypatch.setattr(kit.subprocess, "run", lambda *a, **k: Done)
    if absent:
        assert kit.gh("repos/o/r/environments/release", ok_404=True) is None
    else:
        with pytest.raises(SystemExit):
            kit.gh("repos/o/r/environments/release", ok_404=True)


def test_a_repository_key_fails_the_kit_after_it_has_run(monkeypatch, capsys):
    fake = _settled_repo()
    _all_rulesets_live(fake, app=5568680)
    fake.env = {"name": "release", "deployment_branch_policy": dict(kit.RELEASE_ENV_POLICY)}
    fake.policies = [{"id": 2, "name": "main", "type": "branch"}]
    fake.env_secret = True
    fake.repo_secret = True
    monkeypatch.setattr(kit, "gh", fake)
    assert kit.main(["o/r", "--release-app-id", "5568680"]) == 1
    out, err = capsys.readouterr()
    assert "already has the golden-pipeline settings" in out  # what it did is still shown
    assert "ERROR: the repository secret RELEASE_APP_PRIVATE_KEY exists" in err
    fake.repo_secret = False
    assert kit.main(["o/r", "--release-app-id", "5568680"]) == 0
    fake.env_secret = False  # no key in the environment yet: a note, not a failure
    assert kit.main(["o/r", "--release-app-id", "5568680"]) == 0
    assert "gh secret set RELEASE_APP_PRIVATE_KEY --env release" in capsys.readouterr().err


def test_an_organization_key_visible_to_the_repo_fails_the_kit(monkeypatch):
    fake = _settled_repo()
    fake.env = {"name": "release", "deployment_branch_policy": dict(kit.RELEASE_ENV_POLICY)}
    fake.env_secret = True
    fake.policies = [{"id": 2, "name": "main", "type": "branch"}]
    fake.org_secrets = [f"S{i}" for i in range(150)] + ["RELEASE_APP_PRIVATE_KEY"]  # on the second page
    monkeypatch.setattr(kit, "gh", fake)
    [problem], [] = kit.release_key_findings("o/r")
    assert problem.startswith("ERROR") and "organization secret RELEASE_APP_PRIVATE_KEY" in problem
    fake.org_secrets = ["OTHER"]
    assert kit.release_key_findings("o/r") == ([], [])


def test_unreadable_organization_secrets_stop_apply_before_any_write(monkeypatch, capsys):
    # James's approval of #14: an org secret the kit can't rule out is a problem, not a note
    fake = _settled_repo()
    fake.env = {"name": "release", "deployment_branch_policy": dict(kit.RELEASE_ENV_POLICY)}
    fake.policies = [{"id": 2, "name": "main", "type": "branch"}]
    fake.env_secret = True
    fake.org_secrets = None  # 403 or 404
    writes = []

    def gh(*args, body=None, ok_404=False, ok_403=False):
        if "-X" in args:
            writes.append(args)
        return fake(*args, body=body, ok_404=ok_404, ok_403=ok_403)

    monkeypatch.setattr(kit, "gh", gh)
    [problem], [] = kit.release_key_findings("o/r")
    assert problem.startswith("ERROR") and "couldn't list the organization secrets" in problem
    assert kit.main(["o/r", "--release-app-id", "5568680", "--apply"]) == 1
    assert writes == []
    assert "Nothing applied" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arg", "actor"),
    [("Team:5", ("Team", 5)), ("OrganizationAdmin:1", ("OrganizationAdmin", None)), ("DeployKey", ("DeployKey", None))],
)
def test_keep_bypass_takes_type_and_id(arg, actor):
    assert kit._bypass_actor(arg) == actor


@pytest.mark.parametrize("arg", ["", ":5", "Team:", "Team:x", "team:5", "Team"])
def test_keep_bypass_refuses_anything_else(arg):
    with pytest.raises(kit.argparse.ArgumentTypeError):
        kit._bypass_actor(arg)


def test_an_unsafe_key_stops_apply_before_any_write(monkeypatch, capsys):
    # James's review: the develop bypass must not be installed while the key is readable from any branch
    fake = FakeGitHub({"default_branch": "develop"}, False, [])  # a bootstrap: everything is still to do
    fake.repo_secret = True
    writes = []

    def gh(*args, body=None, ok_404=False, ok_403=False):
        if "-X" in args:
            writes.append(args)
        return fake(*args, body=body, ok_404=ok_404, ok_403=ok_403)

    monkeypatch.setattr(kit, "gh", gh)
    assert kit.main(["o/r", "--release-app-id", "5568680", "--apply"]) == 1
    assert writes == []
    assert "Nothing applied" in capsys.readouterr().err
    assert kit.main(["o/r", "--release-app-id", "5568680"]) == 1  # a dry run still shows the plan, then fails
    assert "would create ruleset" in capsys.readouterr().out


def test_release_environment_protection_rules_fail_the_kit(monkeypatch):
    fake = _settled_repo()
    fake.env_secret = True
    fake.policies = [{"id": 2, "name": "main", "type": "branch"}]
    fake.env = {
        "name": "release",
        "deployment_branch_policy": dict(kit.RELEASE_ENV_POLICY),
        "protection_rules": [
            {"id": 1, "type": "branch_policy"},
            {"id": 2, "type": "required_reviewers", "reviewers": []},
            {"id": 3, "type": "wait_timer", "wait_timer": 5},
        ],
    }
    monkeypatch.setattr(kit, "gh", fake)
    [problem], [] = kit.release_key_findings("o/r")
    assert "protection rules (required_reviewers, wait_timer)" in problem
    fake.env["protection_rules"] = [{"id": 1, "type": "branch_policy"}]  # the kit's own main-only policy is fine
    assert kit.release_key_findings("o/r") == ([], [])


def test_same_named_checks_from_two_apps_are_two_checks(monkeypatch):
    # James's review: keyed on context alone, drift elsewhere dropped one of the two
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    develop = fake.live[names["develop: golden pipeline"]]
    checks = kit._status_checks(develop)
    checks[:] = [
        {"context": "golden-pipeline", "integration_id": 111},
        {"context": "golden-pipeline", "integration_id": 222},
        {"context": "lint", "integration_id": 111},
        {"context": "lint", "integration_id": 222},
    ]
    develop["enforcement"] = "evaluate"  # drift: the kit replaces the ruleset
    monkeypatch.setattr(kit, "gh", fake)
    kept: list[str] = []
    calls = kit.plan("o/r", 1, [], kept=kept)
    body = next(c[2] for c in calls if c[1][-1].endswith(f"/{names['develop: golden pipeline']}"))
    have = list(map(kit._check_key, kit._status_checks(body)))
    assert [k for k in have if k[1]] == [
        ("golden-pipeline", 111),
        ("golden-pipeline", 222),
        ("lint", 111),
        ("lint", 222),
    ]
    assert ("golden-pipeline", None) not in have  # kept as the two pinned ones, not added unpinned beside them
    assert "REMOVE" not in "".join(c[0] for c in calls)
    assert "ruleset 'develop: golden pipeline': required check 'lint' (app 222)" in kept
    [(desc, _, body)] = kit.plan("o/r", 1, [], drop_unlisted=True)
    for app in (111, 222):
        assert f"REMOVE required check 'golden-pipeline' (app {app})" in desc
        assert f"REMOVE required check 'lint' (app {app})" in desc
    assert "add required check 'golden-pipeline'" in desc
    assert kit._status_checks(body) == [{"context": c} for c in kit.CHECKS]


@pytest.mark.parametrize(
    ("policy", "policies"),
    [
        (None, []),  # every branch
        ({"protected_branches": True, "custom_branch_policies": False}, []),  # every protected branch, develop too
        (dict(kit.RELEASE_ENV_POLICY), [{"id": 1, "name": "main", "type": "branch"}, {"id": 2, "name": "develop"}]),
        (dict(kit.RELEASE_ENV_POLICY), [{"id": 1, "name": "*", "type": "branch"}]),
        (dict(kit.RELEASE_ENV_POLICY), [{"id": 1, "name": "main", "type": "tag"}]),
    ],
)
def test_a_key_in_an_environment_wider_than_main_stops_apply_before_any_write(monkeypatch, capsys, policy, policies):
    # James's fourth review: the app's develop bypass must not be granted while other branches can deploy the key
    fake = _settled_repo()
    fake.env = {"name": "release", "deployment_branch_policy": policy}
    fake.policies = policies
    fake.env_secret = True
    writes = []
    real = fake.__call__

    def record(*args, **kw):
        if "-X" in args:
            writes.append(args)
        return real(*args, **kw)

    monkeypatch.setattr(kit, "gh", record)
    assert kit.main(["o/r", "--release-app-id", "5568680", "--apply"]) == 1
    assert writes == []
    err = capsys.readouterr().err
    assert "deploys from more than the branch main" in err
    assert "gh secret delete RELEASE_APP_PRIVATE_KEY --env release -R o/r" in err


def test_the_environment_is_narrowed_before_the_app_gets_its_bypass(monkeypatch):
    fake = _settled_repo()
    monkeypatch.setattr(kit, "gh", fake)
    calls = [c[1][-1] for c in kit.plan("o/r", 1, [], 5568680)]
    env_at = min(i for i, path in enumerate(calls) if "environments/release" in path)
    assert all(i > env_at for i, path in enumerate(calls) if "rulesets" in path)


def test_keep_bypass_matches_an_org_admin_by_type_and_warns_when_nothing_matches(monkeypatch, capsys):
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    fake.live[names["main: golden pipeline"]]["bypass_actors"] = [
        {"actor_id": None, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}
    ]
    monkeypatch.setattr(kit, "gh", fake)
    assert kit.main(["o/r", "--keep-bypass", "OrganizationAdmin:1", "--keep-bypass", "Team:9"]) == 0
    out, err = capsys.readouterr()
    assert "bypass actor OrganizationAdmin None (always), kept by --keep-bypass" in out
    assert "already has the golden-pipeline settings" in out
    assert "--keep-bypass Team:9 matches no live bypass actor" in err
    assert "OrganizationAdmin" not in err


def test_keeping_an_app_bypass_checks_the_release_key_too(monkeypatch, capsys):
    # #18's review: --keep-bypass Integration:<release app> without --release-app-id kept the app's bypass of develop
    # but skipped the key checks, so a repository secret holding the key went unreported
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    fake.live[names["develop: golden pipeline"]]["bypass_actors"] = [
        {"actor_id": 5568680, "actor_type": "Integration", "bypass_mode": "always"}
    ]
    fake.repo_secret = True
    writes = []

    def gh(*args, body=None, ok_404=False, ok_403=False):
        if "-X" in args:
            writes.append(args)
        return fake(*args, body=body, ok_404=ok_404, ok_403=ok_403)

    monkeypatch.setattr(kit, "gh", gh)
    assert kit.main(["o/r", "--keep-bypass", "Integration:5568680", "--apply"]) == 1
    err = capsys.readouterr().err
    assert "repository secret RELEASE_APP_PRIVATE_KEY exists" in err and "Nothing applied" in err
    assert writes == []
    fake.repo_secret = False
    assert kit.main(["o/r", "--keep-bypass", "Integration:5568680"]) == 0
    assert "has no secret" not in capsys.readouterr().err  # no release environment is asked for here


def test_a_kept_app_bypass_reports_unlistable_org_secrets_as_a_note(monkeypatch, capsys):
    # #20's review: a repo with no releases that keeps another app's bypass shouldn't be stopped by an org-secret
    # list it can't read; the release-key fixes then name --release-app-id, which narrowing the environment needs
    fake = _settled_repo()
    names = _all_rulesets_live(fake)
    fake.live[names["develop: golden pipeline"]]["bypass_actors"] = [
        {"actor_id": 99, "actor_type": "Integration", "bypass_mode": "always"}
    ]
    fake.org_secrets = None  # 403 or 404
    monkeypatch.setattr(kit, "gh", fake)
    assert kit.main(["o/r", "--keep-bypass", "Integration:99"]) == 0
    assert "couldn't list the organization secrets" in capsys.readouterr().err
    assert kit.release_key_findings("o/r")[0]  # still a problem with --release-app-id
    fake.org_secrets = []
    fake.env = {"name": "release", "deployment_branch_policy": None}
    fake.env_secret = True
    [problem], _ = kit.release_key_findings("o/r", for_app=False)
    assert "run the kit with --release-app-id <the app id> --apply" in problem
