from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from neocortex.deduplication import FULL_ALGORITHM, snapshot_path
from neocortex.capabilities.formats.image.document import (
    DocumentTextEvidence,
    DocumentVerifierRuntime,
)
from neocortex.capabilities.formats.image.route import ImageRoute, ImageRouteConfig
from neocortex.capabilities.formats.image.state import (
    initialize_image_state,
    iter_candidates,
    iter_ocr_text_records,
)


# region [01] Route fixture


class _State:
    def __init__(self, rows):
        self.rows = list(rows)
        self.review_candidates = []
        self.resolutions = []
        self.reconciliations = []

    def iter_route_candidates_by_prefix(self, run_id, mime_prefix):
        yield from (
            (mime, snapshot) for mime, snapshot in self.rows if mime.startswith(mime_prefix)
        )

    def iter_selected_route_candidates_by_prefix(self, run_id, mime_prefix, route_name, selection):
        del route_name, selection
        yield from self.iter_route_candidates_by_prefix(run_id, mime_prefix)

    def store_review_candidates(self, run_id, candidates):
        self.review_candidates.extend(candidates)

    def resolve_review_candidates(self, run_id, route_name, snapshot, resolution_note):
        self.resolutions.append((run_id, route_name, snapshot, resolution_note))
        return 0

    def reconcile_review_candidates(
        self,
        run_id,
        route_name,
        snapshot,
        resolution_note,
        *,
        evaluated_reason_codes,
        active_reason_codes,
    ):
        self.reconciliations.append(
            (
                run_id,
                route_name,
                snapshot,
                resolution_note,
                frozenset(evaluated_reason_codes),
                frozenset(active_reason_codes),
            )
        )
        return 0

    def reconcile_review_candidates_batch(
        self,
        run_id,
        route_name,
        reconciliations,
    ):
        for reconciliation in reconciliations:
            self.reconciliations.append(
                (
                    run_id,
                    route_name,
                    reconciliation.snapshot,
                    reconciliation.resolution_note,
                    frozenset(reconciliation.evaluated_reason_codes),
                    frozenset(reconciliation.active_reason_codes),
                )
            )
        return 0


def _route(
    root: Path,
    state: _State,
    run_id: int,
    **overrides: Any,
) -> ImageRoute:
    dedup_index = overrides.pop("dedup_index", None)
    progress = overrides.pop("progress", None)
    config = ImageRouteConfig(
        state_path=root / "state" / "image.sqlite3",
        root=root,
        workers=2,
        memory_budget_bytes=256 * 1024 * 1024,
        min_free_memory_bytes=0,
        min_free_commit_bytes=0,
        document_ocr_mode="never",
    )
    return ImageRoute(
        replace(config, **overrides),
        state,
        run_id,
        progress=progress,
        dedup_index=dedup_index,
    )


class _FingerprintIndex:
    def __init__(self) -> None:
        self.values: dict[tuple[int, int, int, int, int, str], bytes] = {}

    @staticmethod
    def _key(snapshot, algorithm: str) -> tuple[int, int, int, int, int, str]:
        return (
            snapshot.volume_id,
            snapshot.file_id,
            snapshot.size,
            snapshot.mtime_ns,
            snapshot.birthtime_ns,
            algorithm,
        )

    def cached_fingerprint(self, snapshot, algorithm: str) -> bytes | None:
        return self.values.get(self._key(snapshot, algorithm))

    def store_fingerprint(self, snapshot, algorithm: str, digest: bytes) -> None:
        self.values[self._key(snapshot, algorithm)] = digest


# endregion [01]


# region [02] Incremental classification


class ImageRouteTests(unittest.TestCase):
    def test_migrates_schema_one_without_discarding_image_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "image.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
                    INSERT INTO metadata VALUES('schema_version','1');
                    CREATE TABLE images(
                        file_key TEXT PRIMARY KEY,path TEXT NOT NULL,mime TEXT NOT NULL,
                        size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,
                        birthtime_ns INTEGER NOT NULL,last_seen_run_id INTEGER NOT NULL,
                        processing_signature TEXT,status TEXT NOT NULL DEFAULT 'pending',
                        category TEXT,confidence REAL,runner_up TEXT,runner_up_score REAL,
                        features_json TEXT,attributes_json TEXT,evidence_json TEXT,
                        error_type TEXT,error_message TEXT,updated_ns INTEGER NOT NULL DEFAULT 0
                    ) WITHOUT ROWID;
                    INSERT INTO images(file_key,path,mime,size,mtime_ns,birthtime_ns,
                        last_seen_run_id,status) VALUES('1:1','old.jpg','image/jpeg',1,2,3,4,'pending');
                    """
                )
                connection.commit()

            initialize_image_state(database)
            with closing(sqlite3.connect(database)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(images)")}
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
                count = connection.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            self.assertIn("semantic_json", columns)
            self.assertIn("document_candidate", columns)
            self.assertIn("error_disposition", columns)
            self.assertNotIn("adult_classification", columns)
            self.assertNotIn("adult_evidence_json", columns)
            self.assertIn("ocr_text_zlib", columns)
            self.assertIn("ocr_text_chars", columns)
            self.assertIn("ocr_text_xxh3_128", columns)
            self.assertIn("ocr_text_truncated", columns)
            self.assertEqual(version, "6")
            self.assertEqual(count, 1)

    def test_schema_six_migration_preserves_legacy_cache_hits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "cached.png"
            with Image.new("RGB", (96, 96), "navy") as image:
                image.save(path)
            state = _State((("image/png", snapshot_path(path)),))
            first = _route(root, state, 1).run()
            self.assertEqual(first.classified, 1)

            database = root / "state" / "image.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP INDEX images_ocr_text_idx")
                for column in (
                    "ocr_text_zlib",
                    "ocr_text_chars",
                    "ocr_text_xxh3_128",
                    "ocr_text_truncated",
                ):
                    connection.execute(f"ALTER TABLE images DROP COLUMN {column}")
                connection.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
                connection.commit()

            migrated = _route(root, state, 2).run()

            self.assertEqual(migrated.cache_hits, 1)
            self.assertEqual(migrated.classified, 0)
            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    "SELECT ocr_text_zlib,ocr_text_chars,ocr_text_xxh3_128,"
                    "ocr_text_truncated FROM images"
                ).fetchone()
                version = connection.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual(row, (None, None, None, 0))
            self.assertEqual(version, "6")

    def test_classifies_resumes_caches_errors_and_prunes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            white = root / "invoice_page.jpg"
            dark = root / "field_photo.png"
            corrupt = root / "broken.png"
            with Image.new("RGB", (1200, 800), "white") as image:
                image.save(white)
            with Image.new("RGB", (900, 700), (25, 80, 35)) as image:
                image.save(dark)
            corrupt.write_bytes(b"not a png")
            state = _State(
                (
                    ("image/jpeg", snapshot_path(white)),
                    ("image/png", snapshot_path(dark)),
                    ("image/png", snapshot_path(corrupt)),
                )
            )

            first = _route(root, state, 1).run()
            self.assertEqual(first.candidate_pool, 3)
            self.assertEqual(first.processed, 3)
            self.assertEqual(first.classified, 2)
            self.assertEqual(first.errors, 1)
            self.assertEqual(first.manual_review_errors, 0)
            self.assertEqual(first.deletion_candidates, 1)
            self.assertEqual(first.new_images, 3)
            self.assertEqual(first.retried_images, 0)
            self.assertGreater(first.peak_reserved_bytes, 0)
            self.assertTrue(
                any(
                    candidate.reason_code == "image_container_integrity_failure"
                    and candidate.recommendation == "deletion_candidate"
                    for candidate in state.review_candidates
                )
            )

            second = _route(root, state, 2).run()
            self.assertEqual(second.cache_hits, 2)
            self.assertEqual(second.cached_errors, 1)
            self.assertEqual(second.new_images, 0)
            self.assertEqual(second.retried_images, 0)
            self.assertEqual(second.classified, 0)

            state.rows.pop()
            third = _route(root, state, 3, max_documents=1).run()
            self.assertEqual(third.candidate_pool, 2)
            self.assertEqual(third.candidates, 1)
            self.assertEqual(third.cache_hits, 1)
            self.assertEqual(third.skipped_by_count, 1)
            with closing(sqlite3.connect(root / "state" / "image.sqlite3")) as connection:
                rows = connection.execute(
                    "SELECT status,features_json,evidence_json FROM images ORDER BY path"
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row[0] == "done" for row in rows))
            self.assertTrue(all(row[1] and row[2] for row in rows))

    def test_persists_full_image_fingerprint_and_reuses_it_on_cache_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "panel.png"
            with Image.new("RGB", (640, 480), "navy") as image:
                image.save(source)
            state = _State((("image/png", snapshot_path(source)),))
            fingerprints = _FingerprintIndex()

            first = _route(root, state, 1, dedup_index=fingerprints).run()
            second = _route(root, state, 2, dedup_index=fingerprints).run()

            self.assertEqual(first.full_fingerprints_computed, 1)
            self.assertEqual(first.full_fingerprint_cache_hits, 0)
            self.assertEqual(second.full_fingerprints_computed, 0)
            self.assertEqual(second.full_fingerprint_cache_hits, 1)
            self.assertEqual(second.cache_hits, 1)
            self.assertEqual(len(fingerprints.values), 1)
            ((key, digest),) = fingerprints.values.items()
            self.assertEqual(key[-1], FULL_ALGORITHM)
            self.assertEqual(len(digest), 16)

    def test_candidate_limit_bounds_missing_fingerprints_on_cached_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = tuple(root / f"panel-{index}.png" for index in range(5))
            for index, source in enumerate(paths):
                with Image.new("RGB", (320, 240), (index * 20, 40, 80)) as image:
                    image.save(source)
            state = _State(tuple(("image/png", snapshot_path(path)) for path in paths))

            initial = _route(root, state, 1).run()
            fingerprints = _FingerprintIndex()
            bounded = _route(
                root,
                state,
                2,
                max_documents=2,
                dedup_index=fingerprints,
            ).run()
            replay = _route(
                root,
                state,
                3,
                max_documents=2,
                dedup_index=fingerprints,
            ).run()

            self.assertEqual(initial.classified, 5)
            self.assertEqual(bounded.candidate_pool, 5)
            self.assertEqual(bounded.candidates, 2)
            self.assertEqual(bounded.cache_hits, 2)
            self.assertEqual(bounded.skipped_by_count, 3)
            self.assertEqual(bounded.full_fingerprints_computed, 2)
            self.assertEqual(len(fingerprints.values), 2)
            self.assertEqual(replay.candidates, 2)
            self.assertEqual(replay.cache_hits, 2)
            self.assertEqual(replay.full_fingerprints_computed, 0)
            self.assertEqual(replay.full_fingerprint_cache_hits, 2)
            self.assertEqual(replay.skipped_by_count, 3)

    def test_candidate_limit_prioritizes_new_tail_over_cache_hits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "a.png"
            tail_path = root / "z.png"
            with Image.new("RGB", (320, 240), "navy") as image:
                image.save(first_path)
            with Image.new("RGB", (320, 240), "green") as image:
                image.save(tail_path)
            state = _State((("image/png", snapshot_path(first_path)),))

            first = _route(root, state, 1).run()
            self.assertEqual(first.classified, 1)
            state.rows.append(("image/png", snapshot_path(tail_path)))

            resumed = _route(root, state, 2, max_documents=1).run()
            self.assertEqual(resumed.cache_hits, 0)
            self.assertEqual(resumed.classified, 1)
            self.assertEqual(resumed.processed, 1)
            self.assertEqual(resumed.skipped_by_count, 1)
            with closing(sqlite3.connect(root / "state" / "image.sqlite3")) as connection:
                statuses = dict(connection.execute("SELECT path,status FROM images"))
            self.assertEqual(statuses[str(tail_path)], "done")

    def test_unbounded_resume_reports_persisted_cache_before_new_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cached_path = root / "a-cached.png"
            new_path = root / "z-new.png"
            with Image.new("RGB", (96, 96), "navy") as image:
                image.save(cached_path)
            with Image.new("RGB", (96, 96), "green") as image:
                image.save(new_path)
            state = _State((("image/png", snapshot_path(cached_path)),))

            initial = _route(root, state, 1).run()
            self.assertEqual(initial.classified, 1)
            state.rows.append(("image/png", snapshot_path(new_path)))
            events = []

            resumed = _route(root, state, 2, progress=events.append).run()

            first_advanced = next(
                event
                for event in events
                if event.operation == "image" and event.phase == "classify" and event.completed > 0
            )
            metrics = {metric.name: metric.value for metric in first_advanced.metrics}
            self.assertEqual(first_advanced.completed, 1)
            self.assertEqual(metrics["cache_hits"], 1)
            self.assertEqual(metrics["new_work"], 0)
            self.assertEqual(resumed.cache_hits, 1)
            self.assertEqual(resumed.classified, 1)

    def test_keyboard_interrupt_flushes_completed_classification_for_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "checkpoint.png"
            with Image.new("RGB", (96, 96), "navy") as image:
                image.save(path)
            state = _State((("image/png", snapshot_path(path)),))

            def interrupt_after_first_result(event):
                if (
                    event.operation == "image"
                    and event.phase == "classify"
                    and event.completed == 1
                ):
                    raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                _route(root, state, 1, progress=interrupt_after_first_result).run()

            resumed = _route(root, state, 2).run()
            self.assertEqual(resumed.cache_hits, 1)
            self.assertEqual(resumed.classified, 0)

    def test_retryable_old_error_precedes_old_done_reclassification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            done_path = root / "a.png"
            error_path = root / "z.png"
            with Image.new("RGB", (320, 240), "navy") as image:
                image.save(done_path)
            error_path.write_bytes(b"not an image")
            state = _State(
                (
                    ("image/png", snapshot_path(done_path)),
                    ("image/png", snapshot_path(error_path)),
                )
            )
            first = _route(root, state, 1).run()
            self.assertEqual((first.classified, first.errors), (1, 1))
            database = root / "state" / "image.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE images SET processing_signature='legacy-signature'")
                connection.commit()

            retried = _route(
                root,
                state,
                2,
                max_documents=1,
                retry_errors=True,
            ).run()

            self.assertEqual(retried.retried_images, 1)
            self.assertEqual(retried.reclassified_images, 0)
            self.assertEqual(retried.errors, 1)
            self.assertEqual(retried.skipped_by_count, 1)
            with closing(sqlite3.connect(database)) as connection:
                signatures = dict(
                    connection.execute("SELECT path,processing_signature FROM images")
                )
            self.assertNotEqual(signatures[str(error_path)], "legacy-signature")
            self.assertEqual(signatures[str(done_path)], "legacy-signature")

    def test_size_limit_does_not_discard_live_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            small = root / "small.png"
            large = root / "large.png"
            with Image.new("RGB", (32, 32), "white") as image:
                image.save(small)
            with Image.new("RGB", (800, 800), "white") as image:
                image.save(large)
            state = _State(
                (
                    ("image/png", snapshot_path(small)),
                    ("image/png", snapshot_path(large)),
                )
            )
            limit = small.stat().st_size
            summary = _route(root, state, 1, max_file_bytes=limit).run()
            self.assertEqual(summary.candidate_pool, 2)
            self.assertEqual(summary.candidates, 1)
            self.assertEqual(summary.skipped_by_size, 1)
            with closing(sqlite3.connect(root / "state" / "image.sqlite3")) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0], 2)

    def test_decision_upgrade_reuses_features_without_decoding_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "page.png"
            second_path = root / "graphic.png"
            with Image.new("RGB", (900, 700), "white") as image:
                image.save(first_path)
            with Image.new("RGB", (900, 700), (20, 80, 120)) as image:
                image.save(second_path)
            state = _State(
                (
                    ("image/png", snapshot_path(first_path)),
                    ("image/png", snapshot_path(second_path)),
                )
            )
            first = _route(root, state, 1).run()
            self.assertEqual(first.classified, 2)

            database = root / "state" / "image.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "UPDATE images SET processing_signature=?",
                    ("image-route-v2|image-analysis-v3",),
                )
                connection.commit()

            with patch(
                "neocortex.capabilities.formats.image.analysis.extract_features",
                side_effect=AssertionError("cached decision upgrade decoded an image"),
            ):
                upgraded = _route(
                    root,
                    state,
                    2,
                ).run()
            self.assertEqual(upgraded.feature_cache_hits, 2)
            self.assertEqual(upgraded.reclassified_images, 2)
            self.assertEqual(upgraded.classified, 2)
            self.assertEqual(upgraded.processed, 2)
            self.assertEqual(upgraded.errors, 0)

    def test_candidate_pagination_does_not_repeat_rows_after_priority_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "image.sqlite3"
            initialize_image_state(database)
            rows = [
                (
                    f"{1:032x}:{identity:032x}",
                    f"C:/images/{identity:04d}.png",
                    "image/png",
                    100,
                    identity,
                    identity,
                    7,
                    "legacy-signature",
                    "done",
                )
                for identity in range(513)
            ]
            with closing(sqlite3.connect(database)) as connection:
                connection.executemany(
                    """INSERT INTO images(
                        file_key,path,mime,size,mtime_ns,birthtime_ns,last_seen_run_id,
                        processing_signature,status) VALUES(?,?,?,?,?,?,?,?,?)""",
                    rows,
                )
                connection.commit()

            yielded: list[str] = []
            with closing(sqlite3.connect(database)) as writer:
                for row in iter_candidates(
                    database,
                    7,
                    None,
                    None,
                    processing_signature="current-signature",
                ):
                    key = str(row["file_key"])
                    yielded.append(key)
                    writer.execute(
                        "UPDATE images SET processing_signature=? WHERE file_key=?",
                        ("current-signature", key),
                    )
                    writer.commit()

            self.assertEqual(len(yielded), 513)
            self.assertEqual(len(set(yielded)), 513)

    def test_preserves_industrial_path_evidence_and_uncertainty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "Subestacion" / "Mantenimiento"
            folder.mkdir(parents=True)
            path = folder / "transformador_pruebas_epp.jpg"
            with Image.new("RGB", (900, 700), (70, 90, 75)) as image:
                image.save(path)
            state = _State((("image/jpeg", snapshot_path(path)),))

            summary = _route(root, state, 1).run()
            self.assertEqual(summary.industrial_context_candidates, 1)
            with closing(sqlite3.connect(root / "state" / "image.sqlite3")) as connection:
                payload = connection.execute(
                    "SELECT semantic_json FROM images WHERE path=?", (str(path),)
                ).fetchone()[0]
            semantic = json.loads(payload)
            self.assertIn("transformador", [item["label"] for item in semantic["entities"]])
            self.assertIn("mantenimiento", [item["label"] for item in semantic["activities"]])
            self.assertEqual(
                semantic["uncertainty"],
                "evidencia_semantica_limitada_a_nombre_y_ruta",
            )

    def test_report_container_name_does_not_override_photo_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "reporte_transformador_image1.jpeg"
            with Image.effect_noise((900, 700), 80).convert("RGB") as image:
                image.save(path, quality=88)
            state = _State((("image/jpeg", snapshot_path(path)),))

            summary = _route(root, state, 1).run()
            self.assertEqual(summary.photo_candidates, 1)
            self.assertEqual(summary.document_candidates, 0)
            with closing(sqlite3.connect(root / "state" / "image.sqlite3")) as connection:
                category = connection.execute(
                    "SELECT category FROM images WHERE path=?", (str(path),)
                ).fetchone()[0]
            self.assertEqual(category, "foto")

    def test_persists_unicode_ocr_text_without_json_duplication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "factura_inspección_subestación.png"
            with Image.new("RGB", (1200, 900), "white") as image:
                image.save(path)
            state = _State((("image/png", snapshot_path(path)),))
            recognized = "Inspección de subestación: transformador y protección eléctrica"
            runtime = DocumentVerifierRuntime(
                enabled=True,
                lang="spa+eng",
                timeout_seconds=12.0,
                tesseract_cmd="tesseract-test",
                tessdata_dir=None,
                signature="test-document-verifier",
                provenance="test-tesseract",
            )
            evidence = DocumentTextEvidence(
                attempted=True,
                available=True,
                word_count=8,
                line_count=2,
                character_count=56,
                recognized_text=recognized,
                recognized_text_truncated=False,
                text_coverage=0.2,
                mean_confidence=91.0,
                industrial_entities=("transformador", "subestacion"),
                provenance="test-tesseract",
            )

            with (
                patch(
                    "neocortex.capabilities.formats.image.route.resolve_document_verifier",
                    return_value=runtime,
                ),
                patch(
                    "neocortex.capabilities.formats.image.analysis.verify_document_text",
                    return_value=evidence,
                ) as verifier,
            ):
                summary = _route(
                    root,
                    state,
                    1,
                    document_ocr_mode="auto",
                    isolate_decoders=False,
                ).run()

            self.assertEqual(summary.classified, 1)
            self.assertTrue(verifier.called)
            records = list(iter_ocr_text_records(root / "state" / "image.sqlite3"))
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.text, recognized)
            self.assertEqual(record.characters, len(recognized))
            self.assertFalse(record.truncated)

            with closing(sqlite3.connect(root / "state" / "image.sqlite3")) as connection:
                row = connection.execute(
                    "SELECT ocr_text_chars,ocr_text_xxh3_128,evidence_json,semantic_json "
                    "FROM images WHERE path=?",
                    (str(path),),
                ).fetchone()
            evidence_json = json.loads(row[2])
            semantic_json = json.loads(row[3])
            self.assertEqual(row[0], len(recognized))
            self.assertEqual(row[1], record.xxh3_128)
            self.assertNotIn("recognized_text", evidence_json["document_text"])
            self.assertEqual(
                evidence_json["document_text"]["recognized_text_chars"],
                len(recognized),
            )
            self.assertNotIn(
                recognized,
                json.dumps(semantic_json, ensure_ascii=False),
            )


# endregion [02]


if __name__ == "__main__":
    unittest.main()
