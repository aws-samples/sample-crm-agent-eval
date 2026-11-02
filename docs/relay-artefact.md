# The relay deployment artefact: build, verify, deploy guard

See also: [documentation map](../README.md#documentation) · [agent_relay module](../infra/modules/agent_relay/README.md)

Operational reference for `scripts/package_relay.sh` and `scripts/check_relay_zip.py`, which build
and staleness-check the streaming relay Lambda (`src/relay/`).

The relay is the only TypeScript outside `frontend/`, so it has its own packaging script rather than
`scripts/package_lambdas.sh`. Both write into the same build directory; `package_lambdas.sh` clears
that directory but preserves `relay.zip`, so the two can run in either order.

![The streaming relay's AWS path: the browser runs the React SPA it downloads through CloudFront (served from a private S3 bucket); CloudFront /agent/* signs with OAC to the relay's AWS_IAM Function URL, the relay verifies the Cognito JWT and calls the AgentCore Runtime over SigV4; an inset shows one turn, with plain HTTP rejections before the stream opens and an in-band SSE error frame after the first byte](figures/relay-artefact-aws-path.svg)

## Quick start

```bash
scripts/package_relay.sh                      # build + verify, writes infra/build/lambda/relay.zip
.venv/bin/python3 scripts/check_relay_zip.py  # is that zip current for src/relay?
```

Neither needs AWS credentials or costs anything.

## Build directory

`infra/main.tf`'s `var.lambda_build_dir` defaults to the relative path `build/lambda`, which
Terraform resolves against its own working directory (`infra/`). The artefact therefore belongs at
`infra/build/lambda/relay.zip`, beside the Python zips and `agent-source.zip`. A zip written to
`<repo>/build/lambda/` is never read by `terraform apply`. Two tests pin the resolution
(`test_default_build_dir_is_the_one_terraform_actually_reads` in `tests/unit/test_package_relay.py`,
`test_the_checked_zip_is_the_one_terraform_reads` in `tests/unit/test_deploy_script.py`).

`build/` is gitignored.

### Overriding the build directory

`LAMBDA_BUILD_DIR` and `--build-dir` are honoured by the packaging scripts and the deploy guards,
but **not by Terraform**: `scripts/deploy.sh` does not forward the variable to `terraform apply`.
Exporting `LAMBDA_BUILD_DIR` to a non-default path makes the guards pass (they look where the zips
were written) and the apply fail on a missing zip. Leave it unset unless you also pass
`-var lambda_build_dir=...` to Terraform yourself.

`scripts/check_relay_zip.py` reads `--build-dir` (or `--zip`) but **not** `LAMBDA_BUILD_DIR`. On the
deploy path this is harmless, because `deploy.sh` passes `--zip` explicitly; a standalone run against
a non-default directory needs the flag.

## `scripts/package_relay.sh`

| Flag | Effect |
|---|---|
| *(none)* | `npm ci`, compile, stage, install production deps, archive, verify the import |
| `--build-dir DIR` | Output directory. Overrides `LAMBDA_BUILD_DIR`, and logs that it did |
| `--skip-install` | Reuse the existing `src/relay/node_modules` (fails if absent) |
| `--no-verify-import` | Skip the import probe. For speed in tests; not for a deploy |
| `--verify-only ZIP` | Only run the import probe against an existing zip. Builds nothing |

Any other argument exits with `unknown argument`.

Output directory resolution is **flag, then `LAMBDA_BUILD_DIR`, then default**, matching
`scripts/deploy.sh`.

### Zip contents

- **`index.js` at the archive root.** The handler is `index.handler`
  (`infra/modules/agent_relay/variables.tf`). A zip containing `build/index.js` deploys and then fails
  every invocation with `Runtime.ImportModuleError`.
- **`package.json` with `"type": "module"`.** The compiled output is ESM; without this, loading
  depends on Node's module-syntax detection. Asserted structurally.
- **`node_modules/` from `npm ci --omit=dev`.** Production dependencies are bundled: `aws-jwt-verify`
  is not in any Lambda runtime, and bundling the AWS SDK v3 clients ties behaviour to the committed
  lockfile rather than the runtime's patch level. No devDependency ships.
- **`.relay-build.json`**, the build manifest (below).

The zip is **byte-deterministic** (normalised mtimes, `zip -X`, `LC_ALL=C`-sorted entries), so an
unchanged tree yields an unchanged hash and relay churn in a plan is a real change. The Python zips
from `package_lambdas.sh` are not deterministic.

## The build manifest

`.relay-build.json` at the archive root records the SHA-256 of every compile input:

```json
{ "schema": 1, "input_roots": ["package-lock.json", "package.json", "src", "tsconfig.build.json", "tsconfig.json"], "sources": { "src/index.ts": "f13c6f…" } }
```

The relay zip contains compiled JavaScript and no TypeScript, so it cannot be compared against
`src/relay` directly; the manifest is what makes staleness observable.

`package_relay.sh` writes the manifest and never compares it; `scripts/check_relay_zip.py` is the
sole implementation of the currency check. The set of inputs is defined once, by
`MANIFEST_INPUT_PATHS` in the packaging script, and travels inside the artefact as `input_roots`.
`tests/unit/test_check_relay_zip.py` asserts the Python gate's mirror of those constants still
matches.

## `scripts/check_relay_zip.py`

```bash
.venv/bin/python3 scripts/check_relay_zip.py [--zip PATH | --build-dir DIR] [--require-zip] [--json]
```

| Exit | Meaning |
|---|---|
| `0` | The zip matches `src/relay` for every compile input |
| `1` | An input changed, was added or was removed; the manifest is missing or unusable; or the zip is absent under `--require-zip` |
| `3` | Verified nothing: there is no zip and `--require-zip` was not passed, or `src/relay` is missing. Rendered `[SKIP]`, never counted as a pass |

The deploy path passes `--require-zip`, so an absent zip is a failure there rather than a skip.

The check fails closed: a zip with no manifest is refused. Every failure names the offending path.

**Where it runs.** `scripts/run_gates.py` registers it without `--require-zip`, so `make verify`
reports a stale zip when one exists and reports `[SKIP]` on a clean clone with no `infra/build/`.
`scripts/deploy.sh` runs it with `--require-zip` as a hard gate. `tests/unit/test_check_relay_zip.py`
exercises its logic against fixtures.

## The deploy guard

![Relay build flow: package_relay.sh writes relay.zip with its build manifest into infra/build/lambda, package_lambdas.sh keeps it, and the deploy.sh guard runs check_relay_zip.py then the import probe before terraform init, refusing on a stale, absent or unimportable zip](figures/relay-artefact-build-flow.svg)

`scripts/deploy.sh` runs both checks before `terraform init`, next to the Python-zip guard:

1. `check_relay_zip.py --require-zip --zip <path>`: currency.
2. `package_relay.sh --verify-only <zip>`: importability.

Currency runs first, so a stale-but-importable zip is not reported healthy. Both `die` with "nothing
was applied". Exit `3` has its own fatal branch on the deploy path. Neither guard rebuilds; run
`scripts/package_relay.sh` yourself.

## Common failures

| Symptom | Cause and fix |
|---|---|
| `deploy.sh`: *the relay zip is stale, absent or unreadable relative to src/relay* | `src/relay/**`, the lockfile or a tsconfig changed without a rebuild. The lines above the message name every differing path. Run `scripts/package_relay.sh` |
| `deploy.sh`: *verified nothing (exit 3)* | `src/relay/` is missing or unreadable. Not a stale zip; check the working tree |
| `check_relay_zip`: *carries no .relay-build.json* | The zip was built without a manifest. Rebuild it |
| `package_relay.sh`: *compile produced no .js output* | Empty compile scope (`tsc` exits TS18003). Deliberately fatal |
| `package_relay.sh`: *no index.js at the archive root* | The handler did not compile, or staging flattened wrongly. Lambda cannot resolve `index.handler` |
| `ERR_MODULE_NOT_FOUND` from the import probe | A production dependency is missing. Usually `--skip-install` against a stale or dev-only `node_modules` |
| `does not declare "type": "module"` | The staged `package.json` lost its `type` field |
| `node <major> is older than the nodejs24.x target runtime` | Host Node predates the deployed runtime family |

## Tests

```bash
.venv/bin/python -m pytest tests/unit/test_package_relay.py tests/unit/test_check_relay_zip.py tests/unit/test_deploy_script.py
```

Each guard has tests asserting it fails on a planted defect: a missing dependency, a missing handler,
a missing or `type`-less `package.json`, a changed/added/removed input, an unmanifested zip, a
malformed manifest, and an absent source tree. `test_deploy_script.py` asserts wiring and ordering
only. `test_package_relay.py` runs real `npm ci` installs and is the slowest file in `tests/unit`.

**Limit.** The import probe runs on the host's Node, not on `nodejs24.x`. It proves import
resolution, the dependency closure and the export's shape, not version-conditional behaviour on the
deployed runtime. Streaming behaviour is verified only against a deployed stack.

## Environment contract

The build-dependent tests need `node`, `npm`, `zip` and `unzip`. They skip when those are absent
unless `CRM_REQUIRE_RELAY_PACKAGING=1`, in which case they fail. `make verify` exports it (see
`docs/development.md`, or `grep -n 'CRM_REQUIRE' Makefile`).

`CRM_REQUIRE_RELAY_PACKAGING` covers the artefact's importability, not the relay's vitest suite,
which is `make test-relay` under `CRM_REQUIRE_RELAY_TESTS`. That target needs
`src/relay/node_modules`, which is why the top-level `README.md` Install section includes
`cd src/relay && npm ci`. `scripts/package_relay.sh` runs its own `npm ci`, so packaging does not
depend on that install.
