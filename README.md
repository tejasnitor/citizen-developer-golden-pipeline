# Citizen Developer Golden Pipeline

Nitor-owned repository for Procurement Partners
Golden Pipeline CI implementation.

## Objectives

- CI automation
- Code quality checks
- Security scanning
- Automated testing

## Golden Pipeline Architecture

### Phase 1: CI Workflow Validation

Validates all workflow YAML files for syntax correctness and GitHub Actions schema compliance.

**Tools:**

- `yamllint` v1.35.1 — YAML syntax validation
- `actionlint` v1.7.12 — GitHub Actions schema and best practices validation

**Triggers:**

- Runs on every push to `develop` branch
- Runs on every pull request to `develop` branch
- Can be manually triggered via workflow_dispatch

**Workflows:**

- **Orchestrator:** `.github/workflows/ci.yml`
- **Reusable workflow:** `.github/workflows/reusable-validate.yml`

**Permissions:** `contents: read` (minimum required)

**Jobs:**

- `validate-yaml` — Validates YAML syntax across all `.yml` and `.yaml` files
- `validate-actions` — Validates GitHub Actions workflow schema and best practices

Both jobs run in parallel for faster feedback.

### Phase 2: Code Quality Checks

Checks all Markdown documentation files for formatting consistency and structural correctness.

**Tools:**

- `markdownlint-cli2` v0.24.0 — Markdown linting via `DavidAnson/markdownlint-cli2-action`

**Triggers:**

- Runs after Phase 1 passes
- Runs on every push to `develop` branch
- Runs on every pull request to `develop` branch
- Can be manually triggered via workflow_dispatch

**Workflows:**

- **Orchestrator:** `.github/workflows/ci.yml`
- **Reusable workflow:** `.github/workflows/reusable-code-quality.yml`

**Permissions:** `contents: read` (minimum required)

**Jobs:**

- `lint-markdown` — Validates all `*.md` files against `.markdownlint.yml` configuration

**Security note:** `actions/checkout` uses `GITHUB_TOKEN` implicitly to fetch the
repository. The `contents: read` permission scopes this token to read-only access,
preventing any write operations to the repository during the workflow run.

### Phase 3: Automated Testing

Tests the Golden Pipeline's own structure and configuration for correctness.

**Tools:**

- `pytest` v9.2.0 — Python testing framework for structural validation
- `PyYAML` — YAML parsing (transitive dependency via yamllint)

**Triggers:**

- Runs after Phase 2 passes
- Runs on every push to `develop` branch
- Runs on every pull request to `develop` branch
- Can be manually triggered via workflow_dispatch

**Workflows:**

- **Orchestrator:** `.github/workflows/ci.yml`
- **Reusable workflow:** `.github/workflows/reusable-testing.yml`

**Permissions:** `contents: read` (minimum required)

**Jobs:**

- `test-pipeline` — Runs pytest test suite validating pipeline structure

**What is tested:**

- Required workflow files exist
- All workflows have correct structure (document start, required keys, permissions)
- External GitHub Actions are pinned to full 40-character commit SHAs
- All dependencies in `requirements.txt` are version-pinned with valid package names
- Required configuration files are present
- Reusable workflows declare `workflow_call` trigger correctly
- Orchestrator references only existing reusable workflows

**Test implementation:** Uses `yaml.BaseLoader` to prevent YAML 1.1 type coercion issues
when parsing workflow files (specifically the `on:` key which would otherwise be interpreted
as a boolean).

### Phase 4: Security Scanning

Scans all code and workflow files for security vulnerabilities using static analysis.

**Tools:**

- `Semgrep` v1.185.0 — Open-source static analysis engine (Community Edition, token-free)
- Docker image: `semgrep/semgrep@sha256:67518c174185a2043fdbed16c70631b79209499b3671178e7194379b48295a0d`

**Triggers:**

- Runs after Phase 3 passes
- Runs on every push to `develop` branch
- Runs on every pull request to `develop` branch
- Can be manually triggered via workflow_dispatch

**Workflows:**

- **Orchestrator:** `.github/workflows/ci.yml`
- **Reusable workflow:** `.github/workflows/reusable-security-scan.yml`

**Permissions:**

- Workflow-level: `contents: read`
- Job-level: `actions: read`, `contents: read`, `security-events: write`

The `security-events: write` permission is required to upload SARIF results to GitHub
Code Scanning for vulnerability tracking and reporting.

**Jobs:**

- `security-scan` — Runs Semgrep with security rulesets and uploads findings to GitHub Security

**Rulesets:**

- `r/yaml.github-actions.security` — GitHub Actions workflow security patterns (12 rules)
- `r/python.lang.security` — Python security patterns (94 rules)

**Behavior:**

- **Blocking mode:** Uses `--error` flag to fail the job if security findings are detected
- **Exit code 0:** Scan completed successfully with no security findings (job passes)
- **Exit code 1:** Security vulnerabilities detected (job fails, blocks pipeline)
- **Exit code 2+:** Scanner or configuration error (job fails)

**SARIF Output:**

- Semgrep generates SARIF (Static Analysis Results Interchange Format) output
- SARIF is uploaded to GitHub Code Scanning via `github/codeql-action/upload-sarif@v4.39.3`
- SARIF upload occurs before final result determination
- Findings are visible in GitHub Security → Code Scanning tab

**Implementation notes:**

- Uses official Semgrep Docker image pinned to specific SHA256 digest for reproducibility
- Captures actual Semgrep exit code explicitly using `SEMGREP_EXIT_CODE` variable
- Validates SARIF generation before upload to catch output configuration issues
- Distinguishes between findings (exit 1) and scanner errors (exit 2+) with different messages
- Verbose logging (`--verbose`) enabled for debugging and audit trails

### Phase 5: Dependency Scanning

Scans Python dependencies for known security vulnerabilities and outdated packages.

**Tools:**

- `pip-audit` v2.7.3 — Python dependency vulnerability scanner

**Triggers:**

- Runs after Phase 4 passes
- Runs on every push to `develop` branch
- Runs on every pull request to `develop` branch
- Can be manually triggered via workflow_dispatch

**Workflows:**

- **Orchestrator:** `.github/workflows/ci.yml`
- **Reusable workflow:** `.github/workflows/reusable-dependency-scan.yml`

**Permissions:**

- Workflow-level: `contents: read`
- Job-level: `contents: read`

**Jobs:**

- `dependency-scan` — Scans `requirements.txt` for vulnerable dependencies

**Behavior:**

- **Blocking mode:** Pipeline fails if vulnerabilities are detected
- **Exit code 0:** No vulnerabilities found (job passes)
- **Exit code 1:** Vulnerabilities detected (job fails, blocks pipeline)

**Report Output:**

- Generates JSON format for detailed findings
- Reports uploaded as workflow artifacts (30-day retention)
- Human-readable column output displayed in logs

**Implementation notes:**

- Uses Python 3.11 for consistency
- JSON and column output formats for comprehensive reporting
- Validates report generation before final result determination
- Provides suppression guidance for false positives

### Phase 6: Secret Scanning

Scans git repository history for hardcoded secrets, API keys, passwords, and credentials.

**Tools:**

- `Gitleaks` v8.30.1 — Open-source secret scanner (Community Edition, MIT license)
- Docker image: `ghcr.io/gitleaks/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f`

**Triggers:**

- Runs after Phase 5 passes
- Runs on every push to `develop` branch
- Runs on every pull request to `develop` branch
- Can be manually triggered via workflow_dispatch

**Workflows:**

- **Orchestrator:** `.github/workflows/ci.yml`
- **Reusable workflow:** `.github/workflows/reusable-secret-scan.yml`

**Permissions:**

- Workflow-level: `contents: read`
- Job-level: `contents: read`, `security-events: write`

The `security-events: write` permission enables SARIF upload to GitHub Code Scanning
(requires GitHub Advanced Security for private repositories). SARIF upload is optional
and uses `continue-on-error: true` to prevent failures on repositories without GHAS.

**Jobs:**

- `secret-scan` — Scans full git history with Gitleaks, uploads findings as artifacts

**Detection Coverage:**

- AWS, GCP, Azure credentials (access keys, secret keys, session tokens)
- GitHub, GitLab tokens (personal access tokens, OAuth tokens)
- API keys (Slack, Stripe, Twilio, SendGrid, Mailgun, etc.)
- Private keys (RSA, SSH, PGP, OpenSSL)
- Database connection strings (PostgreSQL, MySQL, MongoDB)
- JWT tokens
- Generic high-entropy strings (base64, hex patterns)
- 94+ additional built-in detection rules

**Behavior:**

- **Blocking mode:** Pipeline fails immediately if secrets are detected
- **Exit code 0:** No secrets found (job passes)
- **Exit code 1:** Secrets detected (job fails, blocks pipeline)
- **Exit code 126+:** Configuration error (job fails)

**Report Output:**

- Generates SARIF format for GitHub Code Scanning integration
- Generates JSON format for detailed findings
- All secrets are fully redacted (`--redact=100`) in reports for security
- Reports uploaded as workflow artifacts (30-day retention)

**SARIF Upload:**

- SARIF upload attempted if GitHub Advanced Security is enabled
- Upload failure does not block pipeline (findings still available in artifacts)
- Uses `continue-on-error: true` to handle repositories without GHAS gracefully

**Implementation notes:**

- Uses official Gitleaks Docker image pinned to SHA256 digest for reproducibility
- Scans full git history with `fetch-depth: 0` for comprehensive coverage
- Captures actual Gitleaks exit code explicitly using `GITLEAKS_EXIT_CODE` variable
- Validates report generation before final result determination
- Distinguishes between findings (exit 1) and scanner errors (exit 126+)
- Verbose logging (`--verbose`) enabled for debugging and audit trails
- Full redaction (`--redact=100`) prevents secret exposure in artifacts and logs

### Phase 7: Build and Artifact Management

Packages the Golden Pipeline workflows and configurations into a versioned, distributable
archive for easy deployment and sharing.

**Tools:**

- Standard Linux tools (`tar`, `sha256sum`, `jq`) — Packaging and integrity verification
- `actions/upload-artifact` v7.0.2 — GitHub Actions artifact upload

**Triggers:**

- Runs after Phase 6 passes
- Runs on every push to `develop` branch
- Runs on every pull request to `develop` branch
- Can be manually triggered via workflow_dispatch

**Workflows:**

- **Orchestrator:** `.github/workflows/ci.yml`
- **Reusable workflow:** `.github/workflows/reusable-build-artifacts.yml`

**Permissions:** `contents: read` (minimum required)

**Jobs:**

- `build-artifacts` — Packages workflows, generates manifest, creates archive with checksums

**Package Contents:**

The Golden Pipeline package includes:

- **Workflows (7 files):**
  - `ci.yml` — CI orchestrator
  - `reusable-validate.yml` — Phase 1 validation
  - `reusable-code-quality.yml` — Phase 2 code quality
  - `reusable-testing.yml` — Phase 3 testing
  - `reusable-security-scan.yml` — Phase 4 security scanning
  - `reusable-dependency-scan.yml` — Phase 5 dependency scanning
  - `reusable-secret-scan.yml` — Phase 6 secret scanning

- **Configuration Files (3 files):**
  - `.markdownlint.yml` — Markdown linting rules
  - `.yamllint.yml` — YAML linting rules
  - `.semgrepignore` — Semgrep exclusions

- **Metadata:**
  - `manifest.json` — Package metadata (version, commit SHA, build date, file list)
  - `checksums.txt` — SHA256 checksums for integrity verification

**Versioning Strategy:**

Package version is determined in this order:

1. **Manual input** — If version parameter provided via workflow_dispatch
2. **Git tag** — If commit has an exact git tag (e.g., `v1.0.0`)
3. **Git describe** — Fallback to git-describe output (commit SHA with `-dirty` if uncommitted changes)

**Package Naming:** `golden-pipeline-{version}.tar.gz`

**Artifact Management:**

- **Artifact name:** `golden-pipeline-package`
- **Retention:** 90 days
- **Upload behavior:** Fails if files are missing (`if-no-files-found: error`)
- **Contents:**
  - Compressed archive (`golden-pipeline-{version}.tar.gz`)
  - Manifest JSON (`manifest.json`)
  - Checksums file (`checksums.txt`)

**Manifest Schema:**

```json
{
  "name": "golden-pipeline",
  "version": "v1.0.0",
  "commit_sha": "30e46bf5d702b1b2b0cc654aea6637c58d9a4fab",
  "build_date": "2026-11-17T16:30:00Z",
  "repository": "Procurementpartners/citizen-developer-golden-pipeline",
  "ref": "refs/heads/develop",
  "workflows": ["ci.yml", "reusable-validate.yml", ...],
  "configs": [".markdownlint.yml", ".yamllint.yml", ".semgrepignore"]
}
```

**Checksum Verification:**

To verify package integrity after download:

```bash
# Download artifact from GitHub Actions
# Extract checksums.txt

# Verify archive checksum
sha256sum -c checksums.txt

# Expected output:
# golden-pipeline-v1.0.0.tar.gz: OK
# manifest.json: OK
```

**Usage:**

1. **Download Package:**
   - Navigate to GitHub Actions run
   - Download `golden-pipeline-package` artifact

2. **Verify Integrity:**
   - Extract artifact ZIP
   - Run `sha256sum -c checksums.txt`

3. **Extract Pipeline:**
   - Extract archive: `tar -xzf golden-pipeline-{version}.tar.gz`
   - Workflows available in `workflows/` directory
   - Configurations available in `config/` directory

4. **Deploy to Repository:**
   - Copy workflows to target repository's `.github/workflows/`
   - Copy configurations to target repository root
   - Adjust as needed for specific use case

**GitHub Releases:**

GitHub Release creation is **not automated** in the CI pipeline. To create a release:

1. Tag a commit: `git tag v1.0.0 && git push origin v1.0.0`
2. CI runs and generates versioned package (`golden-pipeline-v1.0.0.tar.gz`)
3. Download artifact from GitHub Actions
4. Manually create GitHub Release and attach package
5. (Optional) Create separate release workflow with `workflow_dispatch` trigger

This keeps CI focused on validation and packaging, with release publishing as a
deliberate, manual operation.

**Implementation notes:**

- Package structure is consistent across all builds
- Version determination is automatic (git tags) or manual (input parameter)
- Checksums provide cryptographic integrity verification
- 90-day retention balances availability with storage management
- Full git history (`fetch-depth: 0`) enables accurate version determination
- All external actions pinned to commit SHA for reproducibility
