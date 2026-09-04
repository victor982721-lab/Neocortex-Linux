# NeoCortex — handoff operativo vigente

> Actualizado: 2026-09-03, America/Mexico_City. El basename se conserva para no
> romper referencias, pero no representa la versión actual. Pendiente durable:
> `NEO-CUR-001`.

## Objetivo activo

Convertir NeoCortex en un producto Linux de inteligencia y curación que entregue
un recorrido útil desde inventario hasta plan, revisión, efectos autorizados y
verificación, sin reintroducir autoanálisis productivo ni depender de scripts
laterales.

## Frontera comprobada al iniciar esta cohorte

- Baseline fuente: `3ce58a3c978ab890039cc4da204dc62762735592` en
  `codex/neocortex-local-20260829`.
- La fuente declara `0.9.0`; no asumir que coincide con `current` sin verificar
  manifest, launcher y `source_sha` en el host.
- Linux/Kubuntu es la única plataforma activa.
- `--apply` y `--organization-apply` se abstienen con
  `linux_mutation_backend_unavailable`.
- No se abrió ni modificó corpus o estado productivo durante la auditoría.
- GitHub Actions y proveedores remotos no son gates.
- La documentación canónica fue reducida a contratos con ownership único; Git
  conserva los informes y versiones retirados.

## Decisiones vigentes

1. Code procesa cualquier repositorio como contenido; la validación de desarrollo
   permanece fuera del runtime.
2. El flujo objetivo comparte contratos entre CLI, SDK, GUI y MCP.
3. Evidence, Review, autorización, efecto y recovery son estados separados.
4. Un duplicado destructivo requiere comparación byte a byte.
5. La Papelera Linux objetivo usa KIO con `move <origen> trash:/`, preflight,
   revalidación y recovery; no usa `gio trash` ni fallback a borrado.
6. `0.10.0` entrega evidencia/plan read-only; `0.11.0` entrega efectos Linux
   acotados después de demostrar recovery.

## Riesgos abiertos

- La deduplicación fast puede sobredeclarar igualdad.
- La publicación y retención no cubren todos los owners de forma uniforme.
- Algunos manifests anuncian localizadores más precisos que los hits públicos.
- Curación no produce todavía una vista durable end-to-end única.
- MCP no expone el lifecycle de planes y acciones.
- La foundation `neocortex.safety.kio_trash` está preparada y probada mediante
  dependencias inyectadas, pero no integrada, promovida ni ejecutada contra KIO
  real.

## Próximos pasos, en orden

1. Verificar el estado final de Git y separar cambios preexistentes de esta
   cohorte.
2. Corregir contratos de igualdad, publicación y autorización antes de efectos.
3. Implementar `0.10.0` sobre fixtures de 20–50 elementos, con límites,
   progreso, paginación, estado durable y replay.
4. Integrar la foundation KIO con autorización, ledger, guard same-filesystem y
   recovery; probar cada punto de caída con runner/verificador inyectados y dejar
   cualquier prueba KIO real detrás de un gate explícito.
5. Ejecutar comprobaciones individuales aplicables y la suite combinada cuando
   cambie superficie compartida.
6. Construir e instalar desde el SHA final con wheelhouse autenticado; verificar
   manifest, launcher, smoke, replay, staging y `current + rollback`.
7. Actualizar este handoff y `PENDIENTES.md` con estado vivo; no marcar cierre
   mientras falte publicación o release.

## Fuentes

- visión: `docs/FILE_INTELLIGENCE_AND_CURATION.md`;
- arquitectura: `docs/ARCHITECTURE.md`;
- roadmap: `docs/ROADMAP_90_DAYS.md`;
- operación: `docs/OPERATIONS.md`;
- compromisos: `$CODEX_HOME/PENDIENTES.md`.

No copies aquí receipts, métricas de suites o inventarios históricos; enlaza su
evidencia durable cuando sea necesaria.
