# SPDX-License-Identifier: MIT
"""Tests for `scripts/check_sse_schema_parity.py`.

Covers the structural comparison against synthetic consumers and the regenerate-and-byte-diff pass
against the relay's generated consumer.
"""

from __future__ import annotations

import contextlib
import importlib.util
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from scripts import proc_run

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "check_sse_schema_parity.py"

sys.path.insert(0, str(REPO_ROOT / "src"))
from typing import get_args  # noqa: E402

from agents.event_schema import SCHEMA_VERSION, build_event_schema  # noqa: E402
from agents.events import EVENT_KINDS, ErrorKind  # noqa: E402

#: xdist group shared with `test_package_relay.py` (which drives `scripts/package_relay.sh`), so
#: both modules' use of the `src/relay` npm project runs on one worker under `--dist loadgroup`.
pytestmark = pytest.mark.xdist_group("relay_npm")


def _load_module(name: str, path: Path) -> ModuleType:
    """Import a script from `scripts/` by file path and return the module."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module("check_sse_schema_parity", GATE)
run_gates = _load_module("run_gates_for_test", REPO_ROOT / "scripts" / "run_gates.py")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return proc_run.run(
        [sys.executable, str(GATE), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _consumer_source(
    *,
    event_kinds: list[str] | None = None,
    error_kinds: list[str] | None = None,
    fields: list[str] | None = None,
    version: str | None = None,
) -> str:
    """Render a synthetic generated consumer, defaulting every part to what the schema declares."""
    schema = build_event_schema()
    kinds = event_kinds if event_kinds is not None else sorted(gate.schema_enum(schema, "kind"))
    errors = (
        error_kinds if error_kinds is not None else sorted(gate.schema_enum(schema, "error_kind"))
    )
    names = fields if fields is not None else sorted(gate.schema_properties(schema))
    declared = "\n".join(f"  {name}: unknown;" for name in names)
    # The gate compares the declared `SCHEMA_VERSION` value, not a version mentioned in a comment.
    return (
        f"// Generated from agents.event_schema.\n"
        f'export const SCHEMA_VERSION = "{version or SCHEMA_VERSION}";\n'
        f"export type EventKind = {' | '.join(f'"{k}"' for k in kinds)};\n"
        f"export type ErrorKind = {' | '.join(f'"{k}"' for k in errors)};\n"
        f"export interface AgentEventFrame {{\n{declared}\n}}\n"
    )


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "events.ts"
    path.write_text(source, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The skip disposition.
# ---------------------------------------------------------------------------


def test_skips_with_exit_three_when_no_consumer_exists(tmp_path: Path) -> None:
    """An absent consumer exits 3 with SKIP, never 0."""
    result = _run("--consumer", str(tmp_path / "absent.ts"))
    assert result.returncode == gate.SKIP_EXIT_STATUS
    assert result.returncode == 3
    assert "SKIP" in result.stdout
    assert "nothing was verified" in result.stdout.lower()


def test_the_default_invocation_currently_skips() -> None:
    """The default invocation either passes with OK or skips naming the missing consumer."""
    result = _run()
    assert result.returncode in {0, gate.SKIP_EXIT_STATUS}
    if result.returncode == gate.SKIP_EXIT_STATUS:
        assert "no generated consumer exists yet" in result.stdout
    else:
        assert "OK" in result.stdout


def test_skip_status_matches_run_gates_contract() -> None:
    assert gate.SKIP_EXIT_STATUS == run_gates.GATE_SKIP_EXIT_STATUS


def test_the_skip_message_names_the_generator_output_so_it_is_not_mistaken_for_a_pass() -> None:
    result = _run()
    if result.returncode == gate.SKIP_EXIT_STATUS:
        assert SCHEMA_VERSION in result.stdout


# ---------------------------------------------------------------------------
# A faithful consumer passes.
# ---------------------------------------------------------------------------


def test_a_faithful_generated_consumer_passes(tmp_path: Path) -> None:
    path = _write(tmp_path, _consumer_source())
    result = _run("--consumer", str(path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_a_consumer_may_name_its_types_anything(tmp_path: Path) -> None:
    """Unions are matched by overlap with the schema, not by type name."""
    source = (
        _consumer_source().replace("EventKind", "SseFrameKind").replace("ErrorKind", "SseFailure")
    )
    result = _run("--consumer", str(_write(tmp_path, source)))
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Hand-edit detection.
# ---------------------------------------------------------------------------


class TestHandEditedConsumerFails:
    def test_a_dropped_event_kind_fails(self, tmp_path: Path) -> None:
        """A consumer missing one event kind fails and names it."""
        kinds = sorted(gate.schema_enum(build_event_schema(), "kind"))
        path = _write(tmp_path, _consumer_source(event_kinds=kinds[:-1]))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert "missing" in result.stderr
        assert kinds[-1] in result.stderr

    def test_an_invented_event_kind_fails(self, tmp_path: Path) -> None:
        """An invented `delegation` event kind fails."""
        kinds = sorted(gate.schema_enum(build_event_schema(), "kind")) + ["delegation"]
        path = _write(tmp_path, _consumer_source(event_kinds=kinds))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert "delegation" in result.stderr

    def test_the_other_phantom_kind_completion_fails(self, tmp_path: Path) -> None:
        """An invented `completion` event kind fails."""
        kinds = sorted(gate.schema_enum(build_event_schema(), "kind")) + ["completion"]
        path = _write(tmp_path, _consumer_source(event_kinds=kinds))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert "completion" in result.stderr

    def test_budget_exhausted_promoted_to_an_event_kind_fails(self, tmp_path: Path) -> None:
        """`budget_exhausted`, an error kind, fails when listed as an event kind."""
        kinds = sorted(gate.schema_enum(build_event_schema(), "kind")) + ["budget_exhausted"]
        path = _write(tmp_path, _consumer_source(event_kinds=kinds))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert "budget_exhausted" in result.stderr

    def test_a_dropped_error_kind_fails(self, tmp_path: Path) -> None:
        errors = sorted(gate.schema_enum(build_event_schema(), "error_kind"))
        path = _write(tmp_path, _consumer_source(error_kinds=errors[:-1]))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert errors[-1] in result.stderr

    def test_a_missing_event_kind_union_entirely_fails(self, tmp_path: Path) -> None:
        source = _consumer_source()
        stripped = "\n".join(line for line in source.splitlines() if "EventKind" not in line)
        path = _write(tmp_path, stripped + "\n")
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert "no string-literal union" in result.stderr

    def test_a_renamed_wire_field_fails(self, tmp_path: Path) -> None:
        fields = sorted(gate.schema_properties(build_event_schema()))
        mutated = [*fields[:-1], fields[-1] + "_renamed"]
        path = _write(tmp_path, _consumer_source(fields=mutated))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert fields[-1] in result.stderr

    def test_a_removed_wire_field_fails(self, tmp_path: Path) -> None:
        fields = sorted(gate.schema_properties(build_event_schema()))
        path = _write(tmp_path, _consumer_source(fields=fields[:-1]))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert "wire field" in result.stderr

    def test_a_stale_schema_version_fails(self, tmp_path: Path) -> None:
        """A stale version declaration with no other mention of the current version fails."""
        path = _write(tmp_path, _consumer_source(version="0.9.0"))
        result = _run("--consumer", str(path))
        assert result.returncode == 1
        assert "0.9.0" in result.stderr
        assert SCHEMA_VERSION in result.stderr

    def test_a_stale_version_fails_even_when_the_current_one_occurs_elsewhere(
        self, tmp_path: Path
    ) -> None:
        """A stale version declaration fails even when the current version appears in a comment."""
        source = _consumer_source(version="0.9.0")
        path = _write(tmp_path, f"// generated against contract {SCHEMA_VERSION}\n{source}")

        result = _run("--consumer", str(path))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "0.9.0" in result.stderr

    def test_a_consumer_declaring_no_version_constant_fails_distinctly(
        self, tmp_path: Path
    ) -> None:
        """A consumer with the version only in a comment fails as declaring no version."""
        source = _consumer_source().replace(
            f'export const SCHEMA_VERSION = "{SCHEMA_VERSION}";',
            f"// Schema version {SCHEMA_VERSION}.",
        )
        path = _write(tmp_path, source)

        result = _run("--consumer", str(path))

        assert result.returncode == 1
        assert "declares no `SCHEMA_VERSION` constant" in result.stderr

    def test_a_version_the_schema_version_is_a_prefix_of_fails(self, tmp_path: Path) -> None:
        """A declared version that extends the schema version fails, so the match is equality."""
        path = _write(tmp_path, _consumer_source(version=f"{SCHEMA_VERSION}.1"))

        result = _run("--consumer", str(path))

        assert result.returncode == 1
        assert f"{SCHEMA_VERSION}.1" in result.stderr

    def test_the_version_check_does_not_bind_a_longer_constant_name(self) -> None:
        """`PAGE_CONTEXT_SCHEMA_VERSION = "1"` is not read as the frame version."""
        text = 'export const PAGE_CONTEXT_SCHEMA_VERSION = "1";\n'

        assert gate._TS_VERSION_DECLARATION.search(text) is None

        both = f'{text}export const SCHEMA_VERSION = "{SCHEMA_VERSION}";\n'
        match = gate._TS_VERSION_DECLARATION.search(both)
        assert match is not None
        assert match.group(1) == SCHEMA_VERSION

    def test_the_structural_check_alone_does_not_see_a_comment_only_edit(
        self, tmp_path: Path
    ) -> None:
        """The structural check alone passes a consumer with a comment-only edit."""
        path = _write(tmp_path, _consumer_source() + "\n// hand-appended note\n")
        result = _run("--consumer", str(path))
        assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Expectations derived from the real schema.
# ---------------------------------------------------------------------------


def test_the_expected_enums_come_from_events_py_not_a_hardcoded_list() -> None:
    """The gate's expected enums equal the kinds and error kinds declared in `agents.events`."""
    schema = build_event_schema()
    kinds = gate.schema_enum(schema, "kind")
    errors = gate.schema_enum(schema, "error_kind")
    assert kinds == set(EVENT_KINDS)
    assert errors == set(get_args(ErrorKind))


def test_null_is_not_compared_as_a_union_member() -> None:
    """The schema's `None` enum member is excluded from the compared error kinds."""
    errors = gate.schema_enum(build_event_schema(), "error_kind")
    assert all(isinstance(member, str) for member in errors)
    raw = build_event_schema()["properties"]["error_kind"]["enum"]
    assert None in raw, "the schema is expected to spell nullability as a None enum member"
    assert len(errors) == len(raw) - 1


# ---------------------------------------------------------------------------
# Declaration forms: string-literal unions and `as const` arrays.
# ---------------------------------------------------------------------------


class TestConstArrayDeclarationForm:
    def test_the_as_const_array_form_is_recognised(self, tmp_path: Path) -> None:
        """A consumer declaring its kinds as `as const` arrays passes."""
        schema = build_event_schema()
        kinds = sorted(gate.schema_enum(schema, "kind"))
        errors = sorted(gate.schema_enum(schema, "error_kind"))
        fields = "\n".join(
            f"  readonly {n}: unknown;" for n in sorted(gate.schema_properties(schema))
        )
        source = (
            f'export const SCHEMA_VERSION = "{SCHEMA_VERSION}";\n'
            f"export const EVENT_KINDS = [{', '.join(f'"{k}"' for k in kinds)}] as const;\n"
            "export type EventKind = (typeof EVENT_KINDS)[number];\n"
            f"export const ERROR_KINDS = [{', '.join(f'"{e}"' for e in errors)}] as const;\n"
            "export type ErrorKind = (typeof ERROR_KINDS)[number];\n"
            f"export interface AgentEventFrame {{\n{fields}\n}}\n"
        )
        result = _run("--consumer", str(_write(tmp_path, source)))
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_dropped_kind_in_the_as_const_form_still_fails(self, tmp_path: Path) -> None:
        """A dropped kind in the `as const` form still fails."""
        schema = build_event_schema()
        kinds = sorted(gate.schema_enum(schema, "kind"))[:-1]
        errors = sorted(gate.schema_enum(schema, "error_kind"))
        fields = "\n".join(
            f"  readonly {n}: unknown;" for n in sorted(gate.schema_properties(schema))
        )
        source = (
            f'export const SCHEMA_VERSION = "{SCHEMA_VERSION}";\n'
            f"export const EVENT_KINDS = [{', '.join(f'"{k}"' for k in kinds)}] as const;\n"
            f"export const ERROR_KINDS = [{', '.join(f'"{e}"' for e in errors)}] as const;\n"
            f"export interface AgentEventFrame {{\n{fields}\n}}\n"
        )
        result = _run("--consumer", str(_write(tmp_path, source)))
        assert result.returncode == 1
        assert "missing" in result.stderr


def test_the_real_relay_consumer_matches_the_schema() -> None:
    """The committed relay consumer exists and is in parity with the schema."""
    consumer = REPO_ROOT / "src" / "relay" / "src" / "generated" / "events.ts"
    assert consumer.is_file(), f"missing committed relay consumer {consumer}"
    problems = gate.compare_consumer(
        "events.ts", consumer.read_text(encoding="utf-8"), build_event_schema(), SCHEMA_VERSION
    )
    assert problems == [], problems


# ---------------------------------------------------------------------------
# The string-literal union pattern.
# ---------------------------------------------------------------------------

FRONTEND_CONSUMER = REPO_ROOT / "frontend" / "src" / "types" / "generated" / "agentEvents.ts"


class TestStringLiteralUnionPattern:
    def test_the_relay_consumer_yields_the_schema_event_kinds(self) -> None:
        """`find_matching_union` reads the schema's event-kind set from the committed relay file."""
        expected = gate.schema_enum(build_event_schema(), "kind")
        text = (REPO_ROOT / "src" / "relay" / "src" / "generated" / "events.ts").read_text(
            encoding="utf-8"
        )
        assert gate.find_matching_union(text, expected) == ("EVENT_KINDS", expected)

    def test_the_frontend_consumer_yields_the_schema_event_kinds_when_generated(self) -> None:
        """The frontend generated consumer, when present, yields the same event-kind set."""
        if not FRONTEND_CONSUMER.is_file():
            pytest.skip(f"{FRONTEND_CONSUMER.relative_to(REPO_ROOT)} is not generated")
        expected = gate.schema_enum(build_event_schema(), "kind")
        text = FRONTEND_CONSUMER.read_text(encoding="utf-8")
        assert gate.find_matching_union(text, expected) == ("EVENT_KINDS", expected)

    @pytest.mark.parametrize(
        ("source", "name", "members"),
        [
            ('type K = "a" | "b";', "K", {"a", "b"}),
            ("export type K =\n  | 'a'\n  | 'b'\n  | 'c';\n", "K", {"a", "b", "c"}),
            ("type K = | \"a\" |'b';", "K", {"a", "b"}),
            ("type K = 'a' | \"b\"  ;", "K", {"a", "b"}),
            ('enum K = "only";', "K", {"only"}),
            ("type K =\t'a'\n|\n\t'b';", "K", {"a", "b"}),
            ('type K = "a" "b";', "K", {"a", "b"}),
        ],
    )
    def test_union_forms_are_recognised(self, source: str, name: str, members: set[str]) -> None:
        """Single- and multi-line unions, with or without a leading `|` and mixed quotes, match."""
        match = gate._TS_UNION.search(source)
        assert match is not None
        assert match.group("name") == name
        assert set(gate._TS_STRING_LITERAL.findall(match.group("body"))) == members

    @pytest.mark.parametrize(
        "source",
        [
            'type K = "a" | "b"',
            'type K = "a" | B;',
            "type K = (typeof KINDS)[number];",
            'type K = "a" | | "b";',
            'type K = "";',
            'const K = ["a", "b"] as const;',
            'interface K = "a";',
        ],
    )
    def test_non_unions_are_not_matched(self, source: str) -> None:
        """Unterminated, non-literal, doubled-bar, empty-literal and non-type sources do not match."""
        assert gate._TS_UNION.search(source) is None

    def test_as_const_arrays_still_resolve_through_their_own_pattern(self) -> None:
        """An `as const` array declaration is read by the array pattern, unaffected by the union."""
        text = (
            'export const KINDS = ["a", "b"] as const;\nexport type K = (typeof KINDS)[number];\n'
        )
        assert gate._TS_UNION.search(text) is None
        assert gate.find_matching_union(text, {"a", "b"}) == ("KINDS", {"a", "b"})

    def test_a_pathological_unterminated_union_completes_quickly(self) -> None:
        """A long unterminated union is rejected in well under two seconds."""
        source = "type 0=" + ' "!"' * 5000
        started = time.perf_counter()
        assert gate._TS_UNION.search(source) is None
        assert time.perf_counter() - started < 2.0


# ---------------------------------------------------------------------------
# Regenerate-and-byte-diff, using the consumer's own generator (`npm run gen:types`).
# ---------------------------------------------------------------------------

RELAY_CONSUMER = REPO_ROOT / "src" / "relay" / "src" / "generated" / "events.ts"
RELAY_DIR = REPO_ROOT / "src" / "relay"

_needs_relay = pytest.mark.skipif(
    not (RELAY_DIR / "node_modules").is_dir() or not RELAY_CONSUMER.is_file(),
    reason="relay node_modules or generated consumer absent; the byte-diff cannot run here",
)


@contextlib.contextmanager
def _locked_snapshot(path: Path) -> Iterator[bytes]:
    """Read `path` under the gate's regeneration lock, and restore it under that lock on exit."""
    with gate._regeneration_lock():
        original = path.read_bytes()
    try:
        yield original
    finally:
        with gate._regeneration_lock():
            gate.write_bytes_atomic(path, original)


@pytest.fixture(autouse=True)
def _tracked_consumer_is_restored() -> Iterator[None]:
    """Fail any test in this module that leaves the tracked consumer's bytes changed."""
    before = RELAY_CONSUMER.read_bytes() if RELAY_CONSUMER.is_file() else None
    yield
    after = RELAY_CONSUMER.read_bytes() if RELAY_CONSUMER.is_file() else None
    assert before == after, (
        f"this test left {RELAY_CONSUMER.relative_to(REPO_ROOT).as_posix()} changed. Snapshot and "
        "restore it through `_locked_snapshot`, which holds the gate's own regeneration lock at both "
        "ends. (If a peer ran `npm run typecheck` during this test it also regenerates this file; the "
        "lock cannot prevent that — re-run on a quiet tree to confirm.)"
    )


class TestRegenerateAndDiff:
    @_needs_relay
    def test_the_committed_relay_consumer_is_byte_identical_to_a_regeneration(self) -> None:
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
        assert "byte-identical to a fresh regeneration" in result.stdout

    @_needs_relay
    def test_a_comment_only_hand_edit_is_caught(self) -> None:
        """The byte diff fails a comment-only hand edit that the structural check passes."""
        with _locked_snapshot(RELAY_CONSUMER) as original:
            gate.write_bytes_atomic(RELAY_CONSUMER, original + b"\n// hand-edited by a human\n")

            strict = _run()
            assert strict.returncode == 1, strict.stdout + strict.stderr
            assert "diverged" in strict.stderr

            structural_only = _run("--no-regenerate")
            assert structural_only.returncode == 0, (
                "the structural check is expected to miss a comment-only edit; if it now catches "
                "it, this test no longer demonstrates why the byte diff is needed"
            )

    @_needs_relay
    def test_the_gate_restores_the_file_it_regenerated(self) -> None:
        """A gate run leaves a locally edited consumer exactly as it found it."""
        with _locked_snapshot(RELAY_CONSUMER) as original:
            gate.write_bytes_atomic(RELAY_CONSUMER, original + b"\n// transient local edit\n")
            mutated = RELAY_CONSUMER.read_bytes()
            _run()
            assert RELAY_CONSUMER.read_bytes() == mutated, "the gate left the working tree changed"

    def test_a_missing_node_modules_is_reported_unavailable_not_passed(
        self, tmp_path: Path
    ) -> None:
        """A missing `node_modules` is reported as unavailable, not as a pass."""
        consumer = tmp_path / "events.ts"
        consumer.write_text("export const X = 1;\n", encoding="utf-8")
        violation, reason = gate.regenerate_and_diff(
            consumer, tmp_path, ["npm", "run", "gen:types"]
        )
        assert violation is None
        assert reason is not None
        assert "node_modules" in reason

    def test_a_failing_generator_is_unavailable_not_a_divergence(self, tmp_path: Path) -> None:
        """A failing generator is reported as unavailable, not as a divergence."""
        (tmp_path / "node_modules").mkdir()
        consumer = tmp_path / "events.ts"
        consumer.write_text("export const X = 1;\n", encoding="utf-8")
        violation, reason = gate.regenerate_and_diff(
            consumer, tmp_path, ["bash", "-c", "echo boom >&2; exit 1"]
        )
        assert violation is None
        assert reason is not None
        assert "exited 1" in reason

    def test_a_generator_that_deletes_the_consumer_is_a_violation(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules").mkdir()
        consumer = tmp_path / "events.ts"
        consumer.write_text("export const X = 1;\n", encoding="utf-8")
        violation, reason = gate.regenerate_and_diff(
            consumer, tmp_path, ["bash", "-c", "rm events.ts"]
        )
        assert reason is None
        assert violation is not None
        assert "produced no file" in violation

    def test_the_restore_never_exposes_a_truncated_consumer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restore is atomic, so the consumer is never observed at a truncated size.

        Every write-mode open is intercepted and the consumer's size inspected after it returns.
        """
        (tmp_path / "node_modules").mkdir()
        consumer = tmp_path / "events.ts"
        original = b"export const X = 1;\n" * 64
        consumer.write_bytes(original)
        regenerated = b"export const Y = 2;\n"
        observed_sizes: list[int] = []
        real_open = Path.open

        def observing_open(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            handle = real_open(self, mode, *args, **kwargs)
            if any(flag in mode for flag in "wax+"):
                observed_sizes.append(consumer.stat().st_size)
            return handle

        monkeypatch.setattr(Path, "open", observing_open)
        violation, reason = gate.regenerate_and_diff(
            consumer, tmp_path, ["bash", "-c", f"printf '{regenerated.decode()}' > events.ts"]
        )
        monkeypatch.undo()

        assert violation is not None and reason is None
        assert observed_sizes, "the restore performed no write; the observation proved nothing"
        assert set(observed_sizes) <= {len(original), len(regenerated)}, (
            f"a reader could have seen the consumer at {observed_sizes} bytes mid-restore"
        )
        assert consumer.read_bytes() == original
        assert sorted(p.name for p in tmp_path.iterdir()) == ["events.ts", "node_modules"]

    def test_an_atomic_write_of_identical_bytes_does_not_touch_the_file(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "events.ts"
        target.write_bytes(b"same\n")
        inode = target.stat().st_ino
        gate.write_bytes_atomic(target, b"same\n")
        assert target.stat().st_ino == inode

    def test_the_gate_declares_no_typescript_renderer_of_its_own(self) -> None:
        """The gate's source contains no TypeScript declarations of its own."""
        source = (REPO_ROOT / "scripts" / "check_sse_schema_parity.py").read_text(encoding="utf-8")
        for tell in ("export type", "export interface", "export const"):
            assert tell not in source, (
                f"the gate appears to render TypeScript ({tell!r}); generation belongs to the "
                "consumers' own generators, or there are two sources of truth"
            )
