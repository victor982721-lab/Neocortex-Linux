"""Read-only, bounded admission classification for mixed corpus files."""

from __future__ import annotations

import importlib.util
import gzip
import struct
import zipfile
from pathlib import Path

import pytest

import neocortex.workflow.actions.corpus_admission as admission_module
from neocortex.deduplication import snapshot_path
from neocortex.workflow.actions.corpus_admission import (
    MAX_PREFIX_BYTES,
    AdmissionDecision,
    CorpusAdmissionPolicy,
    assess_file,
    assess_virtual_member,
)


def _snapshot(path: Path):
    return snapshot_path(path)


def _policy(
    *roots: Path,
    scope: str = "projects",
    include_generated: bool = False,
    include_vendored: bool = False,
) -> CorpusAdmissionPolicy:
    return CorpusAdmissionPolicy(
        tuple(roots),
        code_scope=scope,
        include_generated=include_generated,
        include_vendored=include_vendored,
    )


def test_policy_and_decision_are_frozen_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    project = root / "project"
    first = _policy(project)
    second = _policy(project)

    assert first == second
    assert first.to_dict()["code_scope"] == "projects"
    assert first.to_dict()["interested_roots"] == [str(project)]
    assert first.to_dict()["include_generated"] is False
    assert first.to_dict()["include_vendored"] is False
    assert first.signature == second.signature
    assert first.signature.startswith("neocortex.corpus-admission-policy/v1:sha256:")

    decision = AdmissionDecision("metadata_only", "source", "bounded", ("scope:test",))
    assert decision.to_dict() == {
        "disposition": "metadata_only",
        "category": "source",
        "reason": "bounded",
        "evidence": ["scope:test"],
    }
    with pytest.raises(ValueError):
        AdmissionDecision("trash", "source", "invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CorpusAdmissionPolicy((project,), include_generated=1)  # type: ignore[arg-type]


def test_source_scope_is_narrow_without_global_likely_code_filter(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    interested = corpus / "Neocortex"
    inside = interested / "src" / "main.py"
    vendored = interested / "vendor" / "foreign.js"
    vendored_doc = interested / "vendor" / "useful.md"
    outside = corpus / "downloads" / "tool.py"
    markdown = corpus / "AppData" / "report.md"
    data = corpus / ".dotnet" / "docs" / "catalog.json"
    for path, body in (
        (inside, "print('inside')\n"),
        (vendored, "export const foreign = true;\n"),
        (vendored_doc, "# Useful vendor documentation\n"),
        (outside, "print('outside')\n"),
        (markdown, "# Useful report\n"),
        (data, '{"title": "useful"}\n'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    policy = _policy(interested)
    assert assess_file(_snapshot(inside), root=corpus, policy=policy).to_dict() == {
        "disposition": "process",
        "category": "source",
        "reason": "interested_source_root",
        "evidence": ["code:scope:interested"],
    }
    vendored_decision = assess_file(_snapshot(vendored), root=corpus, policy=policy)
    assert vendored_decision.disposition == "metadata_only"
    assert vendored_decision.reason == "dependency"
    assert assess_file(_snapshot(vendored_doc), root=corpus, policy=policy).disposition == "process"
    outside_decision = assess_file(_snapshot(outside), root=corpus, policy=policy)
    assert outside_decision.disposition == "metadata_only"
    assert outside_decision.reason == "source_outside_interested_root"
    assert assess_file(_snapshot(markdown), root=corpus, policy=policy).disposition == "process"
    assert assess_file(_snapshot(data), root=corpus, policy=policy).disposition == "process"

    broad = _policy(scope="broad")
    assert assess_file(_snapshot(outside), root=corpus, policy=broad).disposition == "process"


def test_generated_and_vendored_opt_in_is_root_scoped(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    interested = corpus / "Neocortex"
    vendored = interested / "vendor" / "foreign.js"
    generated = interested / "build" / "generated.py"
    foreign = corpus / "other" / "vendor" / "foreign.js"
    for path, body in (
        (vendored, "export const foreign = true;\n"),
        (generated, "VALUE = 1\n"),
        (foreign, "export const foreign = true;\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    default = _policy(interested)
    opted = _policy(interested, include_generated=True, include_vendored=True)
    assert assess_file(_snapshot(vendored), root=corpus, policy=default).reason == "dependency"
    assert assess_file(_snapshot(generated), root=corpus, policy=default).reason == "generated"
    assert assess_file(_snapshot(vendored), root=corpus, policy=opted).disposition == "process"
    assert assess_file(_snapshot(generated), root=corpus, policy=opted).disposition == "process"
    assert assess_file(_snapshot(foreign), root=corpus, policy=opted).disposition == "metadata_only"
    assert opted.signature != default.signature


@pytest.mark.parametrize(
    ("relative", "expected_category", "expected_disposition"),
    (
        ("LICENSE.txt", "preserved_artifact", "metadata_only"),
        ("fixtures/sample.bin", "fixture", "metadata_only"),
        (".codex/auth.json", "credential", "sensitive"),
        (".env", "credential", "sensitive"),
        ("keys/id_ed25519", "credential", "sensitive"),
    ),
)
def test_preserved_and_private_paths_need_no_body_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    expected_category: str,
    expected_disposition: str,
) -> None:
    corpus = tmp_path / "corpus"
    path = corpus / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"secret-or-valuable-payload")
    observed = _snapshot(path)

    def forbidden_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("path-only preservation must not open the body")

    monkeypatch.setattr(admission_module.os, "open", forbidden_open)
    decision = assess_file(observed, root=corpus, policy=_policy())

    assert decision.disposition == expected_disposition
    assert decision.category == expected_category
    assert path.exists()
    assert path.read_bytes() == b"secret-or-valuable-payload"


def test_only_identified_package_archives_are_metadata_only(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    wheel = corpus / "demo.whl"
    nupkg = corpus / "demo.nupkg"
    npm_tgz = corpus / ".npm" / "_cacache" / "demo.tgz"
    generic_tgz = corpus / "exports" / "report.tgz"
    for path in (wheel, nupkg, npm_tgz, generic_tgz):
        path.parent.mkdir(parents=True, exist_ok=True)
    wheel.write_bytes(b"PK\x03\x04wheel")
    nupkg.write_bytes(b"PK\x03\x04nuget")
    npm_tgz.write_bytes(gzip.compress(b"npm-package-payload"))
    generic_tgz.write_bytes(gzip.compress(b"document-payload"))
    fake_wheel = corpus / "fake.whl"
    fake_wheel.write_bytes(b"plain-not-a-zip")

    policy = _policy()
    for path, expected in ((wheel, "wheel"), (nupkg, "nuget"), (npm_tgz, "npm_tgz")):
        decision = assess_file(_snapshot(path), root=corpus, policy=policy)
        assert decision.disposition == "metadata_only"
        assert decision.category == "retained_archive"
        assert decision.evidence == (f"package:{expected}",)
    assert assess_file(_snapshot(generic_tgz), root=corpus, policy=policy).disposition == "process"
    assert assess_file(_snapshot(fake_wheel), root=corpus, policy=policy).disposition == "process"


def test_zip_document_docx_and_csv_gzip_remain_processable(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    zip_path = corpus / "report.zip"
    docx_path = corpus / "report.docx"
    csv_gzip = corpus / "measurements.csv.gz"
    corpus.mkdir(parents=True)
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("report.md", "# useful\n")
    with zipfile.ZipFile(docx_path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>\n")
        archive.writestr("word/document.xml", "<document/>\n")
    csv_gzip.write_bytes(gzip.compress(b"time,value\n1,2\n"))

    policy = _policy()
    assert assess_file(_snapshot(zip_path), root=corpus, policy=policy).disposition == "process"
    assert assess_file(_snapshot(docx_path), root=corpus, policy=policy).disposition == "process"
    csv_decision = assess_file(_snapshot(csv_gzip), root=corpus, policy=policy)
    assert csv_decision.disposition == "process"
    assert csv_decision.category == "unknown"


def test_package_json_requires_location_and_structure(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    package = corpus / "node_modules" / "demo"
    package.mkdir(parents=True)
    package_json = package / "package.json"
    package_json.write_text(
        '{"name":"demo","version":"1.0.0","dependencies":{}}\n',
        encoding="utf-8",
    )
    generic = corpus / "notes.json"
    generic.write_text('{"name":"demo","version":"1.0.0"}\n', encoding="utf-8")
    malformed = package / "manifest.json"
    malformed.write_text('{"name": "not-complete"}\n', encoding="utf-8")
    root_manifest = corpus / "package.json"
    root_manifest.write_text('{"name":"root","version":"1.0.0"}\n', encoding="utf-8")
    document_manifest = corpus / "manifest.json"
    document_manifest.write_text(
        '{"name":"sensor-export","version":"1","files":["report.pdf"]}\n',
        encoding="utf-8",
    )

    policy = _policy()
    package_decision = assess_file(_snapshot(package_json), root=corpus, policy=policy)
    assert package_decision.disposition == "metadata_only"
    assert package_decision.category == "package_metadata"
    assert package_decision.reason == "package_json_structure_metadata_only"
    assert assess_file(_snapshot(generic), root=corpus, policy=policy).disposition == "process"
    assert assess_file(_snapshot(malformed), root=corpus, policy=policy).disposition == "process"
    assert assess_file(_snapshot(root_manifest), root=corpus, policy=policy).disposition == "process"
    assert assess_file(_snapshot(document_manifest), root=corpus, policy=policy).disposition == "process"


def test_large_lockfile_uses_bounded_top_level_structure_evidence(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    lockfile = corpus / "package-lock.json"
    lockfile.parent.mkdir(parents=True)
    lockfile.write_bytes(
        b'{"name":"demo","lockfileVersion":3,"packages":{"":{'
        + b'"description":"'
        + b"x" * (MAX_PREFIX_BYTES * 2)
        + b'"}}}'
    )

    decision = assess_file(_snapshot(lockfile), root=corpus, policy=_policy())
    assert decision.disposition == "metadata_only"
    assert decision.category == "package_metadata"
    assert decision.evidence == ("json:json_lock_structure_prefix",)


def test_virtual_members_reuse_admission_without_filesystem_access(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    interested = corpus / "Neocortex"
    policy = _policy(interested)

    inside = assess_virtual_member(
        "src/module.py",
        b"print('inside')\n",
        container_path=interested,
        policy=policy,
    )
    foreign = assess_virtual_member(
        "downloads/tool.py",
        b"print('foreign')\n",
        container_path=corpus,
        policy=policy,
    )
    assert inside.disposition == "process"
    assert foreign.disposition == "metadata_only"

    virtual_vendored = assess_virtual_member(
        "vendor/foreign.js",
        b"export const foreign = true;\n",
        container_path=interested,
        policy=policy,
    )
    virtual_doc = assess_virtual_member(
        "vendor/useful.md",
        b"# Useful documentation\n",
        container_path=interested,
        policy=policy,
    )
    assert virtual_vendored.disposition == "metadata_only"
    assert virtual_vendored.reason == "dependency"
    assert virtual_doc.disposition == "process"
    virtual_opted = assess_virtual_member(
        "vendor/foreign.js",
        b"export const foreign = true;\n",
        container_path=interested,
        policy=_policy(interested, include_vendored=True),
    )
    assert virtual_opted.disposition == "process"

    for name in ("report.md", "table.csv", "record.json", "doc.xml"):
        decision = assess_virtual_member(
            name,
            b"useful document payload",
            container_path=corpus,
            policy=policy,
        )
        assert decision.disposition == "process"

    secret = assess_virtual_member(
        ".codex/auth.json",
        b'{"token":"must-not-appear"}',
        container_path=corpus,
        policy=policy,
    )
    assert secret.disposition == "sensitive"
    assert "must-not-appear" not in str(secret.to_dict())

    package = assess_virtual_member(
        "node_modules/demo/package.json",
        b'{"name":"demo","version":"1.0.0"}',
        container_path=corpus,
        policy=policy,
    )
    assert package.disposition == "metadata_only"
    assert package.category == "package_metadata"

    pdf = assess_virtual_member(
        "renamed.py",
        b"%PDF-1.7\nreport",
        container_path=corpus,
        policy=policy,
    )
    assert pdf.disposition == "process"
    assert pdf.category == "document"

    with pytest.raises(ValueError):
        assess_virtual_member("../secret.py", b"x", container_path=corpus, policy=policy)
    with pytest.raises(ValueError):
        assess_virtual_member("/absolute.py", b"x", container_path=corpus, policy=policy)
    with pytest.raises(ValueError):
        assess_virtual_member(
            "large.txt",
            b"x" * (MAX_PREFIX_BYTES + 1),
            container_path=corpus,
            policy=policy,
        )


def test_runtime_binary_context_is_bounded_and_sqlite_is_preserved(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    runtime = corpus / "AppData" / "Local" / "Programs" / "demo" / "demo.so"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"\x7fELF\x02\x01\x01\x00runtime")
    sqlite = corpus / "data" / "state.sqlite3"
    sqlite.parent.mkdir()
    sqlite.write_bytes(b"SQLite format 3\x00database")
    useful = corpus / ".dotnet" / "docs" / "guide.md"
    useful.parent.mkdir(parents=True)
    useful.write_text("# Keep this document\n", encoding="utf-8")

    policy = _policy()
    binary_decision = assess_file(_snapshot(runtime), root=corpus, policy=policy)
    assert binary_decision.disposition == "metadata_only"
    assert binary_decision.category == "runtime_binary"
    assert assess_file(_snapshot(sqlite), root=corpus, policy=policy).to_dict() == {
        "disposition": "process",
        "category": "sqlite",
        "reason": "sqlite_preserved_no_trash",
        "evidence": ["magic:sqlite"],
    }
    assert assess_file(_snapshot(useful), root=corpus, policy=policy).disposition == "process"


def test_current_runtime_bytecode_is_metadata_only_but_invalid_magic_is_unknown(
    tmp_path: Path,
) -> None:
    corpus = tmp_path / "corpus"
    cache = corpus / "__pycache__"
    cache.mkdir(parents=True)
    valid = cache / "module.cpython-314.pyc"
    valid.write_bytes(importlib.util.MAGIC_NUMBER + struct.pack("<I", 0) + b"\0" * 8 + b"body")
    invalid = cache / "foreign.pyc"
    invalid.write_bytes(b"\0\0\0\0" + b"\0" * 32)

    policy = _policy()
    valid_decision = assess_file(_snapshot(valid), root=corpus, policy=policy)
    assert valid_decision.disposition == "metadata_only"
    assert valid_decision.category == "bytecode_cache"
    assert assess_file(_snapshot(invalid), root=corpus, policy=policy).category == "unknown"


def test_document_magic_wins_over_a_code_like_suffix(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    misnamed = corpus / "valuable.py"
    misnamed.parent.mkdir(parents=True)
    misnamed.write_bytes(b"%PDF-1.7\nvaluable report\n")

    decision = assess_file(_snapshot(misnamed), root=corpus, policy=_policy())
    assert decision.disposition == "process"
    assert decision.category == "document"
    assert decision.reason == "content_magic_precedes_path_suffix"


def test_prefix_read_is_bounded_and_identity_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = tmp_path / "corpus"
    candidate = corpus / "unknown.bin"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"x" * (MAX_PREFIX_BYTES * 4))
    observed = _snapshot(candidate)
    original_read = admission_module.os.read
    read_sizes: list[int] = []

    def bounded_read(fd: int, count: int) -> bytes:
        read_sizes.append(count)
        return original_read(fd, count)

    monkeypatch.setattr(admission_module.os, "read", bounded_read)
    decision = assess_file(observed, root=corpus, policy=_policy())
    assert decision.disposition == "process"
    assert read_sizes and max(read_sizes) <= MAX_PREFIX_BYTES

    candidate.write_bytes(b"changed-and-longer")
    stale = assess_file(observed, root=corpus, policy=_policy())
    assert stale.disposition == "sensitive"
    assert stale.category == "unverified"
    assert stale.reason == "identity_or_prefix_unverified"


def test_outside_root_symlink_and_cancellation_fail_closed(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    link = corpus / "link.bin"
    link.symlink_to(outside)

    outside_decision = assess_file(
        _snapshot(outside),
        root=corpus,
        policy=_policy(),
    )
    assert outside_decision.disposition == "sensitive"
    assert outside_decision.category == "out_of_scope"
    assert assess_file(_snapshot(link), root=corpus, policy=_policy()).disposition == "sensitive"

    parent = corpus / "parent-link"
    parent.symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "nested.md").write_text("# useful\n", encoding="utf-8")
    nested = parent / "nested.md"
    nested_decision = assess_file(
        _snapshot(nested),
        root=corpus,
        policy=_policy(),
    )
    assert nested_decision.disposition == "sensitive"

    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("cancelled")

    candidate = corpus / "document.json"
    candidate.write_text('{"title":"keep"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="cancelled"):
        assess_file(_snapshot(candidate), root=corpus, policy=_policy(), cancellation_check=cancel)
    assert calls == 1
