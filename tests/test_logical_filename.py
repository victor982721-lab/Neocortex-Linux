from pathlib import Path

import pytest

from neocortex.platform.logical_filename import LogicalFilename, collision_path, identity_token
from neocortex.platform.content_types import DetectedType
from neocortex.workflow.actions.action_policy import corrected_path
from neocortex.workflow.actions.redlist import redlist_match


@pytest.mark.parametrize('name,stem,extension,decorator,gnu', [
    ('reporte.pdf', 'reporte', '.pdf', '', ()),
    ('reporte.pdf.~1~', 'reporte', '.pdf', '', ('.~1~',)),
    ('reporte.pdf.~25~', 'reporte', '.pdf', '', ('.~25~',)),
    ('reporte__a1b2c3d4.pdf', 'reporte', '.pdf', '__a1b2c3d4', ()),
    ('reporte~a1b2c3d4.pdf', 'reporte', '.pdf', '~a1b2c3d4', ()),
    ('archivo_sin_extension', 'archivo_sin_extension', '', '', ()),
    ('archivo.docx.~1~.~2~', 'archivo', '.docx', '', ('.~1~', '.~2~')),
    ('.gitignore', '.gitignore', '', '', ()),
    ('notas.backup', 'notas', '.backup', '', ()),
])
def test_one_owner_for_logical_filename(name, stem, extension, decorator, gnu):
    logical = LogicalFilename.parse(Path('/corpus') / name)
    assert (logical.stem, logical.logical_extension, logical.collision_decorator, logical.gnu_suffixes) == (stem, extension, decorator, gnu)


def test_compound_extensions_are_explicit_and_never_consumed_as_backups():
    logical = LogicalFilename.parse('src.tar.gz.~2~')
    assert logical.compound_extension == '.tar.gz'
    assert logical.logical_extension == '.gz'
    assert logical.normalized_basename == 'src.tar.gz'


@pytest.mark.parametrize('source,target', [
    ('archivo.docx.~1~', 'archivo.docx'),
    ('archivo.dat.~1~', 'archivo.docx'),
    ('archivo__a1b2c3d4.dat.~1~', 'archivo__a1b2c3d4.docx'),
    ('archivo_sin_extension', 'archivo_sin_extension.docx'),
])
def test_normalization_never_duplicates_logical_extension(source, target):
    assert corrected_path(Path(source), '.docx') == Path(target)


def test_redlist_uses_logical_suffix_without_redlisting_backup_families():
    assert redlist_match('setup.exe.~1~') == '.exe'
    for name in ('personal.bak.~2~', 'personal.back', 'personal.backup', 'personal.pdbxml'):
        assert redlist_match(name) is None
    assert redlist_match('0.1-informe.pdf.~1~') is None


def test_detector_acceptance_uses_logical_extension():
    pdf = DetectedType('application/pdf', '.pdf', frozenset({'.pdf'}), 'magic:pdf')
    assert pdf.accepts('document.PDF.~25~')


def test_collision_names_are_identity_stable_and_do_not_stack_decorators():
    token = identity_token(31, 42, -1)
    requested = Path('reporte.pdf.~2~')
    target = collision_path(requested, token)
    assert target == Path(f'reporte__{token}.pdf')
    assert collision_path(target, token) == target
    assert collision_path(target, token, attempt=2) == Path(f'reporte__{token}_2.pdf')
    assert '.~' not in str(target)
    assert identity_token(31, 43, -1) != token


def test_collision_byte_limit_and_invalid_tokens():
    assert len(collision_path(Path('ñ' * 120 + '.pdf'), 'a' * 12).name.encode()) <= 240
    with pytest.raises(ValueError):
        collision_path(Path('a.pdf'), '../evil')
