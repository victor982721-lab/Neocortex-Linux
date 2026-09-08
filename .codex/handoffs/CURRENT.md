# Handoff operativo vigente — NeoCortex

**Última verificación:** 2026-09-07, `America/Mexico_City`  
**Checkout:** `/home/winterboss/Neocortex/Repository`  
**Fuente viva tras la integración del turno:** `main` = `origin/main` =
`0cda31194c6888fc1045b45d1925935de6069d39`

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
- El agente raíz integró los cambios de código concurrentes, publicó `main`,
  reconstruyó la release final y comprobó el smoke/replay público; el historial
  externo requiere todavía el gate de escritura de `HISTORIAL.md`.
- El código publicado conserva fail-closed para KIO real, MCP escrito y corpus.

## Marcador para el cierre de integración

- **SHA final publicado:** `0cda31194c6888fc1045b45d1925935de6069d39`
- **Release:** `0.12.0-0cda31194c68-cp314-linux-x86_64`, current con rollback `0.12.0-4ed8cc124911-cp314-linux-x86_64`
- **Manifest/árbol/launcher:** `b41f6d554387f20310606bf89e39c1fb5387a96a024bffec75044d66423595ae` / `33d496e408f61c4e334fbd9329193694f1aaa13d76dd2768407f57fa46097a61` / `eff9573a0ae82cdc8a27872f99e85b54783fdf80eb8a0feab547991d56fbef2d`
- **Release, manifest, launcher y smoke público:** pendiente de actualizar con
  sus receipts canónicos, si el alcance cruza instalación
- **Árbol de trabajo final:** pendiente de comprobar después de la integración

## Estado del checkout al crear este archivo

Había cambios concurrentes fuera del ownership documental en
`.codex/config.toml`, `pyproject.toml`, `neocortex/persistence/` y
`tests/test_pytest_runner.py`; deben conservarse y conciliarse por su agente
owner. Este handoff no los valida ni los presenta como parte de la corrección
documental.
