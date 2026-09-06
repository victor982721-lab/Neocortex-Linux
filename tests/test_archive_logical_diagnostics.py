from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from neocortex.capabilities.formats.archive import state as archive_state
from neocortex.capabilities.formats.archive.logical import (
    identify_logical_document,
    issue_diagnosis,
)
from neocortex.capabilities.formats.archive.route import (
    ARCHIVE_MIME,
    ArchiveRoute,
    ArchiveRouteConfig,
    _zip_document_kind,
)
from neocortex.capabilities.formats.archive.state import (
    archive_database,
    initialize_archive_state,
    list_archive_issues,
    list_archive_logical_documents,
    list_archive_members,
    read_archive_status,
    search_archive_state,
)
from neocortex.deduplication import snapshot_path
from neocortex.runtime.control.cancellation import CancellationToken
from neocortex.safety.route_filters import CandidateSelection


TEST_CAPABILITIES = ("base",)
OTT_MIME = "application/vnd.oasis.opendocument.text-template"


class _Framework:
    def __init__(self, paths: tuple[Path, ...]):
        self.candidates = tuple(snapshot_path(path) for path in paths)

    def selected_route_candidate_counts(
        self,
        _run_id: int,
        mime: str,
        max_file_bytes: int | None,
        route_name: str,
        _selection: CandidateSelection,
    ) -> tuple[int, int]:
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        return len(self.candidates), sum(
            max_file_bytes is None or item.size <= max_file_bytes for item in self.candidates
        )

    def iter_selected_route_candidates(
        self,
        _run_id: int,
        mime: str,
        route_name: str,
        _selection: CandidateSelection,
    ):
        assert mime == ARCHIVE_MIME
        assert route_name == "archive"
        yield from self.candidates


def _zip(entries: dict[str, str | bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _odf(
    mime: str = OTT_MIME, *, content: str = "<document>plantilla recuperada</document>"
) -> bytes:
    return _zip(
        {
            "mimetype": mime,
            "content.xml": content,
            "styles.xml": "<styles>formato</styles>",
            "META-INF/manifest.xml": "<manifest/>",
        }
    )


def _run(state: Path, *sources: Path, **limits):
    return ArchiveRoute(
        ArchiveRouteConfig(state, ocr_mode="never", **limits),
        _Framework(sources),  # type: ignore[arg-type]
        1,
        cancellation=CancellationToken(),
    ).run()


def test_outer_ott_reclaims_one_logical_unit_without_rename_or_integrity_claim(
    tmp_path: Path,
) -> None:
    source = tmp_path / "f17697256.zip"
    source.write_bytes(_odf())
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    state = tmp_path / "archive.sqlite3"

    first = _run(state, source)
    observations = list_archive_logical_documents(state)
    assert len(observations) == 1
    observation = observations[0]
    assert observation.member_chain == ""
    assert observation.virtual_path == str(source)
    assert observation.physical_media_type == "application/zip"
    assert observation.declared_mime == OTT_MIME
    assert observation.logical_kind == "ott"
    assert observation.proposed_extension == ".ott"
    assert observation.identification_status == "identified"
    assert observation.evidence == ("mimetype", "content.xml", "META-INF/manifest.xml")
    assert observation.integrity_status == observation.opening_status == "not_verified"
    assert observation.independently_organizable
    assert not observation.independently_disposable
    members = list_archive_members(state)
    root = next(item for item in members if item.member_chain == "")
    assert root.virtual_path == str(source)
    assert root.file_key.startswith("archive:")
    assert root.content_kind == "ott"
    assert root.media_type == OTT_MIME
    assert root.document_role == "logical_document"
    components = [item for item in members if item.member_chain]
    assert len(components) == first.members_seen == 4
    assert all(item.document_role == "document_component" for item in components)
    assert all(item.logical_document_chain == "" for item in components)
    assert not any(
        item.independently_organizable or item.independently_disposable for item in components
    )
    assert (
        next(item for item in components if item.member_chain == "mimetype").media_type
        == "text/plain"
    )
    assert [item.file_key for item in search_archive_state(state, "plantilla")] == [root.file_key]
    assert len(search_archive_state(state, "plantilla", include_components=True)) == 2
    issue = list_archive_issues(state).items[0]
    assert issue.reason_code == "archive_logical_extension_mismatch"
    assert issue.coverage_impact == "identification_only"
    assert issue.recoverability == "review_identification"
    assert json.loads(issue.detail)["effect_authorized"] is False
    assert first.containers_complete == 1
    assert first.containers_partial == first.errors == 0

    replay = _run(state, source)
    assert replay.cache_hits == 1
    assert list_archive_logical_documents(state) == observations
    assert list_archive_members(state) == members
    assert list_archive_issues(state).items == (issue,)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    assert not source.with_suffix(".ott").exists()


def test_nested_subtypes_are_individual_and_mismatches_do_not_rename(tmp_path: Path) -> None:
    source = tmp_path / "batch.zip"
    source.write_bytes(
        _zip(
            {
                "template.zip": _odf(),
                "incorrect.odt": _odf(),
                "correct.ott": _odf(),
                "spreadsheet.zip": _odf("application/vnd.oasis.opendocument.spreadsheet"),
                "ordinary.zip": _zip({"content.xml": "<x>plain XML</x>"}),
            }
        )
    )
    state = tmp_path / "archive.sqlite3"
    _run(state, source)
    observations = {item.member_chain: item for item in list_archive_logical_documents(state)}
    assert set(observations) == {"template.zip", "incorrect.odt", "correct.ott", "spreadsheet.zip"}
    assert observations["template.zip"].logical_kind == "ott"
    assert observations["spreadsheet.zip"].logical_kind == "ods"
    hits = search_archive_state(state, "plantilla")
    assert len(hits) == 4
    assert all(item.document_role == "logical_document" for item in hits)
    assert all(
        item.independently_organizable and not item.independently_disposable for item in hits
    )
    assert {
        item.member_chain
        for item in list_archive_issues(
            state, reason_code="archive_logical_extension_mismatch"
        ).items
    } == {"template.zip", "incorrect.odt", "spreadsheet.zip"}
    assert _zip_document_kind(_odf()) == "ott"
    assert _zip_document_kind(_zip({"mimetype": OTT_MIME})) == "archive"


@pytest.mark.parametrize("mime", [OTT_MIME, "application/unknown+zip"])
def test_declaration_without_structure_never_becomes_confirmed_document(
    tmp_path: Path, mime: str
) -> None:
    source = tmp_path / "candidate.zip"
    source.write_bytes(_zip({"mimetype": mime, "note.txt": "not a complete package"}))
    state = tmp_path / "archive.sqlite3"
    _run(state, source)
    observation = list_archive_logical_documents(state)[0]
    assert observation.declared_mime == mime
    assert observation.logical_kind is observation.proposed_extension is None
    assert not observation.independently_organizable
    assert observation.identification_status != "identified"
    assert not any(
        item.document_role == "document_component" for item in list_archive_members(state)
    )


def test_minimum_member_names_do_not_claim_xml_integrity_or_openability(tmp_path: Path) -> None:
    source = tmp_path / "broken-content.zip"
    source.write_bytes(_odf(content="<document>truncated"))
    state = tmp_path / "archive.sqlite3"
    summary = _run(state, source)
    observation = list_archive_logical_documents(state)[0]
    assert observation.logical_kind == "ott"
    assert observation.integrity_status == observation.opening_status == "not_verified"
    assert summary.containers_partial == 1
    issues = list_archive_issues(state, member_fragment="content.xml").items
    assert len(issues) == 1
    assert issues[0].reason_code == "archive_text_decode_error"
    assert issues[0].recoverability == "not_verified"


def test_mimetype_identification_has_hard_read_bound(tmp_path: Path) -> None:
    source = tmp_path / "mime-bomb.zip"
    source.write_bytes(
        _zip(
            {
                "mimetype": "x" * 513,
                "content.xml": "<x/>",
                "META-INF/manifest.xml": "<x/>",
            }
        )
    )
    state = tmp_path / "archive.sqlite3"
    _run(state, source)
    assert not list_archive_logical_documents(state)
    issues = list_archive_issues(state, reason_code="archive_logical_mimetype_unreadable").items
    assert len(issues) == 1
    assert "limit is 512" in issues[0].detail


def test_duplicate_declarations_and_marker_names_abstain(tmp_path: Path) -> None:
    evidence = identify_logical_document(
        ("mimetype", "content.xml", "content.xml", "META-INF/manifest.xml"), OTT_MIME
    )
    assert evidence is not None
    assert evidence.identification_status == "ambiguous_structure"
    assert evidence.logical_kind is None
    source = tmp_path / "ambiguous.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("mimetype", OTT_MIME)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<x/>")
        archive.writestr("META-INF/manifest.xml", "<x/>")
    state = tmp_path / "archive.sqlite3"
    _run(state, source)
    assert not list_archive_logical_documents(state)
    assert list_archive_issues(state, reason_code="archive_logical_ambiguous_mimetype").items


def test_issue_keyset_is_scoped_filtered_and_revision_bound(tmp_path: Path) -> None:
    source = tmp_path / "scope%_one.zip"
    source.write_bytes(_zip({f"../bad-{index}": "payload" for index in range(7)}))
    other = tmp_path / "scopeXXone.zip"
    other.write_bytes(_zip({"../other": "payload"}))
    state = tmp_path / "archive.sqlite3"
    _run(state, source, other)
    filters = {"container_fragment": "scope%_one", "reason_code": "archive_unsafe_member_name"}
    first = list_archive_issues(state, 2, **filters)
    assert len(first.items) == 2 and first.next_cursor
    assert first.scope["population"] == "published_archive_issues_only"
    assert all(item.container_path == str(source) for item in first.items)
    assert all(item.coverage_impact == "member_excluded" for item in first.items)
    assert all(item.recoverability == "manual_review_required" for item in first.items)
    ids = [item.issue_id for item in first.items]
    cursor = first.next_cursor
    while cursor:
        page = list_archive_issues(state, 2, cursor=cursor, **filters)
        ids.extend(item.issue_id for item in page.items)
        cursor = page.next_cursor
    assert len(ids) == len(set(ids)) == 7
    assert ids == sorted(ids)
    assert len(list_archive_issues(state, coverage_impact="member_excluded").items) == 8
    assert len(list_archive_issues(state, member_fragment="bad-3").items) == 1
    assert not list_archive_issues(state, recoverability="credentials_required").items
    with pytest.raises(ValueError, match="scope"):
        list_archive_issues(state, 2, cursor=first.next_cursor)
    with pytest.raises(ValueError, match="cursor"):
        list_archive_issues(state, 2, cursor="not-cursor", **filters)
    _run(state, source, other)
    with pytest.raises(ValueError, match="revision"):
        list_archive_issues(state, 2, cursor=first.next_cursor, **filters)


def _v1_state(path: Path) -> None:
    with archive_database(path) as connection:
        archive_state._create_archive_v1_schema(connection)
        connection.execute("INSERT INTO metadata VALUES('schema_version','1')")
        connection.commit()


def test_reader_does_not_migrate_v1_and_writer_migrates_atomically(tmp_path: Path) -> None:
    state = tmp_path / "legacy.sqlite3"
    _v1_state(state)
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="migration required"):
        list_archive_issues(state)
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
    assert not Path(f"{state}-wal").exists()
    assert not Path(f"{state}-shm").exists()
    initialize_archive_state(state)
    assert read_archive_status(state).schema_version == 2
    assert not list_archive_logical_documents(state)
    initialize_archive_state(state)
    assert not list_archive_issues(state).items


def test_migration_failure_rolls_back_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "legacy.sqlite3"
    _v1_state(state)
    monkeypatch.setattr(
        archive_state, "_ARCHIVE_V2_DDL", (*archive_state._ARCHIVE_V2_DDL, "INVALID SQL")
    )
    with pytest.raises(sqlite3.OperationalError):
        initialize_archive_state(state)
    with archive_database(state, readonly=True) as connection:
        assert (
            connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[
                0
            ]
            == "1"
        )
        assert "document_role" not in {
            row[1] for row in connection.execute("PRAGMA table_info(documents)")
        }


def test_future_or_corrupt_schema_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "future.sqlite3"
    initialize_archive_state(state)
    with archive_database(state, create=False) as connection:
        connection.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        connection.commit()
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="newer"):
        initialize_archive_state(state)
    with pytest.raises(RuntimeError, match="incompatible"):
        list_archive_logical_documents(state)
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
    corrupt = tmp_path / "corrupt.sqlite3"
    _v1_state(corrupt)
    with archive_database(corrupt, create=False) as connection:
        connection.execute("DROP INDEX archive_documents_status_idx")
        connection.commit()
    with pytest.raises(RuntimeError):
        initialize_archive_state(corrupt)


def test_recovery_labels_never_equate_protection_or_limits_with_corruption() -> None:
    assert issue_diagnosis("archive_encrypted_member") == (
        "member_text_unavailable",
        "credentials_required",
    )
    assert issue_diagnosis("archive_depth_limit") == ("coverage_limited", "bounded_retry_review")
    assert issue_diagnosis("unknown_reason") == ("member_text_unavailable", "not_verified")


def test_outer_extension_change_recomputes_identification_issue_not_source_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.zip"
    source.write_bytes(_odf())
    state = tmp_path / "archive.sqlite3"
    _run(state, source)
    key = list_archive_logical_documents(state)[0].container_key
    new_path = source.with_suffix(".ott")
    source.rename(new_path)
    rerun = _run(state, new_path)
    assert rerun.cache_hits == 0
    assert list_archive_logical_documents(state)[0].container_key == key
    assert not list_archive_issues(state, reason_code="archive_logical_extension_mismatch").items


def test_container_failures_are_queryable_without_conflating_member_issues(tmp_path: Path) -> None:
    source = tmp_path / "damaged.zip"
    source.write_bytes(b"PK\x03\x04truncated")
    state = tmp_path / "archive.sqlite3"
    summary = _run(state, source)
    assert summary.errors == 1
    issue = list_archive_issues(state).items[0]
    assert issue.member_chain is None
    assert issue.reason_code == "archive_corrupt_container"
    assert issue.coverage_impact == "container_unavailable"
    assert issue.recoverability == "not_verified"
    assert read_archive_status(state).issues == summary.safety_issues == 1
    replay = _run(state, source)
    assert replay.cached_errors == 1
    assert replay.safety_issues == 1
    assert list_archive_issues(state).items == (issue,)


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.5])
def test_invalid_issue_limits_do_not_create_state(tmp_path: Path, limit: int) -> None:
    state = tmp_path / "absent.sqlite3"
    with pytest.raises(ValueError, match="limit"):
        list_archive_issues(state, limit)
    assert not state.exists()


def test_competing_package_markers_preserve_declaration_without_selecting_kind() -> None:
    observation = identify_logical_document(
        (
            "mimetype",
            "content.xml",
            "META-INF/manifest.xml",
            "[Content_Types].xml",
            "word/document.xml",
        ),
        OTT_MIME,
    )
    assert observation is not None
    assert observation.declared_mime == OTT_MIME
    assert observation.logical_kind is None
    assert observation.identification_status == "ambiguous_structure"


def test_nested_document_budget_keeps_specific_coverage_reason(tmp_path: Path) -> None:
    source = tmp_path / "bounded.zip"
    source.write_bytes(_zip({"template.zip": _odf()}))
    state = tmp_path / "archive.sqlite3"
    _run(state, source, max_members=2)
    observation = list_archive_logical_documents(state)[0]
    assert observation.logical_kind == "ott"
    issues = list_archive_issues(state, reason_code="archive_member_count_limit").items
    assert len(issues) == 1
    assert issues[0].member_chain == "template.zip"
    assert issues[0].coverage_impact == "coverage_limited"
    assert issues[0].recoverability == "bounded_retry_review"


def test_read_queries_do_not_change_v2_owner_bytes_or_materialize_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "package.zip"
    source.write_bytes(_odf())
    state = tmp_path / "archive.sqlite3"
    _run(state, source)
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    list_archive_logical_documents(state)
    list_archive_issues(state)
    search_archive_state(state, "plantilla")
    list_archive_members(state)
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before
    assert not Path(f"{state}-wal").exists()
    assert not Path(f"{state}-shm").exists()


def test_unversioned_nonempty_owner_is_not_implicitly_repaired(tmp_path: Path) -> None:
    state = tmp_path / "unversioned.sqlite3"
    _v1_state(state)
    with archive_database(state, create=False) as connection:
        connection.execute("DELETE FROM metadata WHERE key='schema_version'")
        connection.commit()
    before = hashlib.sha256(state.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="schema_version"):
        initialize_archive_state(state)
    assert hashlib.sha256(state.read_bytes()).hexdigest() == before


def test_issue_physical_path_scope_has_exact_case_sensitive_component_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "A"
    child = root / "child"
    neighbor = tmp_path / "A-extra"
    differently_cased = tmp_path / "a"
    for directory in (child, neighbor, differently_cased):
        directory.mkdir(parents=True)
    main = root / "bundle.zip"
    main.write_bytes(
        _zip(
            {
                "outside-name/inner.zip": _odf(),
                "ordinary.zip": _zip({"deep.zip": _odf()}),
            }
        )
    )
    child_file = child / "bundle.zip"
    child_file.write_bytes(_zip({"template.zip": _odf()}))
    neighbor_file = neighbor / "bundle.zip"
    neighbor_file.write_bytes(_odf())
    lower_file = differently_cased / "bundle.zip"
    lower_file.write_bytes(_odf())
    state = tmp_path / "archive.sqlite3"
    _run(state, main, child_file, neighbor_file, lower_file)

    scoped = list_archive_issues(state, path_scope=str(root))
    assert {item.container_path for item in scoped.items} == {str(main), str(child_file)}
    assert {item.member_chain for item in scoped.items} == {
        "outside-name/inner.zip",
        "ordinary.zip!/deep.zip",
        "template.zip",
    }
    assert len(scoped.items) == 3
    assert scoped.scope["filters"]["path_scope"] == str(root)
    assert list_archive_issues(state, path_scope=f"{root}/").items == scoped.items
    exact = list_archive_issues(state, path_scope=str(main))
    assert len(exact.items) == 2
    assert all(item.container_path == str(main) for item in exact.items)
    assert any(item.archive_depth == 2 for item in exact.items)
    assert len(list_archive_issues(state, path_scope="/").items) == 5


def test_issue_container_key_is_exact_and_intersects_physical_scope(tmp_path: Path) -> None:
    root = tmp_path / "A"
    root.mkdir()
    first = root / "one.zip"
    second = tmp_path / "two.zip"
    first.write_bytes(_zip({"inside.zip": _odf()}))
    second.write_bytes(_odf())
    state = tmp_path / "archive.sqlite3"
    _run(state, first, second)
    all_items = list_archive_issues(state).items
    key = next(item.container_key for item in all_items if item.container_path == str(first))
    other_key = next(item.container_key for item in all_items if item.container_path == str(second))
    page = list_archive_issues(state, container_key=key)
    assert len(page.items) == 1
    assert page.items[0].container_path == str(first)
    assert page.items[0].member_chain == "inside.zip"
    assert page.scope["filters"]["container_key"] == key
    assert not list_archive_issues(state, container_key=key[:-1]).items
    assert not list_archive_issues(state, path_scope=str(root), container_key=other_key).items
    assert list_archive_issues(state, path_scope=str(root), container_key=key).items == page.items


def test_issue_cursor_binds_both_new_physical_scope_filters(tmp_path: Path) -> None:
    root = tmp_path / "A"
    root.mkdir()
    source = root / "bundle.zip"
    source.write_bytes(_zip({"one.zip": _odf(), "two.zip": _odf()}))
    state = tmp_path / "archive.sqlite3"
    _run(state, source)
    first = list_archive_issues(state, 1, path_scope=str(root))
    assert first.next_cursor
    key = first.items[0].container_key
    with pytest.raises(ValueError, match="scope"):
        list_archive_issues(state, 1, path_scope=f"{root}-extra", cursor=first.next_cursor)
    with pytest.raises(ValueError, match="scope"):
        list_archive_issues(
            state, 1, path_scope=str(root), container_key=key, cursor=first.next_cursor
        )
    second = list_archive_issues(state, 1, path_scope=str(root), cursor=first.next_cursor)
    assert len(second.items) == 1
    assert second.items[0].issue_id != first.items[0].issue_id
    assert second.next_cursor is None
    exact = list_archive_issues(state, 1, path_scope=str(root), container_key=key)
    assert exact.next_cursor
    with pytest.raises(ValueError, match="scope"):
        list_archive_issues(
            state, 1, path_scope=str(root), container_key=f"{key}x", cursor=exact.next_cursor
        )


@pytest.mark.parametrize(
    "path_scope", ["", "relative/A", "/fixture/../A", "/fixture/./A", "/bad\x00path"]
)
def test_invalid_physical_issue_scope_never_opens_or_creates_state(
    tmp_path: Path, path_scope: str
) -> None:
    state = tmp_path / "absent.sqlite3"
    with pytest.raises(ValueError, match="absolute Linux"):
        list_archive_issues(state, path_scope=path_scope)
    assert not state.exists()
