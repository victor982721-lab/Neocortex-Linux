"""The public lexical search binds every default owner, including Video."""

from __future__ import annotations

from pathlib import Path

import pytest

from neocortex.api.cli.cli_app import dispatch_direct
from neocortex.api.cli.cli_parser import build_parser
from neocortex.api.cli.cli_validation import validate_arguments
from neocortex.capabilities.formats.audio.state import initialize_audio_state
from neocortex.capabilities.formats.docx.state import initialize_docx_state
from neocortex.capabilities.formats.office.state import initialize_office_state
from neocortex.capabilities.formats.pdf.pdf_state import initialize_pdf_state
from neocortex.capabilities.formats.text.text_state import initialize_text_state
from neocortex.capabilities.formats.video.state import initialize_video_state, video_database
from neocortex.safety.protected_content import ProtectedContentPolicy
from neocortex.semantic import semantic_service as service
from neocortex.semantic.semantic_lexical import LexicalAvailability
from neocortex.semantic.semantic_sources import semantic_source_database
from tests.internal_paths_test_support import disjoint_internal_paths_policy


TEST_CAPABILITIES = ("base", "inference")
pytestmark = pytest.mark.capability("base", "inference")


@pytest.fixture(autouse=True)
def _isolated_owner_policies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = disjoint_internal_paths_policy(tmp_path.parent / f"{tmp_path.name}-policy")
    monkeypatch.setattr(
        "neocortex.safety.internal_paths.canonical_internal_paths_policy", lambda: policy
    )
    monkeypatch.setattr(
        "neocortex.safety.protected_content.canonical_protected_content_policy",
        lambda: ProtectedContentPolicy.capture(()),
    )

    def no_inference(*_args, **_kwargs):
        raise AssertionError("lexical-only search must not load an inference backend")

    monkeypatch.setattr(service, "_backend", no_inference)


def _initialize_owners(root: Path, *, include_video: bool = True) -> None:
    for name, initialize in (
        ("pdf.sqlite3", initialize_pdf_state),
        ("docx.sqlite3", initialize_docx_state),
        ("office.sqlite3", initialize_office_state),
        ("audio.sqlite3", initialize_audio_state),
        ("text.sqlite3", initialize_text_state),
    ):
        initialize(root / name)
    if include_video:
        initialize_video_state(semantic_source_database(root, "video"))


def _publish_video_match(root: Path) -> None:
    with video_database(semantic_source_database(root, "video"), create=False) as connection:
        connection.execute(
            """INSERT INTO documents(file_key,path,mime,size,mtime_ns,birthtime_ns,
                processing_signature,status,title,duration_seconds,frame_count,
                ocr_frame_count,ocr_text_chars,last_seen_run_id,updated_ns)
            VALUES('1:2','/fixtures/inspection.mp4','video/mp4',42,100,-1,
                'video-fixture-v1','complete','Inspection',1.0,1,1,10,1,1)"""
        )
        connection.execute(
            """INSERT INTO frames(file_key,frame_index,timestamp_ms,sampling_reasons_json,
                width,height,content_xxh3_128,ocr_available,ocr_text,ocr_mean_confidence)
            VALUES('1:2',0,250,'["interval"]',640,480,
                '0123456789abcdef0123456789abcdef',1,'AGUAMARINA',99.0)"""
        )
        connection.execute(
            """INSERT INTO frame_fts(file_key,path,title,timestamp_ms,body)
            VALUES('1:2','/fixtures/inspection.mp4','Inspection',250,'AGUAMARINA')"""
        )
        connection.commit()


def _dispatch_lexical(root: Path) -> int:
    args = build_parser().parse_args(
        [
            "--state-directory",
            str(root),
            "--semantic-search",
            "AGUAMARINA",
            "--semantic-search-mode",
            "lexical",
        ]
    )
    validate_arguments(args)
    result = dispatch_direct(args)
    assert result is not None
    return result


@pytest.mark.parametrize("matching_video", (False, True))
def test_default_video_owner_is_available_and_public_cli_is_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], matching_video: bool
) -> None:
    _initialize_owners(tmp_path)
    if matching_video:
        _publish_video_match(tmp_path)

    result = service.search_semantic_index(
        tmp_path, "AGUAMARINA", include_text=False, include_images=False
    )
    video = next(ranking for ranking in result.lexical_rankings if ranking.source_kind == "video")
    assert video.state_path == semantic_source_database(tmp_path, "video")
    assert video.availability is LexicalAvailability.AVAILABLE
    assert len(video.hits) == int(matching_video)
    assert len(result.fused) == int(matching_video)
    assert result.complete
    if matching_video:
        assert video.hits[0].section_kind == "video_frame_ocr"
        assert video.hits[0].path == "/fixtures/inspection.mp4"

    assert _dispatch_lexical(tmp_path) == 0
    output = capsys.readouterr().out
    assert (
        f"LEXICAL_RANKING name=fts_video availability=available hits={int(matching_video)}"
        in output
    )


def test_missing_default_video_owner_stays_incomplete_and_is_not_created(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _initialize_owners(tmp_path, include_video=False)
    result = service.search_semantic_index(
        tmp_path, "AGUAMARINA", include_text=False, include_images=False
    )
    video = next(ranking for ranking in result.lexical_rankings if ranking.source_kind == "video")
    assert video.availability is LexicalAvailability.DATABASE_MISSING
    assert video.unavailable_reason == "state_database_missing"
    assert not result.complete
    assert _dispatch_lexical(tmp_path) == 2
    assert "LEXICAL_RANKING name=fts_video availability=database_missing" in capsys.readouterr().out
    assert not semantic_source_database(tmp_path, "video").exists()
