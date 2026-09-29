"""Final filesystem categories never become self-reinforcing input evidence."""

import pytest

from neocortex.documents.document_signals import (
    classification_path_signal, is_framework_managed_path,
)


@pytest.mark.parametrize("directory", [
    "Corpus_ordenado/Ingenieria/Pruebas", "Sin_clasificar/_MIME/application/pdf",
    "Consulta_Tecnica_Organizada/Informes",
])
def test_managed_layout_does_not_reinforce_prior_classification(directory):
    path = f"/fixture/corpus/{directory}/informe.pdf"
    assert is_framework_managed_path(path)
    assert classification_path_signal(path) == "informe.pdf"


def test_original_unmanaged_context_is_retained():
    path = "/fixture/corpus/Cliente/Proyecto/SAT/informe.pdf"
    assert not is_framework_managed_path(path)
    assert classification_path_signal(path) == path
