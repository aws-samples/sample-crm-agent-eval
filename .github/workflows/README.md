# CI workflows

Two GitHub Actions workflows live here, and the repository root carries an equivalent GitLab CI
definition. `tests/unit/test_ci_parity.py` parses all of them and is the enforcement for the
properties below.

See also: [documentation map](../../README.md#documentation) · [development](../../docs/development.md) · [runbook](../../docs/runbook.md).

| Definition | Trigger | Jobs | Cost | Credential |
|---|---|---|---|---|
| `.github/workflows/verify.yml` | `push` to `main`/`mainline`, `pull_request` | `verify`, `frontend` | none | none |
| `.github/workflows/browser-journeys.yml` | `workflow_dispatch` only | `browser-journeys` | **billable** | repository secrets |
| `.gitlab-ci.yml` | merge requests and branch pipelines | `verify`, `frontend` | none | none |

## `verify` and `frontend` jobs

`verify` runs `make verify` with the same pinned interpreter (`.python-version`), the frozen
`requirements.txt` installed with `--no-deps`, and the same aggregate target as a local run. Before
that install, each job's `.venv` gets `pip==${PIP_VERSION}`. DynamoDB Local runs as a service container
with `CRM_REQUIRE_DYNAMODB_LOCAL=1`, so the integration tier runs rather than skipping. The job also
provisions terraform, gitleaks, shellcheck, and Node `NODE_VERSION` for the relay suite. The major of
`NODE_VERSION` must equal the relay Lambda runtime in [`infra/modules/agent_relay/variables.tf`](../../infra/modules/agent_relay/variables.tf) (see the [module README](../../infra/modules/agent_relay/README.md)). Before
`make verify`, the job installs `rsync` and `perl`, then builds the Lambda and relay zips
(`scripts/package_lambdas.sh`, `scripts/package_relay.sh`, then `check_relay_zip.py --require-zip`), so
`check_lambda_zip_imports` and `check_relay_zip` have zips to verify. It reaches no AWS endpoint and
reads no secret.

`frontend` runs the emitted-CSS gate (`CRM_REQUIRE_EMITTED_CSS=1`). In `frontend/` it then runs
`gen:types`, the full `check_toolspec_parity.py` (including the frontend consumer that `verify`
defers with `--skip-frontend`), and `lint`, `typecheck`, `test` and `build`.

`DEBIAN_FRONTEND=noninteractive` is set for every job. Every `sudo apt-get` also sets it inline,
because `sudo` resets the environment.

`.gitlab-ci.yml` defines the same two jobs, and its comments document the differences:
- GitLab's `verify` job requests the runner tags `arch:arm64` and `size:large`; `frontend` sets no
  tags and runs on the project's default runner. GitHub runs both jobs on `ubuntu-24.04` (x64).
- GitLab picks the terraform, gitleaks, shellcheck and Node release assets by the runner's
  architecture (x86_64 or aarch64). The gitleaks, shellcheck and Node assets are checked against a
  digest pinned for each architecture, and any other architecture fails the install.
- GitLab services run as separate containers, so it bridges DynamoDB Local onto loopback with `socat`.
- GitLab installs Node from the nodejs.org tarball, checked against the `NODE_SHA256_*` digests pinned
  for each architecture.
- GitHub uses `actions/setup-node` with the exact `NODE_VERSION`. That release is pinned, but it is
  not checked against a digest held in this repository.

`test_ci_parity.py` asserts that both definitions have the same:
- job names, apt tool set and locale;
- shellcheck pin;
- `CRM_REQUIRE_*` surface and worker-count derivation;
- `DEBIAN_FRONTEND`;
- `PIP_VERSION` and its install before the lock;
- `NODE_VERSION` and its runtime-major check;
- zip build before `make verify`;
- full toolspec gate in `frontend`.

Change a shared pin in both files in the same commit. There is no GitLab equivalent of
`browser-journeys.yml`.

**Execution status:** both GitLab jobs have been reproduced locally in Docker. `verify.yml` has
not yet run on GitHub Actions. The parity tests parse it and execute its Node derivation, so its
properties are verified statically only until a real run.

## `browser-journeys.yml`

Runs `tests/e2e/browser/journeys/` (Playwright) against a **deployed dev stack**. It is separate from
`verify.yml` because the journeys need a live stack and cost money.

### Dispatching it

Both gates are required and neither defaults to on:

| Input | Type | Default | Notes |
|---|---|---|---|
| `run_browser_journeys` | boolean | `false` | The opt-in. Sets `CRM_RUN_BROWSER_JOURNEYS=1` in the job. |
| `billable_acknowledgment` | string | `""` | Must be exactly `--i-understand-this-is-billable` — the same token `scripts/verify_deployment.sh` requires. |
| `base_url` | string | `""` | `CRM_E2E_BASE_URL`. Leave empty to resolve from the outputs secret. Never a production URL. |

```bash
gh workflow run browser-journeys.yml \
  -f run_browser_journeys=true \
  -f billable_acknowledgment=--i-understand-this-is-billable \
  -f base_url=https://<dev-distribution>.cloudfront.net
```

### Required secrets

| Secret | Needed when | Contents |
|---|---|---|
| `CRM_DEMO_USER_EMAIL` | always | Demo-tenant user, provisioned by `scripts/provision_demo_user.py`. |
| `CRM_DEMO_USER_PASSWORD` | always | Its password. Sign-in is driven through the Hosted UI. |
| `CRM_TF_OUTPUTS_JSON` | when `base_url` is empty | A `terraform -chdir=infra output -json` document. Written to `results/tf-outputs.json` and exported as `CRM_TF_OUTPUTS_JSON`; never echoed. |

The workflow needs no AWS credential and assumes no OIDC role; it resolves the target from an input
or the saved outputs document.

### A missing gate fails the job

Without both gates the first step after checkout exits 3 with a `::error::` naming the missing gate.
The job has no job-level `if:`, because a skipped job produces a green run that is indistinguishable
from a passing one. `test_without_the_opt_in_the_browser_tier_cannot_report_success` asserts this.

### Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Job fails immediately, `Browser tier did not run` | One of the two gates was not supplied | Re-dispatch with both. The run verified nothing. |
| `No base_url input and no CRM_TF_OUTPUTS_JSON secret` | No target source | Supply `base_url`, or set the secret. |
| `CRM_DEMO_USER_EMAIL is unset` | Secret missing | Provision the demo user and set both credential secrets. |
| `globalSetup` refuses the target | The URL looks like production, the outputs are stale or incomplete, or nothing resolved | Point at a dev stack. There is no `localhost` fallback. |
| Journey fails with no local repro | — | Download the `browser-journey-artifacts` artifact, uploaded on failure with 7-day retention. It holds Playwright's `test-results/` output: traces, videos and screenshots of failed tests. No HTML report is generated, and the JSON report (`results/e2e-browser.json`) is not uploaded. |

### Scope

It does not deploy, seed, or tear down. It expects a deployed stack with a seeded `demo` tenant
(`scripts/seed_demo_tenant.py`). Deploy, smoke and teardown are `scripts/deploy.sh`,
`scripts/verify_deployment.sh` and `scripts/teardown.sh`.

## Conventions

Asserted over every workflow file in this directory:

- `permissions: {contents: read}` (`test_permissions_are_read_only`).
- Every `uses:` pinned to a full commit SHA or an exact `vX.Y.Z` tag
  (`test_every_github_action_is_pinned_to_an_exact_commit_sha_or_tag`).
- Only the browser workflow references repository secrets
  (`test_only_the_browser_workflow_references_repository_secrets`).

Per-file properties:

- **Version pins.** Python from `.python-version`. The frontend job pins Node from `frontend/.nvmrc`;
  the relay's Node major is derived from `runtime` in `infra/modules/agent_relay/variables.tf`.
- **Lockfiles are the sole install authority**: `npm ci`, and `pip install --no-deps -r requirements.txt`.
- **No browser step swallows a failure** (`test_no_browser_step_swallows_a_failure`). `verify.yml`
  uses `|| true` in two places where the captured value is checked immediately afterwards.
- **The browser workflow does not list spec files**; Playwright discovers them from the journeys
  directory (`test_the_browser_workflow_does_not_enumerate_the_journey_spec_files`).
