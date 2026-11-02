# SPDX-License-Identifier: MIT
.PHONY: typecheck lint format-check test test-stub test-integration test-relay test-journeys-static check-cost-withheld verify-gates verify-shellcheck verify verify-deploy

typecheck:
	.venv/bin/mypy src scripts tests

lint:
	.venv/bin/ruff check .

format-check:
	.venv/bin/ruff format --check .

#: Worker processes for the unit, stub and integration suites (pytest-xdist). Override per
#: invocation, e.g. `make test PYTEST_WORKERS=8`; `PYTEST_WORKERS=0` runs serially.
PYTEST_WORKERS ?= 4

# `pyproject.toml`'s `addopts` already carries `-q`; a second `-q` would suppress pytest's summary
# footer. `--dist loadgroup` makes `pytest.mark.xdist_group` take effect
# (tests/unit/test_verify_gate_aggregation.py relies on it).
test:
	.venv/bin/python -m pytest tests/unit -n $(PYTEST_WORKERS) --dist loadgroup --durations=25

# Stub-mode tests: no model call, no cost, no AWS credential, through the default CLI entry point.
# Requires DynamoDB Local, as `test-integration` does.
test-stub:
	.venv/bin/python -m pytest tests/stub -n $(PYTEST_WORKERS) --dist loadfile --durations=25

# Integration tests against DynamoDB Local. Skip when the engine is unreachable unless
# CRM_REQUIRE_DYNAMODB_LOCAL=1, which `verify` sets. Every DynamoDB Local table prefix is
# uuid-unique, so workers share one engine; `--dist loadfile` keeps module fixtures to one per file.
test-integration:
	.venv/bin/python -m pytest tests/integration -n $(PYTEST_WORKERS) --dist loadfile --durations=25

# The relay's TypeScript typecheck and vitest suite. Skips when npm or src/relay/node_modules is
# absent, and fails instead under CRM_REQUIRE_RELAY_TESTS=1 (which `verify` exports). The relay
# build runs in `scripts/package_relay.sh`, not here.
test-relay:
	@if ! command -v npm >/dev/null 2>&1; then \
	  reason="npm is not on PATH"; \
	elif [ ! -d src/relay/node_modules ]; then \
	  reason="src/relay/node_modules is absent — run: cd src/relay && npm ci"; \
	else \
	  reason=""; \
	fi; \
	if [ -n "$$reason" ]; then \
	  if [ "$$CRM_REQUIRE_RELAY_TESTS" = "1" ]; then \
	    echo "FAIL test-relay: $$reason" >&2; \
	    exit 1; \
	  fi; \
	  echo "[SKIP] test-relay: $$reason; verified nothing"; \
	  exit 0; \
	fi; \
	cd src/relay && npm run --silent typecheck && npm run --silent test

# The browser journeys' two static gates: `tsc --noEmit` (types) and `playwright test --list`
# (collection; no browser, no deployed stack). Skips when npm or tests/e2e/browser/node_modules is
# absent, and fails instead under CRM_REQUIRE_JOURNEY_STATIC=1 (which `verify` exports). Uses the
# pinned local binaries; `npx tsc` resolves an unrelated public package.
test-journeys-static:
	@if ! command -v npm >/dev/null 2>&1; then \
	  reason="npm is not on PATH"; \
	elif [ ! -d tests/e2e/browser/node_modules ]; then \
	  reason="tests/e2e/browser/node_modules is absent — run: cd tests/e2e/browser && npm ci"; \
	else \
	  reason=""; \
	fi; \
	if [ -n "$$reason" ]; then \
	  if [ "$$CRM_REQUIRE_JOURNEY_STATIC" = "1" ]; then \
	    echo "FAIL test-journeys-static: $$reason" >&2; \
	    exit 1; \
	  fi; \
	  echo "[SKIP] test-journeys-static: $$reason; verified nothing"; \
	  exit 0; \
	fi; \
	cd tests/e2e/browser && ./node_modules/.bin/tsc --noEmit -p tsconfig.json && ./node_modules/.bin/playwright test --list

# Fails when an absolute currency figure appears in a published artifact (results/matrix.md,
# matrix.csv, matrix.json, results/raw/) while config/pricing.yaml is provisional, and fails when
# cost_withheld_provisional is false on any arm while every configured rate is zero.
check-cost-withheld:
	bash scripts/check_cost_withheld.sh

# Layer 3 plan-time security baseline plus the remaining offline shell/Python gates, aggregated by
# scripts/run_gates.py so every failure is reported. No AWS credential and no network call.
verify-gates:
	.venv/bin/python3 scripts/run_gates.py

# shellcheck over every tracked file with a shell shebang, derived from `git ls-files`. Fails if
# the derived set is empty or misses any file matching `scripts/*.sh infra/*.sh`; a file outside
# that glob can leave the set without failing. A tracked path containing whitespace is refused.
# POSIX sh only, since make runs recipes under /bin/sh. shellcheck's status is checked explicitly
# because the recipe's last command is the summary `echo`.
verify-shellcheck:
	@if git ls-files | grep '[[:space:]]' >/dev/null; then \
	  echo "ERROR: a tracked path contains whitespace; this scan cannot be trusted" >&2; exit 1; \
	fi; \
	files=$$(git ls-files | while IFS= read -r f; do \
	  if [ -f "$$f" ] && head -n1 -- "$$f" 2>/dev/null | grep '^#!.*sh' >/dev/null; then \
	    printf '%s\n' "$$f"; \
	  fi; \
	done); \
	if [ -z "$$files" ]; then \
	  echo "ERROR: shebang scan matched no shell files; refusing to pass" >&2; exit 1; \
	fi; \
	for g in scripts/*.sh infra/*.sh; do \
	  printf '%s\n' "$$files" | grep -xF -- "$$g" >/dev/null \
	    || { echo "ERROR: $$g is not in the derived set; the scan regressed" >&2; exit 1; }; \
	done; \
	if ! shellcheck $$files; then exit 1; fi; \
	echo "shellcheck: $$(printf '%s\n' "$$files" | grep -c .) tracked shell files clean"

# Aggregate offline gate (Layers 1-3). `-k` runs every target and exits non-zero if any failed.
# The CRM_REQUIRE_* exports below turn each skippable suite's skip into a failure, for this target
# only. Needs no AWS credential; live-deployment checks are `make verify-deploy`.
verify: export CRM_REQUIRE_DYNAMODB_LOCAL = 1
# Relay packaging tests, which otherwise skip without node/npm/zip/unzip.
verify: export CRM_REQUIRE_RELAY_PACKAGING = 1
# Plan-time infra tests (`tests/unit/_terraform_fixture.py`), which otherwise skip without
# terraform on PATH or `infra/.terraform/providers`. Offline: mock credentials, `-backend=false`.
verify: export CRM_REQUIRE_TERRAFORM = 1
# The relay's vitest suite and typecheck; needs `src/relay/node_modules` (`cd src/relay && npm ci`).
verify: export CRM_REQUIRE_RELAY_TESTS = 1
# The browser journeys' static gates; needs `tests/e2e/browser/node_modules` (`npm ci` there).
verify: export CRM_REQUIRE_JOURNEY_STATIC = 1
verify:
	$(MAKE) -k lint format-check typecheck test test-stub test-integration test-relay test-journeys-static check-cost-withheld verify-gates verify-shellcheck

# Layers 4-6 against a live deployment via scripts/verify_deployment.sh. Requires AWS credentials
# and incurs cost. The script refuses to run without --i-understand-this-is-billable (or --dry-run),
# which this target does not supply. Pass flags through VERIFY_DEPLOY_ARGS:
#   make verify-deploy VERIFY_DEPLOY_ARGS="--i-understand-this-is-billable --token $JWT"
#   make verify-deploy VERIFY_DEPLOY_ARGS="--dry-run"
VERIFY_DEPLOY_ARGS ?=
verify-deploy:
	bash scripts/verify_deployment.sh $(VERIFY_DEPLOY_ARGS)

