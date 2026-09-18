"""Negative contracts for schema identity, provenance and adapter boundaries."""
from __future__ import annotations

import os
import pickle
import runpy
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from neocortex.persistence import sqlite_schema_contract as schema
from neocortex.foundation import processing_provenance as provenance

ROOT = Path(__file__).resolve().parents[1]


def _schema(sql: str) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(sql)
    return connection


@pytest.mark.parametrize("expected,actual", [
    ("CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET x='a  b'; END", "CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET x='a b'; END"),
    ("CREATE VIEW v AS SELECT 'A  B' AS x", "CREATE VIEW v AS SELECT 'A B' AS x"),
    ("CREATE VIEW v AS SELECT 'it''s  two' AS x", "CREATE VIEW v AS SELECT 'it''s two' AS x"),
    ('CREATE VIEW v AS SELECT "UPPER" AS x', 'CREATE VIEW v AS SELECT "upper" AS x'),
    ("CREATE UNIQUE INDEX i ON t(x) WHERE a NOT NULL", "CREATE UNIQUE INDEX i ON t(x) WHERE anotnull"),
    ("CREATE INDEX i ON t(x) WHERE x='a  b'", "CREATE INDEX i ON t(x) WHERE x='a b'"),
])
def test_objects_preserve_literal_data_and_lexical_boundaries(expected, actual):
    prefix = "CREATE TABLE t(x TEXT, a INT, anotnull INT);"
    left, right = _schema(prefix + expected), _schema(prefix + actual)
    try:
        contract = schema.capture_sqlite_schema_contract(left)
        with pytest.raises(schema.SQLiteSchemaContractError):
            schema.validate_sqlite_schema_contract(right, contract, label="object", exact=True)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("expected,actual", [
    ("CREATE VIEW v AS SELECT 'keep  this' AS x", "create view v AS SELECT /* comment */ 'keep  this' AS x"),
    ("CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET x='keep  it'; END", "create trigger tr AFTER INSERT ON t BEGIN UPDATE t SET x = 'keep  it' ; END"),
    ("CREATE UNIQUE INDEX i ON t(x) WHERE a NOT NULL", "create unique index IF NOT EXISTS i ON t ( x ) WHERE a /* comment */ NOT NULL"),
])
def test_object_trivia_and_optional_creation_guard_are_noncontractual(expected, actual):
    prefix = "CREATE TABLE t(x TEXT, a INT, anotnull INT);"
    left, right = _schema(prefix + expected), _schema(prefix + actual)
    try:
        schema.validate_sqlite_schema_contract(right, schema.capture_sqlite_schema_contract(left), label="object", exact=True)
    finally:
        left.close()
        right.close()


def test_unknown_schema_objects_still_fail_exact_validation():
    left, right = _schema("CREATE TABLE t(x);"), _schema("CREATE TABLE t(x);CREATE VIEW unknown AS SELECT x FROM t;")
    try:
        with pytest.raises(schema.SQLiteSchemaContractError):
            schema.validate_sqlite_schema_contract(right, schema.capture_sqlite_schema_contract(left), label="object", exact=True)
    finally:
        left.close()
        right.close()


@pytest.fixture(autouse=True)
def _fresh_provenance():
    provenance.clear_processing_provenance_caches()
    yield
    provenance.clear_processing_provenance_caches()


def test_artifact_cache_reuses_unchanged_but_rehashes_replacement_and_inplace_rewrite(tmp_path):
    path = tmp_path / "model.bin"
    path.write_bytes(b"AAAA")
    original = path.stat()
    first = provenance.fingerprint_file_xxh3_128(path)
    before = provenance._fingerprint_file_cached.cache_info()
    assert provenance.fingerprint_file_xxh3_128(path) == first
    assert provenance._fingerprint_file_cached.cache_info().hits == before.hits + 1
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"BBBB")
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    replacement.replace(path)
    second = provenance.fingerprint_file_xxh3_128(path)
    assert second != first
    path.write_bytes(b"CCCC")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert provenance.fingerprint_file_xxh3_128(path) not in {first, second}


def test_hash_rejects_content_change_during_stream_and_does_not_cache_mixed_digest(tmp_path):
    path = tmp_path / "model.bin"
    path.write_bytes(b"before")
    original_factory = provenance.xxhash.xxh3_128
    changed = False

    class ChangingDigest:
        def __init__(self):
            self.digest = original_factory()
        def update(self, value):
            nonlocal changed
            self.digest.update(value)
            if not changed:
                changed = True
                path.write_bytes(b"after!")
        def hexdigest(self):
            return self.digest.hexdigest()

    with patch.object(provenance.xxhash, "xxh3_128", side_effect=ChangingDigest):
        with pytest.raises(provenance.ProcessingArtifactChangedError):
            provenance.fingerprint_file_xxh3_128(path)
    assert provenance.fingerprint_file_xxh3_128(path) == original_factory(path.read_bytes()).hexdigest()


def test_hash_rejects_path_replacement_after_open(tmp_path):
    path = tmp_path / "model.bin"
    replacement = tmp_path / "new.bin"
    path.write_bytes(b"before")
    replacement.write_bytes(b"after!")
    original_open = os.open

    def replace_after_open(value, flags, *args, **kwargs):
        descriptor = original_open(value, flags, *args, **kwargs)
        if os.fspath(value) == str(path):
            replacement.replace(path)
        return descriptor

    with patch.object(provenance.os, "open", side_effect=replace_after_open):
        with pytest.raises(provenance.ProcessingArtifactChangedError):
            provenance.file_artifact(path)


def test_nonregular_artifact_rejected_without_opening_fifo(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(FileNotFoundError):
        provenance.fingerprint_file_xxh3_128(path)


def test_executable_replacement_updates_version_and_hash_without_private_clear(tmp_path):
    path = tmp_path / "probe"
    path.write_text('#!/bin/sh\nprintf "version-A\\n"\n')
    path.chmod(0o700)
    stat = path.stat()
    first = provenance.executable_component("probe", default_name="probe", explicit=str(path))
    before = provenance._executable_component_json.cache_info()
    assert provenance.executable_component("probe", default_name="probe", explicit=str(path)) == first
    assert provenance._executable_component_json.cache_info().hits == before.hits + 1
    path.write_text('#!/bin/sh\nprintf "version-B\\n"\n')
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = provenance.executable_component("probe", default_name="probe", explicit=str(path))
    assert second['version'] == 'version-B'
    assert second['binary']['xxh3_128'] != first['binary']['xxh3_128']


def test_public_revision_discards_inflight_old_observation():
    ready, release = threading.Event(), threading.Event()
    versions = {'value': 'old'}
    outputs = []

    @provenance.processing_provenance_cache(maxsize=4)
    def captured():
        value = versions['value']
        if value == 'old':
            ready.set()
            assert release.wait(3)
        return value

    thread = threading.Thread(target=lambda: outputs.append(captured()))
    thread.start()
    try:
        assert ready.wait(3)
        versions['value'] = 'new'
        provenance.clear_processing_provenance_caches()
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert outputs == ['new']
    assert captured() == 'new'


@pytest.mark.parametrize('route', ['pdf', 'docx', 'office'])
def test_public_clear_refreshes_all_config_only_route_caches(tmp_path, route):
    import importlib
    if route == 'pdf':
        module = importlib.import_module('neocortex.capabilities.formats.pdf.pdf_route_models')
        config = module.PdfRouteConfig(tmp_path / 'pdf.sqlite3', ocr_mode='never', pdfminer_fallback=False)
    else:
        module = importlib.import_module(f'neocortex.capabilities.formats.{route}.models')
        config = getattr(module, 'DocxRouteConfig' if route == 'docx' else 'OfficeRouteConfig')(tmp_path / 'state.sqlite3')
    version = {'value': 'v1'}
    def component(name, distribution, **kwargs):
        return {'name': name, 'version': version['value']}
    with patch.object(module, 'distribution_component', side_effect=component):
        first = config.processing_signature
        assert config.processing_signature == first
        version['value'] = 'v2'
        provenance.clear_processing_provenance_caches()
        assert config.processing_signature != first


def _architecture():
    return runpy.run_path(str(ROOT / 'tests/architecture/test_boundaries.py'))


@pytest.mark.parametrize('module,filename,source,expected', [
    ('neocortex.api.fake', 'fake.py', 'from ..interface import entrypoint', {'neocortex.interface', 'neocortex.interface.entrypoint'}),
    ('neocortex.api.fake', 'fake.py', 'from . import helper', {'neocortex.api', 'neocortex.api.helper'}),
    ('neocortex.api', '__init__.py', 'from . import helper', {'neocortex.api', 'neocortex.api.helper'}),
    ('neocortex.api', '__init__.py', 'from ..interface import entrypoint', {'neocortex.interface', 'neocortex.interface.entrypoint'}),
])
def test_import_resolution_uses_package_and_keeps_submodules(tmp_path, module, filename, source, expected):
    path = tmp_path / filename
    path.write_text(source)
    assert set(_architecture()['_imports'](module, path)) == expected


def test_import_context_distinguishes_type_checking_body_else_and_function(tmp_path):
    path = tmp_path / 'fixture.py'
    path.write_text('''from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from . import typed
else:
    from . import runtime

def later():
    from . import deferred
''')
    observed = {target: (kind, contexts) for target, kind, contexts in _architecture()['_import_edges']('pkg.fixture', path)}
    assert observed['pkg.typed'] == ('type_checking', ())
    assert observed['pkg.runtime'] == ('eager', ())
    assert observed['pkg.deferred'] == ('deferred', ('later',))


def test_shared_exit_contract_does_not_load_cli_and_reexports_preserve_identity():
    source = '''import sys
from neocortex.api import status_codes
assert not any(name.startswith('neocortex.api.cli') for name in sys.modules)
from neocortex.api import read_api_port
assert not any(name.startswith('neocortex.api.cli') for name in sys.modules)
from neocortex.api.cli import cli_knowledge
assert cli_knowledge.knowledge_search_exit_code is status_codes.knowledge_search_exit_code
assert cli_knowledge.knowledge_context_exit_code is status_codes.knowledge_context_exit_code
'''
    result = subprocess.run([sys.executable, '-c', source], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_text_source_record_legacy_reexport_and_pickle_identity():
    from neocortex.semantic.semantic_models import TextSourceRecord as shared
    from neocortex.semantic.semantic_sources import TextSourceRecord as legacy
    from neocortex.semantic.video_source import TextSourceRecord as video
    assert shared is legacy is video
    assert shared.__module__ == 'neocortex.semantic.semantic_sources'
    value = shared(None, None)
    assert type(pickle.loads(pickle.dumps(value))) is shared


def test_type_checking_aliases_are_recognized_without_hiding_unrelated_flags(tmp_path):
    path = tmp_path / 'fixture.py'
    path.write_text("""import typing as t
from typing import TYPE_CHECKING as TC
if TC:
    from . import typed_alias
if t.TYPE_CHECKING:
    from . import typed_module
if settings.TYPE_CHECKING:
    from . import arbitrary_flag
""")
    observed = {target: kind for target, kind, _context in _architecture()['_import_edges']('pkg.fixture', path)}
    assert observed['pkg.typed_alias'] == 'type_checking'
    assert observed['pkg.typed_module'] == 'type_checking'
    assert observed['pkg.arbitrary_flag'] == 'eager'
