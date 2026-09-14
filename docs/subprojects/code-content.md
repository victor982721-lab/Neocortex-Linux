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

La procedencia se expresa como señal (`dependency`, `vendored`, `generated`,
`build_artifact`, `cache`, `binary`, `project_code` o `unknown`), nunca como
prueba de autoría. `--all` usa `projects` por defecto; las raíces de proyecto
propias se declaran con `--code-project-root` y una exploración `broad` es un
opt-in explícito. En el flujo normal `--all`, la limpieza de terceros prepara un
plan automáticamente (y `--apply` lo ejecuta) para candidatos regulares con
señales fuertes y límite configurable; lo ambiguo, las licencias,
los contenedores y los miembros virtuales quedan fuera. Las carpetas que el
inventario excluye (`.venv`, `node_modules`, `.git`, cachés, etc.) tampoco son
escaneadas ni afectadas por este modo.

## Validación proporcional

Reutiliza `tests/architecture/test_boundaries.py` y las pruebas focales de ingesta,
búsqueda y recursos Code pertinentes. Pytest, Ruff, Mypy/Pyright y Semgrep se
ejecutan individualmente desde desarrollo, no mediante una API productiva nueva.
Un fixture de código es dato no confiable; aísla su estado y proyecto explícitos.
