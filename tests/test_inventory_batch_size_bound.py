"""Hard inventory batch-size bound regressions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import tools.benchmark_curation_scale as benchmark_module
from neocortex.deduplication import DedupIndex
from neocortex.deduplication.inventory import scanner as scanner_module


def test_inventory_batch_size_rejects_before_scan_creation(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "fixture.bin").write_bytes(b"fixture")
    database = tmp_path / "state.sqlite3"

    with DedupIndex(database) as index:
        with patch.object(scanner_module.RootIdentity, "capture") as capture:
            with pytest.raises(
                ValueError,
                match=rf"batch_size must be between 1 and {scanner_module.MAX_BATCH_SIZE}",
            ):
                index.scan(
                    corpus,
                    batch_size=scanner_module.MAX_BATCH_SIZE + 1,
                    excluded_paths=(),
                )
        capture.assert_not_called()
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone() == (0,)
        assert index._connection.execute("SELECT COUNT(*) FROM files").fetchone() == (0,)


@pytest.mark.parametrize("value", (0, -1, True, False, 1.0, "512"))
def test_inventory_batch_size_rejects_invalid_values_without_scan_creation(
    tmp_path: Path,
    value: object,
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    database = tmp_path / "state.sqlite3"

    with DedupIndex(database) as index:
        with pytest.raises(ValueError, match="batch_size must be between"):
            index.scan(corpus, batch_size=value, excluded_paths=())  # type: ignore[arg-type]
        assert index._connection.execute("SELECT COUNT(*) FROM scans").fetchone() == (0,)


def test_benchmark_and_inventory_use_the_same_batch_cap() -> None:
    assert benchmark_module.MAX_BATCH_SIZE == scanner_module.MAX_BATCH_SIZE
    assert benchmark_module.validate_config(
        count=1,
        batch_size=scanner_module.MAX_BATCH_SIZE,
    ).batch_size == scanner_module.MAX_BATCH_SIZE
    with pytest.raises(ValueError, match="between 1 and"):
        benchmark_module.validate_config(
            count=1,
            batch_size=scanner_module.MAX_BATCH_SIZE + 1,
        )
