# SPDX-License-Identifier: MIT
"""Tests for `scripts/consolidate_inventory.py`, the published consolidated inventory.

The tests cover:

1. A `defer` (for example the CloudFront TLS 1.2 gap) never rendering as a pass.
2. A `skip` always appearing on the page.
3. No AWS account id or absolute currency figure reaching the published page.
4. The prose caveats that no JSON artifact encodes never being dropped.

Each guard is also shown to fail when its control is removed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from scripts import consolidate_inventory as ci
from scripts.consolidate_inventory import (
    _BLANKET_VERIFICATION_CLAIM,
    CARRIED_CAVEATS,
    DEFER,
    FAIL,
    PASS,
    SKIP,
    CheckResult,
    Provenance,
    ResourceClassResult,
    _assert_no_disposition_laundered,
    _assert_no_unqualified_verification_claim,
    evidence_freshness,
    load_inventories,
    main,
    render,
)

from crm.config import DEFAULT_REGION


def _result(
    *,
    layer: int = 4,
    resource_class: str = "cloudfront",
    checks: tuple[CheckResult, ...] = (),
    provenance: Provenance | None = None,
) -> ResourceClassResult:
    return ResourceClassResult(
        layer=layer, resource_class=resource_class, checks=checks, provenance=provenance
    )


_TLS_DEFER = CheckResult(
    resource="distribution",
    check="configured_tls_minimum",
    status=DEFER,
    detail="NOT VERIFIED and NOT ENFORCED: TLSv1 is inert on the default certificate",
    defer_reason="missing_custom_acm_certificate",
)


# ---------------------------------------------------------------------------
# 1. A defer is never rendered as a pass, and always carries its reason.
# ---------------------------------------------------------------------------


def test_a_deferred_control_renders_as_defer_never_as_a_pass() -> None:
    document = render([_result(checks=(_TLS_DEFER,))])
    assert "**DEFER**" in document
    assert "missing_custom_acm_certificate" in document
    # The headline tallies defer separately, so no aggregate can read as full verification.
    assert "0 pass" in document and "1 DEFER" in document


def test_the_publication_guard_rejects_a_document_that_dropped_a_defer() -> None:
    """A rendered document that lost its DEFER marker is rejected by the publication guard."""
    results = [_result(checks=(_TLS_DEFER,))]
    laundered = render(results).replace("**DEFER**", "pass")
    with pytest.raises(RuntimeError, match="DEFER markers"):
        _assert_no_disposition_laundered(laundered, results)


def test_the_publication_guard_rejects_a_defer_without_its_reason() -> None:
    results = [_result(checks=(_TLS_DEFER,))]
    stripped = render(results).replace("missing_custom_acm_certificate", "n/a")
    with pytest.raises(RuntimeError, match="defer_reason"):
        _assert_no_disposition_laundered(stripped, results)


def test_the_publication_guard_rejects_a_document_that_dropped_a_failure() -> None:
    failing = CheckResult(resource="r", check="c", status=FAIL, detail="broke")
    results = [_result(checks=(failing,))]
    laundered = render(results).replace("**FAIL**", "pass")
    with pytest.raises(RuntimeError, match="FAIL markers"):
        _assert_no_disposition_laundered(laundered, results)


# ---------------------------------------------------------------------------
# 2. Skips survive to the page.
# ---------------------------------------------------------------------------


def test_every_skip_is_listed_with_its_detail() -> None:
    skipped = CheckResult(
        resource="role/pre-token-generation",
        check="leading_keys",
        status=SKIP,
        detail="not_applicable: no LeadingKeys condition on this grant",
    )
    document = render([_result(resource_class="iam_roles", checks=(skipped,))])
    assert "Skipped checks" in document
    assert "SKIP" in document
    assert "leading_keys" in document
    assert "no LeadingKeys condition on this grant" in document


# ---------------------------------------------------------------------------
# 3. No account id, no currency figure.
# ---------------------------------------------------------------------------

#: A 12-digit, account-id-shaped value other than the placeholder, assembled at runtime because
#: `check_no_account_id` forbids any 12-digit literal in tracked content.
_FOREIGN_ACCOUNT_ID: Final[str] = "2109" + "8765" + "4321"


def test_an_account_id_in_a_detail_string_is_redacted_to_the_placeholder() -> None:
    arn = f"arn:aws:lambda:us-east-1:{_FOREIGN_ACCOUNT_ID}:function:x"
    check = CheckResult(
        resource=arn,
        check="invoke",
        status=PASS,
        detail=f"invoked {arn}",
    )
    document = render([_result(checks=(check,))])
    assert _FOREIGN_ACCOUNT_ID not in document
    assert "123456789012" in document
    # And the independent guard agrees the finished page is clean.
    _assert_no_disposition_laundered(document, [_result(checks=(check,))])


def test_the_publication_guard_rejects_an_unredacted_account_id() -> None:
    """The publication guard rejects an account id independently of the renderer's redaction."""
    results = [_result(checks=(CheckResult(resource="r", check="c", status=PASS),))]
    with pytest.raises(RuntimeError, match="account-id-shaped"):
        _assert_no_disposition_laundered(f"account {_FOREIGN_ACCOUNT_ID}\n", results)


@pytest.mark.parametrize("figure", ["$0.95", "0.95 USD", "£1", "€1"])
def test_the_publication_guard_rejects_any_absolute_currency_figure(figure: str) -> None:
    results = [_result(checks=(CheckResult(resource="r", check="c", status=PASS),))]
    with pytest.raises(RuntimeError, match="currency"):
        _assert_no_disposition_laundered(f"cost was {figure}\n", results)


def test_the_real_consolidated_render_publishes_no_currency_figure() -> None:
    """The rendered document, caveat prose included, carries no currency figure."""
    document = render([_result(checks=(_TLS_DEFER,))])
    _assert_no_disposition_laundered(document, [_result(checks=(_TLS_DEFER,))])


# ---------------------------------------------------------------------------
# 4. The prose caveats no artifact encodes cannot be silently dropped.
# ---------------------------------------------------------------------------

#: A distinctive phrase from every caveat the consolidated document is required to carry.
_REQUIRED_CAVEAT_PHRASES = (
    "accepted residual risk",  # the pre-token-generation leading_keys skip
    "authorization boundaries this invocation does not observe",  # the lambda_actions skips
    "proves only the actions it enumerates",  # iam_roles' scoped greens
    "green while the turn path is broken",  # the agents probe is not a turn
    "`git_tree_dirty: true`",  # Layer 5 inventories came from a dirty tree
    "`after_payload` restricted to an allowlist",  # the proposal-chain dependency
    "the deployed Runtime is not",  # the eval sweep does not exercise the live Runtime
    "The model-driven half is deferred",  # the submit_proposal product gap
    "Declared and reasoned",  # the surplus IAM grants
    "Bounded and disclosed",  # packaging non-determinism
    "`_agent_identity` channel",  # offline/deployed dispatch divergence
)


@pytest.mark.parametrize("phrase", _REQUIRED_CAVEAT_PHRASES)
def test_every_required_caveat_is_published(phrase: str) -> None:
    document = render([_result(checks=(_TLS_DEFER,))])
    assert phrase in document, f"the consolidated document dropped the caveat carrying {phrase!r}"


def test_the_open_product_gaps_are_named_as_open() -> None:
    document = render([_result(checks=(_TLS_DEFER,))])
    assert "no model has chosen `submit_proposal` in natural language" in document
    assert "no browser has rendered the proposal card" in document
    assert "TLS 1.2 is NOT enforced" in document
    assert "There is no WAF anywhere" in document


def test_caveat_table_has_one_row_per_declared_caveat() -> None:
    document = render([_result(checks=(_TLS_DEFER,))])
    section = document.split("## Caveats carried forward", 1)[1].split("## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ") and "---" not in line]
    # One header row plus one row per caveat.
    assert len(rows) == len(CARRIED_CAVEATS) + 1


# ---------------------------------------------------------------------------
# 5. Loading: every artifact is consolidated, and a missing directory is an error not a blank page.
# ---------------------------------------------------------------------------


def test_load_inventories_reads_every_artifact_layer_then_class_ordered(tmp_path: Path) -> None:
    directory = tmp_path / "verification-inventory"
    directory.mkdir()
    (directory / "z_layer4.json").write_text(
        json.dumps(
            {
                "layer": 4,
                "resource_class": "zzz",
                "checks": [{"resource": "r", "check": "c", "status": "pass", "detail": ""}],
            }
        ),
        encoding="utf-8",
    )
    (directory / "a_layer6.json").write_text(
        json.dumps(
            {
                "layer": 6,
                "resource_class": "aaa",
                "checks": [{"resource": "r", "check": "c", "status": "pass", "detail": ""}],
            }
        ),
        encoding="utf-8",
    )
    results = load_inventories(directory)
    assert [(r.layer, r.resource_class) for r in results] == [(4, "zzz"), (6, "aaa")]


def test_an_empty_inventory_directory_is_an_error_not_an_empty_document(tmp_path: Path) -> None:
    directory = tmp_path / "empty"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="no inventory artifacts"):
        load_inventories(directory)


def test_a_missing_inventory_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="inventory directory not found"):
        load_inventories(tmp_path / "nope")


def test_an_unparseable_artifact_fails_rather_than_being_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        load_inventories(directory)


def test_a_defer_in_an_artifact_round_trips_through_load_and_render(tmp_path: Path) -> None:
    directory = tmp_path / "inv"
    directory.mkdir()
    (directory / "cloudfront.json").write_text(
        json.dumps(
            {
                "layer": 4,
                "resource_class": "cloudfront",
                "provenance": {
                    "generated_at": "2026-09-04T01:38:39Z",
                    "git_commit": "abc12345",
                    "git_tree_dirty": True,
                    "region": "us-east-1",
                    "resource_class": "cloudfront",
                },
                "checks": [
                    {
                        "resource": "distribution",
                        "check": "configured_tls_minimum",
                        "status": "defer",
                        "detail": "TLSv1 is inert",
                        "defer_reason": "missing_custom_acm_certificate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "--no-residue",
            "--inventory-dir",
            str(directory),
            "--out",
            str(tmp_path / "out.md"),
            "--json-out",
            str(tmp_path / "out.json"),
        ]
    )
    assert exit_code == 0
    document = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert "**DEFER**" in document
    assert "missing_custom_acm_certificate" in document
    assert "dirty" in document


def test_a_failing_check_makes_the_run_exit_nonzero(tmp_path: Path) -> None:
    directory = tmp_path / "inv"
    directory.mkdir()
    (directory / "residue.json").write_text(
        json.dumps(
            {
                "layer": 0,
                "resource_class": "residue",
                "checks": [
                    {
                        "resource": "dynamodb",
                        "check": "no_residue_tenant_rows",
                        "status": "fail",
                        "detail": "residue remains",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    exit_code = main(
        [
            "--no-residue",
            "--inventory-dir",
            str(directory),
            "--out",
            str(tmp_path / "out.md"),
            "--json-out",
            str(tmp_path / "out.json"),
        ]
    )
    assert exit_code == 1


def test_a_skip_alone_does_not_fail_the_run(tmp_path: Path) -> None:
    directory = tmp_path / "inv"
    directory.mkdir()
    (directory / "iam.json").write_text(
        json.dumps(
            {
                "layer": 4,
                "resource_class": "iam_roles",
                "checks": [
                    {
                        "resource": "role",
                        "check": "leading_keys",
                        "status": "skip",
                        "detail": "not applicable",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--no-residue",
                "--inventory-dir",
                str(directory),
                "--out",
                str(tmp_path / "out.md"),
                "--json-out",
                str(tmp_path / "out.json"),
            ]
        )
        == 0
    )


# ---------------------------------------------------------------------------
# 8. The document's own claim about its currency.
# ---------------------------------------------------------------------------


def _prov(generated_at: str) -> Provenance:
    return Provenance(
        generated_at=generated_at,
        git_commit="abc12345",
        git_tree_dirty=False,
        region="us-east-1",
        resource_class="cloudfront",
    )


_PASS = CheckResult(resource="r", check="c", status=PASS)


def test_the_blanket_claim_is_made_only_when_every_class_is_current() -> None:
    """One class, dated, with itself as the reference: nothing is stale, so the claim is allowed."""
    results = [_result(provenance=_prov("2026-09-06T00:00:00Z"), checks=(_PASS,))]
    document = render(results)
    assert _BLANKET_VERIFICATION_CLAIM in document
    assert "Evidence age" not in document
    _assert_no_unqualified_verification_claim(document, evidence_freshness(results))


def test_a_class_older_than_the_reference_removes_the_blanket_claim() -> None:
    results = [
        _result(
            resource_class="lambda_actions",
            provenance=_prov("2026-09-04T12:48:24Z"),
            checks=(_PASS,),
        ),
        _result(
            resource_class="residue", provenance=_prov("2026-09-06T00:14:36Z"), checks=(_PASS,)
        ),
    ]
    document = render(results)
    assert _BLANKET_VERIFICATION_CLAIM not in document
    assert (
        "does NOT claim that every class was verified against the deployment as it stands now"
        in (document)
    )
    assert "## Evidence age" in document
    assert "`lambda_actions`" in document
    _assert_no_unqualified_verification_claim(document, evidence_freshness(results))


def test_a_supplied_deployment_time_makes_even_the_newest_evidence_stale() -> None:
    """A supplied deployment time marks evidence older than it stale, even the freshest class."""
    results = [
        _result(resource_class="residue", provenance=_prov("2026-09-06T00:14:36Z"), checks=(_PASS,))
    ]
    assert evidence_freshness(results).fully_current is True
    freshness = evidence_freshness(results, deployment_changed_at="2026-09-06T06:00:00Z")
    assert freshness.fully_current is False
    assert [entry.resource_class for entry in freshness.stale] == ["residue"]
    document = render(results, deployment_changed_at="2026-09-06T06:00:00Z")
    assert "5h 45m" in document
    assert _BLANKET_VERIFICATION_CLAIM not in document


def test_a_class_with_no_provenance_is_never_counted_as_current() -> None:
    results = [_result(resource_class="api_routes", provenance=None, checks=(_PASS,))]
    freshness = evidence_freshness(results)
    assert freshness.unknown == ("api_routes",)
    assert freshness.fully_current is False
    document = render(results)
    assert _BLANKET_VERIFICATION_CLAIM not in document
    assert "no usable provenance timestamp" in document


def test_an_unparseable_timestamp_is_reported_unknown_not_fresh() -> None:
    results = [_result(provenance=_prov("not-a-timestamp"), checks=(_PASS,))]
    freshness = evidence_freshness(results)
    assert freshness.unknown == ("cloudfront",)
    assert freshness.reference is None


def test_the_guard_rejects_a_blanket_claim_reintroduced_after_rendering() -> None:
    """The guard rejects a blanket verification claim appended to a stale rendered document."""
    results = [
        _result(resource_class="old", provenance=_prov("2026-09-01T00:00:00Z"), checks=(_PASS,)),
        _result(resource_class="new", provenance=_prov("2026-09-06T00:00:00Z"), checks=(_PASS,)),
    ]
    tampered = render(results) + f"\n{_BLANKET_VERIFICATION_CLAIM}.\n"
    with pytest.raises(RuntimeError, match="unqualified claim"):
        _assert_no_unqualified_verification_claim(tampered, evidence_freshness(results))


def test_the_guard_rejects_a_stale_document_whose_age_section_was_stripped() -> None:
    results = [
        _result(resource_class="old", provenance=_prov("2026-09-01T00:00:00Z"), checks=(_PASS,)),
        _result(resource_class="new", provenance=_prov("2026-09-06T00:00:00Z"), checks=(_PASS,)),
    ]
    stripped = render(results).replace("## Evidence age", "## Something else")
    with pytest.raises(RuntimeError, match="omits the per-class evidence-age section"):
        _assert_no_unqualified_verification_claim(stripped, evidence_freshness(results))


def test_the_four_existing_publication_guards_still_hold_on_a_stale_document() -> None:
    """The disposition-laundering guards still hold on a stale document."""
    results = [
        _result(
            resource_class="old", provenance=_prov("2026-09-01T00:00:00Z"), checks=(_TLS_DEFER,)
        ),
        _result(resource_class="new", provenance=_prov("2026-09-06T00:00:00Z"), checks=(_PASS,)),
    ]
    document = render(results)
    _assert_no_disposition_laundered(document, results)
    assert "**DEFER**" in document
    assert "missing_custom_acm_certificate" in document
    with pytest.raises(RuntimeError, match="DEFER markers"):
        _assert_no_disposition_laundered(document.replace("**DEFER**", "pass"), results)


# -- the catalogue check counts run rows only ----------------------------------------------


def _ddb_row(run_id: str, status: str) -> dict[str, Any]:
    return {"run_id": {"S": run_id}, "status": {"S": status}}


def test_job_rows_are_not_counted_as_non_terminal_runs() -> None:
    from eval.runs import JOB_ROW_STATUS, job_row_id

    catalogue = [
        _ddb_row("r1", "torn_down"),
        _ddb_row(job_row_id("r1"), JOB_ROW_STATUS),
        _ddb_row(job_row_id("r2"), JOB_ROW_STATUS),
    ]
    result = ci.catalogue_terminal_check("dynamodb:eval_runs", catalogue)
    assert result.status == "pass", result.detail
    assert "2 job rows not counted" in result.detail


def test_a_run_row_left_running_still_fails_beside_job_rows() -> None:
    from eval.runs import JOB_ROW_STATUS, job_row_id

    catalogue = [_ddb_row("r1", "running"), _ddb_row(job_row_id("r1"), JOB_ROW_STATUS)]
    result = ci.catalogue_terminal_check("dynamodb:eval_runs", catalogue)
    assert result.status == "fail"
    assert "{'running': 1}" in result.detail and "'job'" not in result.detail


class _ResidueReachedError(Exception):
    """Raised by the residue stand-in once it has recorded the region it was handed."""


@pytest.mark.parametrize(
    ("argv_region", "env_region", "expected"),
    [
        (["--region", "us-west-2"], "us-east-2", "us-west-2"),
        ([], "us-west-2", "us-west-2"),
        ([], None, DEFAULT_REGION),
    ],
    ids=["flag", "environment", "default"],
)
def test_the_residue_query_region_is_flag_then_aws_region_then_the_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv_region: list[str],
    env_region: str | None,
    expected: str,
) -> None:
    """`main` hands the residue query the flag, else `AWS_REGION`, else `DEFAULT_REGION`."""
    if env_region is None:
        monkeypatch.delenv("AWS_REGION", raising=False)
    else:
        monkeypatch.setenv("AWS_REGION", env_region)
    directory = tmp_path / "inv"
    directory.mkdir()
    (directory / "cloudfront.json").write_text(
        json.dumps({"layer": 4, "resource_class": "cloudfront", "checks": []}), encoding="utf-8"
    )
    seen: list[str] = []

    def _capture(**kwargs: Any) -> ResourceClassResult:
        seen.append(kwargs["region"])
        raise _ResidueReachedError

    monkeypatch.setattr(ci, "residue_checks", _capture)
    with pytest.raises(_ResidueReachedError):
        main(["--inventory-dir", str(directory), *argv_region])
    assert seen == [expected]
