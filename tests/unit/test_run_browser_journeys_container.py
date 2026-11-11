# SPDX-License-Identifier: MIT
"""`scripts/run_browser_journeys_container.sh` runs the T4 journeys in the pinned Playwright image.

Each cell runs a copy of the script in a scratch repository whose `docker` is a fake that records
its argv and environment, so no container starts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_browser_journeys_container.sh"
REAL_LOCK = REPO_ROOT / "tests" / "e2e" / "browser" / "package-lock.json"

_SECRET = "s3cret-value-not-for-argv"  # nosec B105  # planted test value, not a credential  # pragma: allowlist secret  # gitleaks:allow

_FAKE_DOCKER = """#!/usr/bin/env bash
python3 - "$@" <<'PY'
import json, os, sys
json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, open(os.environ["DOCKER_LOG"], "w"))
PY
exit "${FAKE_DOCKER_RC:-0}"
"""

#: A provenance checker stand-in: exit status from the environment, like `--check`'s 0 or 3.
_FAKE_PROVENANCE = """import os, sys
sys.exit(int(os.environ.get("FAKE_FRESHNESS_RC", "0")))
"""


def _lock(version: str = "1.56.1", *, core: str | None = None) -> dict[str, object]:
    return {
        "packages": {
            "node_modules/@playwright/test": {"version": version},
            "node_modules/playwright": {"version": version},
            "node_modules/playwright-core": {"version": core or version},
        }
    }


@dataclass(frozen=True)
class Run:
    returncode: int
    stderr: str
    argv: list[str] | None
    env: dict[str, str] | None


def _run(
    tmp_path: Path,
    *,
    lock: dict[str, object] | str | None = None,
    outputs: bool = True,
    freshness_rc: int = 0,
    env: dict[str, str] | None = None,
    args: tuple[str, ...] = (),
) -> Run:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, repo / "scripts" / SCRIPT.name)
    (repo / "scripts" / "outputs_provenance.py").write_text(_FAKE_PROVENANCE, encoding="utf-8")
    journeys = repo / "tests" / "e2e" / "browser"
    journeys.mkdir(parents=True)
    if lock is None:
        shutil.copy2(REAL_LOCK, journeys / "package-lock.json")
    elif isinstance(lock, str):
        (journeys / "package-lock.json").write_text(lock, encoding="utf-8")
    else:
        (journeys / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python3").symlink_to(shutil.which("python3") or "/usr/bin/python3")
    if outputs:
        (repo / "results").mkdir()
        (repo / "results" / "tf-outputs.json").write_text("{}", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_FAKE_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    log = tmp_path / "docker.json"

    system_dirs = [p for p in ("/usr/bin", "/bin", "/usr/local/bin") if Path(p).is_dir()]
    child_env = {
        "PATH": os.pathsep.join([str(bin_dir), *system_dirs]),
        "HOME": str(tmp_path),
        "DOCKER_LOG": str(log),
        "FAKE_FRESHNESS_RC": str(freshness_rc),
        **(env or {}),
    }
    result = proc_run.run(
        ["bash", str(repo / "scripts" / SCRIPT.name), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=child_env,
        timeout=60,
        check=False,
    )
    recorded = json.loads(log.read_text(encoding="utf-8")) if log.exists() else None
    return Run(
        result.returncode,
        result.stderr,
        recorded["argv"] if recorded else None,
        recorded["env"] if recorded else None,
    )


def _real_lock_version() -> str:
    packages = json.loads(REAL_LOCK.read_text(encoding="utf-8"))["packages"]
    version = packages["node_modules/@playwright/test"]["version"]
    assert isinstance(version, str)
    return version


def test_the_image_tag_is_read_from_the_committed_lock(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.argv is not None
    assert f"mcr.microsoft.com/playwright:v{_real_lock_version()}-noble" in run.argv


def test_a_different_lock_version_changes_the_tag(tmp_path: Path) -> None:
    run = _run(tmp_path, lock=_lock("1.60.2"))
    assert run.argv is not None
    assert "mcr.microsoft.com/playwright:v1.60.2-noble" in run.argv
    assert not any(a.startswith("mcr.microsoft.com/playwright:v1.56.1") for a in run.argv)


@pytest.mark.parametrize(
    "lock",
    ["not json", {"packages": {}}, _lock("1.56.1", core="1.57.0"), _lock("latest")],
)
def test_an_unreadable_or_inconsistent_lock_fails_without_docker(
    tmp_path: Path, lock: dict[str, object] | str
) -> None:
    run = _run(tmp_path, lock=lock)
    assert run.returncode != 0
    assert run.argv is None
    assert "Playwright version" in run.stderr


def test_the_platform_is_linux_amd64(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.argv is not None
    assert run.argv[run.argv.index("--platform") + 1] == "linux/amd64"


def test_the_container_runs_npm_ci_then_the_pinned_playwright(tmp_path: Path) -> None:
    run = _run(tmp_path, args=("journeys/01-signin.spec.ts",))
    assert run.argv is not None
    script = run.argv[run.argv.index("-c") + 1]
    assert script.startswith("npm ci ") and "./node_modules/.bin/playwright test" in script
    assert run.argv[-1] == "journeys/01-signin.spec.ts"
    assert "npx" not in " ".join(run.argv)


def test_no_browser_override_is_passed(tmp_path: Path) -> None:
    run = _run(tmp_path, env={"PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"})
    assert run.argv is not None
    joined = " ".join(run.argv)
    for forbidden in (
        "--channel",
        "channel=",
        "--headed",
        "headless",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
    ):
        assert forbidden not in joined


def test_host_node_modules_is_shadowed_by_a_container_volume(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.argv is not None
    volumes = [run.argv[i + 1] for i, a in enumerate(run.argv) if a == "-v"]
    assert "/repo/tests/e2e/browser/node_modules" in volumes


def test_a_missing_base_url_fails_without_docker(tmp_path: Path) -> None:
    run = _run(tmp_path, outputs=False)
    assert run.returncode != 0
    assert run.argv is None
    assert "CRM_E2E_BASE_URL" in run.stderr


def test_stale_outputs_fail_without_docker(tmp_path: Path) -> None:
    run = _run(tmp_path, freshness_rc=3)
    assert run.returncode != 0
    assert run.argv is None
    assert "stale" in run.stderr


def test_fresh_outputs_are_handed_to_the_container_by_path(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.argv is not None
    assert "CRM_TF_OUTPUTS_JSON=/repo/results/tf-outputs.json" in run.argv


def test_an_explicit_base_url_needs_no_outputs_and_is_passed_by_name(tmp_path: Path) -> None:
    run = _run(
        tmp_path, outputs=False, env={"CRM_E2E_BASE_URL": "https://d111111abcdef8.cloudfront.net"}
    )
    assert run.returncode == 0, run.stderr
    assert run.argv is not None and run.env is not None
    assert "CRM_E2E_BASE_URL" in run.argv
    assert run.env["CRM_E2E_BASE_URL"] == "https://d111111abcdef8.cloudfront.net"


def test_secrets_reach_docker_by_env_name_never_in_argv(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        env={"CRM_DEMO_USER_PASSWORD": _SECRET, "CRM_DEMO_USER_EMAIL": "demo@example.com"},
    )
    assert run.argv is not None and run.env is not None
    assert _SECRET not in " ".join(run.argv)
    assert "CRM_DEMO_USER_PASSWORD" in run.argv
    assert run.env["CRM_DEMO_USER_PASSWORD"] == _SECRET


def test_unset_passthrough_variables_are_not_forwarded(tmp_path: Path) -> None:
    run = _run(tmp_path)
    assert run.argv is not None
    assert "CRM_DEMO_USER_PASSWORD" not in run.argv
    assert "CRM_E2E_ADMIN_PASSWORD" not in run.argv


def test_dockers_exit_status_is_the_scripts(tmp_path: Path) -> None:
    run = _run(tmp_path, env={"FAKE_DOCKER_RC": "7"})
    assert run.returncode == 7


# ---------------------------------------------------------------------------------------------
# The executed-count floor: scripts/check-executed.mjs and its post-run invocation (TB-20)
# ---------------------------------------------------------------------------------------------

BROWSER_DIR = REPO_ROOT / "tests" / "e2e" / "browser"
CHECKER = BROWSER_DIR / "scripts" / "check-executed.mjs"
_REQUIRE_NODE_ENV_VAR = "CRM_REQUIRE_JOURNEY_STATIC"

_OPT_IN_SKIPS = {
    "CRM_E2E_ALLOW_LIVE_SWEEP": (
        "15-live-sweep-admin/live-sweep-admin.spec.ts",
        "a confirmed live sweep runs to completed with a per-tier report for each arm",
    ),
    "CRM_E2E_ALLOW_LIVE_SWEEP_CANCEL": (
        "16-sweep-cancel-resume/sweep-cancel-resume.spec.ts",
        "cancelling a running live sweep from the page ends it cancelled with N/total and its spend",
    ),
}
_DATA_GUARDED_SKIP = (
    "02-pipeline.spec.ts",
    "a refused transition is explained and offers no way to submit it",
)


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        if os.environ.get(_REQUIRE_NODE_ENV_VAR):
            pytest.fail(f"{_REQUIRE_NODE_ENV_VAR} is set and no `node` is on PATH")
        pytest.skip("no `node` on PATH to run tests/e2e/browser/scripts/check-executed.mjs")
    return node


def _report(
    *, executed: int, skipped: list[tuple[str, str]], passed_title: str = "a passing journey"
) -> dict[str, object]:
    specs: list[dict[str, object]] = [
        {"file": file, "title": title, "tests": [{"status": "skipped"}]} for file, title in skipped
    ]
    specs += [
        {
            "file": "01-signin.spec.ts",
            "title": f"{passed_title} {i}",
            "tests": [{"status": "expected"}],
        }
        for i in range(executed)
    ]
    return {
        "suites": [{"title": "root", "specs": specs[:1], "suites": [{"specs": specs[1:]}]}],
        "stats": {"expected": executed, "unexpected": 0, "flaky": 0, "skipped": len(skipped)},
    }


def _check_report(
    tmp_path: Path, report: dict[str, object] | str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "e2e-browser.json"
    path.write_text(report if isinstance(report, str) else json.dumps(report), encoding="utf-8")
    return proc_run.run(
        [_node(), str(CHECKER), str(path)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), **(env or {})},
        timeout=60,
        check=False,
    )


def test_a_report_where_everything_skipped_fails(tmp_path: Path) -> None:
    result = _check_report(tmp_path, _report(executed=0, skipped=[_DATA_GUARDED_SKIP]))
    assert result.returncode == 1
    assert "nothing executed" in result.stderr


def test_a_healthy_report_passes(tmp_path: Path) -> None:
    skipped = [_DATA_GUARDED_SKIP, *_OPT_IN_SKIPS.values()]
    result = _check_report(tmp_path, _report(executed=3, skipped=skipped))
    assert result.returncode == 0, result.stderr
    assert "3 executed, 3 declared skip(s)" in result.stdout


_HOSTED_UI_SKIPS = (
    ("01-signin.spec.ts", "after sign-out, a Hosted UI sign-in asks for credentials again"),
    ("01-signin.spec.ts", "a Hosted UI logout with no session still lands on the signed-out page"),
)


def test_only_journey_01s_two_hosted_ui_skips_are_declared(tmp_path: Path) -> None:
    """Both Hosted UI skips pass together; any other journey 01 skip beside them still fails."""
    declared = _check_report(tmp_path, _report(executed=2, skipped=list(_HOSTED_UI_SKIPS)))
    assert declared.returncode == 0, declared.stderr
    third = ("01-signin.spec.ts", "the /logout route serves the signed-out sign-in page")
    result = _check_report(tmp_path, _report(executed=2, skipped=[*_HOSTED_UI_SKIPS, third]))
    assert result.returncode == 1
    assert result.stderr.count("undeclared skip:") == 1
    assert f"undeclared skip: {third[0]} › {third[1]}" in result.stderr


def test_an_undeclared_skip_fails_even_when_others_executed(tmp_path: Path) -> None:
    undeclared = ("01-signin.spec.ts", "a protected route renders no CRM data before sign-in")
    result = _check_report(tmp_path, _report(executed=5, skipped=[undeclared]))
    assert result.returncode == 1
    assert f"undeclared skip: {undeclared[0]} › {undeclared[1]}" in result.stderr


@pytest.mark.parametrize("variable", sorted(_OPT_IN_SKIPS))
def test_an_opt_in_skip_is_allowed_only_while_its_variable_is_unset(
    tmp_path: Path, variable: str
) -> None:
    report = _report(executed=1, skipped=[_OPT_IN_SKIPS[variable]])
    assert _check_report(tmp_path, report).returncode == 0
    assert _check_report(tmp_path, report, {variable: "0"}).returncode == 0
    opted_in = _check_report(tmp_path, report, {variable: "1"})
    assert opted_in.returncode == 1
    assert "undeclared skip" in opted_in.stderr


@pytest.mark.parametrize(
    "report",
    ["not json", json.dumps({"suites": []}), json.dumps({"stats": {"expected": "3"}})],
    ids=["unparseable", "no-stats", "non-integer-stats"],
)
def test_an_unreadable_or_malformed_report_fails(tmp_path: Path, report: str) -> None:
    assert _check_report(tmp_path, report).returncode == 1


def test_a_missing_report_fails(tmp_path: Path) -> None:
    result = proc_run.run(
        [_node(), str(CHECKER), str(tmp_path / "absent.json")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "cannot read" in result.stderr


def test_every_allowed_skip_names_a_skip_guard_in_its_journey() -> None:
    """Each ALLOWED_SKIPS entry is a real `test(...)` title in its spec with a `test.skip(` guard,
    and each opt-in names the variable that spec reads."""
    source = CHECKER.read_text(encoding="utf-8")
    entries = re.findall(r'file: "([^"]+)",\s*title: "([^"]+)",(?:\s*optIn: "([^"]+)",)?', source)
    assert len(entries) == 6, entries
    for file, title, opt_in in entries:
        spec = (BROWSER_DIR / "journeys" / file).read_text(encoding="utf-8")
        start = spec.find(f'test("{title}"')
        assert start >= 0, f"{file} declares no test titled {title!r}"
        next_test = spec.find("\ntest(", start + 1)
        body = spec[start : next_test if next_test >= 0 else len(spec)]
        assert "test.skip(" in body, f"{file} › {title!r} has no skip guard to allow"
        if opt_in:
            assert f'"{opt_in}"' in spec and "LIVE_OPT_IN" in body, (file, opt_in)
    assert {opt_in for _, _, opt_in in entries if opt_in} == set(_OPT_IN_SKIPS)


def test_the_checker_imports_only_node_builtins() -> None:
    source = CHECKER.read_text(encoding="utf-8")
    specifiers = re.findall(r"""(?m)^\s*import\b[^;]*?from\s*["']([^"']+)["']""", source)
    assert specifiers
    assert all(specifier.startswith("node:") for specifier in specifiers), specifiers
    assert source.splitlines()[1] == "// SPDX-License-Identifier: MIT"


_FAKE_PLAYWRIGHT = """#!/usr/bin/env bash
if [[ -n "${FAKE_REPORT:-}" ]]; then
    mkdir -p ../../../results
    printf '%s' "$FAKE_REPORT" > ../../../results/e2e-browser.json
fi
exit "${FAKE_PW_RC:-0}"
"""


def _container_script(tmp_path: Path) -> list[str]:
    run = _run(tmp_path / "record")
    assert run.argv is not None
    index = run.argv.index("-c")
    return run.argv[index - 1 : index + 3]


def _run_container_script(
    tmp_path: Path, *, report: dict[str, object] | None, pw_rc: int = 0, stale: bool = False
) -> int:
    """Run the recorded in-container command on the host with fake npm and Playwright."""
    command = _container_script(tmp_path)
    repo = tmp_path / "container-repo"
    browser = repo / "tests" / "e2e" / "browser"
    (browser / "scripts").mkdir(parents=True)
    shutil.copy2(CHECKER, browser / "scripts" / CHECKER.name)
    playwright = browser / "node_modules" / ".bin" / "playwright"
    playwright.parent.mkdir(parents=True)
    playwright.write_text(_FAKE_PLAYWRIGHT, encoding="utf-8")
    playwright.chmod(0o755)
    if stale:
        (repo / "results").mkdir()
        (repo / "results" / "e2e-browser.json").write_text(
            json.dumps(_report(executed=4, skipped=[])), encoding="utf-8"
        )
    bin_dir = tmp_path / "container-bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)
    node_dir = str(Path(_node()).parent)
    env = {
        "PATH": os.pathsep.join([str(bin_dir), node_dir, "/usr/bin", "/bin"]),
        "FAKE_PW_RC": str(pw_rc),
        **({"FAKE_REPORT": json.dumps(report)} if report is not None else {}),
    }
    result = proc_run.run(
        command, cwd=browser, capture_output=True, text=True, env=env, timeout=60, check=False
    )
    return result.returncode


def test_the_container_run_fails_when_the_report_executed_nothing(tmp_path: Path) -> None:
    assert _run_container_script(tmp_path, report=_report(executed=0, skipped=[])) == 1


def test_the_container_run_passes_a_healthy_report(tmp_path: Path) -> None:
    assert _run_container_script(tmp_path, report=_report(executed=2, skipped=[])) == 0


def test_the_container_run_keeps_playwrights_own_failure(tmp_path: Path) -> None:
    assert _run_container_script(tmp_path, report=_report(executed=2, skipped=[]), pw_rc=4) == 4


def test_a_stale_report_cannot_pass_a_run_that_wrote_none(tmp_path: Path) -> None:
    assert _run_container_script(tmp_path, report=None, stale=True) == 1
