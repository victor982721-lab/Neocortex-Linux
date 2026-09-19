"""Acceptance fixtures for corpus admission and regeneration-bounded actions.

These tests intentionally use only ``tmp_path`` for corpus, state, archives,
and the injected effect backend.  They are written against the new admission
and regeneration contracts and are expected to fail until the root
integration is present.
"""

from __future__ import annotations

import json
import hashlib
import base64
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from neocortex.curation.application import BackendOutcome
from neocortex.deduplication import DedupIndex, DedupPlanner, KeeperPolicy, snapshot_path
from neocortex.persistence.framework_state_writer import FrameworkState
from neocortex.runtime.models import FrameworkConfig
from neocortex.runtime.orchestration.orchestrator import FrameworkOrchestrator
from neocortex.workflow.actions.actions import FrameworkActions
from neocortex.workflow.actions.corpus_admission import (
    CorpusAdmissionPolicy,
    assess_file,
)
from neocortex.workflow.actions.regeneration import (
    find_regeneration_proof,
    revalidate_regeneration_proof,
)
from tests.internal_paths_test_support import begin_signed_normal_run


@dataclass(frozen=True, slots=True)
class _CorpusFixture:
    root: Path
    state: Path
    interested: Path
    foreign: Path
    foreign_data: Path
    appdata_doc: Path
    cache_doc: Path
    owned_source: Path
    generated: Path
    unknown: Path
    license: Path
    wheel: Path
    nupkg: Path
    secret: Path


def _write(path: Path, payload: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_bytes(payload)
    return path


def _zip_member(path: Path, member: str, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload)
    return path


def _standard_package_archive(path: Path, member: str, payload: bytes) -> Path:
    """Build a minimal standards-shaped wheel or nupkg witness."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.casefold() == ".whl":
        dist_info = "fixture_pkg-1.0.dist-info"
        files: dict[str, bytes] = {
            member: payload,
            f"{dist_info}/WHEEL": (
                b"Wheel-Version: 1.0\n"
                b"Generator: acceptance-fixture\n"
                b"Root-Is-Purelib: true\n"
                b"Tag: py3-none-any\n"
            ),
            f"{dist_info}/METADATA": (
                b"Metadata-Version: 2.1\nName: fixture-pkg\nVersion: 1.0\n"
            ),
        }
        record_name = f"{dist_info}/RECORD"
        rows: list[str] = []
        for name, value in files.items():
            encoded = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode().rstrip("=")
            rows.append(f"{name},sha256={encoded},{len(value)}")
        rows.append(f"{record_name},,")
        files[record_name] = ("\n".join(rows) + "\n").encode("utf-8")
    else:
        files = {
            member: payload,
            "Fixture.nuspec": (
                b"<package><metadata><id>Fixture</id>"
                b"<version>1.0.0</version></metadata></package>"
            ),
        }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return path


def _fixture(tmp_path: Path) -> _CorpusFixture:
    root = tmp_path / "corpus"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()

    interested = root / "owned-project"
    _write(interested / "package.json", '{"name":"owned-fixture","scripts":{"build":"fixture"}}\n')
    owned_source = _write(
        interested / "src" / "main.js",
        "export const value = 1;\n",
    )
    generated = _write(interested / "build" / "main.js", owned_source.read_bytes())

    foreign = root / "foreign-project"
    _write(foreign / "package.json", '{"name":"foreign-fixture"}\n')
    _write(foreign / "module.py", "VALUE = 'foreign'\n")
    _write(foreign / "module.js", "export const foreignValue = 1;\n")
    _write(foreign / "original-source.py", "ORIGINAL = True\n")
    foreign_data = _write(foreign / "data.json", '{"useful":"foreign-data"}\n')
    license_path = _write(foreign / "vendor" / "LICENSE.txt", "Attribution must remain.\n")

    appdata_doc = _write(root / "AppData" / "Local" / "report.json", '{"useful":true}\n')
    cache_doc = _write(root / ".cache" / "notes" / "operational.md", "# Useful note\n")
    _write(root / "AppData" / "Local" / "table.csv", "name,value\nalpha,1\n")
    _write(root / ".cache" / "notes" / "readme.txt", "retained text\n")

    unknown = _write(root / "foreign-project" / "opaque.bin", b"\x00\x01\x02opaque")
    secret = _write(root / "foreign-project" / ".env", "TOKEN=fixture-secret\n")
    wheel = _zip_member(
        root / "foreign-project" / "dist" / "fixture-1.0-py3-none-any.whl",
        "fixture_pkg/__init__.py",
        b"VALUE = 7\n",
    )
    nupkg = _zip_member(
        root / "foreign-project" / "dist" / "Fixture.1.0.0.nupkg",
        "lib/net8.0/Fixture.dll",
        b"fixture-binary-member",
    )
    return _CorpusFixture(
        root=root,
        state=state,
        interested=interested,
        foreign=foreign,
        foreign_data=foreign_data,
        appdata_doc=appdata_doc,
        cache_doc=cache_doc,
        owned_source=owned_source,
        generated=generated,
        unknown=unknown,
        license=license_path,
        wheel=wheel,
        nupkg=nupkg,
        secret=secret,
    )


def _policy(fixture: _CorpusFixture) -> CorpusAdmissionPolicy:
    return CorpusAdmissionPolicy(
        interested_roots=(fixture.interested,),
        code_scope="projects",
    )


def _disposition(decision: object) -> str:
    value = getattr(decision, "disposition", decision)
    return str(getattr(value, "value", value))


def _snapshot_paths(database: Path, table: str, column: str) -> set[str]:
    with sqlite3.connect(database) as connection:
        where = "status='current'" if table == "files" else "1=1"
        return {
            str(row[0])
            for row in connection.execute(f"SELECT {column} FROM {table} WHERE {where}")
        }


def test_admission_policy_separates_code_interest_data_and_sensitive_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    policy = _policy(fixture)

    process_paths = (fixture.owned_source, fixture.appdata_doc, fixture.cache_doc)
    for path in process_paths:
        decision = assess_file(
            snapshot_path(path),
            root=fixture.root,
            policy=policy,
            cancellation_check=lambda: None,
        )
        assert _disposition(decision) == "process", path

    foreign_data_decision = assess_file(
        snapshot_path(fixture.foreign_data),
        root=fixture.root,
        policy=policy,
        cancellation_check=lambda: None,
    )
    assert _disposition(foreign_data_decision) == "process"

    for path in (
        fixture.foreign / "module.py",
        fixture.foreign / "module.js",
        fixture.foreign / "original-source.py",
    ):
        decision = assess_file(
            snapshot_path(path),
            root=fixture.root,
            policy=policy,
            cancellation_check=lambda: None,
        )
        assert _disposition(decision) == "metadata_only", path

    unknown_decision = assess_file(
        snapshot_path(fixture.unknown),
        root=fixture.root,
        policy=policy,
        cancellation_check=lambda: None,
    )
    assert _disposition(unknown_decision) == "process"

    for path in (fixture.license, fixture.wheel, fixture.nupkg):
        decision = assess_file(
            snapshot_path(path),
            root=fixture.root,
            policy=policy,
            cancellation_check=lambda: None,
        )
        assert _disposition(decision) != "process", path

    import neocortex.workflow.actions.corpus_admission as admission_module

    original_open = admission_module.os.open

    def guarded_open(path, flags, *args, **kwargs):
        if Path(path) == fixture.secret:
            raise AssertionError("credential body must not be opened")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(admission_module.os, "open", guarded_open)
    secret_decision = assess_file(
        snapshot_path(fixture.secret),
        root=fixture.root,
        policy=policy,
        cancellation_check=lambda: None,
    )
    assert _disposition(secret_decision) == "sensitive"
    assert "fixture-secret" not in repr(secret_decision)


def test_orchestrator_keeps_foreign_code_out_but_processes_appdata_and_cache_text_and_replay(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    config = FrameworkConfig(
        root=fixture.root,
        state_directory=fixture.state,
        route="text,code",
        code_candidate_scope="projects",
        code_project_roots=(fixture.interested,),
        document_catalog_enabled=False,
        global_memory_budget_bytes=512 * 1024 * 1024,
        global_min_free_memory_bytes=0,
        global_min_free_commit_bytes=0,
        global_cpu_slots=2,
        text_worker_memory_bytes=128 * 1024 * 1024,
        text_worker_timeout_seconds=30.0,
        heartbeat_interval_seconds=0.01,
    )

    first = FrameworkOrchestrator(config).run_initial()
    assert first.text is not None
    assert first.code is not None

    code_paths = _snapshot_paths(config.code_database, "files", "current_path")
    text_paths = _snapshot_paths(config.text_database, "documents", "path")
    assert str(fixture.owned_source) in code_paths
    assert not any(path.startswith(str(fixture.foreign)) for path in code_paths)
    assert str(fixture.appdata_doc) in text_paths
    assert str(fixture.cache_doc) in text_paths
    assert str(fixture.foreign_data) in text_paths
    assert str(fixture.foreign / "module.py") not in text_paths
    assert str(fixture.foreign / "module.js") not in text_paths
    assert str(fixture.foreign / "original-source.py") not in text_paths

    _write(fixture.foreign / "module.js", "export const foreignValue = 2;\n")
    second = FrameworkOrchestrator(config).run_initial()
    assert second.code is not None
    assert second.text is not None
    replay_code_paths = _snapshot_paths(config.code_database, "files", "current_path")
    replay_text_paths = _snapshot_paths(config.text_database, "documents", "path")
    assert not any(path.startswith(str(fixture.foreign)) for path in replay_code_paths)
    assert str(fixture.foreign_data) in replay_text_paths
    assert str(fixture.foreign / "module.py") not in replay_text_paths
    assert str(fixture.foreign / "module.js") not in replay_text_paths
    assert str(fixture.foreign / "original-source.py") not in replay_text_paths


class _FixtureRegenerationBackend:
    """Contained reversible backend; KIO and desktop Trash are never selected."""

    def __init__(self, trash_root: Path) -> None:
        self.trash_root = trash_root
        self.calls: list[tuple[str, ...]] = []

    def apply_many_snapshots(self, items, *, root: Path):
        del root
        items = tuple(items)
        self.calls.append(tuple(snapshot.path for snapshot, _digest in items))
        self.trash_root.mkdir(parents=True, exist_ok=True)
        outcomes: list[BackendOutcome] = []
        for snapshot, _digest in items:
            source = Path(snapshot.path)
            target = self.trash_root / source.name
            source.replace(target)
            outcomes.append(
                BackendOutcome(
                    "applied",
                    "fixture_regeneration_effect",
                    receipt_json=json.dumps(
                        {"schema": "fixture-trash/v1", "source": str(source), "target": str(target)},
                        sort_keys=True,
                    ),
                )
            )
        return tuple(outcomes)


def _duplicate_plan(fixture: _CorpusFixture, index: DedupIndex):
    scan = index.scan(fixture.root, excluded_paths=())
    return scan, DedupPlanner(
        index,
        keeper_policy=KeeperPolicy(preferred_roots=(str(fixture.owned_source.parent),)),
    ).plan(scan.scan_id, exact_compare=True, preview_limit=20)


def test_framework_actions_preview_and_apply_only_regenerable_with_retained_witness(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    policy = _policy(fixture)
    trash_root = tmp_path / "fixture-trash"
    before = {
        path: path.read_bytes()
        for path in (fixture.owned_source, fixture.license, fixture.wheel, fixture.nupkg, fixture.unknown, fixture.secret)
    }

    with DedupIndex(fixture.state / "dedup.sqlite3") as index:
        scan, plan = _duplicate_plan(fixture, index)
        with FrameworkState(fixture.state / "framework.sqlite3") as state:
            preview_run = begin_signed_normal_run(state, fixture.root)
            preview_backend = _FixtureRegenerationBackend(trash_root)
            preview = FrameworkActions(
                index,
                state,
                preview_run,
                scan.scan_id,
                apply=False,
                excluded_paths=(),
                trash_backend=preview_backend,  # type: ignore[arg-type]
                corpus_admission_policy=policy,
            ).execute(plan, cleanup_empty_directories=False)
            assert preview.apply_actions is False
            assert preview_backend.calls == []
            assert fixture.generated.exists()

        with FrameworkState(fixture.state / "framework.sqlite3") as state:
            apply_run = begin_signed_normal_run(state, fixture.root)
            backend = _FixtureRegenerationBackend(trash_root)
            applied = FrameworkActions(
                index,
                state,
                apply_run,
                scan.scan_id,
                apply=True,
                excluded_paths=(),
                trash_backend=backend,  # type: ignore[arg-type]
                corpus_admission_policy=policy,
            ).execute(plan, cleanup_empty_directories=False)

    assert applied.duplicates_trashed == 1
    assert backend.calls == [(str(fixture.generated),)]
    assert not fixture.generated.exists()
    assert fixture.owned_source.exists()
    assert {path: path.read_bytes() for path in before} == before
    assert fixture.wheel.exists()
    assert fixture.nupkg.exists()
    assert fixture.license.exists()
    assert fixture.unknown.exists()
    assert fixture.secret.exists()


@pytest.mark.parametrize(
    ("archive_name", "member", "payload"),
    (
        ("fixture-1.0-py3-none-any.whl", "fixture_pkg/__init__.py", b"VALUE = 1\n"),
        ("Fixture.1.0.0.nupkg", "lib/net8.0/Fixture.dll", b"fixture-binary"),
    ),
)
def test_regeneration_proof_accepts_exact_retained_wheel_or_nupkg_member(
    tmp_path: Path,
    archive_name: str,
    member: str,
    payload: bytes,
) -> None:
    root = tmp_path / "corpus"
    output = root / member
    archive = _standard_package_archive(root / "retained" / archive_name, member, payload)
    _write(output, payload)

    proof = find_regeneration_proof(
        snapshot_path(output),
        root=root,
        archive_paths=(archive,),
        cancellation_check=lambda: None,
    )
    assert proof is not None
    assert proof.source_member == member
    assert revalidate_regeneration_proof(proof, root=root) is True

    _write(output, b"changed after proof\n")
    assert revalidate_regeneration_proof(proof, root=root) is False
