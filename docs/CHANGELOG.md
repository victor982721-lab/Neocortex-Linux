# Registro de cambios

Este archivo conserva cambios observables del producto. Métricas, receipts,
comandos de auditoría y estado de una instalación pertenecen a evidencia fechada
fuera de `docs/`.

## Unreleased

### Documentación

- Se definió File Intelligence & Curation como visión estable del producto y se
  separaron visión, arquitectura, CLI, operación, persistencia, seguridad,
  Knowledge, recovery, roadmap e historia.
- Se retiraron del árbol activo auditorías fechadas, handoffs sustituidos,
  snapshots de licencias, instrucciones Windows y la documentación operativa del
  antiguo autoanálisis.
- Se documentó para `0.11.0` la promoción de la foundation KIO ya preparada a
  una Papelera KDE same-filesystem y reversible, con plan, autorización y
  recovery, sin `gio trash` ni fallback destructivo.

### Producto en el árbol posterior a 0.9.0

- Code permanece limitado a contenido: ingesta, detección, estructura,
  persistencia, búsqueda y relaciones.
- Las herramientas de desarrollo dejaron de formar parte del runtime productivo.
- Image ya no incorpora NudeNet ni su cadena de clasificación.
- `Neocortex databases` expone status, backup, restore y purge con preview,
  manifests, locks y confirmaciones.
- `--state-health` usa snapshots compatibles y reporta owners/sidecars.
- `--curation-preview` compone planes read-only con cobertura.
- `neocortex.safety.kio_trash` prepara descubrimiento, preflight, validación de
  snapshot, ejecución inyectable y receipts KIO, pero permanece desconectado de
  Linux `--apply` y no fue probado contra KIO real.
- `curate plan` y la herramienta MCP `curation_plan` consultan páginas de
  propuestas con cursor y digest ligado al snapshot; la API de evidencia acepta
  `evidence_id` y snapshot esperado para evitar reasignar alias.
- `curate review`/`curation_review` publican páginas con cobertura completa como
  ReviewTasks advisory, ligadas a `plan_digest` y snapshot; el replay de la misma
  página es idempotente.
- `curate decide`/`curation_decide` registran por CAS una decisión humana
  `resolved` o `dismissed`, con scope, actor y event head esperado. Sólo escriben
  eventos ReviewTask: no crean `file_actions`, no autorizan, no invocan KIO y no
  cambian corpus ni sistemas externos.
- `curate authorize` y la API/SDK `curation_authorize_payload` emiten un
  AuthorizationGrant append-only dentro de la extensión Framework
  `curation_authorization_grants`. Exigen plan vigente, ReviewTasks resueltas,
  actor, acción, expiración y presupuestos; el replay equivalente es idempotente.
- Emitir el grant no crea `file_actions`, no llama KIO y conserva
  `physical_effect_applied=false`. MCP no expone authorize hasta resolver un
  principal autenticado; la brecha siguiente es `apply → verify → reconcile`.
- El lifecycle de curación no incorpora exportación ni ZIP; `--json` devuelve el
  envelope de la operación.
- La publicación cross-owner, el ledger generacional Code, Review y el manifest
  multimodal avanzaron en el árbol fuente, pero requieren validación conjunta y
  una release instalada desde el SHA final.

## 0.9.0 — 2026-08-10

- Se estableció Linux/Kubuntu y CPython 3.13–3.14 como plataforma activa.
- Se introdujo la fachada humana de status, search, ask, inspect, Review y MCP
  read-only.
- Knowledge, Semantic y Code ampliaron evidencia, linaje y consultas.
- Se separaron dependencias productivas de herramientas de desarrollo.
- Se añadió instalación Linux versionada con manifest, launcher estable y
  política `current + rollback inmediato`.
- Linux mantuvo mutaciones de corpus deshabilitadas con
  `linux_mutation_backend_unavailable`.
- Cambios posteriores retiraron la plataforma de validación interna y el soporte
  activo Windows sin reinterpretar datos históricos.

## 0.8.0 — 2026-08-09

- Se amplió el procesamiento Linux de PDF, DOCX, Office, ZIP, Text, Audio, Video,
  Image y Code.
- Se añadieron contratos de capacidades, selección de providers y cobertura.
- Se fortalecieron publicación generacional, replay y búsquedas multimodales.
- Se incorporó la interfaz PySide6 Linux y el aislamiento de workers.
- Se preservó Windows como compatibilidad histórica mientras Linux permanecía
  fail-closed para efectos.

## 0.7.2 — 2026-07-31

- Se introdujeron perfiles internos de análisis del repositorio, providers
  externos, receipts y Review de código.
- Se añadieron inventarios de dependencias, validación de artefactos y guards de
  rutas internas.
- Se amplió recovery de acciones, retención y estado read-only.
- Esa plataforma se retiró posteriormente: sus datos son historia y no forman
  parte de la interfaz productiva actual.

## 0.7.1 — 2026-07-26

- Se integró Knowledge sobre owners existentes sin crear una base paralela.
- Se añadieron contexto citado, rankings separados y evaluación reproducible.
- Se mejoraron límites de recursos, cancelación y reanudación.
- Se publicaron mejoras de Semantic y Code con evidencia de cobertura.

## 0.7.0 — 2026-07-25

- Se introdujeron owners y publicaciones para Knowledge, catálogo y Semantic.
- Se añadieron snapshots lógicos y lectores read-only.
- Se fortalecieron contratos de identidad, errores y salida estructurada.
- Las migraciones permanecieron aditivas y fail-closed.

## 0.6.0 — 2026-07-25

- Se añadió observación durable de acciones inciertas y un planificador de
  retención read-only.
- Se ampliaron factories SQLite, diagnósticos y contratos de conexión.
- Se separó observación de recovery respecto de decisión y autorización.

## 0.5.0 — 2026-07-24

- Se introdujeron publicación generacional de inventario, catálogo y Semantic.
- Se añadieron estados de acción y conciliación después de interrupciones.
- La implementación original de efectos estaba ligada a NTFS y quedó fuera del
  alcance Linux posterior.

## 0.4.1 — 2026-07-24

- Se corrigieron publicación parcial, cursor de inventario y transacciones.
- Se reforzaron migraciones, foreign keys y rollback.
- Se añadió validación de artefactos e instalación aislada.

## 0.4.0 — fecha no verificada

- Se consolidaron inventario, extracción, búsqueda y organización iniciales bajo
  el comando `Neocortex`.
- La ausencia de una fecha verificada se conserva explícita; no se infiere una
  fecha desde commits posteriores.
