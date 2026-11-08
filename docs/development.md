# Development

How to set up a development environment, run the test tiers and offline gates, and contribute
changes. For installing and deploying the system, start with the top-level [`README.md`](../README.md).

See also: [documentation map](../README.md#documentation) · [runbook](runbook.md) · [CI workflows](../.github/workflows/README.md) · [eval methodology](eval-methodology.md).

## Environment

The project's own virtualenv is mandatory: every `make` target invokes `.venv/bin/mypy`,
`.venv/bin/ruff` and `.venv/bin/python` by path, and a system `python3` has no `pytest`.

```bash
python3.13 -m venv .venv                   # the interpreter whose version matches .python-version
# On a version mismatch this prints "wrong interpreter" and installs no Python packages.
{ [ "$(.venv/bin/python --version)" = "Python $(cat .python-version)" ] || { echo "wrong interpreter" >&2; false; }; } &&
  .venv/bin/pip install --no-input --no-deps -r requirements.txt &&
  .venv/bin/pip install --no-input --no-deps -e .
(cd src/relay && npm ci)                                # needed by `make verify`
(cd frontend && npm ci)                                 # needed by check_emitted_css
(cd tests/e2e/browser && npm ci)                        # only for the browser journeys
terraform -chdir=infra init -backend=false              # plan-time tests; no credentials needed
scripts/run_local_dynamodb.sh start                     # DynamoDB Local; start|stop|status, needs Docker
git config core.hooksPath githooks                      # or: pre-commit install
```

- `requirements.txt` is the sole install authority: a fully frozen transitive tree with exact pins.
  `pyproject.toml` lists direct dependencies only. Install with `--no-deps` so no resolver can
  substitute a version.
- `check_pin_drift` compares `pyproject.toml`, `requirements.txt` and `.python-version` against each
  other. It does not inspect the installed environment, so change `pyproject.toml` and
  `requirements.txt` together in one commit.
- `frontend/`, `src/relay/` and `tests/e2e/browser/` are three separate npm projects, each with its
  own lockfile. They are not a workspace; run `npm ci` in each one you use.
- The git hooks run `check_no_account_id`, `check_no_vendor_keys`, `check_log_redaction` and
  gitleaks against staged content, then `check_secret_scan` over the working tree. Any non-zero
  `check_secret_scan` status fails the hook, including 3 (gitleaks absent).
- Figures under `docs/figures/` are drawn and exported with draw.io desktop 32.5.5 (Homebrew
  cask `drawio`, whose CLI is `drawio`), using `drawio -x -f svg --embed-svg-images --theme light`;
  see [`docs/figures/README.md`](figures/README.md).
- Security-relevant pins: `cryptography` 50.0.2, `PyJWT` 2.15.1, `urllib3` 2.8.0 and
  `pytest` 9.1.1 (`requirements.txt`); `vite` 8.3.6 and `tailwindcss` 4.3.5 (`frontend/package.json`;
  Tailwind 4 needs Safari 16.4, Chrome 111 or Firefox 128, see [`frontend/README.md`](../frontend/README.md)); `hashicorp/aws`
  6.71.0 (exact constraint) and `hashicorp/null` 3.3.2 (lock file only; no version constraint), in
  `infra/.terraform.lock.hcl`.

## Running tests

```bash
.venv/bin/python -m pytest tests/unit/test_authz.py
.venv/bin/python -m pytest tests/unit/test_authz.py::test_name
.venv/bin/python3 scripts/check_toolspec_parity.py      # every gate runs on its own
.venv/bin/python3 scripts/run_gates.py --json
```

- `make test` runs the unit suite under pytest-xdist with `PYTEST_WORKERS` workers (default 4).
  Use `make test PYTEST_WORKERS=0` for a serial run with a readable traceback.
- Do not add `-q` to a pytest command line. `pyproject.toml`'s `addopts` already sets it, and `-qq`
  suppresses the summary footer.

Frontend (`cd frontend`, Node pinned in `.nvmrc`):

```bash
npm run gen:types    # generated types from the Python toolspec and the SSE event schema
npm run typecheck
npm run test
npm run lint
npm run build
```

Relay (`cd src/relay`, Node 24 or later): `npm run gen:types`, `npm run typecheck`, `npm run test`,
`npm run build`.

Browser journeys (`cd tests/e2e/browser`). Use the pinned local binaries, because `npx tsc` can
resolve to an unrelated registry package:

```bash
./node_modules/.bin/tsc --noEmit -p tsconfig.json   # type check
./node_modules/.bin/playwright test --list          # collection only, no type check
./node_modules/.bin/playwright test                 # a real run; needs a deployed dev stack
```

The base URL is discovered from `CRM_E2E_BASE_URL`, then `CRM_TF_OUTPUTS_JSON`, then
`results/tf-outputs.json`, then `terraform output -json`. A stale outputs document is refused. See
[`tests/e2e/browser/journeys/README.md`](../tests/e2e/browser/journeys/README.md).

## Test tiers

| Directory | Tier | Needs |
|---|---|---|
| `tests/unit/` | Layer 1: unit tests, including tests of every gate and smoke module | nothing |
| `tests/stub/`, `tests/integration/` | Layer 2: stub-mode sweeps and integration tests against DynamoDB Local | Docker |
| plan-time gates (`check_plan_security_baseline`, terraform-backed unit tests) | Layer 3: the security baseline against a Terraform plan | `terraform` and initialised providers |
| `scripts/smoke/` | Layers 4-6: per-resource smoke, component integration and end-to-end checks | a deployed stack and credentials; billable |
| `tests/e2e/browser/` | Browser journeys through the deployed SPA | a deployed stack; billable |

![The test ladder: Layer 1 unit tests need nothing, Layer 2 stub and integration tests need Docker, Layer 3 plan-time gates need terraform; make verify runs Layers 1-3 offline. Layers 4-6 smoke checks and the T4 browser journeys need a deployed stack and are billable; make verify-deploy runs Layers 4-6](figures/development-test-ladder.svg)

## Make targets

| Target | Runs | Enforces |
|---|---|---|
| `typecheck` | `mypy --strict src scripts tests` | type hints on every signature |
| `lint` | `ruff check .` | lint rules, including ruff's `S` (flake8-bandit) family outside `tests/` |
| `format-check` | `ruff format --check .` | formatting |
| `test` | `pytest tests/unit` under pytest-xdist | Layer 1 |
| `test-stub` | `pytest tests/stub` | stub-mode sweeps make no model call |
| `test-integration` | `pytest tests/integration` | Layer 2 against DynamoDB Local |
| `test-relay` | `npm run typecheck && npm run test` in `src/relay` | the relay's typecheck and vitest suite |
| `test-journeys-static` | `tsc --noEmit` and `playwright test --list` in `tests/e2e/browser`, with the project's pinned binaries | every browser journey type-checks and is collected; no browser is launched and no stack is needed |
| `check-cost-withheld` | `scripts/check_cost_withheld.sh` | no absolute currency figure is published while the price table is provisional |
| `verify-shellcheck` | `shellcheck` over every tracked file with a shell shebang, derived from `git ls-files` | shell lint |
| `verify-gates` | `scripts/run_gates.py` | every offline gate below, with all failures collected |
| `verify` | all of the above, with `make -k` | the whole offline suite (Layers 1-3) |
| `verify-deploy` | `scripts/verify_deployment.sh` | Layers 4-6 against a deployed stack; needs credentials and is billable |

`make verify` reports every failing target, not only the first. Targets that need no Docker:
`test`, `typecheck`, `lint`, `format-check`, `verify-gates`, `verify-shellcheck`,
`check-cost-withheld`.

### Variables that turn a skip into a failure

`make verify` exports these. Run a target on its own without them and the corresponding tests skip
when the dependency is missing. The authoritative set is
`grep -oE 'CRM_REQUIRE_[A-Z_]+' Makefile | sort -u`.

| Variable | Forces | Skips without it when |
|---|---|---|
| `CRM_REQUIRE_DYNAMODB_LOCAL=1` | `test-stub`, `test-integration` | DynamoDB Local is unreachable |
| `CRM_REQUIRE_RELAY_PACKAGING=1` | the relay packaging tests | `node`, `npm`, `zip` or `unzip` is missing |
| `CRM_REQUIRE_TERRAFORM=1` | the plan-time infra tests | `terraform` or `infra/.terraform/providers` is missing |
| `CRM_REQUIRE_RELAY_TESTS=1` | `test-relay` | `npm` or `src/relay/node_modules` is missing |
| `CRM_REQUIRE_JOURNEY_STATIC=1` | `test-journeys-static` | `npm` or `tests/e2e/browser/node_modules` is missing (`cd tests/e2e/browser && npm ci`) |
| `CRM_REQUIRE_TS_CONTRACTS=1` | `tests/integration/test_envelope_groups_contract.py`, `tests/integration/test_eval_envelopes_contract.py` | `node` or `frontend/node_modules` is missing. **Not exported by `make verify`:** only the CI `frontend` job sets it, on the frontend's pinned Node |

`tests/stub/test_stub_default_no_credentials.py` fails rather than skips without DynamoDB Local.

### Deployed verification

```bash
make verify-deploy VERIFY_DEPLOY_ARGS="--i-understand-this-is-billable"
bash scripts/verify_deployment.sh --dry-run                 # preview; no AWS call
bash scripts/verify_deployment.sh --layer 4 --i-understand-this-is-billable
bash scripts/verify_deployment.sh --layer inventory         # consolidate existing evidence
```

`verify_deployment.sh` refuses to run without `--i-understand-this-is-billable` or `--dry-run`, and
asserts the target account and region against `results/tf-outputs.json` before any check. It stops
at the first failing layer. `--layer inventory` re-runs nothing and writes
`results/verification-inventory.md`. [`docs/runbook.md`](runbook.md) lists every check per layer.

`scripts/verify_fresh_clone.sh --materialise-only [--ref REF]` copies tracked content only (`HEAD` by
default, `--ref -` for the index) into a scratch directory and prints its path. The script runs no
gate, and without `--materialise-only` it deletes the copy on exit. Run the gates against the copy
to catch one that passes only because of an untracked local file.

## Offline gates

`scripts/run_gates.py` runs these gates. Each one also runs on its own. Exit codes: `0` pass, `1`
violation, `2` usage or environment error, `3` verified nothing (shown as `[SKIP]`, never counted as
a pass). A gate that exits 0 with deferred controls is shown as `[PARTIAL]`, with a `[DEFER]` line
for each deferred control.

| Gate | Enforces |
|---|---|
| `check_no_account_id` | no AWS account id, VPC-family resource id (`vpc-`, `subnet-`, `sg-`, `vpce-`, `rtb-`, `igw-`, `vo_`), IAM user ARN (other than the placeholder users `example-deployer` and `smoke-test`), absolute home path or private key in the tracked working tree |
| `check_no_account_id_head` | the same patterns against committed history (`--ref HEAD`) |
| `check_no_vendor_keys` | no vendor API key in the tracked working tree |
| `check_no_commit_ids` | no git commit id in any tracked text file: a 40-hex token, or a 7–39-hex token with a letter and a digit beside a commit keyword or as the value of a commit-naming key (`git_revision`, `captured_from_commit`, `base`, …); sha256 and longer hex runs, UUIDs, `sha256:`/`sha256-` digests, all-digit tokens, `#` colours and the `FAKE_COMMIT_IDS` placeholders are not ids. Reads file contents only, so a clone of any history gives the same result. `[SKIP]` when no tracked text file |
| `check_no_untracked_refs` | no comment, docstring or tracked Markdown line references content that exists only in untracked local spec trees: requirement, decision and acceptance ids, section-sign references, review-finding ids, the names of the untracked decision, design, spec, review and evidence files, or a spec slug (the shapes are `REFERENCE_RE` in the script). Scans `#`, `//`, `/* */` and `<!-- -->` comments, Python docstrings and whole `.md` files; string literals are not scanned (`SCAN_STRING_LITERALS` is off). Ids that tracked files define are allowlisted, and reference text a test reads is exempted, each entry naming the tracked file it depends on; an entry whose file no longer backs it fails the gate. `[SKIP]` when no scannable tracked file |
| `check_no_vendor_keys_head` | the same patterns against committed history |
| `check_log_redaction` | no token, secret or full payload reaches a log call |
| `check_license_headers` | an SPDX header on every first-party `*.py` and `*.sh`, the frontend, relay and browser-journey JS/TS sources, and `githooks/pre-commit` |
| `check_toolspec_parity` | every consumer of `src/crm/toolspec/definitions.py` matches it: the tool count, the generated frontend types, the Terraform tool transcription, and handler input validation |
| `check_pin_drift` | `pyproject.toml`, `requirements.txt` and `.python-version` agree, with exact pins |
| `check_cost_withheld` | no absolute currency figure in `results/**`, `README.md` or the eval dashboard while `config/pricing.yaml` is provisional; `[SKIP]` when nothing is in scope |
| `check_tracked_tree_self_contained` | every first-party import under `scripts/`, `src/` and `tests/` resolves inside the tracked tree |
| `check_subprocess_funnel` | no tracked `*.py` under `scripts/` or `tests/` calls `subprocess.run`, `Popen`, `call`, `check_call` or `check_output`, from-imports one of them, or aliases the `subprocess` import; only `scripts/proc_run.py`, the argv-only, no-shell funnel, calls `subprocess` directly. A stored reference such as `real_run = subprocess.run` is not a call and is not reported |
| `check_doc_figures` | every image in `README.md` and `docs/**/*.md`, `docs/figures/README.md` included (outside fenced and inline code), is an inline `![alt](path)` link (no reference-style link, no HTML `<img>`) with alt text, naming a tracked `.svg` under `docs/figures/`; every `.svg` there is linked from some markdown file, every `.svg` and `.drawio` has its tracked sibling, and nothing else but `docs/figures/README.md` lives there; each `.drawio` is an uncompressed `<mxfile>` (one `<mxGraphModel>` per `<diagram>`) with no `html=1` or `whiteSpace=wrap` style; each `.svg` has non-blank `<text>` and no `<foreignObject>`, `<script>`, `on*=` attribute, `javascript:`, protocol-relative reference or `light-dark(`; and no `.svg` or `.drawio` holds an `http(s)` URL beyond the XML namespaces, the SVG 1.1 DTD, `https://www.drawio.com` and the `<mxfile>` `host` attribute; and each `.svg` is fresh against its `.drawio` (R10): every label line of every `mxCell` value is the text of an SVG `<text>` element, and every SVG `<text>` line is a source label line. Embedded `data:image/svg+xml` images are decoded and checked too, and one that cannot be decoded fails. `[SKIP]` when no figure and no image link is tracked |
| `check_doc_links` | every tracked `.md`'s inline links, images and reference definitions (outside fenced and inline code and HTML comments) name a tracked file or directory, case-sensitively; every `#anchor` on a link to a tracked `.md` matches a GitHub heading slug or an `<a id>`/`<a name>` in it; every tracked `.md` is reachable from `README.md` through `.md` links, except the `EXEMPT_UNREACHABLE` entries (none today). External schemes are skipped. `[SKIP]` when no `.md` is tracked |
| `check_pipefail_sigpipe` | no pipeline in code running under `pipefail` has a consumer that can stop reading early (`grep -q`/`--quiet`/`-m`, `head`, `sed Nq`) in a non-first position, since its producer's SIGPIPE turns a match into a failure: tracked shell files (shebang-derived plus `*.sh`, after the `set -o pipefail` line), Makefile recipe lines whose shell enables it, GitHub `run:` blocks and GitLab `before_script`+`script`/`after_script`. A `$(… \| head -1 \|\| true)` capture is allowed; a masked pipeline used as a condition is not. `[SKIP]` when no shell file is tracked |
| `check_untracked_artifact_secrets` | no vendor key in a generated artifact, and untracked artifacts that hold account material stay gitignored |
| `check_secret_scan` | gitleaks over the working tree's non-ignored files (`git ls-files --cached --others --exclude-standard`), with findings redacted: the untracked local `.gitleaks.toml` when present, else gitleaks' built-in rules; `[SKIP]` without `gitleaks`. Gitignored files (caches, `.venv`, local scanner output) are not read; `check_untracked_artifact_secrets` is their only coverage |
| `check_emitted_css` | every static class token in frontend source resolves to a rule in the built CSS; `[SKIP]` without `npm ci` |
| `verify_fixtures` | the synthetic dataset regenerates byte-identically |
| `check_plan_security_baseline` | the infrastructure security baseline (`docs/security-baseline.md`) against a plan fixture; reports `[PARTIAL]` for controls that cannot be resolved from a plan |
| `check_no_public_exposure` | no unauthenticated Function URL, no wildcard invoke principal, the default `execute-api` endpoint disabled, a `vpc_config` on every Lambda, the `AWS_IAM` auth-type condition on every Function URL grant, and no public S3 surface |
| `check_vpc_endpoint_coverage` | every AWS service `src/` builds a client for has a VPC endpoint or an allowlist entry |
| `check_gsi_projection_parity` | every Terraform GSI's projected attributes match the schema's declared projection |
| `check_handler_grant_coverage` | each action Lambda's DynamoDB, KMS and log grants match the calls its handler code reaches, in both directions |
| `check_payload_validation_coverage` | every path that persists a proposal payload validates it first |
| `check_lambda_zip_imports` | each built Lambda zip, by phase: `closure` (handler imports resolve inside the zip), `vendored_record` (vendored `RECORD` files match the zip contents), `wheel_platform` (Linux x86_64, CPython 3.13 or `abi3` tags), `marker_requires` (dependency markers evaluated for the Lambda environment), `isolated_import` (the handler imports in an isolated interpreter), `source_parity` (first-party files match `src/`), and `tracked_provenance` (first-party files match tracked git content) |
| `check_lambda_adapter_isolation` | no Lambda handler reaches a vendor-SDK model adapter: `import_time` (no adapter import on any handler's import path) and `stub_literal` (`eval.trigger` takes only an allowlist of names from `eval.harness`, references no `eval.live`, `providers.factory` or `eval.sweep_units`, and only `submit_sweep` takes a mode-selecting parameter) |
| `verify_terraform_plan_with_kms_absent` | `lambda_action`'s KMS grant count comes from an input variable, so the module still plans once the key is gone; `[SKIP]` without `terraform` |
| `check_sse_schema_parity` | every generated SSE event consumer matches `src/agents/event_schema.py` |
| `check_readme_gate_phases` | a gate that lists its phases in its module docstring names every phase in its row of this table |
| `check_relay_thinness` | the relay imports no CRM module, constructs no identity, and stays under a size ceiling |
| `check_relay_zip` | the built `relay.zip` matches the `src/relay/` tree recorded in its build manifest; `[SKIP]` when no zip exists |
| `check_identity_channel_parity` | the relay strips every identity-channel key that `src/crm/envelope.py` defines |
| `check_page_context_no_rows` | no `PageContext` variant carries retrieved records |
| `check_design_tokens` | no raw colour, `px` font size or off-scale spacing value in component source |
| `check_testid_preservation` | every `data-testid` a test uses exists in component source, and live-region counts do not drop |
| `check_region_literals` | no `us-east-1` literal or AZ id in a tracked code, IaC or config file outside `infra/regions.json`, the exempt classes (tests, `*.md`, figures, `results/`) and the per-file pins in `EXEMPT_FILES`, each pinned to an exact count with a category; no tracked Markdown matches `BANNED_PHRASE_PATTERN` (a single-region claim). `--plan` checks a plan JSON's region strings against `PLAN_ALLOWED_PATHS` |
| `check_region_map` | `infra/regions.json` passes every `scripts/region_map.py` validation rule |
| `check_canary_tenant_empty` | the `canary` tenant holds no records; `[SKIP]` offline because it needs a real store |

To add a gate, register it in `GATES` in `scripts/run_gates.py`, add a unit test under
`tests/unit/`, add its row here, and commit the script in the same change. A registered script that
is not tracked fails only on a fresh clone.

## Static analysis

ruff `S` runs in `make lint`. bandit, checkov and detect-secrets are not configured in the
repository. No CI job runs any of them, `pip-audit` or `npm audit`, so a clean result is a
point-in-time measurement.

| Tool | Config | What it covers |
|---|---|---|
| ruff `S` | `pyproject.toml` `[tool.ruff.lint]` | flake8-bandit rules over every Python file in `make lint`; `tests/**` ignores `S101`, `S105`-`S108`, `S310`, `S603` and `S607` |

```bash
uvx --from 'pip-audit==2.10.1' pip-audit --no-deps --disable-pip -r requirements.txt
(cd frontend && npm audit --audit-level=high)      # likewise in src/relay and tests/e2e/browser
```

## CI

`.github/workflows/verify.yml` runs `make verify` and the frontend checks on pushes to `main` and on
pull requests. `.github/workflows/browser-journeys.yml` is started manually and runs the browser
journeys against a deployed dev stack.
`tests/unit/test_ci_parity.py` holds the workflow's gate set equal to the `Makefile`'s. See
[`.github/workflows/README.md`](../.github/workflows/README.md).

## Contributor rules

- Every first-party `*.py` under `src/`, `scripts/`, `tests/` and `fixtures/` (empty `__init__.py`
  included), every `*.sh` under `scripts/` and `infra/`, and `githooks/pre-commit` starts with
  `# SPDX-License-Identifier: MIT`. Every `*.ts`/`*.tsx`/`*.js`/`*.mjs`/`*.cjs` under `frontend/`,
  and every `*.ts`/`*.js`/`*.mjs`/`*.cjs` under `src/relay/` and `tests/e2e/browser/`, carries
  `// SPDX-License-Identifier: MIT` (or the same text in a block comment). Gitignored output is
  not checked.
- `mypy --strict` over `src scripts tests`; `ruff` with line length 100.
- No AWS account id, VPC, subnet or security-group id, or private key in tracked content. Use
  `123456789012` as a placeholder account id. In test fixtures, assemble id-shaped strings at
  runtime instead of writing literals.
- Never hand-edit a generated artifact: the frontend types, the Gateway tool definitions, the SSE
  event consumers, the fixture dataset, or anything under `results/`.
- In-code documentation is interface-level: what a module does and how to use it.

## Deploy internals

- `scripts/package_lambdas.sh [--build-dir DIR] [--print-pins]` builds the Python Lambda zips. It
  cleans the build directory first but preserves `relay.zip`, so it and `scripts/package_relay.sh
  [--build-dir DIR] [--skip-install] [--no-verify-import]` can run in either order. Both write to
  `infra/build/lambda/`, which is what `scripts/deploy.sh` checks and Terraform reads; keep that
  default when deploying.
  `scripts/package_relay.sh --verify-only ZIP` checks an existing zip.
- `scripts/deploy.sh` refuses before applying anything if: the git index has unmerged paths; `aws`,
  `terraform` or `python3` is missing; the `.venv` cannot import boto3; the STS identity cannot be
  resolved or does not match the expected account; `infra/terraform.tfvars` is missing or still holds
  placeholder ids; a Lambda or relay zip is stale or missing; or `check_no_public_exposure` fails.
- Lambda packaging is not byte-deterministic, so every apply shows Lambda changes. The post-apply
  `terraform plan -detailed-exitcode` exits 2, and `scripts/classify_post_apply_drift.py` treats the
  result as a warning only when every pending change is a Lambda-packaging resource.
