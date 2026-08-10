# NeoCortex — handoff operativo actual

> Actualizado: 2026-08-10. El nombre del archivo es histórico y se conserva
> como ruta estable. Este documento es la fuente única de la frontera vigente;
> no guarda un SHA de cierre porque Git, la release instalada y GitHub deben
> demostrarlo dinámicamente.

## Preferencia operativa de Víctor

- GitHub conserva únicamente `main`; no se usan PR ni ramas para la evolución
  ordinaria de este proyecto personal.
- Cada entrega se integra mediante commits atómicos directos en `main`, una
  release Linux del SHA final exacto, el launcher público verificado y CI verde.
- El estado vivo, el corpus y el launcher instalado son el SSOT operativo.
- En Linux no se usa `--apply` ni `--organization-apply`. Resultados de
  búsqueda, OCR, modelos o similitud nunca autorizan una mutación.
- Originales y estado publicado se preservan; fixtures, benchmarks y pilotos
  sólo autorizan la siguiente prueba acotada, no una promoción automática.

## Veredicto y frontera 0.9.0

La auditoría externa de 0.8.0 fue correcta en su diagnóstico central:
NeoCortex ya era una plataforma local de conocimiento madura y fail-closed,
pero CI, supply chain, rendimiento del guard y ciclos de imports no estaban al
nivel de su amplitud. La campaña 0.9.0 cerró esos cuatro bloqueos y añadió un
piloto de producto medido, sin habilitar mutación Linux ni fabricar calidad de
modelos.

La versión fuente es `0.9.0`. Su cierre no se infiere de este documento: sólo
existe cuando se cumplen juntos los criterios dinámicos de la última sección.

## Cinco fases completadas

### Fase 0 — supply chain y fronteras de arquitectura

- El runtime principal usa `mcp==1.29.0`; el intercambio MCP stdio real cubre
  initialize, listado, llamada y cierre sin depender de transporte HTTP,
  WebSocket ni tareas experimentales.
- Semgrep `1.172.0` se retiró del runtime principal porque fija
  `mcp==1.23.3`. Vive en un tool-runtime administrado, scan-only, fuera de
  `PATH`, con wrapper, lock, inventario, artefactos y receipt verificados.
- Las tres vulnerabilidades MCP del tool-runtime son excepciones explícitas,
  no alcanzables por su superficie, y vencen el **2026-09-30**. El runtime
  principal no hereda ninguna excepción.
- Todo bootstrap mantenido parte de un venv sin pip y autentica el wheel oficial
  `pip 26.1.2` por nombre y SHA-256 antes de ejecutarlo. CI, release Linux y la
  receta Windows usan `python -I tools/bootstrap_pip.py`.
- Pyright `1.1.411` se instala con Node `24.18.1` desde manifest y lock npm
  versionados. Se exige integridad exacta, `npm ci`, scripts deshabilitados,
  entorno hostil limpiado y verificación viva antes del gate estático.
- El empaquetado separa base, `agent` y `analysis`; la instalación personal
  canónica sigue usando `full`. Semgrep permanece aislado incluso de `full`.
- La distribución es privada (`LicenseRef-Proprietary`, clasificador
  `Private :: Do Not Upload`) y declara autor, mantenedor y URLs válidas.

### Fase 1 — barreras integrales y CI

- El inventario dinámico final contiene **265 archivos de prueba**. Coverage
  ejecutó la suite completa dos veces de forma independiente: **4,125 passed,
  146 skipped y 98 subtests** en ambas corridas.
- Baseline branch-aware aprobado: **56,933/67,720 líneas** y
  **14,600/21,110 ramas**. También ratchetea las rutas aprobadas de tests y de
  las **326 fuentes de producción**: las adiciones pasan; un retiro exige una
  reescritura explícita y revisada.
- Los dos shards son una partición exacta: 132 archivos con 1,911 passed y 133
  archivos con 2,214 passed. La suma coincide con la suite completa.
- La matriz obligatoria es el producto Windows/Ubuntu × Python 3.13/3.14 × dos
  shards: **8 jobs**, no una rotación incompleta entre versión y shard.
- CI construye e instala el wheel `full`, ejecuta un smoke fuera del checkout
  sobre seis paquetes, `Orquestador`, package-data, metadata, versión y
  entrypoint, y después prueba el árbol fuente completo. Cada job revalida SHA
  y worktree limpio al final; `quality` también lo hace tras Coverage.
- El gate estático versionado conserva deuda sin permitir crecimiento:
  Ruff **76**, Mypy **94**, Pyright **214**. No significa “cero deuda”; significa
  cero diagnósticos nuevos por ruta/regla y versión exacta de cada herramienta.
- Grimp exige el baseline acíclico v2, seis contratos exactos y evidencia viva.
  Estado final: **325 módulos, 1,316 relaciones, 0 violaciones y 0 SCC**.
- Supply chain real se evalúa en cada SHA con `pip-audit` del runtime principal
  y del tool-runtime Semgrep contra su receipt/policy; Coverage JSON se publica
  como artefacto ligado al SHA.

### Fase 2 — rendimiento sin perder seguridad TOCTOU

- El guard de mutación conserva revalidación de root, identidad, componente,
  contención y no-replace por candidato, pero reutiliza el snapshot inmutable
  de la frontera por lote.
- El caso grande pasó de aproximadamente **113 s** a **8.6 s** y conserva un
  máximo de seis reconstrucciones completas.
- Una primera optimización reveló dos regresiones de sustitución entre preflight
  y uso. Se corrigieron antes de aceptar el cambio y ambas pruebas TOCTOU pasan;
  el batching no convirtió una mejora de velocidad en autorización por ruta.

### Fase 3 — arquitectura acíclica

- Se eliminaron, sin mover los owners públicos, los SCC de contratos
  semánticos, Knowledge, políticas de corpus y el ciclo central de 14 módulos.
- Protocols estructurales de sólo lectura y modelos hoja fijan la dirección de
  dependencias. Reexports conservan identidad y compatibilidad pública.
- El baseline histórico de cuatro ciclos quedó sustituido por
  `KNOWN_CYCLE_BASELINE = ()`; reintroducir cualquiera de ellos vuelve a fallar
  el contrato principal.

### Fase 4 — pilotos de producto y persistencia

- La página **Consulta** de la GUI ejecuta lecturas fuera del hilo Qt, permite
  cancelación y copia y conserva modo sólo lectura. El render offscreen
  1440×900 fue inspeccionado visualmente.
- Un piloto aislado de ocho videos reales produjo 7 completos y un error
  esperado de contenido sólo-audio, 4 `visual_only`, 29 frames, 13 keyframes,
  26 intervalos y OCR positivo en 27/29 intentos. El replay obtuvo 8 cache hits,
  0 OCR nuevo y terminó en aproximadamente 1.1 s.
- Audio enlazó los mismos ocho elementos: 1 transcrito, 3 `no_speech`, 4
  `no_audio`, 0 errores. La consulta `MALPASO transformer` recuperó evidencia
  OCR/frame, aunque con ruido; la muestra no demuestra todavía OCR DE/ZH fuerte.
- Dedup schema 10 añade dos índices identity-bound para los joins productivos
  de Knowledge. En una copia poblada v9→v10 preservó 414,421 archivos,
  347,239,389,995 bytes, 3,518 miembros planeados y FK/integridad; migró en
  5.40 s y la consulta medida bajó aproximadamente de 1.33 s a 0.106 s.
- Fresh v10, v9→v10, cadena 1→10, rollback, DDL adversarial y `EXPLAIN QUERY
  PLAN` productivo tienen regresiones específicas. La migración del estado vivo
  sólo ocurre dentro del cierre protegido de release descrito abajo.

## Capacidades que permanecen fail-closed

1. **Linux mutation:** sigue intencionalmente deshabilitada. Un backend POSIX
   sólo puede abrirse como proyecto separado con `openat2`/dirfd, identidad por
   descriptor, `renameat2`, journal durable y pruebas adversariales equivalentes
   a NTFS.
2. **CLIP textual:** las distribuciones positivas y negativas reales siguen
   solapadas; no existe un umbral publicado y la búsqueda se abstiene.
3. **MiniLM:** el resultado offline favorece al modelo compacto, pero no hay
   todavía 20–50 consultas humanas ES/EN/DE/ZH etiquetadas. Permanece shadow y
   separado del espacio Jina.
4. **Archive Semantic:** no se publican miles de chunks por inercia; sólo procede
   por selector si supera FTS en preguntas reales.
5. **OCR DE/ZH en video:** runtime y perfiles están listos, pero el piloto no
   aportó evidencia representativa suficiente para declararlo validado.
6. **Recuperación incierta:** una acción `recovery_required` continúa exigiendo
   revisión manual; nunca se reintenta automáticamente.

## Deuda residual explícita

- El baseline estático contiene 76/94/214 hallazgos y agrupa por ruta/regla, no
  por fingerprint de cada mensaje. Debe reducirse gradualmente y nunca usarse
  para intercambiar deuda nueva por vieja.
- Permanecen hotspots grandes en evidencia externa, validaciones Knowledge y el
  parser legado. La campaña eliminó ciclos, no fingió haber reducido toda la
  complejidad ciclomática.
- La suite integral prueba el árbol fuente después de instalar el wheel; el
  artefacto instalado tiene un smoke aislado fuerte, no una segunda ejecución
  artificial de los 4,125 casos que dependen también de tools/docs del checkout.
- Windows conserva receta mantenida y CI completa, pero aún no tiene un
  instalador Python integral equivalente a `tools/release_linux.py`.
- Las excepciones Semgrep dejan de ser válidas el 2026-09-30. Antes de esa fecha
  se actualiza o sustituye Semgrep; no se prolonga el vencimiento por rutina.

## Próximos pasos, en orden

1. Usar `Neocortex search|ask`, la página Consulta y `review value` sobre
   preguntas reales, siempre sin mutación.
2. Etiquetar con Víctor 20–50 consultas ES/EN/DE/ZH y comparar MiniLM shadow
   contra Jina con relevancia, latencia, procedencia y calibración por fuente.
3. Ampliar CLIP con positivos y negativos humanos por idioma/tipo de imagen;
   promover sólo una política que separe las distribuciones de forma robusta.
4. Autorizar una muestra pequeña con texto alemán y chino visible para repetir
   Video/OCR/Audio y su replay cacheado.
5. Evaluar Archive Semantic por selectores útiles contra FTS y reducir después
   un hotspot o bucket estático por cambio, sin mezclarlo con funciones nuevas.
6. Diseñar el backend Linux identity-bound sólo si la organización física en
   Kubuntu se vuelve prioridad; hasta entonces mantener el rechazo actual.

## Criterio dinámico de cierre de 0.9.0

La release queda cerrada únicamente cuando se comprueba todo lo siguiente sobre
el mismo SHA comprometido y un worktree limpio:

1. `git rev-parse HEAD` = `git rev-parse origin/main`;
2. `current/neocortex-release.json:source_sha` = ese mismo SHA;
3. `python3.14 tools/release_linux.py verify` y `Neocortex --version` informan
   una release válida `0.9.0`;
4. el pre-push canónico aprueba arquitectura, estática, supply, Coverage y SHA;
5. antes de la primera corrida 0.9 se respalda cada SQLite con la API de backup
   online y se verifica integridad/FK de las copias;
6. dos corridas `Neocortex --all` desde el launcher final terminan en dry-run,
   migran Dedup a v10, conservan originales y demuestran replay incremental;
7. las trece bases, corpus before/after, status/search/ask/review, MCP stdio y GUI
   instalados aprueban;
8. el push de GitHub termina con todos los checks verdes y GitHub sólo expone
   `main`.

La evidencia mínima de cierre se conserva bajo
`$HOME/.codex/vault/evidence/neocortex-0.9-release-2026-08-10/`; el backup
pre-0.9 se conserva separadamente bajo `$HOME/.codex/vault/backups/neocortex/`.
Si cualquiera de los ocho puntos difiere, 0.9.0 sigue abierta y se corrige antes
de declarar éxito.
