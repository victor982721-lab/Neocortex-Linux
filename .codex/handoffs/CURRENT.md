# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-07, `America/Mexico_City`  
**Checkout:** `/home/winterboss/Neocortex/Repository`  
**Fuente viva tras la integración del turno:** `main` = `origin/main` =
`c0e2b72` (release aún pendiente de reconstrucción desde este SHA)

## Alcance actual

La línea activa es la mejora funcional de Knowledge y sus contratos públicos de
consulta, evidencia, diagnóstico, curación y MCP. El producto conserva la
separación entre evidencia verificable y suficiencia de respuesta del LLM,
mantiene CA-12 y CA-15 como resultados parciales, y no convierte una reserva o
un timeout en aprobación. La evidencia detallada y los receipts permanecen en
sus destinos canónicos fuera de este archivo.

## Interfaces públicas documentadas

- La CLI estructurada mantiene `ask`, `--knowledge-context`, consultas
  operacionales y las rutas de curación advisory, con las opciones verificadas
  contra la ayuda y el parser actuales.
- El servidor MCP stdio expone 15 tools: consultas read-only (`status`,
  `lifecycle_status`, `content_diagnostics`, `search`, `context`,
  `operational_query`, `evidence`, `inspect_code`, `lineage`, `asset_health`,
  `curation_plan`, `curation_scan`, `curation_verify`) y escritura limitada a
  ReviewTask (`curation_review`, `curation_decide`). No expone autorización,
  aplicación, conciliación escrita ni restore de acciones.
- `docs/CLI.md` y `README.md` son la referencia breve; este archivo es el
  puntero reanudable y no sustituye los contratos técnicos ni la evidencia
  externa.

## Barreras y siguiente paso

- NeoCortex sigue siendo Linux/Kubuntu-only, sin GitHub Actions, proveedores
  remotos, KIO real ni mutación del corpus en esta línea.
- El agente raíz integró los cambios de código concurrentes y debe completar la
  reconstrucción de release, el smoke/replay público y la actualización del
  `PENDIENTES.md` externo antes de cerrar.
- El código publicado conserva fail-closed para KIO real, MCP escrito y corpus.

## Marcador para el cierre de integración

- **SHA final publicado:** `c0e2b72` (pendiente de comprobación contra `origin/main`)
- **Release, manifest, launcher y smoke público:** pendiente de actualizar con
  sus receipts canónicos, si el alcance cruza instalación
- **Árbol de trabajo final:** pendiente de comprobar después de la integración

## Estado del checkout al crear este archivo

Había cambios concurrentes fuera del ownership documental en
`.codex/config.toml`, `pyproject.toml`, `neocortex/persistence/` y
`tests/test_pytest_runner.py`; deben conservarse y conciliarse por su agente
owner. Este handoff no los valida ni los presenta como parte de la corrección
documental.
