"""The integrated Archive route uses the same interest fence after spawning."""

from __future__ import annotations

import pickle
import zipfile
from pathlib import Path

from neocortex.capabilities.formats.archive.route import ArchiveMemberAdmissionContext, ArchiveRoute
from neocortex.capabilities.formats.archive.state import archive_database
from neocortex.runtime.config.application_config_projections import archive_route_config_from_application
from neocortex.runtime.models import FrameworkConfig
from tests.test_archive_corpus_admission import _Framework, _zip


def test_integrated_archive_projection_retains_docs_not_foreign_code_or_secrets(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    container = root / "mixed.zip"
    container.write_bytes(_zip({
        "vendor/library.py": "SHOULD_NOT_BE_INDEXED = 1\n",
        "AppData/notes.md": "Información técnica conservada\n",
        ".codex/auth.json": '{"fixture_private":"not indexed"}',
        "nested.zip": _zip({"foreign.js": "const PRIVATE_CODE = 1;", "report.txt": "Informe interior"}),
    }))
    config = FrameworkConfig(
        root=root,
        state_directory=tmp_path / "state",
        route="archive",
        code_project_roots=(root / "owned",),
        archive_ocr_mode="never",
        document_catalog_enabled=False,
    )
    archive_config = archive_route_config_from_application(config)
    archive_config = pickle.loads(pickle.dumps(archive_config))
    summary = ArchiveRoute(archive_config, _Framework(container), 1).run()
    assert summary.errors == 0
    with archive_database(config.archive_database, readonly=True) as connection:
        rows = {
            row["member_chain"]: (row["status"], row["text_zlib"])
            for row in connection.execute("SELECT member_chain,status,text_zlib FROM documents")
        }
    for name in ("vendor/library.py", ".codex/auth.json", "nested.zip!/foreign.js"):
        assert rows[name] == ("metadata_only", None)
    assert rows["AppData/notes.md"][1] is not None
    assert rows["nested.zip!/report.txt"][1] is not None
    with zipfile.ZipFile(container) as original:
        assert original.read("vendor/library.py") == b"SHOULD_NOT_BE_INDEXED = 1\n"


def test_archive_projection_signs_and_transmits_explicit_code_opt_ins(tmp_path: Path) -> None:
    owned = tmp_path / "owned"
    signatures = set()
    for enabled in (False, True):
        config = FrameworkConfig(
            code_project_roots=(owned,),
            code_include_generated=enabled,
            code_include_vendored=enabled,
        )
        projected = pickle.loads(pickle.dumps(archive_route_config_from_application(config)))
        signatures.add(projected.member_admission_signature)
        for member_name in ("vendor/library.py", "build/generated.py"):
            context = ArchiveMemberAdmissionContext(
                member_name=member_name, member_chain=member_name, depth=0,
                container_path=owned / "sources.zip", size=10, compressed_size=10,
                crc32=0, prefix=b"VALUE = 1\n",
            )
            assert projected.member_admission(context).disposition == (
                "process" if enabled else "metadata_only"
            )
    assert len(signatures) == 2
