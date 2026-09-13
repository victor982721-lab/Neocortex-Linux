# Subproyectos de NeoCortex

## Cómo entrar sin ampliar la tarea

La raíz es `/home/winterboss/Neocortex/Repository`, un monorepositorio y paquete
coordinados. Elige el dominio por el archivo o capacidad solicitada; lee sólo
su ficha y las instrucciones que hereda el archivo. Los AGENTS de dos carpetas
hermanas no se heredan entre sí: al cambiar ambas, consulta ambos. No cargues
todo este catálogo como una lista de trabajos pendientes.

| Dominio | Raíces con AGENTS específicos | Contrato |
|---|---|---|
| [Runtime y persistencia](subprojects/platform-state.md) | [`neocortex/runtime`](../neocortex/runtime/AGENTS.md), [`neocortex/persistence`](../neocortex/persistence/AGENTS.md) | resources, lifecycle, owners, fences, migraciones |
| [Inventario y catálogo](subprojects/inventory-catalog.md) | [`neocortex/deduplication`](../neocortex/deduplication/AGENTS.md), [`neocortex/documents`](../neocortex/documents/AGENTS.md) | identidad, catálogo, duplicados, ámbito y planificación |
| [Ingesta de formatos](subprojects/formats.md) | [`neocortex/capabilities`](../neocortex/capabilities/AGENTS.md) | rutas, extracción, límites y cobertura |
| [Code como contenido](subprojects/code-content.md) | [`neocortex/code`](../neocortex/code/AGENTS.md) | lenguajes, proyectos, símbolos y relaciones |
| [Semantic y Knowledge](subprojects/retrieval-context.md) | [`neocortex/semantic`](../neocortex/semantic/AGENTS.md), [`neocortex/knowledge`](../neocortex/knowledge/AGENTS.md) | publicación, recuperación, citas y contexto |
| [Curación y efectos](subprojects/curation-effects.md) | [`neocortex/curation`](../neocortex/curation/AGENTS.md), [`neocortex/workflow`](../neocortex/workflow/AGENTS.md) | review, grants, aplicación, verificación y recuperación |
| [Interfaces públicas](subprojects/interfaces.md) | [`neocortex/api`](../neocortex/api/AGENTS.md), [`neocortex/interface`](../neocortex/interface/AGENTS.md) | CLI, API/SDK, GUI y MCP |
| [Desarrollo y distribución](subprojects/development-release.md) | [`tools`](../tools/AGENTS.md), [`tests`](../tests/AGENTS.md) | validación individual, fixtures, empaquetado y release |

## Superficies compartidas

`foundation`, `platform`, `safety`, `enumeration`, `progress`, `integrations` y
`sdk` conservan el contrato raíz. Consulta la ficha del productor/consumidor
afectado sin inventar un AGENTS independiente para cada módulo pequeño:
identidad/enumeración se relaciona con inventario, seguridad de efectos con
curación, progreso/control con runtime y SDK con interfaces. `semgrep` es una
herramienta de desarrollo, no una capacidad del producto.

Los tests heredan el AGENTS de `tests`; sus contratos de comportamiento están
en la ficha del dominio bajo prueba. No desplaces fixtures ni módulos para que
encajen en el mapa, no cambies namespaces, wheels ni fronteras de empaquetado.
Separar un repositorio o release requiere una decisión expresa posterior.

## Documentación y estado

Estas fichas explican ownership y selección de validación, no repiten los
manuales. Consulta [visión](FILE_INTELLIGENCE_AND_CURATION.md) para resultado de
producto y [Roadmap](ROADMAP_90_DAYS.md) para prioridades futuras,
[Architecture](ARCHITECTURE.md) para flujos, [Operations](OPERATIONS.md)
para operar, [Persistence](PERSISTENCE.md) para owners y [Recovery](RECOVERY.md)
para recuperación. [README](../README.md#plataforma-y-rutas) conserva las rutas
XDG de corpus, estado, modelos, launcher y releases, fuera del checkout.

[CURRENT](../.codex/handoffs/CURRENT.md) y `$CODEX_HOME/PENDIENTES.md` localizan
estado y evidencia reciente; sus afirmaciones se verifican antes de actuar.
Una ficha no declara incidentes cerrados, reservas aprobadas ni instalaciones
vigentes y no vuelve a abrir trabajo histórico por aparecer en un handoff.

## Contexto durable tras desactivar memorias

Las fichas y contratos siguientes son la memoria operativa del monorepositorio; no repiten estados, hashes ni resultados fechados. Para cada dominio, abre primero su ficha y después el AGENTS más cercano:

| Dominio | Fuente durable principal |
|---|---|
| Runtime y persistencia | `docs/subprojects/platform-state.md`, `docs/PERSISTENCE.md` |
| Inventario y catálogo | `docs/subprojects/inventory-catalog.md` |
| Ingesta de formatos | `docs/subprojects/formats.md` |
| Code como contenido | `docs/subprojects/code-content.md` |
| Semantic y Knowledge | `docs/subprojects/retrieval-context.md`, `docs/KNOWLEDGE.md` |
| Curación y efectos | `docs/subprojects/curation-effects.md` |
| Interfaces públicas | `docs/subprojects/interfaces.md` |
| Desarrollo y distribución | `docs/subprojects/development-release.md` |

Los hechos vivos, heads, receipts, releases, procesos, fixtures, corpus y estados de publicación se consultan en sus fuentes propias (`.codex/handoffs/CURRENT.md`, manifests y registros), nunca se convierten en reglas permanentes por aparecer en un resumen de memoria.

## Capacidades de desarrollo opcionales

Las skills del repositorio son procedimientos, no hooks ni tareas automáticas:
[desarrollo](../.agents/skills/neocortex-development/SKILL.md) para cambios
solicitados, [release](../.agents/skills/neocortex-release/SKILL.md) sólo cuando
la tarea incluya instalación/distribución, y [observación](../.agents/skills/neocortex-observe-run/SKILL.md)
para una corrida identificada. No cambian permisos, configuración ni el runtime.
