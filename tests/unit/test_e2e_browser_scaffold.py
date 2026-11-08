# SPDX-License-Identifier: MIT
"""Contract tests for the T4 browser-journey scaffold.

`npx playwright test --list` proves only that the config parses and specs can be enumerated, so the
scaffold's guarantees are asserted here:

* The base URL is discovered from stack outputs, never hardcoded.
* There is no `localhost` fallback.
* `globalSetup` refuses an unresolved, stale or production-looking target; it does not run for
  `--list`.
* Sign-in is driven through the SPA's own sign-in form, not injected.

Most assertions read the TypeScript sources; the freshness refusal executes `globalSetup`.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Final

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
BROWSER_DIR = REPO_ROOT / "tests" / "e2e" / "browser"
CONFIG = BROWSER_DIR / "playwright.config.ts"
GLOBAL_SETUP = BROWSER_DIR / "fixtures" / "globalSetup.ts"
AUTH_FIXTURE = BROWSER_DIR / "fixtures" / "auth.ts"
CSP_TRAP_FIXTURE = BROWSER_DIR / "fixtures" / "cspTrap.ts"
SSE_RECORDER_FIXTURE = BROWSER_DIR / "fixtures" / "sseRecorder.ts"

#: The only fixture files allowed to call `addInitScript`: the CSP violation trap and the SSE frame
#: recorder. Any other fixture, including one added later, is refused it.
INIT_SCRIPT_ALLOWED_FIXTURES: Final = frozenset({CSP_TRAP_FIXTURE.name, SSE_RECORDER_FIXTURE.name})

#: Lowercased identifiers naming browser storage or a credential; none may appear in the code of a
#: fixture allowed an init script.
CREDENTIAL_SURFACE_TERMS: Final = (
    "localstorage",
    "sessionstorage",
    "indexeddb",
    "cookie",
    "storagestate",
    "token",
    "credential",
    "password",
    "secret",
    "authorization",
    "setextrahttpheaders",
    "jwt",
)
SIGN_IN_FORM = REPO_ROOT / "frontend" / "src" / "auth" / "SignInForm.tsx"
EVAL_ARTIFACT_LIST = REPO_ROOT / "frontend" / "src" / "pages" / "evals" / "EvalArtifactList.tsx"
APP_SHELL = REPO_ROOT / "frontend" / "src" / "AppShell.tsx"
SRP_SESSION = REPO_ROOT / "frontend" / "src" / "auth" / "CognitoAuthSession.ts"

#: An absolute http(s) URL literal; an interpolation immediately after the scheme is not a match.
_URL_LITERAL = re.compile(r"https?://(?!\$\{)[A-Za-z0-9]")

#: A local address used as a *value* — `//localhost`, `http://127.0.0.1`, `localhost:3000` — rather
#: than the bare word.
_LOCAL_TARGET = re.compile(r"//(?:localhost|127\.0\.0\.1)|\b(?:localhost|127\.0\.0\.1):\d")


def strip_comments(source: str) -> str:
    """Strip TypeScript comments without destroying URLs.

    The negative lookbehind on `:` keeps the `//` inside `https://host`.
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", without_block)


def test_the_comment_stripper_preserves_urls() -> None:
    """The comment stripper removes comments and keeps URLs."""
    assert "https://example.test" in strip_comments('const a = "https://example.test"; // note')
    assert "note" not in strip_comments("const a = 1; // note")
    assert "block" not in strip_comments("/* block */ const a = 1;")


def _requires_node() -> None:
    if not (BROWSER_DIR / "node_modules").is_dir():
        pytest.skip("tests/e2e/browser/node_modules absent; run `npm ci` there")


# ---------------------------------------------------------------------------
# The files exist and cohere.
# ---------------------------------------------------------------------------


def test_the_scaffold_files_exist() -> None:
    for path in (CONFIG, GLOBAL_SETUP, AUTH_FIXTURE):
        assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"


def test_the_lockfile_is_committed_and_pins_playwright() -> None:
    """The lockfile is committed and `@playwright/test` is pinned to an exact version."""
    lockfile = BROWSER_DIR / "package-lock.json"
    assert lockfile.is_file()
    manifest = json.loads((BROWSER_DIR / "package.json").read_text(encoding="utf-8"))
    pinned = manifest["devDependencies"]["@playwright/test"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pinned), (
        f"@playwright/test is {pinned!r}; pin an exact version rather than a range"
    )


#: Package names of this repository's own JS packages, read from their manifests.
def first_party_package_names() -> frozenset[str]:
    names = set()
    for manifest_path in (
        REPO_ROOT / "frontend" / "package.json",
        REPO_ROOT / "src" / "relay" / "package.json",
    ):
        if manifest_path.is_file():
            name = json.loads(manifest_path.read_text(encoding="utf-8")).get("name")
            if isinstance(name, str) and name:
                names.add(name)
    return frozenset(names)


#: A dependency specifier that resolves to a path rather than to a registry version.
_PATH_SPECIFIER = re.compile(r"^(?:file:|link:|portal:|workspace:|git|https?:|\.{1,2}/)")


def dependency_reaches_application_code(name: str, specifier: str) -> str | None:
    """Why this dependency could resolve application code, or ``None`` if it cannot.

    A dependency can do so by naming one of this repository's packages or by a path specifier.
    """
    if name in first_party_package_names():
        return f"{name!r} is a package in this repository"
    if _PATH_SPECIFIER.match(specifier):
        return f"{name!r} is specified as {specifier!r}, a path rather than a registry version"
    return None


def test_no_dependency_can_resolve_application_code() -> None:
    """No declared dependency of the journey package can resolve application code."""
    manifest = json.loads((BROWSER_DIR / "package.json").read_text(encoding="utf-8"))
    declared = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
    assert declared, "the manifest declares no dependencies at all; this check would be vacuous"

    offenders = {
        name: reason
        for name, specifier in declared.items()
        if (reason := dependency_reaches_application_code(name, str(specifier))) is not None
    }
    assert not offenders, (
        f"tests/e2e/browser depends on application code: {offenders}. The journeys must exercise the "
        "deployed stack; a dependency that resolves first-party source lets one test a module instead."
    )

    # Every dependency is exactly pinned.
    unpinned = {n: s for n, s in declared.items() if not re.fullmatch(r"\d+\.\d+\.\d+", str(s))}
    assert not unpinned, f"pin exact versions rather than ranges: {unpinned}"


def test_the_dependency_predicate_rejects_the_things_it_exists_to_catch() -> None:
    """The dependency predicate rejects first-party names and path specifiers and admits registry
    packages."""
    assert first_party_package_names(), (
        "no first-party package names were read; the check is vacuous"
    )

    for name, specifier in (
        ("crm-eval-frontend", "1.0.0"),
        ("crm-eval-relay", "1.0.0"),
        ("@crm/app", "file:../../../frontend"),
        ("app", "link:../../../frontend"),
        ("app", "workspace:*"),
        ("app", "../../../frontend"),
    ):
        assert dependency_reaches_application_code(name, specifier) is not None, (
            f"{name}@{specifier} must be rejected"
        )

    # The legitimate toolchain passes.
    for name, specifier in (
        ("@playwright/test", "1.56.1"),
        ("typescript", "5.9.3"),
        ("@types/node", "26.4.1"),
    ):
        assert dependency_reaches_application_code(name, specifier) is None, (
            f"{name}@{specifier} is a registry package and cannot resolve application code"
        )


#: Import specifiers in the statement positions that can pull in a module, anchored on the statement
#: keyword.
_IMPORT_PATTERNS = (
    re.compile(r"(?m)^\s*(?:import|export)\b[^;\n]*?\sfrom\s*[\"']([^\"']+)[\"']"),
    re.compile(r"(?m)^\s*import\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\bimport\(\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\brequire\(\s*[\"']([^\"']+)[\"']"),
)


def import_specifiers(source: str) -> list[str]:
    """Every module specifier `source` imports, comments stripped first."""
    code = strip_comments(source)
    found: list[str] = []
    for pattern in _IMPORT_PATTERNS:
        found.extend(pattern.findall(code))
    return found


def import_escapes_the_package(specifier: str, containing: Path) -> bool:
    """Whether a relative `specifier` resolves outside `tests/e2e/browser/`.

    A bare specifier resolves through `node_modules` and is covered by the dependency check above.
    """
    if not specifier.startswith("."):
        return False
    resolved = (containing / specifier).resolve()
    return BROWSER_DIR.resolve() not in resolved.parents and resolved != BROWSER_DIR.resolve()


#: Journey specs that live one directory down; each must be enumerated by `typescript_sources`.
NESTED_JOURNEY_FLOOR: Final = (
    "journeys/14-tier-tuning/tier-tuning.spec.ts",
    "journeys/15-live-sweep-admin/live-sweep-admin.spec.ts",
    "journeys/16-sweep-cancel-resume/sweep-cancel-resume.spec.ts",
    "journeys/17-audit-detail/audit-detail.spec.ts",
    "journeys/18-approval-rebase/approval-rebase.spec.ts",
    "journeys/19-account-opportunities/account-opportunities.spec.ts",
    "journeys/20-chat-cost/chat-cost.spec.ts",
)


def typescript_sources(directory: Path, suffix: str = ".ts") -> list[Path]:
    """Every file under `directory`, at any depth, ending in `suffix`, outside `node_modules`."""
    return sorted(
        path
        for path in directory.rglob(f"*{suffix}")
        if path.is_file() and "node_modules" not in path.relative_to(directory).parts
    )


def test_the_source_enumerator_descends_and_skips_node_modules(tmp_path: Path) -> None:
    """`typescript_sources` finds a nested spec and ignores one under `node_modules`."""
    (tmp_path / "14-x").mkdir()
    (tmp_path / "14-x" / "x.spec.ts").write_text("", encoding="utf-8")
    (tmp_path / "top.ts").write_text("", encoding="utf-8")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.ts").write_text("", encoding="utf-8")
    assert typescript_sources(tmp_path) == [tmp_path / "14-x" / "x.spec.ts", tmp_path / "top.ts"]


def test_no_journey_imports_application_code() -> None:
    """No journey, fixture or config source, at any depth, imports a first-party package or a path
    outside the package."""
    sources = [
        CONFIG,
        *typescript_sources(BROWSER_DIR / "journeys"),
        *typescript_sources(BROWSER_DIR / "fixtures"),
    ]
    enumerated = {path.relative_to(BROWSER_DIR).as_posix() for path in sources}
    missing = [spec for spec in NESTED_JOURNEY_FLOOR if spec not in enumerated]
    assert not missing, f"nested journey specs not enumerated, so not checked: {missing}"

    first_party = first_party_package_names()
    for path in sources:
        shown = path.relative_to(BROWSER_DIR).as_posix()
        specifiers = import_specifiers(path.read_text(encoding="utf-8"))
        assert specifiers, f"{shown} imports nothing; the extractor is probably broken"
        for specifier in specifiers:
            assert specifier not in first_party, (
                f"{shown} imports {specifier!r}, a package in this repository"
            )
            assert not import_escapes_the_package(specifier, path.parent), (
                f"{shown} imports {specifier!r}, which resolves outside tests/e2e/browser — a "
                "journey must exercise the deployed stack, not a module from the application tree"
            )


PAGE_API_FIXTURE = BROWSER_DIR / "fixtures" / "pageApi.ts"

_CONFIRMED_TRUE = re.compile(r"\bconfirmed\s*:\s*true\b")


def test_no_api_probe_can_start_a_live_sweep() -> None:
    """`postAsSignedInUser` refuses a live+confirmed body before any request, and no journey source
    carries `confirmed: true`."""
    fixture = strip_comments(PAGE_API_FIXTURE.read_text(encoding="utf-8"))
    guard = re.search(r'body\.mode === "live" && body\.confirmed === true', fixture)
    assert guard, "pageApi.ts no longer recognises a live+confirmed body"
    post = fixture.index("export async function postAsSignedInUser")
    refusal = fixture.find("if (startsALiveSweep(body))", post)
    assert 0 <= refusal < fixture.index("page.evaluate", post), (
        "postAsSignedInUser must refuse a live+confirmed body before it sends anything"
    )

    offenders = [
        path.relative_to(BROWSER_DIR).as_posix()
        for path in typescript_sources(BROWSER_DIR / "journeys")
        if _CONFIRMED_TRUE.search(strip_comments(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, f"journey sources carry `confirmed: true`: {offenders}"


_TEST_START = re.compile(r"(?m)^test\(")
_LIVE_CONFIRM_LINE = re.compile(r'^.*"Start live sweep".*$', re.MULTILINE)
_ASSIGNED_NAME = re.compile(r"\bconst\s+(\w+)\s*=")
_OPT_IN_SKIP = re.compile(r"test\.skip\(\s*process\.env\[(\w+)\]\s*!==\s*\"1\"")


def live_sweep_starts(source: str) -> list[tuple[int, str | None]]:
    """Each test in `source` that clicks the "Start live sweep" confirm, as (offset, opt-in).

    The opt-in is the env variable named by a `test.skip(process.env[NAME] !== "1", …)` that comes
    before the click in the same test, resolved through a module `const NAME = "VAR"`; `None` when
    there is no such skip.
    """
    code = strip_comments(source)
    constants = dict(re.findall(r'(?m)^const\s+(\w+)\s*=\s*"([A-Z0-9_]+)";', code))
    bounds = [match.start() for match in _TEST_START.finditer(code)] + [len(code)]
    starts: list[tuple[int, str | None]] = []
    for begin, end in itertools.pairwise(bounds):
        block = code[begin:end]
        click = -1
        for line in _LIVE_CONFIRM_LINE.finditer(block):
            if line.group(0).rstrip().endswith(".click();"):
                click = line.start()
                break
            assigned = _ASSIGNED_NAME.search(line.group(0))
            if assigned:
                found = block.find(f"{assigned.group(1)}.click()", line.end())
                if found >= 0:
                    click = found
                    break
        if click < 0:
            continue
        skip = _OPT_IN_SKIP.search(block[:click])
        starts.append((begin, constants.get(skip.group(1)) if skip else None))
    return starts


def test_each_live_sweep_start_sits_under_its_own_opt_in() -> None:
    """Every journey step that confirms a live sweep is skipped unless its opt-in is set, and no
    opt-in starts more than one live sweep."""
    by_opt_in: dict[str | None, list[str]] = {}
    for path in typescript_sources(BROWSER_DIR / "journeys"):
        shown = path.relative_to(BROWSER_DIR).as_posix()
        for offset, opt_in in live_sweep_starts(path.read_text(encoding="utf-8")):
            by_opt_in.setdefault(opt_in, []).append(f"{shown}@{offset}")
    assert by_opt_in, "no journey confirms a live sweep; this check would be vacuous"
    assert None not in by_opt_in, f"live sweep starts with no opt-in skip: {by_opt_in.get(None)}"
    shared = {opt_in: sites for opt_in, sites in by_opt_in.items() if len(sites) > 1}
    assert not shared, f"one opt-in starts more than one live sweep: {shared}"


# ---------------------------------------------------------------------------
# Per-test teardown of CRM writes, and the journey 04/17 steps it moved.
# ---------------------------------------------------------------------------

TEARDOWN_FIXTURE = BROWSER_DIR / "fixtures" / "teardown.ts"
AUDIT_DETAIL_VITEST = (
    REPO_ROOT / "frontend" / "src" / "pages" / "audit" / "AuditDetailPage.test.tsx"
)
LEGACY_WORDING = "written without before and after snapshots"

#: A CRM write a journey can make: a click on a submit/create/approve control, a write route called
#: through `postAsSignedInUser`, or a change requested from the assistant panel.
_CRM_WRITE = re.compile(
    r'name: "(?:Submit proposal|Create account|Confirm delete proposal|Approve)" \}\)\.click\(\)'
    r'|postAsSignedInUser\(\s*\w+,\s*"(?:submit_proposal|approve_proposal|log_activity'
    r'|create_\w+|update_\w+)"'
    r"|Move opportunity"
)
_MODULE_FUNCTION = re.compile(r"(?m)^(?:async\s+)?function\s+(\w+)\s*\(")
_TEARDOWN_ROUTE = re.compile(r'call\(\s*"(\w+)"|listAll\(\s*"(\w+)"')

#: Tests that write CRM data, per journey file. A detector regression or a new writing journey
#: changes this inventory.
WRITING_TESTS: Final[dict[str, int]] = {
    "journeys/02-pipeline.spec.ts": 1,
    "journeys/03-approvals.spec.ts": 2,
    "journeys/04-audit-revert.spec.ts": 2,
    "journeys/06-panel-proposal.spec.ts": 2,
    "journeys/17-audit-detail/audit-detail.spec.ts": 2,
    "journeys/18-approval-rebase/approval-rebase.spec.ts": 2,
    "journeys/19-account-opportunities/account-opportunities.spec.ts": 1,
}


def writing_tests(source: str) -> list[tuple[str, bool]]:
    """Each test in `source` that writes CRM data, as (its title line, whether it takes `writes`).

    A test writes when its body holds a write, or calls a module function whose body does.
    """
    code = strip_comments(source)
    functions = list(_MODULE_FUNCTION.finditer(code))
    writers: set[str] = set()
    for match in functions:
        end = code.find("\n}\n", match.end())
        if _CRM_WRITE.search(code[match.end() : end if end >= 0 else len(code)]):
            writers.add(match.group(1))
    bounds = [match.start() for match in _TEST_START.finditer(code)] + [len(code)]
    found: list[tuple[str, bool]] = []
    for begin, end in itertools.pairwise(bounds):
        block = code[begin:end]
        calls_writer = any(re.search(rf"\b{re.escape(name)}\(", block) for name in writers)
        if _CRM_WRITE.search(block) or calls_writer:
            found.append(
                (block.splitlines()[0], re.search(r"^\s*writes,\s*$", block, re.M) is not None)
            )
    return found


def test_the_writing_test_detector_reads_direct_and_helper_writes() -> None:
    helper = 'async function propose(page) {\n  await b({ name: "Submit proposal" }).click();\n}\n'
    source = (
        helper
        + 'test("a", async ({\n  signedInPage: page,\n  writes,\n}) => {\n  await propose(page);\n});\n'
        + 'test("b", async ({\n  signedInPage: page,\n}) => {\n  await b({ name: "Create account" }).click();\n});\n'
        + 'test("c", async ({\n  signedInPage: page,\n}) => {\n  await expect(b({ name: "Submit proposal" })).toHaveCount(0);\n});\n'
    )
    assert writing_tests(source) == [('test("a", async ({', True), ('test("b", async ({', False)]


def test_every_writing_journey_test_is_torn_down_by_the_ledger() -> None:
    """Each test that writes CRM data takes the `writes` ledger from `fixtures/teardown.ts`, and
    every `journey-…` label it writes is tagged with the run-scoped marker."""
    inventory: dict[str, int] = {}
    for path in typescript_sources(BROWSER_DIR / "journeys", ".spec.ts"):
        shown = path.relative_to(BROWSER_DIR).as_posix()
        source = path.read_text(encoding="utf-8")
        tests = writing_tests(source)
        if not tests:
            continue
        inventory[shown] = len(tests)
        untorn = [title for title, takes_writes in tests if not takes_writes]
        assert not untorn, f"{shown}: writing tests without the `writes` ledger: {untorn}"
        assert re.search(
            r'import \{[^}]*\btest\b[^}]*\} from "(?:\.\./)+fixtures/teardown"', source
        ), f"{shown} writes but does not take `test` from fixtures/teardown.ts"
        code = strip_comments(source)
        assert code.count("`journey-") == code.count("writes.tag(`journey-"), (
            f"{shown} writes a `journey-…` label that is not tagged with writes.tag(…)"
        )
    assert inventory == WRITING_TESTS


def test_the_teardown_only_reads_rejects_and_reverts_and_fails_loudly() -> None:
    """The teardown's API surface is reads plus `reject_proposal`/`revert_audit_record`; it collects
    failures and asserts the list empty, and it extends the signed-in fixture."""
    code = strip_comments(TEARDOWN_FIXTURE.read_text(encoding="utf-8"))
    routes = {a or b for a, b in _TEARDOWN_ROUTE.findall(code)}
    assert routes == {
        "list_proposals",
        "list_audit_records",
        "get_audit_record",
        "reject_proposal",
        "revert_audit_record",
    }
    assert "expect(failures" in code and ".toEqual([])" in code
    assert 'from "./auth"' in code and "signedInTest.extend<" in code
    assert "await ledger.teardown(signedInPage)" in code
    assert "audit.after_snapshot.last_touch === created.occurred_at" in code, (
        "an activity's teardown must also revert its opportunity touch"
    )


def test_journey_17_drops_the_live_legacy_step_for_the_vitest_cell() -> None:
    journey = (BROWSER_DIR / "journeys/17-audit-detail/audit-detail.spec.ts").read_text(
        encoding="utf-8"
    )
    assert LEGACY_WORDING not in strip_comments(journey)
    assert "AuditDetailPage.test.tsx" in journey, "journey 17 must point at the vitest cell"
    vitest = AUDIT_DETAIL_VITEST.read_text(encoding="utf-8")
    assert "legacyAuditRecordResponse" in vitest and LEGACY_WORDING in vitest


def test_journey_04_reads_the_revert_time_from_the_detail_view() -> None:
    code = strip_comments(
        (BROWSER_DIR / "journeys/04-audit-revert.spec.ts").read_text(encoding="utf-8")
    )
    assert "Reverted at" not in code, "the list projection carries no revert time"
    assert 'row.getByText("Reverted", { exact: true })' in code
    assert 'detailValue(page, "Status")).toHaveText("Reverted")' in code
    assert 'detailValue(page, "Reverted").locator("time")' in code


def test_journey_13_asks_for_an_answer_long_enough_to_stream() -> None:
    """The streamed answer spans several ~0.5 s bursts, so the prompt asks for more than one
    sentence, and it asks for book-level aggregates rather than a per-account fan-out."""
    code = strip_comments(
        (BROWSER_DIR / "journeys/13-streaming-answer.spec.ts").read_text(encoding="utf-8")
    )
    assert "in a sentence" not in code
    assert ".fill(LONG_ANSWER_PROMPT)" in code
    assert "paragraph" in code
    assert "Do not look up or list individual accounts" in code


def test_journey_08_opens_each_artefact_through_its_presigned_url_popup() -> None:
    """Journey 08 clicks each artefact control, catches the new tab, and asserts that tab's own
    navigation answered 200 with the artefact's content type."""
    code = strip_comments(
        (BROWSER_DIR / "journeys/08-eval-dashboard.spec.ts").read_text(encoding="utf-8")
    )
    assert 'artifactSection.getByRole("link")' not in code
    assert 'page.waitForEvent("popup")' in code
    assert "toBe(200)" in code and "artefactContentType(" in code
    assert "runStubSweep(page)" in code
    compact = re.sub(r"\s+", "", code)
    assert (
        'constcontrols=artifactSection.getByRole("button")'
        '.and(artifactSection.locator(`:not([data-testid="${MATRIX_DOWNLOAD_TEST_ID}"])`));'
    ) in compact, "the popup loop must cover every artefact button except the in-place download"
    assert "controls.nth(index)" in code


def test_journey_08_excludes_only_the_matrix_download_and_asserts_it_once() -> None:
    """The one artefact control that downloads in place is named by the test id the SPA renders,
    excluded from journey 08's popup loop, and asserted present exactly once."""
    code = strip_comments(
        (BROWSER_DIR / "journeys/08-eval-dashboard.spec.ts").read_text(encoding="utf-8")
    )
    compact = re.sub(r"\s+", "", code)
    assert 'const MATRIX_DOWNLOAD_TEST_ID = "download-matrix-md";' in code
    assert compact.count("MATRIX_DOWNLOAD_TEST_ID") == 3
    assert (
        "constdownloadControl=artifactSection.getByTestId(MATRIX_DOWNLOAD_TEST_ID);"
        "awaitexpect(downloadControl,"
    ) in compact
    assert re.search(r"awaitexpect\(downloadControl,[^)]*\)\.toHaveCount\(1\)", compact)
    artifact_list = (EVAL_ARTIFACT_LIST).read_text(encoding="utf-8")
    assert 'data-testid="download-matrix-md"' in artifact_list
    assert "window.location.assign(url)" in artifact_list


def test_the_live_start_detector_reads_both_click_forms() -> None:
    """`live_sweep_starts` sees a direct and a variable click, resolves the opt-in, ignores a
    confirm that is only inspected, and reports an unguarded start as `None`."""
    source = """
const OPT = "CRM_E2E_X";
test("a", async () => {
  test.skip(process.env[OPT] !== "1", "billable");
  const confirm = dialog.getByRole("button", { name: "Start live sweep" });
  await confirm.click();
});
test("b", async () => {
  await expect(dialog.getByRole("button", { name: "Start live sweep" })).toBeEnabled();
});
test("c", async () => {
  await dialog.getByRole("button", { name: "Start live sweep" }).click();
});
"""
    assert [opt_in for _, opt_in in live_sweep_starts(source)] == ["CRM_E2E_X", None]


def test_the_import_checks_reject_the_things_they_exist_to_catch() -> None:
    """The import checks reject an escaping import and accept in-package, bare, prose and
    commented-out forms, on synthetic sources."""
    journeys_dir = BROWSER_DIR / "journeys"

    escaping = 'import { formatCurrency } from "../../../../frontend/src/lib/format";'
    assert import_specifiers(escaping) == ["../../../../frontend/src/lib/format"]
    assert import_escapes_the_package(import_specifiers(escaping)[0], journeys_dir)

    # A relative import that stays inside the package is accepted.
    assert not import_escapes_the_package("../fixtures/auth", journeys_dir)
    assert not import_escapes_the_package("@playwright/test", journeys_dir)

    # Prose containing the word `from` inside a string is not an import.
    prose = """throw new Error("cannot distinguish 'auth works' from 'auth is absent'.");"""
    assert import_specifiers(prose) == []

    # A commented-out import is not an import.
    assert import_specifiers('// import { x } from "../../../frontend/src/x";') == []


# ---------------------------------------------------------------------------
# Endpoint discovery.
# ---------------------------------------------------------------------------


class TestEndpointDiscovery:
    def test_the_config_hardcodes_no_url(self) -> None:
        """The config's code, comments stripped, contains no URL literal."""
        code = strip_comments(CONFIG.read_text(encoding="utf-8"))
        found = _URL_LITERAL.findall(code)
        assert not found, (
            f"playwright.config.ts contains a hardcoded URL literal ({found}); the base URL must "
            "come from stack outputs so it cannot point at a stale distribution after a redeploy"
        )

    def test_no_localhost_default_anywhere_in_the_scaffold(self) -> None:
        """No scaffold source uses a local address as a value."""
        for path in (CONFIG, GLOBAL_SETUP, AUTH_FIXTURE):
            code = strip_comments(path.read_text(encoding="utf-8"))
            found = _LOCAL_TARGET.findall(code)
            assert not found, (
                f"{path.name} uses a local address as a value ({found}); the journeys run against a "
                "deployed stack and a local default would mask a discovery failure"
            )

    def test_the_config_reads_terraform_outputs(self) -> None:
        source = CONFIG.read_text(encoding="utf-8")
        assert "cloudfront_domain_name" in source
        assert "output" in source and "-json" in source
        assert "tf-outputs.json" in source

    def test_resolution_is_documented_as_ordered_with_an_explicit_override_first(self) -> None:
        """The config reads the `CRM_E2E_BASE_URL` and `CRM_TF_OUTPUTS_JSON` overrides."""
        source = CONFIG.read_text(encoding="utf-8")
        assert "CRM_E2E_BASE_URL" in source
        assert "CRM_TF_OUTPUTS_JSON" in source


# ---------------------------------------------------------------------------
# globalSetup — refuses a run it cannot target, or must not target.
# ---------------------------------------------------------------------------


#: A complete, non-production outputs document, so only the freshness refusal can fire.
_COMPLETE_OUTPUTS: Final[dict[str, dict[str, str]]] = {
    "cloudfront_domain_name": {"value": "d111111abcdef8.cloudfront.net"},
    "cognito_user_pool_id": {"value": "us-east-1_ExAmPlE01"},
    "cognito_spa_client_id": {"value": "1example23456789abcdefghij"},
    "cognito_domain": {"value": "crm-agent-eval-example.auth.us-east-1.amazoncognito.com"},
}

#: Matches no journey title, so `globalSetup` runs and zero tests execute.
_MATCHES_NO_JOURNEY: Final[str] = "zzz-this-title-matches-no-journey"


def _oldest_infra_source_mtime() -> float:
    infra = REPO_ROOT / "infra"
    return min(
        path.stat().st_mtime for path in infra.rglob("*.tf") if ".terraform" not in path.parts
    )


def _run_global_setup_with_outputs(outputs_path: Path) -> subprocess.CompletedProcess[str]:
    """Drive `globalSetup` for real, selecting no journey so nothing touches the network."""
    _requires_node()
    return proc_run.run(
        [
            "./node_modules/.bin/playwright",
            "test",
            "--grep",
            _MATCHES_NO_JOURNEY,
            "--pass-with-no-tests",
        ],
        cwd=BROWSER_DIR,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CRM_TF_OUTPUTS_JSON": str(outputs_path)},
    )


class TestOutputsFreshness:
    """A discovered outputs snapshot older than `infra/` is refused and named as stale."""

    def _write(self, tmp_path: Path, *, mtime: float) -> Path:
        path = tmp_path / "tf-outputs.json"
        path.write_text(json.dumps(_COMPLETE_OUTPUTS), encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_a_backdated_outputs_file_is_refused(self, tmp_path: Path) -> None:
        """A snapshot older than `infra/` is refused before test selection."""
        stale = self._write(tmp_path, mtime=_oldest_infra_source_mtime() - 86_400)
        result = _run_global_setup_with_outputs(stale)
        combined = result.stdout + result.stderr

        assert result.returncode != 0, (
            "a stale outputs snapshot was accepted; the journeys would have driven whatever the "
            f"previous apply deployed.\n{combined}"
        )
        assert "STALE" in combined, (
            f"the refusal does not name staleness, so a reader cannot tell it from any other "
            f"discovery failure:\n{combined}"
        )

    def test_the_refusal_distinguishes_stale_from_absent(self, tmp_path: Path) -> None:
        """The stale refusal's wording differs from the absent-outputs refusal's."""
        stale = self._write(tmp_path, mtime=_oldest_infra_source_mtime() - 86_400)
        result = _run_global_setup_with_outputs(stale)
        combined = result.stdout + result.stderr

        assert "was not discovered from stack outputs" not in combined, (
            "the stale path emitted the ABSENT message; the two diagnoses must not share wording"
        )
        # Wording only the stale branch can produce.
        for expected in ("infra source file(s) are newer than it", "after a SUCCESSFUL apply"):
            assert expected in combined, f"the refusal never says {expected!r}:\n{combined}"

    def test_a_current_outputs_file_is_not_refused(self, tmp_path: Path) -> None:
        """The same document with a newer mtime is not refused."""
        current = self._write(tmp_path, mtime=time.time() + 3_600)
        result = _run_global_setup_with_outputs(current)
        combined = result.stdout + result.stderr

        assert "STALE" not in combined, (
            f"a current outputs snapshot was refused as stale; the predicate ignores the "
            f"timestamp:\n{combined}"
        )
        assert result.returncode == 0, combined

    def test_the_predicate_matches_plan_freshness(self) -> None:
        """The config's freshness walk skips `.terraform` and names `plan_freshness` (source-level)."""
        source = CONFIG.read_text(encoding="utf-8")
        assert '".terraform"' in source, (
            "the walk must skip .terraform, or a provider plugin's own timestamp would decide "
            "whether the journeys are allowed to run"
        )
        assert ".tf" in source and "plan_freshness" in source, (
            "the shared predicate should be named, so a change to one side prompts a look at the other"
        )


class TestGlobalSetupRefusals:
    def test_it_throws_when_the_base_url_is_unresolved(self) -> None:
        source = GLOBAL_SETUP.read_text(encoding="utf-8")
        assert "throw new Error" in source
        assert "resolveBaseUrl" in source
        assert "missingOutputs" in source

    def test_it_refuses_a_production_looking_target(self) -> None:
        """`globalSetup` carries each production marker it refuses on."""
        source = GLOBAL_SETUP.read_text(encoding="utf-8")
        for marker in ("prod", "production", "prd"):
            assert f'"{marker}"' in source, f"no production marker for {marker!r}"

    def test_it_is_registered_as_the_config_global_setup(self) -> None:
        """The config registers `globalSetup`."""
        assert "globalSetup" in CONFIG.read_text(encoding="utf-8")

    def test_global_setup_is_not_invoked_by_list(self) -> None:
        """`--list` succeeds offline without `globalSetup`'s refusal firing."""
        _requires_node()
        result = proc_run.run(
            ["npx", "playwright", "test", "--list", "--pass-with-no-tests"],
            cwd=BROWSER_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Refusing to run browser journeys" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Sign-in is driven, never injected.
# ---------------------------------------------------------------------------


class TestAuthFixtureDrivesRealSignIn:
    def test_it_fills_the_spa_sign_in_form_by_the_labels_the_form_renders(self) -> None:
        """Both sides: every selector auth.ts drives is one SignInForm.tsx actually renders."""
        fixture = AUTH_FIXTURE.read_text(encoding="utf-8")
        form = SIGN_IN_FORM.read_text(encoding="utf-8")
        assert ".fill(" in fixture
        for label in ("Username", "Password"):
            assert f'getByLabel("{label}", {{ exact: true }})' in fixture
            assert f">\n              {label}\n            </label>" in form, label
        assert 'name: "Sign in", exact: true' in fixture
        assert '"Sign in"' in form
        assert 'getByRole("form", { name: "Sign in to FirstCRM" })' in fixture
        assert "Sign in to FirstCRM" in form and "aria-labelledby" in form

    def test_it_never_waits_on_a_hosted_ui_redirect(self) -> None:
        """The deployed SPA signs in with in-app SRP, so a URL wait would hang until timeout."""
        assert "waitForURL" not in strip_comments(AUTH_FIXTURE.read_text(encoding="utf-8"))

    def test_it_resolves_on_the_shell_or_the_forms_error_never_on_a_timeout(self) -> None:
        """Both sides: the signed-in and refused signals are testids the app renders, and a
        challenge is recognised by the prefix the SRP session throws."""
        fixture = AUTH_FIXTURE.read_text(encoding="utf-8")
        assert 'getByTestId("app-shell")' in fixture
        assert 'data-testid="app-shell"' in APP_SHELL.read_text(encoding="utf-8")
        assert 'getByTestId("signin-error")' in fixture
        assert 'data-testid="signin-error"' in SIGN_IN_FORM.read_text(encoding="utf-8")
        assert "SignInRejectedError" in fixture and "SignInChallengeError" in fixture
        assert 'UNSUPPORTED_CHALLENGE_PREFIX = "unsupported Cognito challenge:"' in fixture
        assert "`unsupported Cognito challenge: ${challengeName}`" in SRP_SESSION.read_text(
            encoding="utf-8"
        )

    def test_it_injects_no_token_or_storage_state(self) -> None:
        """The auth fixture's code seeds no token, storage state or auth header."""
        code = strip_comments(AUTH_FIXTURE.read_text(encoding="utf-8"))
        for forbidden in (
            "addInitScript",
            "localStorage.setItem",
            "sessionStorage.setItem",
            "storageState",
            "setExtraHTTPHeaders",
            "Authorization:",
            "id_token",
            "access_token",
        ):
            assert forbidden not in code, (
                f"auth.ts uses {forbidden!r}, which suggests an injected credential. The journeys "
                "must drive the real sign-in form: a fixture that bypasses sign-in cannot distinguish "
                "'auth works' from 'auth is absent'."
            )

    def test_no_fixture_outside_the_allowance_calls_add_init_script(self) -> None:
        """Every fixture file, present or added later, is refused `addInitScript` unless it is in
        the allowance, and auth.ts is never allowed it."""
        fixtures = typescript_sources(BROWSER_DIR / "fixtures")
        names = {path.name for path in fixtures}
        assert AUTH_FIXTURE.name in names
        assert AUTH_FIXTURE.name not in INIT_SCRIPT_ALLOWED_FIXTURES
        assert names >= INIT_SCRIPT_ALLOWED_FIXTURES, "an allowance names no existing fixture"
        offenders = [
            path.name
            for path in fixtures
            if path.name not in INIT_SCRIPT_ALLOWED_FIXTURES
            and "addInitScript" in strip_comments(path.read_text(encoding="utf-8"))
        ]
        assert offenders == [], (
            f"{offenders} call addInitScript, which can seed a credential before the page runs; only "
            f"{sorted(INIT_SCRIPT_ALLOWED_FIXTURES)} may."
        )

    def test_the_csp_trap_registers_one_violation_listener(self) -> None:
        """The CSP trap's one init script is the `securitypolicyviolation` listener."""
        code = strip_comments(CSP_TRAP_FIXTURE.read_text(encoding="utf-8"))
        assert code.count("addInitScript(") == 1
        assert '"securitypolicyviolation"' in code

    @pytest.mark.parametrize("name", sorted(INIT_SCRIPT_ALLOWED_FIXTURES))
    def test_an_init_script_fixture_names_no_storage_or_credential(self, name: str) -> None:
        """A fixture allowed an init script names no storage, cookie or credential identifier
        anywhere in its code."""
        lowered = strip_comments(
            (BROWSER_DIR / "fixtures" / name).read_text(encoding="utf-8")
        ).lower()
        found = [term for term in CREDENTIAL_SURFACE_TERMS if term in lowered]
        assert found == [], (
            f"{name} names {found}; its init script runs in every document before the app, so it "
            "must not read or write storage, cookies or credentials."
        )

    def test_it_reads_credentials_from_the_environment_and_fails_loudly(self) -> None:
        source = AUTH_FIXTURE.read_text(encoding="utf-8")
        assert "CRM_DEMO_USER_PASSWORD" in source, (
            "reuse the variable scripts/provision_demo_user.py reads, so one provisioning step "
            "serves both"
        )
        assert "throw new Error" in source, "a missing credential must fail, not silently skip"

    def test_it_starts_from_the_app_root(self) -> None:
        """Sign-in starts from the app root, where the SPA renders its sign-in form."""
        source = AUTH_FIXTURE.read_text(encoding="utf-8")
        assert 'page.goto("/")' in source

    def test_it_waits_for_a_signed_in_affordance_not_merely_the_callback_url(self) -> None:
        """Sign-in completes on a signed-in affordance ("sign out"), not on the callback URL."""
        source = AUTH_FIXTURE.read_text(encoding="utf-8")
        assert "sign out" in source.lower()


# ---------------------------------------------------------------------------
# What `--list` proves.
# ---------------------------------------------------------------------------


def test_the_run_command_enumerates_specs_but_no_journey_exists_yet() -> None:
    """`--list` succeeds and its collected inventory agrees with the journey specs on disk."""
    _requires_node()
    result = proc_run.run(
        ["npx", "playwright", "test", "--list", "--pass-with-no-tests"],
        cwd=BROWSER_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    journeys = typescript_sources(BROWSER_DIR / "journeys", ".spec.ts")
    if not journeys:
        assert "Total: 0 tests" in combined, (
            "no journey spec is on disk, so the inventory must be empty; a non-empty one here would "
            "mean specs are being collected from somewhere unexpected"
        )
    else:
        assert "Total: 0 tests" not in combined, (
            f"{len(journeys)} journey spec(s) exist but Playwright collected none — check testDir"
        )
