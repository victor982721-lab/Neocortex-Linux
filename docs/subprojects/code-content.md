# Code como contenido

## Ownership y límite de producto

`neocortex/code` descubre proyectos y lenguajes, extrae estructura y símbolos,
persiste identidad, indexa, busca y relaciona archivos/entidades. No ejecuta el
código observado, importa herramientas de desarrollo ni audita el propio repositorio.
Consulta [Architecture](../ARCHITECTURE.md#code-como-contenido).

No agregues review, experimentos, proveedores externos, autoanálisis, receipts de
calidad o un circuito `trusted-deep` al runtime. `Neocortex --all` no selecciona
Code: la ruta integrada usa la redlist del Corpus y reserva Code para
`--route code` explícito.
Las operaciones públicas exponen contenido: ingesta, estado de índice, búsqueda,
proyectos y relaciones/reconstrucción respaldadas por publicación productiva.

Los nombres legacy en DDL/migraciones o aliases de lectura pueden ser datos de
compatibilidad: no los borres ni reescribas por coincidir con una palabra prohibida.
Conserva la lectura histórica y demuestra el límite ejecutable actual.

La procedencia se expresa como señal (`dependency`, `vendored`, `generated`,
`build_artifact`, `cache`, `binary`, `project_code` o `unknown`), nunca como
prueba de autoría. `--route code` usa `projects` por defecto; las raíces de
proyecto propias se declaran con `--code-project-root` y una exploración `broad`
es un opt-in explícito. Las raíces predeterminadas corresponden a NeoCortex, MTF y
bitácoras EPS. Una lista vacía o disjunta no cae a descubrimiento por marcadores;
raíces y alcance forman parte de la firma de procesamiento.

`--all --semantic-source code` se rechaza porque `--all` no activa la ruta Code.
Para producir estado Code usa `--route code`; para consumir un cache Code ya
publicado desde Semantic, usa una operación Semantic explícita fuera de `--all`.

La admisión de corpus pertenece al flujo de acciones/rutas, no a otro analizador
de Code. En la ruta integrada el inventario observa metadatos de dependencias y
cachés para decidir por archivo, preservando raíces canónicas protegidas y VCS.
El código fuera del interés no se cuela como texto ni por ZIP. `--route code`
prepara un plan de regenerables y `--apply` ejecuta sólo los que tengan testigo local
conservado y comparación exacta, o bytecode reproducible desde su fuente.
Score y señal de tercero siguen sin ser prueba de reconstrucción. En `--all --apply`,
la decisión de Papelera no usa este clasificador: la redlist case-insensitive
se aplica antes de hashing y validación. Licencias,
fixtures, credenciales, paquetes fuente, ambiguos y miembros virtuales no se
convierten en efectos físicos por esa clasificación.

## Validación proporcional

Reutiliza `tests/architecture/test_boundaries.py` y las pruebas focales de ingesta,
búsqueda y recursos Code pertinentes. Pytest, Ruff, Mypy/Pyright y Semgrep se
ejecutan individualmente desde desarrollo, no mediante una API productiva nueva.
Un fixture de código es dato no confiable; aísla su estado y proyecto explícitos.
