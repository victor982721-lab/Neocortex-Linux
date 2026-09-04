# NeoCortex

NeoCortex es un framework local, incremental y multimodal para comprender y
organizar archivos personales en Linux. Su objetivo es sustituir inventarios,
auditorías y scripts improvisados por un flujo reproducible que conserve
identidad, evidencia, incertidumbre y trazabilidad.

La fuente auditada declara `0.9.0`. El comportamiento efectivo siempre se
comprueba con el ejecutable instalado y con su estado publicado; una versión en
el árbol fuente no demuestra qué release está activa.

## Qué resuelve hoy

NeoCortex puede:

- inventariar archivos sin seguir enlaces simbólicos;
- detectar tipos y extraer contenido de documentos, archivos comprimidos,
  imágenes, audio, video, texto y código;
- conservar resultados en owners SQLite separados y publicar proyecciones
  incrementales;
- planear duplicados, clasificación y organización sin modificar originales;
- buscar evidencia mediante CLI, API Python, GUI y un servidor MCP local de
  sólo lectura;
- exponer cobertura, errores, procedencia y localizadores cuando el productor
  puede demostrarlos;
- respaldar, restaurar, inspeccionar y purgar el estado mediante comandos
  explícitos.

Todavía no ofrece un recorrido Linux completo que aplique movimientos o envíe
archivos a la Papelera. `--apply` y `--organization-apply` se abstienen con
`linux_mutation_backend_unavailable`. La fuente ya contiene la foundation KIO
fail-closed en `neocortex/safety/kio_trash.py`, pero no está conectada a esas
flags, promovida ni verificada contra KIO real. Integrarla con plan, autorización,
recovery y límites same-filesystem sigue siendo trabajo planificado; no se usará
`gio trash` ni habrá fallback a borrado permanente.

## Empieza por una consulta

Estas operaciones no recorren el corpus ni crean estado:

```bash
Neocortex --version
Neocortex --help
Neocortex help
Neocortex status --scope all
Neocortex search "consulta" --scope personal --limit 20
Neocortex ask "consulta" --scope personal --limit 12
Neocortex inspect code "consulta" --scope personal
Neocortex inspect lineage IDENTIFICADOR --scope personal
Neocortex curate plan --limit 20
```

Si el estado no tiene cobertura, prueba una sola ruta sobre una muestra acotada.
Esta corrida sí lee contenido y actualiza estado, aunque no modifica los
originales:

```bash
Root="$HOME/Documentos/NeoCortex/Pilot"
test -d "$Root" || exit 2
Neocortex --root "$Root" --route pdf --max-count 25 --strict-exit-codes
```

Repite el mismo comando y revisa cache, errores y tiempo antes de ampliar el
alcance. `--all` ejecuta todas las rutas de contenido registradas, incluida Code
como contenido; no ejecuta el código observado ni reintroduce el antiguo
autoanálisis del propio repositorio.

## Ruta de curación actual

En el checkout actual, la vista integrada disponible es de sólo lectura y puede
consultarse directamente desde la fachada humana. La release instalada previa
puede no incluir todavía este subcomando hasta una instalación desde el SHA que
lo contiene:

```bash
Neocortex curate plan --limit 20
Neocortex curate plan --limit 20 --cursor TOKEN
```

Combina propuestas publicadas de duplicados, organización y archivos vacíos,
entrega un digest integral independiente del tamaño de página y conserva el
cursor ligado al snapshot. Una propuesta no es una autorización y un grupo por
huella no equivale a igualdad byte a byte. Antes de cualquier futura aplicación
se exigirán plan inmutable, revisión humana, autorización ligada al plan,
revalidación inmediata, efecto reversible, verificación y conciliación tras
interrupciones. `--curation-preview 50 --curation-json` permanece como compatibilidad
plana.

## Plataforma y rutas

La única plataforma objetivo vigente es Kubuntu/Linux.

```text
Fuente:      ~/Neocortex/Repository
Corpus:      ${XDG_DOCUMENTS_DIR}/NeoCortex/Corpus
Estado:      ${XDG_STATE_HOME:-~/.local/state}/Neocortex/state
Datos:       ${XDG_DATA_HOME:-~/.local/share}/Neocortex
Launcher:    ~/.local/share/Neocortex/bin/Neocortex
Alias:       ~/.local/bin/Neocortex
```

La instalación canónica usa `tools/release_linux.py` y un wheelhouse local
autenticado. No instales dependencias globalmente ni valides una release mediante
imports desde el checkout.

## Documentación

- [Visión de File Intelligence & Curation](docs/FILE_INTELLIGENCE_AND_CURATION.md)
- [Arquitectura](docs/ARCHITECTURE.md)
- [CLI](docs/CLI.md)
- [Operación](docs/OPERATIONS.md)
- [Persistencia](docs/PERSISTENCE.md)
- [Seguridad](docs/SECURITY.md)
- [Knowledge](docs/KNOWLEDGE.md)
- [Recuperación](docs/RECOVERY.md)
- [Kubuntu/Linux](docs/LINUX_KUBUNTU.md)
- [Roadmap](docs/ROADMAP_90_DAYS.md)
- [Registro de cambios](docs/CHANGELOG.md)

Las reglas para contribuir mediante Codex están en [AGENTS.md](AGENTS.md). El
estado reanudable de una campaña activa vive en el único handoff vigente, no en
los contratos estables del producto.
