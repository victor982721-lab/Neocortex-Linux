from io import BytesIO
import json
import zipfile

import pytest

from neocortex.platform.content_types import HEADER_LIMIT, detect_content_type, identify
from neocortex.platform.identification_probe import PROBE_CHUNK_BYTES, structured_probe
from neocortex.runtime.control.cancellation import CancellationRequested


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16', 'utf-32'])
@pytest.mark.parametrize('kind,mime,extension', [
    ('json', 'application/json', '.json'),
    ('xml', 'application/xml', '.xml'),
    ('html', 'text/html', '.html'),
    ('csv', 'text/csv', '.csv'),
    ('tsv', 'text/tab-separated-values', '.tsv'),
    ('jsonl', 'application/x-ndjson', '.jsonl'),
])
def test_large_structured_text_escalates_instead_of_becoming_plain(tmp_path, encoding, kind, mime, extension):
    row = 'evidencia útil 12345 ' * 1800
    payload = {
        'json': json.dumps({'items': [row] * 4}, ensure_ascii=False),
        'xml': f'<document><record>{row}</record><record>{row}</record></document>',
        'html': f'<!doctype html><html><body><p>{row}</p><p>{row}</p></body></html>',
        'csv': 'equipo,valor\n' + 'turbina,42\n' * 9000,
        'tsv': 'equipo\tvalor\n' + 'turbina\t42\n' * 9000,
        'jsonl': '\n'.join(json.dumps({'id': n, 'value': 'señal'}) for n in range(3500)),
    }[kind]
    source = tmp_path / 'wrong.dat'
    source.write_bytes(payload.encode(encoding))
    assert source.stat().st_size > HEADER_LIMIT
    result = identify(source)
    assert (result.mime, result.canonical_extension, result.confidence) == (mime, extension, 'high')


@pytest.mark.parametrize('codec,bom', [('utf-16-le', b'\xff\xfe'), ('utf-16-be', b'\xfe\xff'),
                                      ('utf-32-le', b'\xff\xfe\x00\x00'), ('utf-32-be', b'\x00\x00\xfe\xff')])
def test_explicit_unicode_byte_orders(tmp_path, codec, bom):
    source = tmp_path / 'document'
    source.write_bytes(bom + '{"título":"medición"}'.encode(codec))
    assert identify(source).mime == 'application/json'


def test_utf8_prefix_boundary_is_not_redecoded_as_cp1252(tmp_path):
    source = tmp_path / 'document'
    prefix = '{"text":"' + 'a' * (HEADER_LIMIT - 10)
    source.write_text(prefix + 'ñ valor"}', encoding='utf-8')
    assert identify(source).mime == 'application/json'


def test_exact_header_length_json_is_complete(tmp_path):
    source = tmp_path / 'document'
    payload = b'{"x":"' + b'a' * (HEADER_LIMIT - 8) + b'"}'
    assert len(payload) == HEADER_LIMIT
    source.write_bytes(payload)
    assert identify(source).mime == 'application/json'


def test_ndjson_and_json_are_distinct_and_extensions_accepted(tmp_path):
    source = tmp_path / 'events.ndjson'
    source.write_text('{"a":1}\n{"a":2}\n')
    result = identify(source)
    assert result.mime == 'application/x-ndjson'
    assert result.canonical_extension == '.jsonl' and result.accepts()
    source.write_text('{"a":1}')
    assert identify(source).mime == 'application/json'


def test_zip_unsupported_reader_degrades_without_inventing_ooxml(tmp_path, monkeypatch):
    source = tmp_path / 'document.docx'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('word/document.xml', '<doc/>')
    def unsupported(*_a, **_k):
        raise NotImplementedError('zip file version 14.8')
    monkeypatch.setattr(zipfile, 'ZipFile', unsupported)
    result = identify(source)
    assert result.mime == 'application/zip'
    assert result.canonical_extension == '.zip'


def test_adaptive_probe_has_a_hard_byte_cap():
    class Meter(BytesIO):
        total = 0
        largest_read = 0
        def read(self, n=-1):
            assert n >= 0
            self.largest_read = max(self.largest_read, n)
            value = super().read(n)
            self.total += len(value)
            return value
    prefix = b'{' * HEADER_LIMIT
    source = Meter(b'x' * (HEADER_LIMIT * 10))
    with structured_probe(source, prefix, size=HEADER_LIMIT * 11, limit=HEADER_LIMIT * 3) as (data, complete):
        assert not complete and len(data) == HEADER_LIMIT * 3
    assert source.total == HEADER_LIMIT * 2
    assert source.largest_read <= PROBE_CHUNK_BYTES


def test_cancellation_is_observed_during_escalation():
    calls = []
    def cancel():
        calls.append(1)
        if len(calls) == 3:
            raise CancellationRequested('test checkpoint')
    with pytest.raises(CancellationRequested):
        with structured_probe(BytesIO(b'x' * (HEADER_LIMIT * 8)), b'{' * HEADER_LIMIT,
                              size=HEADER_LIMIT * 9, checkpoint=cancel):
            raise AssertionError('cancelled input must not be admitted')


def test_oversized_or_entity_bearing_xml_cannot_be_guessed(tmp_path, monkeypatch):
    source = tmp_path / 'unknown.dat'
    source.write_text('<!DOCTYPE x [<!ENTITY e "content">]><x>&e;</x>')
    assert detect_content_type(source) is None
    source.write_text('{"data":"' + 'a' * HEADER_LIMIT * 3 + '"}')
    from neocortex.platform import content_types
    original = structured_probe
    def capped(*args, **kwargs):
        return original(*args, limit=HEADER_LIMIT * 2, **kwargs)
    monkeypatch.setattr(content_types, 'structured_probe', capped)
    assert detect_content_type(source) is None


def test_large_plain_text_does_not_escalate(tmp_path, monkeypatch):
    source = tmp_path / 'notes.txt'
    source.write_text('Notas de mantenimiento sin tablas ni contenedores.\n' * 3000)
    from neocortex.platform import content_types
    monkeypatch.setattr(content_types, 'structured_probe', lambda *_a, **_k: pytest.fail('unneeded escalation'))
    assert identify(source).mime == 'text/plain'
