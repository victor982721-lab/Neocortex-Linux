"""Catalog computation overlaps, while SQLite effects stay caller-owned."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
from unittest.mock import patch
import zlib

import pytest

from neocortex.documents import document_catalog as catalog
from neocortex.documents import document_catalog_workers as workers
from neocortex.documents import document_taxonomy as taxonomy_module
from neocortex.runtime.control.cancellation import CancellationRequested, CancellationToken
from tests.test_catalog_binding_write_amplification import _source
from tests.test_document_catalog_multimodal import _make_image_owner


def _two_sources(tmp_path):
    root, docx = _source(tmp_path, 1)
    image_path = root / "image.png"
    image_path.write_bytes(b"image fixture")
    image = tmp_path / "image.sqlite3"
    _make_image_owner(image, image_path)
    return root, docx, image


def test_independent_sources_compute_together_and_serialize_sql_effects(tmp_path, monkeypatch):
    root, docx, image = _two_sources(tmp_path)
    target = tmp_path / "catalog.sqlite3"
    catalog.initialize_document_catalog(target)
    ready = threading.Barrier(2)
    original_classify = catalog.classify_document
    original_store = catalog._store_classification
    classified_threads = set()
    writer_threads = set()

    def together(*args, **kwargs):
        classified_threads.add(threading.get_ident())
        ready.wait(timeout=5)
        return original_classify(*args, **kwargs)

    def serial_writer(connection, *args, **kwargs):
        original_store(connection, *args, **kwargs)
        writer_threads.add(threading.get_ident())
        assert connection.in_transaction
        # The exact write boundary must exclude another SQLite writer even
        # though the classification callbacks above overlap.
        with sqlite3.connect(target, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")

    monkeypatch.setattr(catalog, "classify_document", together)
    monkeypatch.setattr(catalog, "_store_classification", serial_writer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(catalog.update_document_catalog_source, target, path, kind, source_root=root)
                   for path, kind in ((docx, "docx"), (image, "image"))]
        summaries = [future.result(timeout=15) for future in futures]
    assert len(classified_threads) == 2 and writer_threads == classified_threads
    assert [summary.classified for summary in summaries] == [1, 1]
    assert len(catalog.list_catalog_documents(target, limit=10)) == 2


def test_warm_input_validation_does_not_hold_global_catalog_exclusion(tmp_path, monkeypatch):
    root, docx, image = _two_sources(tmp_path)
    target = tmp_path / "catalog.sqlite3"
    catalog.update_document_catalog_source(target, docx, "docx", source_root=root)
    observing = threading.Event()
    other_finished = threading.Event()
    original = catalog._iter_source_documents
    held_once = False

    def hold_replay(connection, source_kind, **kwargs):
        nonlocal held_once
        yield from original(connection, source_kind, **kwargs)
        if source_kind == "docx" and not held_once:
            held_once = True
            observing.set()
            assert other_finished.wait(5), "warm input validation blocked another source publication"

    monkeypatch.setattr(catalog, "_iter_source_documents", hold_replay)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replay = pool.submit(catalog.update_document_catalog_source, target, docx, "docx", source_root=root)
        assert observing.wait(5)
        changed = catalog.update_document_catalog_source(target, image, "image", source_root=root)
        other_finished.set()
        summary = replay.result(timeout=10)
    assert changed.classified == 1
    # A concurrent commit conservatively invalidates the old read fence, so
    # replay may rebuild from verified cache rows instead of issuing a receipt.
    assert summary.cache_hits == 1 and summary.classified == 0
    assert len(catalog.list_catalog_documents(target, limit=10)) == 2


def test_publication_preparation_retries_sibling_commit_without_global_exclusion(tmp_path, monkeypatch):
    root, docx, image = _two_sources(tmp_path)
    target = tmp_path / "catalog.sqlite3"
    prepared = threading.Event()
    sibling_finished = threading.Event()
    original = catalog._prepare_catalog_publication
    observations = []

    def hold_first(connection, build, *args, **kwargs):
        result = original(connection, build, *args, **kwargs)
        observations.append(build.source_kind)
        if build.source_kind == "docx" and observations.count("docx") == 1:
            prepared.set()
            assert sibling_finished.wait(8), "publication preparation held global catalog exclusion"
        return result

    monkeypatch.setattr(catalog, "_prepare_catalog_publication", hold_first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        primary = pool.submit(catalog.update_document_catalog_source, target, docx, "docx", source_root=root)
        assert prepared.wait(8)
        sibling = catalog.update_document_catalog_source(target, image, "image", source_root=root)
        sibling_finished.set()
    result = primary.result(timeout=10)
    assert result.classified == sibling.classified == 1
    # An unrelated source commit no longer invalidates this prepared DOCX
    # publication.  The source-scoped proof still permits both builders to
    # publish without a retry.
    assert observations.count("docx") == 1
    assert len(catalog.list_catalog_documents(target, limit=10)) == 2


class _RecordingGate:
    cancellation = None

    def __init__(self):
        self.buffer_live = False
        self.events = []
        self.delay_first = True

    def worker_capacity(self, **kwargs):
        return 2

    @contextmanager
    def admit(self, estimate, **kwargs):
        class Grant:
            native_env = {}

            def release_cpu(self):
                pass

            def checkpoint(self, **_kwargs):
                pass

        if kwargs.get("phase") == "catalog_classify":
            assert self.buffer_live
            assert estimate >= catalog.CATALOG_RESULT_BUFFER_BYTES
            assert kwargs["io_slots"] == 1 and kwargs["io_device"] is not None
            if self.delay_first:
                self.delay_first = False
                time.sleep(0.1)
        self.events.append((estimate, kwargs))
        yield Grant()

    @contextmanager
    def resident(self, estimate, **kwargs):
        assert estimate == catalog.CATALOG_RESULT_BUFFER_BYTES
        self.buffer_live = True
        gate = self

        class Buffer:
            @contextmanager
            def drain_admission(self, **options):
                with gate.admit(0, **options) as grant:
                    yield grant
        try:
            yield Buffer()
        finally:
            self.buffer_live = False


def _recorded_classification(task):
    destination = Path(task.document.path).parent.parent / "worker-pids.log"
    descriptor = os.open(destination, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
    finally:
        os.close(descriptor)
    return _original_classification_task(task)


# This stable top-level reference is importable by spawn. The parent-only
# patch below replaces the dispatch seam, not the child classifier itself.
_original_classification_task = workers.classify_catalog_task


def test_coordinated_catalog_uses_process_readers_and_one_sqlite_owner(tmp_path, monkeypatch):
    root, source = _source(tmp_path, 12)
    target = tmp_path / "catalog.sqlite3"
    gate = _RecordingGate()
    owner = threading.get_ident()
    writers = []
    original_store = catalog._store_classification

    def owner_store(connection, *args, **kwargs):
        writers.append(threading.get_ident())
        original_store(connection, *args, **kwargs)

    monkeypatch.setattr(catalog, "_store_classification", owner_store)
    monkeypatch.setattr(workers, "classify_catalog_task", _recorded_classification)
    with patch("neocortex.runtime.control.global_resources.resource_gate", return_value=gate):
        summary = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
        replay = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    pids = {int(value) for value in (tmp_path / "worker-pids.log").read_text().splitlines()}
    assert pids and os.getpid() not in pids
    assert summary.classified == 12 and writers == [owner] * 12
    assert replay.publication_state == "unchanged" and replay.cache_hits == 12
    assert not gate.buffer_live
    assert sum(kwargs.get("phase") == "catalog_write" for _, kwargs in gate.events) == 1


@pytest.mark.parametrize("version_name", ("CLASSIFIER_VERSION", "NAMING_VERSION"))
def test_replaced_runtime_identity_classifies_in_its_admitted_owner(tmp_path, monkeypatch, version_name):
    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    gate = _RecordingGate()
    monkeypatch.setattr(taxonomy_module, version_name, "explicit-owner-version-fixture")
    expected = catalog.document_classifier_signature(catalog.load_taxonomy())
    with patch("neocortex.runtime.control.global_resources.resource_gate", return_value=gate):
        summary = catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    assert summary.classified == 1 and summary.errors == 0
    with catalog.document_catalog_database(target, readonly=True) as connection:
        signatures = {row[0] for row in connection.execute("SELECT classifier_signature FROM documents WHERE active=1")}
    assert signatures == {expected}
    assert any(options.get("phase") == "catalog_classify_local" for _, options in gate.events)
    assert not any(options.get("phase") == "catalog_classify" for _, options in gate.events)


def test_worker_rejects_a_job_for_another_classifier_before_reading_source(tmp_path, monkeypatch):
    root, source = _source(tmp_path, 1)
    with catalog._readonly_source(source) as connection:
        document = next(catalog._iter_source_documents(connection, "docx", source_root=root))
    task = workers.CatalogClassificationTask(
        source, document, catalog.load_taxonomy(), 64000, "unavailable-provider-version",
    )

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("a mismatched classifier must not consume source work")

    monkeypatch.setattr("neocortex.persistence.sqlite_immutable.open_immutable_sqlite_connection", forbidden_open)
    with pytest.raises(RuntimeError, match="worker classifier identity differs"):
        workers.classify_catalog_task(task)


def _wrong_identity_classification(task):
    result = _original_classification_task(task)
    return replace(result, classification=replace(result.classification, classifier_signature="different-implementation"))


def test_owner_rejects_mismatched_process_evidence_before_publication(tmp_path, monkeypatch):
    root, source = _source(tmp_path, 1)
    target = tmp_path / "catalog.sqlite3"
    monkeypatch.setattr(workers, "classify_catalog_task", _wrong_identity_classification)
    with pytest.raises(RuntimeError, match="result identity differs from its job"):
        catalog.update_document_catalog_source(target, source, "docx", source_root=root)
    assert catalog.list_catalog_documents(target, limit=10) == ()


def test_compressed_prefix_reads_bounded_input_and_skips_large_unused_tail():
    payload = "".join(random.Random(19).choices("abcdefghijklmnopqrstuvwxyz0123456789", k=700_000))
    compressed = zlib.compress(payload.encode())
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE documents(file_key TEXT, text_zlib BLOB)")
    connection.execute("INSERT INTO documents VALUES('fixture',?)", (compressed,))
    reads = []

    class Reader:
        def execute(self, *args):
            row = connection.execute(*args).fetchone()
            if row is not None and row[0] is not None:
                reads.append(len(row[0]))

            class Cursor:
                def fetchone(self):
                    return row
            return Cursor()

    text = catalog._read_compressed_text_prefix(Reader(), "documents", "text_zlib", "file_key=?", ("fixture",), 64000)
    assert text == payload[:64000]
    assert max(reads) <= 64 * 1024 and sum(reads) < len(compressed)
    connection.close()


def test_compressed_prefix_does_not_guess_a_fixed_compressed_size():
    payload = b"The useful text follows many empty deflate blocks."
    compressed = zlib.compress(payload)
    compressed = compressed[:2] + b"\x00\x00\x00\xff\xff" * 14_000 + compressed[2:]
    assert zlib.decompress(compressed) == payload
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE documents(file_key TEXT, text_zlib BLOB)")
        connection.execute("INSERT INTO documents VALUES('fixture',?)", (compressed,))
        assert catalog._read_compressed_text_prefix(connection, "documents", "text_zlib", "file_key=?", ("fixture",), 10) == "The useful"


def test_worker_cancellation_stops_between_compressed_source_chunks(monkeypatch):
    token = CancellationToken()
    compressed = zlib.compress(b"useful text")
    compressed = compressed[:2] + b"\x00\x00\x00\xff\xff" * 14_000 + compressed[2:]
    calls = 0

    class Reader:
        def execute(self, _sql, arguments):
            nonlocal calls
            calls += 1
            offset, count, _key = arguments
            token.cancel()

            class Cursor:
                def fetchone(self):
                    return (compressed[offset - 1:offset - 1 + count],)
            return Cursor()

    monkeypatch.setattr("neocortex.runtime.control.elastic_workers.current_worker_cancellation", lambda: token)
    with pytest.raises(CancellationRequested):
        catalog._read_compressed_text_prefix(Reader(), "documents", "text_zlib", "file_key=?", ("fixture",), 10)
    assert calls == 1
