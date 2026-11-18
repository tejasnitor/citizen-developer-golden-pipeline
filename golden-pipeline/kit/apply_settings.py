"""Give a repo the golden pipeline's branch settings: a main branch, the merge methods and three rulesets.

    python3 golden-pipeline/kit/apply_settings.py OWNER/REPO            # show what would change
    python3 golden-pipeline/kit/apply_settings.py OWNER/REPO --apply    # change it (needs repo admin)

What it sets, and the requirement of the architecture page each part meets:

- a `main` branch, created at develop's head if missing (2.1 GitFlow-lite, 2.3);
- repo merge methods: squash and merge commits on, rebase off, head branches deleted after merge (2.2, 2.5);
- ruleset "develop: golden pipeline" on develop: pull request with approvals, stale approvals dismissed, code-owner
  review, approval of the last push, conversations resolved, squash only; the required checks, with the branch up to
  date before merge; no deletion or force-push (2.2, 2.5, 2.6, 2.9, P8, 12.9, 15.1);
- ruleset "release: golden pipeline" on release/*: the same pull-request rules and checks, except that a release
  branch can be created from develop without the PR-only checks (do_not_enforce_on_create) and deleted once merged
  (no deletion rule), so `delete_branch_on_merge` cleans it up; force-push stays blocked;
- with --release-app-id, that app may bypass the develop ruleset, so gp-release.yml can merge its back-merge PR (main
  into develop) with a merge commit; a squash would leave develop "behind" main forever (2.7). The kit then also makes
  the "release" environment, whose deployments only main may run, to hold the app's private key, and reports where
  that key is stored (it never reads or writes a secret). Before any write, it checks that the key can't be read
  from another branch: a repository secret of that name, an organization secret the repo can see, an organization
  secret list it can't read (HTTP 403/404), protection rules on the environment, or the key in an environment that
  deploys from more than main. Any of these stops --apply before it changes anything, and fails a dry run after it
  has shown the plan;
- ruleset "main: golden pipeline" on main: the same as develop, but merge commits only, and branch-target (which allows
  only develop, release/*, hotfix/* and the release app's release-please branch) is required (2.3, 9.3, 15.1);
- with --kiro-review, the Kiro review result is a second required check on all three rulesets (3.1): by default
  `kiro-review / Kiro gate`, the gate job of kiro-review.yml called from a caller job named `kiro-review`. Give
  another context, such as `--kiro-review "review / Kiro gate"`, when the caller job has another name. Add the
  kiro-review.yml caller to the repo first, or every PR waits for a check that never reports.

A ruleset of the same name is replaced if it differs, so running it again brings a drifted repo back to the kit.
Bypass actors the kit didn't grant are removed (a bypass weakens the ruleset, so it must be asked for): rerun with
the same --release-app-id, and name any other bypass to keep with --keep-bypass TYPE[:ID] (Team:5, Integration:123;
OrganizationAdmin, DeployKey and EnterpriseOwner by type alone); one that matches no live actor is warned about. A
kept Integration gets the same release-key checks as --release-app-id, since it may be the release app.
Required checks the live ruleset has beyond the kit's are kept (they only add to the gate), unless --drop-unlisted
asks to remove them. Every replacement lists what it adds and REMOVES, and each run lists every live extra it keeps
(bypass actor type and id, required check and the app it is pinned to), so none stays unnoticed.
Nothing else changes: other rulesets, the org's ruleset and the default branch are left alone.

Code-owner review needs a CODEOWNERS on develop whose every rule names a team: with only people as owners, an author
who is the sole owner could never get an approval. The kit refuses to require approvals until then (--approvals 0
applies the rest). Requires the gh CLI, signed in.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import subprocess
import sys

CHECKS = ["golden-pipeline", "pr-checks / pr-title", "pr-checks / commits", "pr-checks / branch-target"]
KIRO_CHECK = "kiro-review / Kiro gate"  # kiro-review.yml's gate job, called from a caller job named kiro-review
REPO_SETTINGS = {
    "allow_squash_merge": True,
    "allow_merge_commit": True,
    "allow_rebase_merge": False,
    "delete_branch_on_merge": True,
    "squash_merge_commit_title": "PR_TITLE",
    "squash_merge_commit_message": "COMMIT_MESSAGES",
}
RELEASE_ENV = "release"  # gp-release.yml's `environment` input defaults to this
RELEASE_SECRET = "RELEASE_APP_PRIVATE_KEY"  # noqa: S105  # the secret's name, not its value
RELEASE_ENV_POLICY = {"protected_branches": False, "custom_branch_policies": True}


def ruleset(
    name: str,
    include: list[str],
    merge_methods: list[str],
    approvals: int,
    checks: list[str],
    bypass: list[dict],
    *,
    release_branch: bool = False,
) -> dict:
    """One golden-pipeline branch ruleset, as the rulesets API takes it.

    A release branch may be created without the required checks (its first commit is develop's head, which never ran
    the PR-only checks) and may be deleted, so it is cleaned up once merged."""
    rules: list[dict] = [] if release_branch else [{"type": "deletion"}]
    return {
        "name": name,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": bypass,
        "conditions": {"ref_name": {"include": include, "exclude": []}},
        "rules": rules
        + [
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": approvals,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": approvals > 0,
                    "require_last_push_approval": approvals > 0,
                    "required_review_thread_resolution": True,
                    "allowed_merge_methods": merge_methods,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": release_branch,
                    "required_status_checks": [{"context": c} for c in checks],
                },
            },
        ],
    }


def rulesets(approvals: int, extra_checks: list[str], release_app_id: int | None = None) -> list[dict]:
    """develop, main and release/*, in that order."""
    checks = list(dict.fromkeys(CHECKS + extra_checks))  # each once, the kit's first
    # The release app merges its back-merge PR (main into develop) with a merge commit: gp-release.yml.
    app = [{"actor_id": release_app_id, "actor_type": "Integration", "bypass_mode": "always"}] if release_app_id else []
    return [
        ruleset("develop: golden pipeline", ["refs/heads/develop"], ["squash"], approvals, checks, app),
        ruleset("main: golden pipeline", ["refs/heads/main"], ["merge"], approvals, checks, []),
        ruleset(
            "release: golden pipeline",
            ["refs/heads/release/*"],
            ["squash"],
            approvals,
            checks,
            [],
            release_branch=True,
        ),
    ]


def _within(want: object, have: object) -> bool:
    """Every key of want is in have with the same value (lists compared item by item)."""
    if isinstance(want, dict):
        return isinstance(have, dict) and all(_within(v, have.get(k)) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(have, list) and len(want) == len(have) and all(map(_within, want, have))
    return want == have


def _actor(a: dict) -> tuple:
    return (a.get("actor_type"), a.get("actor_id"))


# Bypass actor types the rulesets API knows; the last three are one actor per org, enterprise or key type, matched
# by type alone (GitHub may show OrganizationAdmin's id as 1 or null).
ACTOR_TYPES = ("Integration", "Team", "RepositoryRole", "OrganizationAdmin", "DeployKey", "EnterpriseOwner")
TYPE_ONLY = ("OrganizationAdmin", "DeployKey", "EnterpriseOwner")


def _keep_key(a: dict) -> tuple:
    """The actor as --keep-bypass names it: (type, id), or (type, None) for a type matched alone."""
    t = a.get("actor_type")
    return (t, None) if t in TYPE_ONLY else (t, a.get("actor_id"))


def _status_checks(rs: dict) -> list[dict] | None:
    for r in rs.get("rules") or []:
        if r.get("type") == "required_status_checks":
            checks = (r.get("parameters") or {}).get("required_status_checks")
            return checks if isinstance(checks, list) else []
    return None


def keep_live_extras(want: dict, current: dict, keep_bypass: frozenset = frozenset(), checks: bool = True) -> dict:
    """want, plus the live ruleset's bypass actors named in keep_bypass ((type, id) pairs) and, with checks, the
    required checks it has that the kit was not given."""
    if not isinstance(current.get("bypass_actors"), list):
        sys.exit(
            f"can't read the bypass actors of ruleset '{current.get('name')}' (they are shown to repo admins only), "
            "so the kit can't tell what a replacement would remove; run it as a repo admin"
        )
    out = copy.deepcopy(want)
    mine = {_actor(a) for a in out["bypass_actors"]}
    out["bypass_actors"] += [
        {k: a[k] for k in ("actor_id", "actor_type", "bypass_mode") if k in a}
        for a in current["bypass_actors"]
        if _actor(a) not in mine and _keep_key(a) in keep_bypass
    ]
    if not checks:
        return out
    wanted, live = _status_checks(out), _status_checks(current) or []
    if wanted is not None:
        # A required check is (context, app), as GitHub and the back-merge gate key it: the same name from two apps is
        # two checks. A kit check (any app) the live ruleset already requires is kept as the live one(s), pinned to
        # their apps; one it doesn't is added; every other live check is kept.
        live_ctx = {c.get("context") for c in live}
        wanted[:] = [*live, *(c for c in wanted if c["context"] not in live_ctx)]
    return out


def _check_key(check: dict) -> tuple:
    return (check.get("context"), check.get("integration_id"))


def _app(check: dict) -> str:
    app = check.get("integration_id")
    return "any app" if app is None else f"app {app}"


def live_extras(want: dict, current: dict, keep_bypass: frozenset = frozenset(), checks: bool = True) -> list[str]:
    """What keep_live_extras(want, current, keep_bypass, checks) keeps that the kit was not given, one line each."""
    mine = {_actor(a) for a in want["bypass_actors"]}
    out = [
        f"bypass actor {a.get('actor_type')} {a.get('actor_id')} ({a.get('bypass_mode')}), kept by --keep-bypass"
        for a in current.get("bypass_actors") or []
        if _actor(a) not in mine and _keep_key(a) in keep_bypass
    ]
    if not checks:
        return out
    wanted = {c["context"] for c in _status_checks(want) or []}
    for c in _status_checks(current) or []:
        if c.get("context") not in wanted:
            out.append(f"required check '{c.get('context')}' ({_app(c)})")
        elif c.get("integration_id") is not None:
            out.append(f"required check '{c.get('context')}' pinned to {_app(c)}")
    return out


def _bypass_changes(want: dict, current: dict) -> list[str]:
    w = {_actor(a): a for a in want["bypass_actors"]}
    h = {_actor(a): a for a in current.get("bypass_actors") or []}
    out = [f"add bypass actor {k[0]} {k[1]} ({w[k].get('bypass_mode')})" for k in w if k not in h]
    out += [f"REMOVE bypass actor {k[0]} {k[1]} ({h[k].get('bypass_mode')})" for k in h if k not in w]
    out += [
        f"change bypass actor {k[0]} {k[1]}: {h[k].get('bypass_mode')} -> {w[k].get('bypass_mode')}"
        for k in w
        if k in h and w[k].get("bypass_mode") != h[k].get("bypass_mode")
    ]
    return out


def _scope_changes(want: dict, current: dict) -> list[str]:
    out = [
        f"change {key}: {current.get(key)} -> {want[key]}"
        for key in ("target", "enforcement")
        if want[key] != current.get(key)
    ]
    have_ref = (current.get("conditions") or {}).get("ref_name") or {}
    for part, want_refs in want["conditions"]["ref_name"].items():
        have_refs = have_ref.get(part) or []
        out += [f"add {part} {r}" for r in want_refs if r not in have_refs]
        out += [f"REMOVE {part} {r}" for r in have_refs if r not in want_refs]
    return out


def _check_changes(want: dict, current: dict) -> list[str]:
    """Checks keyed on (context, app); a context that loses one app and gains another is shown as one change."""
    w = {_check_key(c): c for c in _status_checks(want) or []}
    h = {_check_key(c): c for c in _status_checks(current) or []}
    added = [k for k in w if k not in h]
    removed = [k for k in h if k not in w]
    out = []
    for ctx in dict.fromkeys(k[0] for k in [*added, *removed]):
        a = [w[k] for k in added if k[0] == ctx]
        r = [h[k] for k in removed if k[0] == ctx]
        if len(a) == 1 and len(r) == 1:
            out.append(f"change required check '{ctx}': {_app(r[0])} -> {_app(a[0])}")
            continue
        out += [f"add required check '{ctx}'" + (f" ({_app(c)})" if c.get("integration_id") else "") for c in a]
        out += [f"REMOVE required check '{ctx}' ({_app(c)})" for c in r]
    return out


def _rule_changes(want: dict, current: dict) -> list[str]:
    have_rules = {r["type"]: r.get("parameters") or {} for r in current.get("rules") or []}
    want_rules = {r["type"]: r.get("parameters") or {} for r in want["rules"]}
    out = [f"add rule {t}" for t in want_rules if t not in have_rules]
    out += [f"REMOVE rule {t}" for t in have_rules if t not in want_rules]
    out += [
        f"change {t}.{k}: {json.dumps(have_rules[t].get(k))} -> {json.dumps(v)}"
        for t, params in want_rules.items()
        if t in have_rules
        for k, v in params.items()
        if k != "required_status_checks" and not _within(v, have_rules[t].get(k))
    ]
    return out


def changes(want: dict, current: dict) -> list[str]:
    """What replacing the live ruleset with want adds, removes and changes; empty when it already is want."""
    return [
        *_bypass_changes(want, current),
        *_scope_changes(want, current),
        *_check_changes(want, current),
        *_rule_changes(want, current),
    ]


def matches(want: dict, current: dict) -> bool:
    """The live ruleset already is the kit's: same scope, enforcement, bypass and rules (the API adds defaults)."""
    return not changes(want, current)


def gh(*args: str, body: dict | None = None, ok_404: bool = False, ok_403: bool = False) -> dict | list | None:
    """gh api; None for an HTTP 404 only when ok_404 (or a 403 when ok_403), and any other failure stops the kit."""
    cmd = ["gh", "api", *args]
    if body is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None, capture_output=True, text=True)
    if r.returncode != 0:
        if (ok_404 and "HTTP 404" in r.stderr) or (ok_403 and "HTTP 403" in r.stderr):
            return None
        sys.exit(f"gh api {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def team_owned(contents: object) -> bool:
    """Every ownership line of .github/CODEOWNERS (a contents API answer) names at least one team."""
    if not isinstance(contents, dict) or "content" not in contents:
        return False
    text = base64.b64decode(contents["content"]).decode()
    rules = [line.split()[1:] for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    return bool(rules) and all(any("/" in owner for owner in owners) for owners in rules)


def _branch_policies(repo: str, env_path: str, env: dict | None) -> list[dict]:
    """The release environment's deployment branch policies; none when it has no custom branch policies."""
    if env is None or not (env.get("deployment_branch_policy") or {}).get("custom_branch_policies"):
        return []
    got = gh(f"{env_path}/deployment-branch-policies?per_page=100")
    if not isinstance(got, dict) or not isinstance(got.get("branch_policies"), list):
        sys.exit(f"gh api {env_path}/deployment-branch-policies: unexpected answer {got!r}")
    policies = got["branch_policies"]
    if got.get("total_count", len(policies)) > len(policies):
        sys.exit(f"{repo}: environment '{RELEASE_ENV}' has more than 100 deployment branch policies")
    return policies


def release_environment(repo: str) -> list[tuple[str, list[str], dict | None]]:
    """The calls that make the release environment deployable from the branch main only."""
    env_path = f"repos/{repo}/environments/{RELEASE_ENV}"
    env = gh(env_path, ok_404=True)
    if env is not None and not isinstance(env, dict):
        sys.exit(f"gh api {env_path}: unexpected answer {env!r}")
    calls: list[tuple[str, list[str], dict | None]] = []
    if env is None or env.get("deployment_branch_policy") != RELEASE_ENV_POLICY:
        calls.append(
            (
                f"{'create' if env is None else 'update'} environment '{RELEASE_ENV}': deployments from named "
                "branches only",
                ["-X", "PUT", env_path],
                {"deployment_branch_policy": RELEASE_ENV_POLICY},
            )
        )
    policies = _branch_policies(repo, env_path, env)
    keep = next((p for p in policies if p.get("name") == "main" and p.get("type", "branch") == "branch"), None)
    calls += [
        (
            f"remove deployment {p.get('type', 'branch')} policy '{p.get('name')}' from environment '{RELEASE_ENV}'",
            ["-X", "DELETE", f"{env_path}/deployment-branch-policies/{p['id']}"],
            None,
        )
        for p in policies
        if p is not keep
    ]
    if keep is None:
        calls.append(
            (
                f"allow deployments to environment '{RELEASE_ENV}' from branch main only",
                ["-X", "POST", f"{env_path}/deployment-branch-policies"],
                {"name": "main", "type": "branch"},
            )
        )
    return calls


def org_secret_names(repo: str) -> list[str] | None:
    """The names of the organization secrets the repo's workflows can read; None when GitHub won't list them to us
    (403 or 404). Every page is read."""
    names: list[str] = []
    for page in range(1, 1000):
        got = gh(f"repos/{repo}/actions/organization-secrets?per_page=100&page={page}", ok_404=True, ok_403=True)
        if got is None:
            return None
        if not isinstance(got, dict) or not isinstance(got.get("secrets"), list):
            sys.exit(f"gh api repos/{repo}/actions/organization-secrets: unexpected answer {got!r}")
        names += [s["name"] for s in got["secrets"] if isinstance(s, dict) and isinstance(s.get("name"), str)]
        if len(got["secrets"]) < 100 or len(names) >= got.get("total_count", 0):
            break
    return names


def release_key_findings(repo: str, for_app: bool = True) -> tuple[list[str], list[str]]:
    """Where the release app's private key is: it belongs in the release environment, never in the repo's secrets
    or in an organization secret the repo can read.

    Returns the problems (a repository or visible organization secret, organization secrets it can't list, protection
    rules on the environment, or a key in an environment that deploys from more than main, which fail the kit) and
    the notes (no key in the environment yet, which a first run expects). Only whether each secret
    exists is read; the kit never sets or deletes one. for_app is False when the check runs only because a kept
    Integration bypass may be the release app: then organization secrets it can't list are a note (the repo may have
    no release at all), and the fixes name --release-app-id, which the narrowing needs."""
    problems, notes = [], []
    if gh(f"repos/{repo}/actions/secrets/{RELEASE_SECRET}", ok_404=True) is not None:
        problems.append(
            f"ERROR: the repository secret {RELEASE_SECRET} exists. A workflow on any branch can read it and push "
            f"to develop as the release app, which defeats the '{RELEASE_ENV}' environment. Once the environment "
            f"has the key, delete it: gh secret delete {RELEASE_SECRET} -R {repo}"
        )
    org = org_secret_names(repo)
    if org is None:
        (problems if for_app else notes).append(
            f"ERROR: couldn't list the organization secrets {repo} can read (HTTP 403/404), so an organization "
            f"secret {RELEASE_SECRET}, which a workflow on any branch could read, isn't ruled out. Run the kit "
            f"signed in as a repo admin (gh auth status), or ask an org owner why "
            f"repos/{repo}/actions/organization-secrets is refused."
        )
    elif RELEASE_SECRET in org:
        problems.append(
            f"ERROR: the organization secret {RELEASE_SECRET} is visible to {repo}. A workflow on any branch can "
            f"read it and push to develop as the release app, which defeats the '{RELEASE_ENV}' environment. Keep "
            f"the key only in the environment: remove {repo} from the secret's repositories, or delete it."
        )
    env = gh(f"repos/{repo}/environments/{RELEASE_ENV}", ok_404=True)
    rules = [
        r
        for r in ((env if isinstance(env, dict) else {}).get("protection_rules") or [])
        if isinstance(r, dict) and r.get("type") != "branch_policy"
    ]
    if rules:
        kinds = ", ".join(sorted({str(r.get("type")) for r in rules}))
        problems.append(
            f"ERROR: the '{RELEASE_ENV}' environment has protection rules ({kinds}). The release job waits on them, "
            "so every release stalls until someone approves it or the timer runs out. Remove them in Settings > "
            f"Environments > {RELEASE_ENV}; the environment is only there to keep the key to main."
        )
    secret = None
    if env is not None:
        secret = gh(f"repos/{repo}/environments/{RELEASE_ENV}/secrets/{RELEASE_SECRET}", ok_404=True)
    if secret is None:
        notes.append(
            f"the '{RELEASE_ENV}' environment (made by --apply) has no secret {RELEASE_SECRET} yet; set it: "
            f"gh secret set {RELEASE_SECRET} --env {RELEASE_ENV} -R {repo}"
        )
    elif not _main_only(repo, env):
        problems.append(
            f"ERROR: the '{RELEASE_ENV}' environment holds {RELEASE_SECRET} but deploys from more than the branch "
            "main, so a workflow on another branch can read the key and push to develop as the release app. Delete "
            f"it (gh secret delete {RELEASE_SECRET} --env {RELEASE_ENV} -R {repo}), run the kit with "
            f"{'' if for_app else '--release-app-id <the app id> '}--apply to "
            f"narrow the environment, then set it again (gh secret set {RELEASE_SECRET} --env {RELEASE_ENV} -R {repo})."
        )
    return problems, notes


def _main_only(repo: str, env: dict | None) -> bool:
    """Whether the release environment deploys from the branch main and nothing else."""
    if not isinstance(env, dict) or env.get("deployment_branch_policy") != RELEASE_ENV_POLICY:
        return False
    policies = _branch_policies(repo, f"repos/{repo}/environments/{RELEASE_ENV}", env)
    return [(p.get("name"), p.get("type", "branch")) for p in policies] == [("main", "branch")]


def plan(
    repo: str,
    approvals: int,
    extra_checks: list[str],
    release_app_id: int | None = None,
    drop_unlisted: bool = False,
    kept: list[str] | None = None,
    keep_bypass: frozenset = frozenset(),
) -> list[tuple[str, list[str], dict | None]]:
    """The API calls that bring the repo to the kit: (description, gh api arguments, body).

    kept, when given, gets a line for each live bypass actor and required check the kit keeps but was not given;
    keep_bypass names the live bypass actors ((type, id) pairs) to keep beside the kit's."""
    calls = _repo_calls(repo, approvals)
    if release_app_id:  # first: the environment is narrowed to main before the app gets its bypass of develop
        calls += release_environment(repo)
    calls += _ruleset_calls(repo, rulesets(approvals, extra_checks, release_app_id), drop_unlisted, kept, keep_bypass)
    return calls


def _repo_calls(repo: str, approvals: int) -> list[tuple[str, list[str], dict | None]]:
    """The repo's own settings and its main branch; stops the kit when the repo isn't ready for them."""
    calls: list[tuple[str, list[str], dict | None]] = []
    info = gh(f"repos/{repo}")
    assert isinstance(info, dict)
    if info.get("default_branch") != "develop":
        sys.exit(f"{repo}: the default branch is {info.get('default_branch')}, not develop; set that first")
    if approvals > 0 and not team_owned(gh(f"repos/{repo}/contents/.github/CODEOWNERS?ref=develop", ok_404=True)):
        sys.exit(
            f"{repo}: code-owner review needs a CODEOWNERS on develop whose owners are teams (@org/team), or no one "
            "but the author could approve. Merge that first, or run with --approvals 0 now and again without it after."
        )
    changed = {k: v for k, v in REPO_SETTINGS.items() if info.get(k) != v}
    if changed:
        calls.append((f"repo settings {changed}", ["-X", "PATCH", f"repos/{repo}"], changed))
    if gh(f"repos/{repo}/branches/main", ok_404=True) is None:
        develop = gh(f"repos/{repo}/git/ref/heads/develop")
        assert isinstance(develop, dict)
        sha = develop["object"]["sha"]
        calls.append(
            (
                f"create main at develop ({sha[:7]})",
                ["-X", "POST", f"repos/{repo}/git/refs"],
                {"ref": "refs/heads/main", "sha": sha},
            )
        )
    return calls


def _ruleset_calls(
    repo: str,
    wanted: list[dict],
    drop_unlisted: bool,
    kept: list[str] | None = None,
    keep_bypass: frozenset = frozenset(),
) -> list[tuple[str, list[str], dict | None]]:
    """Create the kit's rulesets the repo lacks and replace those that differ."""
    calls: list[tuple[str, list[str], dict | None]] = []
    existing = gh(f"repos/{repo}/rulesets?includes_parents=false&per_page=100") or []
    assert isinstance(existing, list)
    by_name = {r["name"]: r["id"] for r in existing}
    # New rulesets first, so a branch moving to a new ruleset (release/* out of an older develop ruleset) is never
    # left without one in between.
    for rs in sorted(wanted, key=lambda r: r["name"] in by_name):
        if rs["name"] not in by_name:
            calls.append((f"create ruleset '{rs['name']}'", ["-X", "POST", f"repos/{repo}/rulesets"], rs))
            continue
        call = _replace_ruleset(repo, rs, by_name[rs["name"]], drop_unlisted, kept, keep_bypass)
        if call is not None:
            calls.append(call)
    return calls


def _replace_ruleset(
    repo: str,
    rs: dict,
    rs_id: int,
    drop_unlisted: bool,
    kept: list[str] | None = None,
    keep_bypass: frozenset = frozenset(),
) -> tuple[str, list[str], dict | None] | None:
    """The call that replaces live ruleset rs_id with rs, or None when it matches; what it keeps goes in kept."""
    current = gh(f"repos/{repo}/rulesets/{rs_id}")
    if not isinstance(current, dict):
        sys.exit(f"gh api repos/{repo}/rulesets/{rs_id}: unexpected answer {current!r}")
    if kept is not None:
        kept += [f"ruleset '{rs['name']}': {e}" for e in live_extras(rs, current, keep_bypass, not drop_unlisted)]
    rs = keep_live_extras(rs, current, keep_bypass, not drop_unlisted)
    diff = changes(rs, current)
    if not diff:
        return None
    return (
        f"replace ruleset '{rs['name']}':" + "".join(f"\n    {d}" for d in diff),
        ["-X", "PUT", f"repos/{repo}/rulesets/{rs_id}"],
        rs,
    )


def _bypass_actor(text: str) -> tuple[str, int | None]:
    """--keep-bypass TYPE[:ID] as the (actor_type, actor_id) pair the rulesets API shows."""
    kind, sep, ident = text.partition(":")
    if kind not in ACTOR_TYPES or (sep and not ident.isdigit()):
        raise argparse.ArgumentTypeError(
            f"{text!r}: give TYPE or TYPE:ID, TYPE one of {', '.join(ACTOR_TYPES)}, such as Team:5 or DeployKey"
        )
    if kind in TYPE_ONLY:
        return (kind, None)
    if not sep:
        raise argparse.ArgumentTypeError(f"{text!r}: a {kind} bypass needs its id, as {kind}:ID")
    return (kind, int(ident))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", help="OWNER/REPO")
    ap.add_argument("--apply", action="store_true", help="make the changes (default: only show them)")
    ap.add_argument(
        "--approvals",
        type=int,
        default=1,
        help="approvals required (default 1; 0 turns off code-owner and last-push review too)",
    )
    ap.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME",
        help="another required check, such as 'tests / Script tests' (repeatable); for Kiro review, --kiro-review",
    )
    ap.add_argument(
        "--kiro-review",
        nargs="?",
        const=KIRO_CHECK,
        default=None,
        metavar="CONTEXT",
        help=f"require the Kiro review result too, on develop, main and release/* (default context: '{KIRO_CHECK}')",
    )
    ap.add_argument(
        "--release-app-id",
        type=int,
        metavar="ID",
        help="the release GitHub App's id (pp-release-bot: 5568680), allowed to merge the back-merge PR into develop; "
        "also makes the 'release' environment for its key",
    )
    ap.add_argument(
        "--drop-unlisted",
        action="store_true",
        help="remove required checks the kit was not given (default: keep the live ones)",
    )
    ap.add_argument(
        "--keep-bypass",
        action="append",
        default=[],
        type=_bypass_actor,
        metavar="TYPE[:ID]",
        help="keep this live bypass actor, which the kit would otherwise remove: Team:5, Integration:123, "
        "RepositoryRole:5; OrganizationAdmin, DeployKey, EnterpriseOwner by type alone (repeatable)",
    )
    a = ap.parse_args(argv)
    if a.kiro_review is not None:
        if not a.kiro_review.strip() or re.fullmatch(r"[\w.-]+/[\w.-]+", a.kiro_review):
            # An OWNER/REPO here means `--kiro-review OWNER/REPO` took the repo as its context.
            ap.error(f"--kiro-review takes a check context, such as '{KIRO_CHECK}', not {a.kiro_review!r}")
        a.check.append(a.kiro_review.strip())
    problems: list[str] = []
    # A kept app bypass may be the release app's (--keep-bypass Integration:ID in place of --release-app-id), so the
    # key is checked for it too.
    kept_app = any(t == "Integration" for t, _ in a.keep_bypass)
    if a.release_app_id or kept_app:
        # Before any write: the plan gives the release app its bypass of develop, which must not exist while the key
        # can be read from any branch.
        problems, notes = release_key_findings(a.repo, for_app=bool(a.release_app_id))
        if not a.release_app_id:  # no environment is asked for, so its missing key isn't news
            notes = [n for n in notes if "has no secret" not in n]
            notes.insert(0, "--keep-bypass keeps an app's bypass, so where the release key can be read is checked too")
        for line in [*notes, *problems]:
            print(line, file=sys.stderr)
        if problems and a.apply:
            print("Nothing applied: fix the problems above first.", file=sys.stderr)
            return 1
    kept: list[str] = []
    calls = plan(a.repo, a.approvals, a.check, a.release_app_id, a.drop_unlisted, kept, frozenset(a.keep_bypass))
    for t, i in a.keep_bypass:
        if not any(f"bypass actor {t} {'' if i is None else f'{i} '}" in k for k in kept):
            name = t if i is None else f"{t}:{i}"
            print(f"warning: --keep-bypass {name} matches no live bypass actor", file=sys.stderr)
    if kept:
        print("Keeping these live settings the kit was not given (--drop-unlisted removes the checks):")
        print("".join(f"  {k}\n" for k in kept))
    for desc, args, body in calls:
        print(("applying: " if a.apply else "would ") + desc)
        if not a.apply and body is not None and "rulesets" in args[-1]:
            print(json.dumps(body, indent=2))
        if a.apply:
            gh(*args, body=body)
    if not calls:
        print(f"{a.repo} already has the golden-pipeline settings")
    elif not a.apply:
        print("\nNothing changed. Rerun with --apply to make these changes.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
