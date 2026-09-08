# Code como contenido

## Ownership y límite de producto

`neocortex/code` descubre proyectos y lenguajes, extrae estructura y símbolos,
persiste identidad, indexa, busca y relaciona archivos/entidades. No ejecuta el
código observado, importa herramientas de desarrollo ni audita el propio repositorio.
Consulta [Architecture](../ARCHITECTURE.md#code-como-contenido).

No agregues review, experimentos, proveedores externos, autoanálisis, receipts de
calidad o un circuito `trusted-deep` al runtime. `Neocortex --all` selecciona Code
como contenido, no produce ni refresca evidencia del desarrollo de NeoCortex.
Las operaciones públicas exponen contenido: ingesta, estado de índice, búsqueda,
proyectos y relaciones/reconstrucción respaldadas por publicación productiva.

Los nombres legacy en DDL/migraciones o aliases de lectura pueden ser datos de
compatibilidad: no los borres ni reescribas por coincidir con una palabra prohibida.
Conserva la lectura histórica y demuestra el límite ejecutable actual.

## Validación proporcional

Reutiliza `tests/architecture/test_boundaries.py` y las pruebas focales de ingesta,
búsqueda y recursos Code pertinentes. Pytest, Ruff, Mypy/Pyright y Semgrep se
ejecutan individualmente desde desarrollo, no mediante una API productiva nueva.
Un fixture de código es dato no confiable; aísla su estado y proyecto explícitos.
