"""
Golden Pipeline structural tests.

Tests validate pipeline configuration correctness:
- Required workflow files exist
- Workflow structure is correct (YAML document start, required keys, permissions)
- External actions are pinned to full 40-char commit SHAs
- Dependencies are version-pinned with valid package names and versions
- Configuration files are present
"""

import os
import re
import yaml
import pytest


# Fixtures

@pytest.fixture
def workflow_dir():
    """Path to .github/workflows directory."""
    return os.path.join(os.path.dirname(__file__), '..', '.github', 'workflows')


@pytest.fixture
def repo_root():
    """Path to repository root."""
    return os.path.join(os.path.dirname(__file__), '..')


def discover_workflow_files(workflow_dir):
    """Discover all .yml files in .github/workflows/."""
    return [f for f in os.listdir(workflow_dir) if f.endswith('.yml')]


def load_workflow_yaml(workflow_dir, filename):
    """
    Load a workflow YAML file using BaseLoader to prevent type coercion.

    Important: yaml.BaseLoader treats all scalars as strings, avoiding the
    YAML 1.1 boolean coercion issue where 'on:' is parsed as True.
    """
    path = os.path.join(workflow_dir, filename)
    with open(path, 'r') as f:
        return yaml.load(f, Loader=yaml.BaseLoader)


# Test Group 1: Workflow File Presence

def test_required_workflow_files_exist(workflow_dir):
    """Verify all required workflow files are present."""
    required = [
        'ci.yml',
        'reusable-validate.yml',
        'reusable-code-quality.yml',
        'reusable-testing.yml',
        'reusable-secret-scan.yml'
    ]
    actual = discover_workflow_files(workflow_dir)

    for required_file in required:
        assert required_file in actual, f"Required workflow file missing: {required_file}"


# Test Group 2: Workflow Structure and Permissions

def test_all_workflows_have_document_start(workflow_dir):
    """Verify all workflows start with YAML document marker '---'."""
    workflows = discover_workflow_files(workflow_dir)

    for filename in workflows:
        path = os.path.join(workflow_dir, filename)
        with open(path, 'r') as f:
            first_line = f.readline().strip()
        assert first_line == '---', f"{filename}: must start with '---' (YAML document start)"


def test_all_workflows_have_required_top_level_keys(workflow_dir):
    """Verify all workflows contain required top-level keys: name, on, permissions, jobs."""
    workflows = discover_workflow_files(workflow_dir)
    required_keys = ['name', 'on', 'permissions', 'jobs']

    for filename in workflows:
        data = load_workflow_yaml(workflow_dir, filename)

        for key in required_keys:
            assert key in data, f"{filename}: missing required top-level key '{key}'"


def test_workflow_level_permissions_are_contents_read_only(workflow_dir):
    """Verify workflow-level permissions are exactly 'contents: read' (with id-token: write allowed for OIDC deploy/rollback)."""
    workflows = discover_workflow_files(workflow_dir)

    for filename in workflows:
        data = load_workflow_yaml(workflow_dir, filename)
        perms = data.get('permissions', {})

        # OIDC cloud deployment workflows require id-token: write
        if filename in ['gp-deploy.yml', 'gp-rollback.yml']:
            expected = {'contents': 'read', 'id-token': 'write'}
        else:
            expected = {'contents': 'read'}
        assert perms == expected, f"{filename}: workflow-level permissions must be {expected}, got {perms}"


def test_job_level_permissions_are_contents_read_only(workflow_dir):
    """Verify job-level permissions, if declared, are exactly 'contents: read'."""
    workflows = discover_workflow_files(workflow_dir)

    # Exception: security-scan job needs security-events: write to upload SARIF
    security_scan_exception = {'actions': 'read', 'contents': 'read', 'security-events': 'write'}
    # Exception: secret-scan job needs actions:read and security-events:write to upload SARIF
    secret_scan_exception = {'actions': 'read', 'contents': 'read', 'security-events': 'write'}

    for filename in workflows:
        data = load_workflow_yaml(workflow_dir, filename)
        jobs = data.get('jobs', {})

        for job_id, job in jobs.items():
            if 'permissions' in job:
                job_perms = job['permissions']

                # Allow security-scan job to have security-events: write (both in reusable and caller)
                if job_id == 'security-scan' and job_perms == security_scan_exception:
                    assert job_perms == security_scan_exception, \
                        f"{filename}: job '{job_id}' permissions must be {security_scan_exception}, got {job_perms}"
                # Allow secret-scan job to have security-events: write
                elif job_id == 'secret-scan' and job_perms == secret_scan_exception:
                    assert job_perms == secret_scan_exception, \
                        f"{filename}: job '{job_id}' permissions must be {secret_scan_exception}, got {job_perms}"
                else:
                    expected = {'contents': 'read'}
                    assert job_perms == expected, \
                        f"{filename}: job '{job_id}' permissions must be {expected}, got {job_perms}"


def test_reusable_workflows_declare_workflow_call(workflow_dir):
    """Verify reusable workflows (not ci.yml) declare workflow_call trigger."""
    workflows = discover_workflow_files(workflow_dir)
    reusable = [f for f in workflows if f.startswith('reusable-')]

    for filename in reusable:
        data = load_workflow_yaml(workflow_dir, filename)
        on_block = data.get('on', {})

        # BaseLoader: check if 'workflow_call' key is present
        assert 'workflow_call' in on_block, f"{filename}: reusable workflow must declare 'workflow_call' trigger"


def test_orchestrator_does_not_declare_workflow_call(workflow_dir):
    """Verify ci.yml (orchestrator) does not declare workflow_call."""
    data = load_workflow_yaml(workflow_dir, 'ci.yml')
    on_block = data.get('on', {})

    assert 'workflow_call' not in on_block, "ci.yml: orchestrator must not declare workflow_call"


# Test Group 3: Security - External Action SHA Pinning

def test_all_external_actions_are_sha_pinned(workflow_dir):
    """
    Verify all external action references use full 40-character commit SHAs.

    Local reusable workflow references (starting with ./) are excluded.
    """
    workflows = discover_workflow_files(workflow_dir)

    # Pattern: uses: <action>@<ref>
    # Captures action and ref separately
    uses_pattern = re.compile(r'^\s+uses:\s+(\S+)@(\S+)', re.MULTILINE)

    failures = []

    for filename in workflows:
        path = os.path.join(workflow_dir, filename)
        with open(path, 'r') as f:
            content = f.read()

        matches = uses_pattern.findall(content)

        for action, ref in matches:
            # Skip local reusable workflow references
            if action.startswith('./'):
                continue

            # External action: ref must be exactly 40 hex characters
            if not re.match(r'^[0-9a-f]{40}$', ref):
                failures.append(f"{filename}: {action}@{ref} is not a 40-char SHA")

    assert not failures, "External actions must be pinned to full commit SHAs:\n" + "\n".join(failures)


# Test Group 4: Dependency Integrity

def test_required_config_files_exist(repo_root):
    """Verify required configuration files are present."""
    required_files = [
        '.yamllint.yml',
        '.markdownlint.yml',
        'requirements.txt'
    ]

    for filename in required_files:
        path = os.path.join(repo_root, filename)
        assert os.path.exists(path), f"Required config file missing: {filename}"


def test_requirements_have_valid_package_and_version(repo_root):
    """
    Verify all dependencies in requirements.txt:
    - Use == version pinning
    - Have a valid package name (PEP 508 compliant)
    - Have a valid non-empty version string with no whitespace
    """
    path = os.path.join(repo_root, 'requirements.txt')

    with open(path, 'r') as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]

    # Valid package name pattern (PEP 508 compliant):
    # - Must start and end with alphanumeric
    # - May contain alphanumeric, dot, hyphen, underscore in between
    package_pattern = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$')

    failures = []

    for line in lines:
        # Check for == separator
        if '==' not in line:
            failures.append(f"Invalid format (must use == for version pinning): {line}")
            continue

        parts = line.split('==', 1)
        if len(parts) != 2:
            failures.append(f"Invalid format (must be package==version): {line}")
            continue

        package, version = parts

        # Validate package name
        if not package:
            failures.append(f"Empty package name: {line}")
            continue

        if not package_pattern.match(package):
            failures.append(f"Invalid package name '{package}' (must start/end with alphanumeric): {line}")

        # Validate version
        if not version:
            failures.append(f"Empty version string: {line}")
            continue

        if re.search(r'\s', version):
            failures.append(f"Version contains whitespace: {line}")

    assert not failures, "requirements.txt validation failures:\n" + "\n".join(failures)


def test_ci_references_only_existing_workflows(workflow_dir):
    """Verify ci.yml only references reusable workflows that actually exist."""
    ci_path = os.path.join(workflow_dir, 'ci.yml')

    with open(ci_path, 'r') as f:
        content = f.read()

    # Find all local workflow references: uses: ./.github/workflows/<filename>
    local_refs = re.findall(r'uses:\s+\./\.github/workflows/(\S+)', content)

    actual_files = discover_workflow_files(workflow_dir)

    for ref in local_refs:
        assert ref in actual_files, f"ci.yml references {ref} which does not exist"


# Test Group 5: Phase 6 Secret Scanning Configuration

def test_phase_6_runs_after_phase_5(workflow_dir):
    """Verify Phase 6 (secret-scan) depends on Phase 5 (dependency-scan)."""
    data = load_workflow_yaml(workflow_dir, 'ci.yml')
    jobs = data.get('jobs', {})

    assert 'secret-scan' in jobs, "ci.yml: secret-scan job not found"
    secret_scan_job = jobs['secret-scan']

    # Check that secret-scan needs dependency-scan
    needs = secret_scan_job.get('needs')
    assert needs == 'dependency-scan', \
        f"secret-scan job must depend on 'dependency-scan', got: {needs}"


def test_gitleaks_cli_version_and_checksum_pinned(workflow_dir):
    """Verify Gitleaks CLI version and release checksum are pinned."""
    workflow_path = os.path.join(workflow_dir, "reusable-secret-scan.yml")

    with open(workflow_path, "r") as f:
        content = f.read()

    assert 'GITLEAKS_VERSION: "8.30.1"' in content
    expected_sha256 = (
        "551f6fc83ea457d62a0d98237cbad105"
        "af8d557003051f41f3e7ca7b3f2470eb"
    )
    assert expected_sha256 in content
    assert "sha256sum -c -" in content
    assert "gitleaks version" in content
    assert "docker run" not in content



def test_checkout_uses_full_git_history(workflow_dir):
    """Verify checkout step uses fetch-depth: 0 for full git history."""
    workflow_path = os.path.join(workflow_dir, 'reusable-secret-scan.yml')
    data = load_workflow_yaml(workflow_dir, 'reusable-secret-scan.yml')

    jobs = data.get('jobs', {})
    assert 'secret-scan' in jobs, "reusable-secret-scan.yml: secret-scan job not found"

    steps = jobs['secret-scan'].get('steps', [])
    checkout_step = None

    for step in steps:
        if 'uses' in step and 'actions/checkout' in step['uses']:
            checkout_step = step
            break

    assert checkout_step is not None, "reusable-secret-scan.yml: checkout step not found"

    # Check fetch-depth: 0
    with_block = checkout_step.get('with', {})
    fetch_depth = with_block.get('fetch-depth')

    # BaseLoader returns strings, so check for '0' or 0
    assert fetch_depth in ['0', 0], \
        f"Checkout step must use fetch-depth: 0 for full history, got: {fetch_depth}"


def test_gitleaks_full_redaction_enabled(workflow_dir):
    """Verify Gitleaks scan uses --redact=100 for full secret redaction."""
    workflow_path = os.path.join(workflow_dir, 'reusable-secret-scan.yml')

    with open(workflow_path, 'r') as f:
        content = f.read()

    # Check for --redact=100 in both SARIF and JSON generation
    redact_pattern = r'--redact=100'
    matches = re.findall(redact_pattern, content)

    # Should appear at least twice (once for SARIF, once for JSON)
    assert len(matches) >= 2, \
        f"Expected --redact=100 to appear at least twice (SARIF + JSON), found {len(matches)} times"


def test_gitleaks_generates_json_and_sarif_reports(workflow_dir):
    """Verify Gitleaks CLI generates both JSON and SARIF reports."""
    workflow_path = os.path.join(workflow_dir, "reusable-secret-scan.yml")

    with open(workflow_path, "r") as f:
        content = f.read()

    assert "--report-format sarif" in content
    assert "--report-path gitleaks.sarif" in content

    assert "--report-format json" in content
    assert "--report-path gitleaks.json" in content

    assert "gitleaks git ." in content



def test_artifacts_have_30_day_retention(workflow_dir):
    """Verify artifact upload has 30-day retention configured."""
    data = load_workflow_yaml(workflow_dir, 'reusable-secret-scan.yml')

    jobs = data.get('jobs', {})
    assert 'secret-scan' in jobs, "reusable-secret-scan.yml: secret-scan job not found"

    steps = jobs['secret-scan'].get('steps', [])
    upload_step = None

    for step in steps:
        if 'uses' in step and 'actions/upload-artifact' in step['uses']:
            upload_step = step
            break

    assert upload_step is not None, "reusable-secret-scan.yml: upload-artifact step not found"

    # Check retention-days: 30
    with_block = upload_step.get('with', {})
    retention_days = with_block.get('retention-days')

    # BaseLoader returns strings, so check for '30' or 30
    assert retention_days in ['30', 30], \
        f"Artifact retention must be 30 days, got: {retention_days}"


def test_artifact_upload_runs_always(workflow_dir):
    """Verify artifact upload runs even when secrets are detected (if: always)."""
    data = load_workflow_yaml(workflow_dir, 'reusable-secret-scan.yml')

    jobs = data.get('jobs', {})
    assert 'secret-scan' in jobs, "reusable-secret-scan.yml: secret-scan job not found"

    steps = jobs['secret-scan'].get('steps', [])
    upload_step = None

    for step in steps:
        if 'uses' in step and 'actions/upload-artifact' in step['uses']:
            upload_step = step
            break

    assert upload_step is not None, "reusable-secret-scan.yml: upload-artifact step not found"

    # Check if: always()
    if_condition = upload_step.get('if')
    assert if_condition == 'always()', \
        f"Artifact upload must use 'if: always()' to run even on failure, got: {if_condition}"


def test_secret_detection_fails_pipeline(workflow_dir):
    """Verify final verification step exits with code 1 when secrets are detected."""
    workflow_path = os.path.join(workflow_dir, 'reusable-secret-scan.yml')

    with open(workflow_path, 'r') as f:
        content = f.read()

    # Check for exit code 1 handling (secrets detected)
    assert 'elif [ "$EXIT_CODE" -eq 1 ]; then' in content, \
        "Missing exit code 1 handling for secret detection"

    assert 'SECRETS DETECTED IN REPOSITORY' in content, \
        "Missing error message for secret detection"

    # Verify the section contains exit 1 to fail the pipeline
    # Find the section that handles EXIT_CODE == 1
    lines = content.split('\n')
    found_exit_1_handler = False
    in_exit_1_section = False
    exit_1_found_in_section = False

    for line in lines:
        if 'elif [ "$EXIT_CODE" -eq 1 ]; then' in line:
            in_exit_1_section = True
            found_exit_1_handler = True
            continue

        if in_exit_1_section:
            if line.strip().startswith('else') or line.strip().startswith('elif'):
                # End of this section
                break
            if 'exit 1' in line:
                exit_1_found_in_section = True
                break

    assert found_exit_1_handler, "Exit code 1 handler not found in workflow"
    assert exit_1_found_in_section, \
        "Verification step must exit 1 when secrets are detected to fail the pipeline"


# Test Group 7: Phase 7 - Build and Artifact Management


def test_phase_7_runs_after_phase_6(workflow_dir):
    """Verify Phase 7 (build-artifacts) runs after Phase 6 (secret-scan)."""
    ci_path = os.path.join(workflow_dir, 'ci.yml')
    data = load_workflow_yaml(workflow_dir, 'ci.yml')

    jobs = data.get('jobs', {})
    assert 'build-artifacts' in jobs, "ci.yml: 'build-artifacts' job not found"

    build_artifacts_job = jobs['build-artifacts']

    # Check needs dependency
    needs = build_artifacts_job.get('needs')
    assert needs == 'secret-scan', \
        f"build-artifacts must run after secret-scan (needs: secret-scan), got: {needs}"

    # Verify it uses the reusable workflow
    uses = build_artifacts_job.get('uses')
    assert uses == './.github/workflows/reusable-build-artifacts.yml', \
        f"build-artifacts must use reusable-build-artifacts.yml, got: {uses}"


def test_phase_7_packages_all_reusable_workflows(workflow_dir):
    """Verify Phase 7 packages all reusable workflows."""
    workflow_path = os.path.join(workflow_dir, 'reusable-build-artifacts.yml')

    with open(workflow_path, 'r') as f:
        content = f.read()

    # Check for workflow collection step
    assert 'Collect Workflow Files' in content, \
        "Missing 'Collect Workflow Files' step in reusable-build-artifacts.yml"

    # Verify it copies reusable workflows
    assert 'cp .github/workflows/reusable-*.yml' in content, \
        "Must copy all reusable-*.yml workflow files"

    # Verify it copies CI orchestrator
    assert 'cp .github/workflows/ci.yml' in content, \
        "Must copy ci.yml orchestrator workflow"

    # Check for expected reusable workflows (at least in comments or verification)
    # The actual files will be collected dynamically, but structure should be documented
    assert 'workflows' in content.lower(), \
        "Workflow collection step should reference workflows directory"


def test_phase_7_includes_configuration_files(workflow_dir):
    """Verify Phase 7 includes configuration files in the package."""
    workflow_path = os.path.join(workflow_dir, 'reusable-build-artifacts.yml')

    with open(workflow_path, 'r') as f:
        content = f.read()

    # Check for configuration collection step
    assert 'Collect Configuration Files' in content, \
        "Missing 'Collect Configuration Files' step"

    # Verify it copies required config files
    required_configs = [
        '.markdownlint.yml',
        '.yamllint.yml',
        '.semgrepignore'
    ]

    for config_file in required_configs:
        assert config_file in content, \
            f"Must include {config_file} in package"


def test_phase_7_generates_manifest(workflow_dir):
    """Verify Phase 7 generates manifest.json with required metadata."""
    workflow_path = os.path.join(workflow_dir, 'reusable-build-artifacts.yml')

    with open(workflow_path, 'r') as f:
        content = f.read()

    # Check for manifest generation step
    assert 'Generate Manifest' in content, \
        "Missing 'Generate Manifest' step"

    # Verify manifest file creation
    assert 'manifest.json' in content, \
        "Must generate manifest.json file"

    # Verify required manifest fields
    required_fields = [
        'version',
        'commit_sha',
        'build_date',
        'repository',
        'workflows',
        'configs'
    ]

    for field in required_fields:
        assert field in content, \
            f"Manifest must include '{field}' field"

    # Verify manifest JSON validation
    assert 'jq' in content, \
        "Should validate manifest JSON with jq"


def test_phase_7_generates_checksums(workflow_dir):
    """Verify Phase 7 generates SHA256 checksums."""
    workflow_path = os.path.join(workflow_dir, 'reusable-build-artifacts.yml')

    with open(workflow_path, 'r') as f:
        content = f.read()

    # Check for checksum generation step
    assert 'Generate Checksums' in content, \
        "Missing 'Generate Checksums' step"

    # Verify SHA256 checksum generation
    assert 'sha256sum' in content, \
        "Must use sha256sum for checksum generation"

    # Verify checksums file
    assert 'checksums.txt' in content, \
        "Must generate checksums.txt file"

    # Verify it checksums both archive and manifest
    checksum_count = content.count('sha256sum')
    assert checksum_count >= 2, \
        "Must generate checksums for at least archive and manifest"


def test_phase_7_artifact_retention_90_days(workflow_dir):
    """Verify Phase 7 artifacts have 90-day retention policy."""
    data = load_workflow_yaml(workflow_dir, 'reusable-build-artifacts.yml')

    jobs = data.get('jobs', {})
    assert 'build-artifacts' in jobs, \
        "reusable-build-artifacts.yml: 'build-artifacts' job not found"

    steps = jobs['build-artifacts'].get('steps', [])
    upload_step = None

    for step in steps:
        if 'uses' in step and 'actions/upload-artifact' in step['uses']:
            upload_step = step
            break

    assert upload_step is not None, \
        "reusable-build-artifacts.yml: upload-artifact step not found"

    # Check retention-days
    with_params = upload_step.get('with', {})
    retention_days = with_params.get('retention-days')

    assert retention_days == '90', \
        f"Artifact retention must be 90 days, got: {retention_days}"


def test_phase_7_uses_pinned_actions(workflow_dir):
    """Verify Phase 7 uses SHA-pinned actions."""
    data = load_workflow_yaml(workflow_dir, 'reusable-build-artifacts.yml')

    jobs = data.get('jobs', {})
    assert 'build-artifacts' in jobs, \
        "reusable-build-artifacts.yml: 'build-artifacts' job not found"

    steps = jobs['build-artifacts'].get('steps', [])

    # Find all external actions
    external_actions = []
    for step in steps:
        if 'uses' in step:
            uses = step['uses']
            # Skip local reusable workflows (start with ./)
            if not uses.startswith('./'):
                external_actions.append(uses)

    assert len(external_actions) > 0, \
        "Expected at least one external action (checkout, upload-artifact)"

    # Verify all external actions are SHA-pinned
    sha_pattern = re.compile(r'^[a-zA-Z0-9-]+/[a-zA-Z0-9-]+@[a-f0-9]{40}')

    for action in external_actions:
        assert sha_pattern.match(action), \
            f"Action must be pinned to full 40-char commit SHA: {action}"


def test_phase_7_version_from_git(workflow_dir):
    """Verify Phase 7 determines version from git tags or commit SHA."""
    workflow_path = os.path.join(workflow_dir, 'reusable-build-artifacts.yml')

    with open(workflow_path, 'r') as f:
        content = f.read()

    # Check for version determination step
    assert 'Determine Version' in content, \
        "Missing 'Determine Version' step"

    # Verify version determination logic
    version_checks = [
        'git describe --tags --exact-match',  # Check for exact tag
        'git describe --always',              # Fallback to commit SHA
    ]

    for check in version_checks:
        assert check in content, \
            f"Version determination must include: {check}"

    # Verify version is stored in output
    assert 'version=' in content and 'GITHUB_OUTPUT' in content, \
        "Version must be stored in step output"

    # Verify version is used in archive name
    assert 'golden-pipeline-${VERSION}.tar.gz' in content or \
           'golden-pipeline-${{ steps.version.outputs.version }}.tar.gz' in content, \
        "Package archive name must include version"
