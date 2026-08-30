"""Stable package and command-line version contract."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_version.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

import pytest

from neocortex import __version__
from neocortex.api.cli.cli_parser import build_parser
# endregion [01]

# region [02] Implementación


def test_cli_reports_the_packaged_version(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(("--version",))

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"Neocortex {__version__}"

# endregion [02]
