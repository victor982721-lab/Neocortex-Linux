"""Private, deterministic tests for bounded package regeneration evidence."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import py_compile
import tarfile
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from neocortex.deduplication.fingerprinting import snapshot_path
from neocortex.runtime.control.cancellation import CancellationRequested
from neocortex.workflow.actions import regeneration
from neocortex.workflow.actions.regeneration import (
    NPM_TGZ_METHOD,
    NUPKG_METHOD,
    WHEEL_METHOD,
    RegenerationProof,
    find_regeneration_proof,
    revalidate_regeneration_proof,
)


@pytest.fixture(autouse=True)
def _private_runtime_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "home").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "tmp").mkdir()


def _digest_field(payload: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(
        b"="
    ).decode("ascii")


def _write_wheel(root: Path, member: str, payload: bytes, *, name: str = "source.whl") -> Path:
    dist_info = "fixture-1.0.dist-info"
    entries = {
        member: payload,
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: regeneration-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        ),
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.1\nName: fixture\nVersion: 1.0\n\n"
        ),
    }
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for path, data in entries.items():
        writer.writerow((path, _digest_field(data), str(len(data))))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    entries[f"{dist_info}/RECORD"] = record.getvalue().encode("utf-8")
    archive_path = root / name
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in entries.items():
            archive.writestr(path, data)
    return archive_path


def _write_nupkg(root: Path, member: str, payload: bytes) -> Path:
    nuspec = (
        b"<?xml version='1.0' encoding='utf-8'?>"
        b"<package><metadata><id>fixture</id><version>1.0.0</version>"
        b"</metadata></package>"
    )
    archive_path = root / "fixture.nupkg"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("fixture.nuspec", nuspec)
        archive.writestr(member, payload)
    return archive_path


def _write_npm_tgz(root: Path, member: str, payload: bytes) -> Path:
    archive_path = root / "fixture.tgz"
    package_json = json.dumps(
        {"name": "fixture-package", "version": "1.0.0", "files": [member]},
        sort_keys=True,
    ).encode("utf-8")
    with tarfile.open(archive_path, "w:gz") as archive:
        metadata = tarfile.TarInfo("package/package.json")
        metadata.size = len(package_json)
        metadata.mtime = 0
        archive.addfile(metadata, io.BytesIO(package_json))
        item = tarfile.TarInfo(f"package/{member}")
        item.size = len(payload)
        item.mtime = 0
        archive.addfile(item, io.BytesIO(payload))
    return archive_path


def _candidate(root: Path, relative: str, payload: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


@pytest.mark.parametrize(
    "relative",
    ["LICENSE.txt", "fixture.json", "keys/auth.pem", "unique.md"],
)
def test_wheel_member_proves_protected_and_unique_candidates(
    tmp_path: Path, relative: str,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    payload = ("preserved-" + relative).encode("utf-8")
    candidate = _candidate(root, relative, payload)
    archive = _write_wheel(root, relative, payload)

    proof = find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    )

    assert proof is not None
    assert proof.method == WHEEL_METHOD
    assert proof.source_member == relative
    assert proof.candidate_sha256 == hashlib.sha256(payload).hexdigest()
    assert proof.witness_sha256 == (hashlib.sha256(archive.read_bytes()).hexdigest(),)
    assert revalidate_regeneration_proof(proof, root=root)
    assert json.loads(json.dumps(proof.to_dict()))["source_member"] == relative


def test_nupkg_member_uses_exact_relative_package_path(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    relative = "lib/net8.0/fixture.dll"
    payload = b"managed payload"
    candidate = _candidate(root, "packages/fixture/1.0.0/" + relative, payload)
    archive = _write_nupkg(root, relative, payload)

    proof = find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    )

    assert proof is not None
    assert proof.method == NUPKG_METHOD
    assert proof.source_member == relative
    assert revalidate_regeneration_proof(proof, root=root)


def test_npm_tgz_maps_candidate_relative_path_only_under_package_prefix(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    relative = "fixture.js"
    payload = b"module.exports = 1;\n"
    candidate = _candidate(root, relative, payload)
    archive = _write_npm_tgz(root, relative, payload)

    proof = find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    )

    assert proof is not None
    assert proof.method == NPM_TGZ_METHOD
    assert proof.source_member == "package/fixture.js"
    assert revalidate_regeneration_proof(proof, root=root)


def test_installed_layout_anchors_are_unique_and_not_arbitrary_suffix_matches(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    payload = b"installed package payload"
    wheel_candidate = _candidate(root, "Replica/site-packages/pkg/x.py", payload)
    wheel = _write_wheel(root, "pkg/x.py", payload, name="installed.whl")
    wheel_proof = find_regeneration_proof(
        snapshot_path(wheel_candidate), root=root, archive_paths=(wheel,)
    )
    assert wheel_proof is not None
    assert wheel_proof.source_member == "pkg/x.py"

    npm_candidate = _candidate(root, "Replica/node_modules/fixture-package/lib.js", payload)
    npm = _write_npm_tgz(root, "lib.js", payload)
    npm_proof = find_regeneration_proof(
        snapshot_path(npm_candidate), root=root, archive_paths=(npm,)
    )
    assert npm_proof is not None
    assert npm_proof.source_member == "package/lib.js"

    unrelated = _write_wheel(root, "other.py", b"other", name="unrelated.whl")
    assert find_regeneration_proof(
        snapshot_path(wheel_candidate), root=root, archive_paths=(unrelated, wheel)
    ) is not None

    arbitrary = _candidate(root, "Replica/random/pkg/x.py", payload)
    assert find_regeneration_proof(
        snapshot_path(arbitrary), root=root, archive_paths=(wheel,)
    ) is None


def test_record_or_same_folder_metadata_without_preserved_archive_is_not_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "pkg/module.py", b"print('payload')\n")
    (root / "pkg" / "RECORD").write_text("pkg/module.py,,\n", encoding="utf-8")

    assert find_regeneration_proof(snapshot_path(candidate), root=root) is None


def test_candidate_or_witness_drift_abstains_at_revalidation(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    payload = b"stable payload"
    candidate = _candidate(root, "pkg/payload.bin", payload)
    archive = _write_wheel(root, "pkg/payload.bin", payload)
    proof = find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    )
    assert proof is not None

    candidate.write_bytes(b"changed payload")
    assert not revalidate_regeneration_proof(proof, root=root)

    candidate.write_bytes(payload)
    archive.write_bytes(archive.read_bytes() + b"drift")
    assert not revalidate_regeneration_proof(proof, root=root)


def test_outside_symlink_and_hardlink_trees_never_prove(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    payload = b"payload"
    candidate = _candidate(root, "pkg/payload.bin", payload)
    archive = _write_wheel(root, "pkg/payload.bin", payload)

    external_archive = tmp_path / "external.whl"
    external_archive.write_bytes(archive.read_bytes())
    assert find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(external_archive,)
    ) is None

    if os.name == "posix":
        symlink = root / "pkg" / "symlink.bin"
        symlink.symlink_to(candidate)
        assert find_regeneration_proof(
            snapshot_path(symlink), root=root, archive_paths=(archive,)
        ) is None

        alias = root / "pkg" / "alias.bin"
        os.link(candidate, alias)
        assert find_regeneration_proof(
            snapshot_path(alias), root=root, archive_paths=(archive,)
        ) is None

        archive_alias = root / "archive-alias.whl"
        os.link(archive, archive_alias)
        assert find_regeneration_proof(
            snapshot_path(candidate), root=root, archive_paths=(archive_alias,)
        ) is None


def test_archive_traversal_and_multiple_witnesses_abstain(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "payload.bin", b"payload")
    bad_archive = root / "bad.whl"
    with zipfile.ZipFile(bad_archive, "w") as archive:
        archive.writestr("../payload.bin", b"payload")
    assert find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(bad_archive,)
    ) is None

    good = _write_wheel(root, "payload.bin", b"payload", name="one.whl")
    second = root / "two.whl"
    second.write_bytes(good.read_bytes())
    assert find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(good, second)
    ) is None


def test_zip_central_directory_preflight_rejects_before_zipfile_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "payload.bin", b"payload")
    archive = _write_wheel(root, "payload.bin", b"payload")
    monkeypatch.setattr(regeneration, "MAX_ARCHIVE_MEMBERS", 1)

    def forbidden_zipfile(*_args: object, **_kwargs: object):
        raise AssertionError("ZipFile must not be constructed after failed preflight")

    monkeypatch.setattr(regeneration.zipfile, "ZipFile", forbidden_zipfile)
    assert find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    ) is None


def test_tar_stream_bounds_reject_before_getmembers_or_payload_materialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "payload.bin", b"payload")
    archive = root / "many.tgz"
    package_json = b'{"name":"fixture-package","version":"1.0.0"}'
    with tarfile.open(archive, "w:gz") as tar:
        metadata = tarfile.TarInfo("package/package.json")
        metadata.size = len(package_json)
        tar.addfile(metadata, io.BytesIO(package_json))
        for position in range(64):
            item = tarfile.TarInfo(f"package/data-{position}.bin")
            item.size = 65_536
            tar.addfile(item, io.BytesIO(b"x" * item.size))

    monkeypatch.setattr(regeneration, "MAX_TOTAL_MEMBER_BYTES", 100)

    def forbidden_getmembers(*_args: object, **_kwargs: object):
        raise AssertionError("stream mode must not materialize all TarInfo members")

    monkeypatch.setattr(regeneration.tarfile.TarFile, "getmembers", forbidden_getmembers)
    assert find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    ) is None

    original = RuntimeError("tar cancellation")

    def cancel() -> None:
        raise original

    with pytest.raises(RuntimeError) as raised:
        find_regeneration_proof(
            snapshot_path(candidate), root=root, archive_paths=(archive,),
            cancellation_check=cancel,
        )
    assert raised.value is original

    monkeypatch.setattr(regeneration, "MAX_ARCHIVE_MEMBERS", 20_000)
    monkeypatch.setattr(regeneration, "MAX_CENTRAL_DIRECTORY_BYTES", 1)
    assert find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    ) is None


def test_cancellation_and_read_time_budgets_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "payload.bin", b"payload")
    archive = _write_wheel(root, "payload.bin", b"payload")
    calls = 0

    def cancel() -> None:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise RuntimeError("fixture cancellation")

    with pytest.raises(RuntimeError, match="fixture cancellation"):
        find_regeneration_proof(
            snapshot_path(candidate),
            root=root,
            archive_paths=(archive,),
            cancellation_check=cancel,
        )

    monkeypatch.setattr(regeneration, "MAX_PROOF_READ_BYTES", 1)
    assert find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    ) is None


def test_pyc_source_proof_and_forged_digest_never_revalidate(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    source = root / "cache_source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    candidate = root / "__pycache__" / "cache_source.cpython-314.pyc"
    candidate.parent.mkdir()
    py_compile.compile(source, cfile=candidate, doraise=True)
    proof = find_regeneration_proof(snapshot_path(candidate), root=root)
    assert proof is not None
    assert proof.method == regeneration.PYC_METHOD
    assert proof.source_member == "cache_source.py"
    assert revalidate_regeneration_proof(proof, root=root)

    hash_source = root / "hash_source.py"
    hash_source.write_text("value = 3\n", encoding="utf-8")
    hash_candidate = root / "__pycache__" / "hash_source.cpython-314.pyc"
    py_compile.compile(
        hash_source,
        cfile=hash_candidate,
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    hash_proof = find_regeneration_proof(snapshot_path(hash_candidate), root=root)
    assert hash_proof is not None
    assert revalidate_regeneration_proof(hash_proof, root=root)

    source.write_text("value = 2\n", encoding="utf-8")
    assert not revalidate_regeneration_proof(proof, root=root)

    candidate = _candidate(root, "payload.bin", b"ordinary payload")
    archive = _write_wheel(root, "payload.bin", candidate.read_bytes(), name="ordinary.whl")
    proof = find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    )
    assert proof is not None
    forged = replace(proof, candidate_sha256="0" * 64)
    assert not revalidate_regeneration_proof(forged, root=root)

    no_archive_proof = RegenerationProof(
        candidate=proof.candidate,
        candidate_sha256=proof.candidate_sha256,
        witnesses=proof.witnesses,
        witness_sha256=proof.witness_sha256,
        method="pyc-source-compile-v1",
        source_member="pkg/source.py",
    )
    assert not revalidate_regeneration_proof(no_archive_proof, root=root)


def test_pyc_compile_uses_bounded_private_copy_and_never_executes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    marker = root / "executed.marker"
    source = root / "marker_source.py"
    source.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "value = 1\n",
        encoding="utf-8",
    )
    candidate = root / "__pycache__" / "marker_source.cpython-314.pyc"
    candidate.parent.mkdir()
    py_compile.compile(source, cfile=candidate, doraise=True)
    assert not marker.exists()

    calls: dict[str, object] = {}
    original_compile = regeneration.py_compile.compile

    def compile_probe(file, *args, **kwargs):
        calls["file"] = Path(file)
        calls["dfile"] = kwargs.get("dfile")
        # Change the original immediately before the product compile call.  A
        # correct implementation compiles its already bounded private copy and
        # then abstains at the post-fence check.
        source.write_text("value = 2\n", encoding="utf-8")
        return original_compile(file, *args, **kwargs)

    monkeypatch.setattr(regeneration.py_compile, "compile", compile_probe)
    assert find_regeneration_proof(snapshot_path(candidate), root=root) is None
    assert calls["file"] != source
    assert calls["dfile"] == str(source)
    assert Path(calls["file"]).parent != source.parent
    assert not marker.exists()


def test_empty_archive_list_does_not_read_candidate_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "unknown.bin", b"payload")

    def unexpected_hash(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("candidate payload must not be read without a witness")

    monkeypatch.setattr(regeneration, "_hash_snapshot", unexpected_hash)
    assert find_regeneration_proof(snapshot_path(candidate), root=root, archive_paths=()) is None


@pytest.mark.parametrize(
    "exception_factory",
    [
        lambda: CancellationRequested("cancelled"),
        lambda: RuntimeError("runtime callback failure"),
        lambda: ValueError("value callback failure"),
    ],
    ids=["cancellation-requested", "runtime-error", "value-error"],
)
def test_find_propagates_original_callback_exception_identity(
    tmp_path: Path, exception_factory,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "payload.bin", b"payload")
    archive = _write_wheel(root, "payload.bin", b"payload")
    original = exception_factory()

    def callback() -> None:
        raise original

    with pytest.raises(type(original)) as raised:
        find_regeneration_proof(
            snapshot_path(candidate), root=root, archive_paths=(archive,),
            cancellation_check=callback,
        )
    assert raised.value is original


@pytest.mark.parametrize(
    "exception_factory",
    [
        lambda: CancellationRequested("cancelled"),
        lambda: RuntimeError("runtime callback failure"),
        lambda: ValueError("value callback failure"),
    ],
    ids=["cancellation-requested", "runtime-error", "value-error"],
)
def test_revalidate_propagates_original_callback_exception_identity(
    tmp_path: Path, exception_factory,
) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    candidate = _candidate(root, "payload.bin", b"payload")
    archive = _write_wheel(root, "payload.bin", b"payload")
    proof = find_regeneration_proof(
        snapshot_path(candidate), root=root, archive_paths=(archive,)
    )
    assert proof is not None
    original = exception_factory()

    def callback() -> None:
        raise original

    with pytest.raises(type(original)) as raised:
        revalidate_regeneration_proof(proof, root=root, cancellation_check=callback)
    assert raised.value is original
